"""원본 파일 내려받기 지원 — 대상 확정 · 열기 · 구간(Range) · 이어받기 기준값.

**흐름에서의 위치**: ``GET /assets/{id}/download`` 가 파일을 내려보내기 직전에 쓰는 계층이다. 구간 헤더 파싱과 기준값은 순수 함수이고,
나머지는 DB 에서 대상과 경로를 확정하고 파일을 연다. **쓰기는 없다**(헌법 6조).

**원본은 DB 에 적힌 경로(``fs_path``)에 있다고 본다**(2026-10-01 결정) — 경로를 바꿔 여는 설정도, 루트 검사도 두지 않는다.
없으면 열 때 실패하고 호출부가 **410** 봉투로 답한다(자산 기록은 있으나 파일이 사라진 상태). 원본은 옮기거나 복사하지 않고 읽기만 한다.
연동 뒤 경로가 루트 기준으로 바뀌면 그때 이 한 곳(``open_original``)에서 마운트 루트를 붙이면 된다
(``service/portal/asset/__init__.py`` 「원본 파일 전제」).

묶음(zip)의 **대상 확정**(``fetch_asset_paths`` · ``collect_bundle_assets``)도 여기 있고, zip 을 만드는 일은 ``bundle_stream`` 이 한다.
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any, BinaryIO

from psycopg import Connection
from psycopg.rows import dict_row

from src.config.filename_util import (
    display_file_name,  # 내려받을 때 보일 파일명 — 저장 시 붙은 id 접두를 떼어 준다
)
from src.domain.status_vocab import AssetStatus

# 묶음 이웃은 반드시 graph_query seam 경유(대칭 엣지 양방향 · status 필터). 직접 graph_edge 쿼리 금지.
from src.relations.graph_query import fetch_active_relations_for_asset

logger = logging.getLogger(__name__)

_BYTES_PREFIX = "bytes="

# 노출 기준은 상세 조회와 같다 — 등록 완료 자산만. 그 외는 404(없는 것과 같게 다룬다).
_DOWNLOAD_TARGET_SQL = """
SELECT asset_id, fs_path, fs_uri, file_size, modality, domain_label, status
FROM asset
WHERE asset_id = %s
LIMIT 1
"""

# 묶음에 담을 자산들의 파일 경로 조회 — 등록 완료 자산만(아직 등록이 끝나지 않은 이웃은 파일이 제자리에 없을 수 있다).
_BUNDLE_PATHS_SQL = f"""
SELECT asset_id, fs_path FROM asset
WHERE asset_id = ANY(%s)
  AND status = '{AssetStatus.REGISTERED}'
"""


def parse_range_header(range_value: str | None, file_size: int) -> tuple[int, int] | None:
    """HTTP ``Range`` 헤더를 ``(start, end)`` 바이트 오프셋(둘 다 포함)으로 파싱한다(순수).

    지원 형식(단일 범위만):
        - ``bytes=start-end`` → ``(start, end)``
        - ``bytes=start-``    → ``(start, file_size-1)`` (열린 끝)
        - ``bytes=-suffix``   → ``(file_size-suffix, file_size-1)`` (마지막 suffix 바이트,
          suffix 가 파일보다 크면 전체로 클램프 — RFC 7233)
    헤더가 ``None`` 이면 ``None``(=전체 다운로드). 끝이 파일 크기 이상이면 ``file_size-1`` 로
    **클램프**한다(RFC 7233 §2.1 — 거부 아님; 일부 플레이어/다운로드 매니저가 stale 한 큰 end 를
    재요청해도 206 응답). 시작이 파일 크기 이상·역순·형식 오류·다중 범위는 ``ValueError`` 로
    거부한다(API 가 416 으로 응답). 범위가 파일 끝을 넘으면 **거부하지 않고 끝까지로 줄인다** —
    표준이 그렇게 정하고 있고, 엄격히 거부하면 이어받기 클라이언트가 실패한다.

    Args:
        range_value: ``Range`` 헤더 값. ``None`` 이면 전체 다운로드라는 뜻이다.
        file_size: 대상 파일 크기(클램프 기준).

    Returns:
        ``(start, end)`` — **둘 다 포함**하는 구간. 헤더가 없으면 ``None``.

    Raises:
        ValueError: 시작이 파일 크기 이상 · 역순 · 형식 오류 · 다중 범위. 호출부가 416 으로 바꾼다.
    """
    if range_value is None:
        return None

    text = range_value.strip()
    if not text.startswith(_BYTES_PREFIX):
        raise ValueError(f"지원하지 않는 Range 단위: {range_value!r}")
    spec = text[len(_BYTES_PREFIX):].strip()

    # 다중 범위(콤마)는 본 MVP 미지원 — 단일 범위만 처리.
    if "," in spec:
        raise ValueError("다중 Range 는 미지원(단일 범위만)")
    if "-" not in spec:
        raise ValueError(f"Range 형식 오류: {range_value!r}")

    start_str, end_str = (part.strip() for part in spec.split("-", 1))
    try:
        if start_str == "":
            # 접미 형식 bytes=-suffix : 마지막 suffix 바이트.
            if end_str == "":
                raise ValueError("Range 형식 오류(빈 범위)")
            suffix = int(end_str)
            if suffix <= 0:
                raise ValueError("Range suffix 는 양수여야 함")
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str != "" else file_size - 1
    except ValueError as exc:
        # int 변환 실패도 형식 오류로 통일(416 의미는 아래 범위 검증에서).
        raise ValueError(f"Range 형식 오류: {range_value!r} ({exc})") from exc

    if start < 0:
        raise ValueError(f"Range 시작이 음수: {range_value!r}")
    if start >= file_size:
        raise ValueError(f"Range 시작이 파일 크기 이상(416): {start} >= {file_size}")
    if end < start:
        raise ValueError(f"Range 역순(416): {start} > {end}")
    # 끝이 파일 크기 이상이면 거부하지 않고 마지막 바이트로 클램프한다(RFC 7233 §2.1).
    if end >= file_size:
        end = file_size - 1
    return (start, end)


def resolve_download_target(
    conn: Connection[Any], *, asset_id: str
) -> dict[str, Any] | None:
    """``asset_id`` 의 단일 다운로드 타깃을 해소한다(노출 가능 여부까지 여기서 판단).

    Returns:
        registered 면 ``{"asset_id","fs_path","fs_uri","file_size","modality","file_name"}``
        (``file_name`` = ``fs_path`` basename). 행 없음/비registered → ``None`` (API 404).

    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DOWNLOAD_TARGET_SQL, (asset_id,))
        row = cur.fetchone()

    if row is None:
        return None
    if row["status"] != AssetStatus.REGISTERED:
        return None

    fs_path = row["fs_path"]
    return {
        "asset_id": str(row["asset_id"]),
        "fs_path": fs_path,
        "fs_uri": row["fs_uri"],
        "file_size": row["file_size"],
        "modality": row["modality"],
        "file_name": display_file_name(fs_path),
    }


def open_original(fs_path: str | None) -> tuple[BinaryIO, int, int]:
    """원본 파일을 **한 번 열어** 핸들과 크기 · 수정 시각을 함께 돌려준다.

    열린 핸들에서 크기를 읽는다(``fstat``) — "있는지 확인 → 나중에 열기" 로 가르면 그 사이에 파일이 사라지거나 바뀌어,
    헤더를 이미 보낸 뒤 스트리밍이 터진다. 여기서 한 번에 확정하면 실패는 **응답을 시작하기 전**에 드러난다.

    Args:
        fs_path: DB 에 적힌 원본 경로. 비었거나 ``None`` 이면 없는 것으로 본다.

    Returns:
        ``(열린 핸들, 바이트 크기, 수정 시각 ns)``. **호출부가 닫는다.**

    Raises:
        OSError: 경로가 비었거나 · 열 수 없거나(없음 · 권한) · 일반 파일이 아닐 때(디렉터리 등). 호출부가 410 으로 바꾼다.
    """
    if not fs_path:
        raise OSError("원본 경로가 비어 있다")
    fh = open(fs_path, "rb")  # noqa: SIM115 — 응답 스트림이 닫을 때까지 살아 있어야 한다(호출부가 닫는다)
    try:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("일반 파일이 아니다")
    except BaseException:
        fh.close()
        raise
    return fh, info.st_size, info.st_mtime_ns


def make_etag(size: int, mtime_ns: int) -> str:
    """이어받기 기준값 — **크기와 수정 시각**으로 만든다(연동 협의안의 변경 대조 기준과 같다).

    따옴표까지 포함한 **강한** ETag 다(``If-Range`` 는 강한 비교만 허용한다). 내용 해시가 아니라 크기 · 수정 시각이라,
    같은 크기로 같은 초에 바뀌면 못 잡는 한계가 있다 — 수 GB 를 읽어 해시를 만들 수는 없어서다.
    """
    return f'"{size:x}-{mtime_ns:x}"'


def make_last_modified(mtime_ns: int) -> str:
    """``Last-Modified`` 값(RFC 7231 HTTP-date · 초 단위)."""
    return format_datetime(datetime.fromtimestamp(mtime_ns / 1e9, UTC), usegmt=True)


def if_range_matches(if_range: str | None, etag: str, last_modified: str) -> bool:
    """``If-Range`` 조건이 지금 파일과 맞는지(순수). 조건이 없으면 맞는 것으로 본다.

    이어받는 도중 **원본이 바뀌었으면 조각이 섞인다**(앞 절반은 옛 파일 · 뒤 절반은 새 파일). 클라이언트가 처음 받은 기준값을
    ``If-Range`` 로 보내면, 맞을 때만 구간으로 답하고 **다르면 구간을 무시하고 전체를 200 으로** 준다(RFC 7233 §3.2).

    Args:
        if_range: ``If-Range`` 헤더 값(ETag 또는 HTTP-date). 없으면 ``None``.
        etag: 지금 파일의 ETag.
        last_modified: 지금 파일의 ``Last-Modified``.

    Returns:
        구간 요청을 그대로 받아도 되면 ``True``.
    """
    if if_range is None:
        return True
    value = if_range.strip()
    if value.startswith(("W/", '"')):
        return value == etag          # 약한 ETag(W/) 는 If-Range 에서 맞지 않는 것으로 본다
    return value == last_modified


def fetch_asset_paths(
    conn: Connection[Any], asset_ids: list[str]
) -> dict[str, Any]:
    """자산 id 들의 파일 경로를 **한 번에** 조회한다(자산마다 따로 묻지 않는다).

    **공개 심볼**: 개체 카드 zip(``portal/mm_meta.py``)도 같은 조회를 필요로 한다 — 밑줄 이름을 남이
    쓰거나 SQL 을 두 벌로 두면 노출 기준(``status='registered'``)이 갈릴 수 있어 공개 이름으로 둔다.

    Args:
        asset_ids: 조회할 자산 목록. 빈 목록이면 DB 를 건드리지 않는다.

    Returns:
        ``{asset_id: fs_path}``. **경로가 없는 자산은 빠진다** — 호출부가 그것으로 묶음에서
        제외할지 판단한다.
    """
    if not asset_ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_BUNDLE_PATHS_SQL, (list(asset_ids),))
        rows = cur.fetchall()
    return {str(r["asset_id"]): r["fs_path"] for r in rows}


def collect_bundle_assets(
    conn: Connection[Any],
    *,
    seed_asset_id: str,
    max_neighbors: int = 50,
    min_confidence: float = 0.0,
) -> list[dict[str, Any]]:
    """기준 자산과 **직접 연결된 이웃**을 묶음 대상으로 모은다.

    절차
        1. ``graph_query``(active, **양방향**)로 이웃을 받아 self-loop 제외·``min_confidence``
           미만 제외·중복 이웃(다중 엣지)은 최고 confidence 로 합친다.
        2. 결정적 정렬 — 이웃을 (confidence desc, asset_id asc)로 줄세운 뒤 ``max_neighbors``
           초과 시 상위 N 으로 절단(허브 자산 메가블롭 방지, 절단 시 ``log`` 경고).
        3. seed 를 맨 앞에 두고 각 자산의 ``fs_path``/``file_name`` 을 채워 반환.

    Args:
        conn: 열려 있는 연결.
        seed_asset_id: 기준 자산.
        max_neighbors: 담을 이웃 수 상한. **연결이 아주 많은 자산 때문에 묶음이 통째로
            거대해지는 것을 막는 안전장치** — 잘리면 경고를 남긴다.
        min_confidence: 이 값 미만인 관계는 버린다. 기본값 0 은 전부 통과다.

    Returns:
        ``[{"asset_id","fs_path","file_name"}, ...]`` — 기준 자산 먼저, 그다음 이웃(순서 고정).
        이웃이 없으면 기준 자산 하나만 담는다. 경로를 모르는 항목은 뺀다.
    """
    seed_id = str(seed_asset_id)
    # **확인된 관계만 담는 것은 의도된 것이다** — 상세 화면이 확인 전까지 보여준다고 해서
    # 여기를 따라 바꾸지 말 것. 보는 것과 내보내는 것은 무게가 다르다: 화면의 오표시는
    # 사용자가 넘기면 끝나지만, 틀린 파일이 담긴 zip 은 메일·보고서·타 시스템으로 퍼지고
    # **회수할 수 없다**. 확인 전 구간의 오류율이 낮지 않아 그대로 내보내면 오배포가 된다.
    # 되돌리기 쉬운 쪽을 먼저 골랐다 — 넣는 건 한 줄, 나간 zip 회수는 불가능하다.
    # 설계 배경: docs/설계_변경이력.md
    neighbors = fetch_active_relations_for_asset(conn, asset_id=seed_id)

    # 이웃 dedup: 같은 자산이 여러 엣지로 와도 한 번만(최고 confidence 보존). None → 0.0 정화.
    best_conf: dict[str, float] = {}
    for n in neighbors:
        nid = str(n["asset_id"])
        if nid == seed_id:
            continue  # self-loop 방어
        conf = n.get("confidence")
        conf = float(conf) if conf is not None else 0.0
        if conf < min_confidence:
            continue
        if nid not in best_conf or conf > best_conf[nid]:
            best_conf[nid] = conf

    # 결정적 정렬: confidence desc, asset_id asc.
    ordered = sorted(best_conf.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) > max_neighbors:
        logger.warning(
            "묶음 이웃 %d개가 max_neighbors=%d 초과 — confidence 상위 %d개로 절단(seed=%s)",
            len(ordered), max_neighbors, max_neighbors, seed_id,
        )
        ordered = ordered[:max_neighbors]

    # 최종 순서: seed 먼저 → 이웃(위 정렬). 경로 일괄 조회 후 enrich.
    ordered_ids = [seed_id] + [nid for nid, _ in ordered]
    path_map = fetch_asset_paths(conn, ordered_ids)

    targets: list[dict[str, Any]] = []
    for aid in ordered_ids:
        fs_path = path_map.get(aid)
        if fs_path is None:
            continue  # asset 행 없음(파일 경로 미상) → 묶음 제외
        targets.append(
            {"asset_id": aid, "fs_path": fs_path, "file_name": display_file_name(fs_path)}
        )
    return targets
