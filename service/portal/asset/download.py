"""원본 파일 내려받기 지원 — 대상 확정 · 열기 · 구간(Range) · 이어받기 기준값. 쓰기는 없다.

원본은 DB 에 적힌 경로(``fs_path``)에 있다고 보고 그대로 연다(경로를 바꿔 여는 설정 없음). 없으면 열 때 실패하고 호출부가 410 으로 답한다.
묶음(zip)의 대상 확정(``fetch_asset_paths`` · ``collect_bundle_assets``)도 여기 있고, zip 을 만드는 일은 ``bundle_stream`` 이 한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from service.portal.asset import origin
from service.portal.asset.origin import OriginFile
from src.config.filename_util import (
    display_file_name,
)
from src.domain.status_vocab import AssetStatus
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
    """``Range`` 헤더를 ``(start, end)``(둘 다 포함)로 파싱한다. 헤더가 없으면 ``None``(전체).

    단일 범위만 받는다: ``bytes=a-b`` · ``bytes=a-`` · ``bytes=-n``. 끝이 파일보다 크면 마지막 바이트로 줄인다(RFC 7233).
    시작이 파일 크기 이상 · 역순 · 형식 오류 · 다중 범위는 ``ValueError``(호출부가 416).
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
    """``asset_id`` 의 내려받기 대상을 찾는다. 등록 완료 자산만이고 아니면 ``None``(404)."""
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


def open_original(fs_path: str | None) -> OriginFile:
    """원본을 한 번 열어 돌려준다(``size`` · ``mtime_ns`` 포함 · 호출부가 닫는다). 경로가 비었거나 열 수 없거나 일반 파일이 아니면 OSError(호출부가 410).

    열린 핸들에서 크기를 읽어, 확인과 열기 사이에 파일이 사라지는 틈이 없다. 읽는 구현은 ``origin`` 이 고른다.
    """
    return origin.get_reader().open(fs_path)


def make_etag(size: int, mtime_ns: int) -> str:
    """이어받기 기준값 — 크기와 수정 시각으로 만든 강한 ETag(내용 해시가 아니라, 같은 크기로 같은 초에 바뀌면 못 잡는다)."""
    return f'"{size:x}-{mtime_ns:x}"'


def make_last_modified(mtime_ns: int) -> str:
    """``Last-Modified`` 값(RFC 7231 HTTP-date · 초 단위)."""
    return format_datetime(datetime.fromtimestamp(mtime_ns / 1e9, UTC), usegmt=True)


def if_range_matches(if_range: str | None, etag: str, last_modified: str) -> bool:
    """``If-Range`` 가 지금 파일과 맞는지. 조건이 없으면 맞는 것으로 본다. 다르면 호출부가 구간을 무시하고 전체를 200 으로 준다(RFC 7233 §3.2)."""
    if if_range is None:
        return True
    value = if_range.strip()
    if value.startswith(("W/", '"')):
        return value == etag          # 약한 ETag(W/) 는 If-Range 에서 맞지 않는 것으로 본다
    return value == last_modified


def fetch_asset_paths(
    conn: Connection[Any], asset_ids: list[str]
) -> dict[str, Any]:
    """자산 id 들의 파일 경로를 한 번에 조회한다(등록 완료 자산만). 경로가 없는 자산은 결과에서 빠진다."""
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
    """기준 자산과 직접 연결된 이웃을 묶음 대상으로 모은다 — 기준 자산 먼저, 이웃은 (confidence 내림차순, asset_id) 로 정렬해 ``max_neighbors`` 로 자른다.

    self-loop 와 ``min_confidence`` 미만은 뺀다. 경로를 모르는 항목도 뺀다.
    """
    seed_id = str(seed_asset_id)
    # 확인된(active) 관계만 담는다 — 틀린 파일이 담긴 zip 은 회수할 수 없다.
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
