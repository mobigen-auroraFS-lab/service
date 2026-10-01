"""사용자용 자산 라우트 — 상세 조회·원본 내려받기·원문·주제 탐색·자산이 속한 개체.

**흐름에서의 위치**: 포탈 화면이 직접 부르는 경로들이다. 조회는 포탈 함수에 위임한다.

🔴 **원본 파일을 내주는 창구**(2026-10-01 다시 구현): 원본 다운로드(``/assets/{id}/download``)와 원문(``/assets/{id}/content``)은 여기 있다.
**원본은 DB 에 적힌 경로(``fs_path``)에 있다고 보고** 읽는다 — 경로를 바꿔 여는 설정은 없고, 없으면 **410** 봉투로 답한다(예외를 그대로 올리지 않는다).
썸네일 · 관계 묶음 · 고른 자산 묶음 · 개체 묶음은 협의가 더 필요해 ``routes/files.py`` 에 **자리만** 있다(501).
원본 위치 · 접근 방식 · 변경 대조 규칙은 ``service/portal/asset/__init__.py`` 의 「원본 파일 전제」(연동 협의안 v1.0)를 따른다.

⚠️ **라우트 선언 순서가 동작을 가른다.** ``/assets/unclassified`` 처럼 고정된 경로를
``/assets/{asset_id}`` 보다 **먼저** 선언해야 한다 — 뒤에 두면 "unclassified" 가 자산 id 로
해석돼 영영 404 가 된다.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Iterator
from typing import Annotated, Any, BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from service.api import params
from service.api.bundle_response import make_plan, zip_response
from service.portal.asset.content import build_content
from service.portal.asset.download import (
    if_range_matches,
    make_etag,
    make_last_modified,
    open_original,
    parse_range_header,
)
from service.portal.asset.selection import MAX_SELECTION, MAX_SELECTION_BYTES
from service.portal.auth import Principal, require_principal
from service.portal.common.db_manager import DbManager
from service.portal.history.access_log import record_access_many
from src.config.filename_util import display_file_name

# 경로가 ``/assets`` 와 ``/topics`` 로 갈려 공통 접두사를 둘 수 없다 — 인증만 라우터에 건다.
# ⚠️ 주체가 필요한 핸들러만 ``principal`` 을 따로 선언한다.
router = APIRouter(tags=["assets"], dependencies=[Depends(require_principal)])

_LOG = logging.getLogger("meta_extract.portal_api")

# 스트리밍 조각 크기(1MiB) — 영상 같은 큰 파일을 메모리에 통째로 올리지 않으면서, 조각마다 드는 스레드 전환 · 파이썬 오버헤드를 줄인다.
# 🔴 [2026-10-01 실측] 같은 208MB 파일을 루프백으로 받았을 때 64KiB 는 한 줄기 약 520MB/s · 여러 줄기 합 약 750MB/s 에서 서버 CPU 가 먼저 찼고,
#    1MiB 는 한 줄기 약 3.3GB/s · 합 약 4.4GB/s 였다(약 6배). 실제 망(1~10Gbps)에서는 망이 먼저 차겠지만, 다운로드를 여러 줄기로 받는
#    묶음(브라우저 zip) 때 서버가 받는 CPU 를 줄인다. 요청당 잡는 메모리는 조각 하나(1MiB)뿐이다.
_STREAM_CHUNK = 1024 * 1024

_DOWNLOAD_404 = "다운로드 대상을 찾을 수 없거나 노출 대상이 아님"
_FILE_GONE_410 = "원본 파일이 존재하지 않거나 접근할 수 없음"
_CONTENT_404 = "자산을 찾을 수 없거나 노출 대상이 아님"


@router.get("/assets/unclassified")
def unclassified_assets(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """주제가 붙지 않은 자산을 페이징해 돌려준다 — 탐색 화면의 '미분류' 폴더.

    주제 트리(``/topics``)는 ``asset_topic`` 조인이라 주제 정본이 없는 자산(분류 실패·무내용)을 누락한다.
    자산을 '빠짐없이' 보이려면 이 엔드포인트로 미분류를 회수한다. 조회 전용·도메인 제외 없음·LLM 0.
    **라우트 순서**: ``/assets/{asset_id}`` catch-all 보다 먼저 등록해야 'unclassified' 가 asset_id 로
    오매칭되지 않는다(이 위치 유지).
    """
    return DbManager.read(lambda repo: repo.asset.unclassified(limit=limit, offset=offset))


@router.get("/assets/{asset_id}")
def asset_detail(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 1건 상세 — 메타·임베딩 요약·관계 미니뷰·자기주제.

    노출 여부 판정은 ``fetch_asset_detail`` 이 맡는다 — 없거나 등록 완료가 아니면 404.
    UUID 형식이 아닌 id 도 **같은 404** 다(둘을 가르면 "그 모양의 id 는 있을 수 있다"를 알려 준다).
    노출을 통과한 자산에는 주제 정보(``topics``·``same_topic_groups``)를 같은 읽기
    트랜잭션에서 함께 싣는다(신규 LLM 0). 게이트 미통과(None)면 주제 seam 미호출.
    """
    params.uuid_or_404(asset_id, detail="자산을 찾을 수 없거나 노출 대상이 아님")

    detail = DbManager.read(
        lambda repo: repo.asset.detail(asset_id=asset_id, clearance=principal.clearance))
    if detail is None:
        raise HTTPException(status_code=404, detail="자산을 찾을 수 없거나 노출 대상이 아님")
    return detail


@router.get("/topics")
def topics_list() -> dict[str, Any]:
    """주제 목록을 2단계(대주제 → 세부주제)로, 주제별 자산 수와 함께 돌려준다(조회 전용).

    ``list_topics`` 가 자기주제 정본(``asset_topic``·도메인 제외 없음)의 ``(topic_ko, subtopic_ko)`` 별 distinct
    정렬을 고정해(대주제 → 세부주제 이름순) 같은 요청이 늘 같은 순서를 낸다. 각 행에
    ``topic_asset_count``(주제 전체 distinct 자산 수) 동반(하위호환 필드).
    """
    return {"topics": DbManager.read(lambda repo: repo.asset.topics())}


@router.get("/topics/{topic}")
def topic_assets(
    topic: str,
    subtopic: str | None = Query(None, description="세부주제(주면 topic 하위로 좁힘·정확 일치)"),
    unassigned: bool = Query(
        False, description="'기타'(subtopic 미부여)만 — 값 매칭이 아닌 subtopic IS NULL"
    ),
    modality: str | None = Query(None, description="모달리티 폴더(text/image/video/audio) 필터"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """특정 주제에 속한 자산을 페이징 조회한다(조회 전용).

    ``assets_in_topic`` 이 그 주제의 자기주제 정본(``asset_topic``) 자산을 distinct·``asset_id asc`` 결정적
    정렬로 페이징한다. ``subtopic`` 미지정=topic 하위 전체·``unassigned=true``='기타'(IS NULL)만·
    ``modality`` 필터. 응답 ``modality_counts`` 는 필터 무관 전체 분포(모달리티 폴더 카운트).
    """
    return DbManager.read(
        lambda repo: repo.asset.topic_assets(
            topic_ko=topic,
            subtopic_ko=subtopic,
            unassigned_only=unassigned,
            modality=modality,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/assets/{asset_id}/mm-meta")
def asset_mm_meta(
    asset_id: str,
) -> dict[str, Any]:
    """자산 상세의 "이 파일이 속한 개체" 블록 — 코어 seam 위임.

    ⚠️ **묶음 크기 1 도 그대로 싣는다.** 목록 창구는 1 인 개체를 감추지만(자산 하나짜리는 "묶음"이
    아니다), 자산 쪽에서 보면 "이 파일이 그 개체에 속한다"는 것은 사실이다. 감출지는 화면이 정한다.

    Args:
        asset_id: 자산 UUID(문자열).

    Returns:
        ``{"items": [{entity_type, entity_uid, name, bundle_size, edge_id, status, reason}]}`` —
        종류·표기 키 오름차순. 소속이 없으면 빈 목록이다.
    """
    # 형식이 아닌 id 는 **없는 자산과 같게** 본다 — 이 창구는 미존재를 200·빈 목록으로 답한다.
    if not params.is_uuid(asset_id):
        return {"items": []}
    items = DbManager.read(lambda repo: repo.asset.entities_of(asset_id=asset_id))
    return {"items": items}


def _guess_content_type(file_name: str, modality: str | None) -> str:
    """내려줄 파일의 MIME 타입을 정한다.

    Args:
        file_name: 확장자를 볼 파일명.
        modality: 확장자로 못 알아냈을 때 쓸 단서. ``None`` 이어도 된다.

    Returns:
        MIME 문자열. 끝까지 모르면 ``application/octet-stream``(브라우저가 열지 않고 저장한다).
    """
    ctype, _ = mimetypes.guess_type(file_name)
    if ctype:
        return ctype
    fallback = {"text": "text/plain; charset=utf-8"}
    return fallback.get(modality or "", "application/octet-stream")


def _content_disposition(file_name: str) -> str:
    """RFC 6266 attachment 헤더(ASCII filename + UTF-8 filename* 병기).

    ⚠️ ASCII 쪽에서 큰따옴표·개행·비-ASCII 를 **반드시 제거한다** — 그대로 두면 헤더가 쪼개져
    응답 위조에 쓰일 수 있다. 한글 파일명은 UTF-8 쪽에 인코딩해 함께 싣는다.

    Args:
        file_name: 내려받을 때 보일 파일명(경로가 아니라 이름만).

    Returns:
        ``Content-Disposition`` 헤더 값.
    """
    ascii_safe = "".join(c for c in file_name if c.isascii() and c.isprintable() and c != '"')
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{quote(file_name)}'


def _file_iterator(fh: BinaryIO, start: int, end: int) -> Iterator[bytes]:
    """열려 있는 파일의 특정 구간을 조각내어 흘려보낸다. 끝나거나 끊기면 파일을 닫는다.

    Args:
        fh: ``open_original`` 이 연 핸들(이 함수가 닫는다).
        start: 시작 바이트(**포함**).
        end: 끝 바이트(**포함**) — 구간 요청 규격이 양 끝을 포함하므로 길이는 ``end-start+1`` 이다.

    Yields:
        바이트 조각. 파일이 도중에 짧아지면 거기서 멈춘다(예외를 올리지 않는다 — 클라이언트는
        ``Content-Length`` 와 받은 길이로 불완전함을 알아챈다).
    """
    remaining = end - start + 1
    try:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        fh.close()


@router.get("/assets/{asset_id}/download")
def download(asset_id: str, request: Request) -> StreamingResponse:
    """자산 원본을 스트리밍한다 — 구간 요청(이어받기 · 영상 탐색)을 지원한다.

    1. 노출 대상인지 먼저 확인한다 — 아니면 404.
    2. 원본을 **한 번 열어** 크기를 확정한다 — DB 에 적힌 경로에 없거나 못 열면 410(자산 기록은 있으나 파일이 사라진 상태).
       열린 핸들에서 크기를 읽어, 확인과 읽기 사이에 파일이 사라지는 틈이 없다.
    3. ``Range`` 헤더가 있으면 구간을 산출해 206 + ``Content-Range``; 범위 위반은 416. 바이트 산출은 **디스크 실제 크기** 기준이다.
       ``If-Range`` 가 지금 파일과 다르면(받는 도중 원본이 바뀜) 구간을 무시하고 전체를 200 으로 준다.
    ``Accept-Ranges: bytes`` · ``ETag``(크기 · 수정 시각) · ``Last-Modified`` 를 항상 싣는다.
    """
    params.uuid_or_404(asset_id, detail=_DOWNLOAD_404)
    target = DbManager.read(lambda repo: repo.asset.download_target(asset_id=asset_id))
    if target is None:
        raise HTTPException(status_code=404, detail=_DOWNLOAD_404)

    fs_path = target.get("fs_path")
    try:
        fh, file_size, mtime_ns = open_original(fs_path)
    except OSError as exc:
        # 서버 경로는 응답에 싣지 않는다 — 운영자가 볼 로그에만 남긴다.
        _LOG.warning("원본을 열 수 없음(410): asset_id=%s fs_path=%s — %s", asset_id, fs_path, type(exc).__name__)
        raise HTTPException(status_code=410, detail=_FILE_GONE_410) from exc

    file_name = target.get("file_name") or display_file_name(fs_path)
    etag, last_modified = make_etag(file_size, mtime_ns), make_last_modified(mtime_ns)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(file_name),
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "private, no-cache",       # 인증이 걸린 응답이라 공유 캐시에 남기지 않는다
        "X-Content-Type-Options": "nosniff",
    }

    range_value = request.headers.get("range")
    if range_value is not None and not if_range_matches(request.headers.get("if-range"), etag, last_modified):
        range_value = None                          # 받는 도중 바뀐 파일 — 조각을 섞지 않고 전체를 준다
    try:
        rng = parse_range_header(range_value, file_size)
    except ValueError as exc:
        fh.close()
        raise HTTPException(
            status_code=416,
            detail=f"요청 범위 충족 불가: {exc}",
            headers={"Content-Range": f"bytes */{file_size}"},
        ) from exc

    # 구간 요청이 아니면 전체를 200 으로, 맞으면 부분 응답 206 으로 — 클라이언트는 이 코드로 이어받기 성공 여부를 판단한다.
    if rng is None:
        start, end, status_code = 0, file_size - 1, 200
    else:
        start, end = rng
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    # 양 끝을 포함하는 구간이라 길이는 +1 이다(빼먹으면 마지막 1바이트가 잘린다). 빈 파일(0바이트)은 길이 0 이다.
    headers["Content-Length"] = str(max(end - start + 1, 0))
    return StreamingResponse(
        _file_iterator(fh, start, end) if file_size else iter(()),
        status_code=status_code,
        media_type=_guess_content_type(file_name, target.get("modality")),
        headers=headers,
        background=BackgroundTask(fh.close),        # 응답이 끊겨 조각 읽기가 멈춰도 핸들은 반드시 닫는다
    )


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str) -> dict[str, Any]:
    """자산의 **글자 내용**을 돌려준다 — 상세 화면의 원문 영역용.

    문서는 원본 파일에서, 소리·영상은 받아쓰기(``ext_meta.stt``)에서. 글자가 없는 자산(그림 · 받아쓰기 없는 소리·영상)도
    404 지만 **문구가 다르다**(「이 자산에는 읽을 수 있는 원문이 없습니다」) — 등록 자산이면 상세(`/assets/{id}`)가
    이미 열리므로 존재 여부를 숨길 이유가 없고, 화면은 이 문구로 '원문 없음'과 '없는 자산'을 가른다.
    ⚠️ 지금 데이터에서 받아쓰기는 소리에만 있다 — 영상은 404 다(``content`` 모듈 설명 참조).

    Raises:
        HTTPException: 노출 대상이 아니거나 글자가 없으면 404 · 원본 파일이 사라졌으면 410.
    """
    params.uuid_or_404(asset_id, detail=_CONTENT_404)
    source = DbManager.read(lambda repo: repo.content.source_of(asset_id))
    if source is None:
        raise HTTPException(status_code=404, detail=_CONTENT_404)
    try:
        # 파일 읽기는 **DB 트랜잭션 밖**에서 한다 — 느린 디스크가 커넥션을 붙잡지 않게.
        body = build_content(source)
    except OSError as exc:
        raise HTTPException(status_code=410, detail=_FILE_GONE_410) from exc
    if body is None:
        raise HTTPException(status_code=404, detail="이 자산에는 읽을 수 있는 원문이 없습니다")
    return body


@router.get("/assets/{asset_id}/bundle")
def bundle(asset_id: str) -> StreamingResponse:
    """기준 자산과 **직접 연결된 이웃들**을 한 zip 으로 — 임시 파일 없이 만들면서 흘려 보낸다.

    seed 는 노출을 먼저 확인한다 — 없음/비registered 면 404. 이웃은 **확인된(active) 관계**만, 등록 완료 자산만 담는다
    (틀린 파일이 담긴 zip 은 회수할 수 없어 확인된 것만 — ``collect_bundle_assets`` 주석).
    원본이 없는 항목은 건너뛰고 ZIP 끝의 ``_manifest.json`` 에 남긴다(부분 zip). 전부 누락이면 목록 파일만 든 zip 이다.
    방식 선택의 배경과 대안(브라우저가 묶기 · 서버 비동기 zip)은 ``service/portal/asset/bundle_stream.py`` 설명에 있다.
    """
    params.uuid_or_404(asset_id, detail="묶음 seed 를 찾을 수 없거나 노출 대상이 아님")
    targets = DbManager.read(lambda repo: repo.asset.bundle_targets(seed_asset_id=asset_id))
    if targets is None:
        raise HTTPException(status_code=404, detail="묶음 seed 를 찾을 수 없거나 노출 대상이 아님")
    plan = make_plan(targets)       # 응답을 시작하기 전에 존재 · 크기를 확인한다(파일 읽기는 DB 트랜잭션 밖)
    return zip_response(
        plan, content_disposition=_content_disposition(f"bundle_{asset_id}.zip"),
        headers={"X-Bundle-Count": str(len(plan.packed)), "X-Bundle-Missing": str(len(plan.unreadable)),
                 "X-Bundle-Bytes": str(plan.total_bytes)},
    )


# ── 고른 자산 여러 건을 한 zip 으로 ───────────────────────────────────────────────
class SelectionBundleRequest(BaseModel):
    """화면에서 고른 자산 목록 — '전체 선택' 같은 암묵 대상은 받지 않는다(관계 검토와 같은 원칙)."""

    asset_ids: list[str]


@router.post("/assets/bundle")
def selection_bundle(
    payload: SelectionBundleRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> StreamingResponse:
    """**고른 자산들**을 한 zip 으로 — 임시 파일 없이 만들면서 흘려 보낸다(목록 화면의 일괄 내려받기).

    관계 묶음과 달리 대상을 사용자가 정한다. 노출 게이트 · 부분 zip · 목록 파일(manifest) 규칙은 같다 — 노출되지 않는 자산은
    빠지되 **빠졌다는 사실은 manifest 에 남는다**. 감사는 담긴 자산마다 ``bundle`` 한 행(미들웨어는 GET 만 기록한다).

    🔴 여러 파일을 받는 방식은 **서버가 묶는 이 창구**와 **웹이 단건 다운로드를 병렬로 받아 브라우저에서 묶는 방식** 둘이 있다
    (``bundle_stream`` 설명). 지금은 이 창구가 정본이고, 웹이 HTTPS 와 스트리밍 저장을 갖추면 옮겨 갈 수 있다.

    Raises:
        HTTPException: 빈 목록 · 형식 오류 · 건수 초과는 400 · 용량 초과는 413 · 내려받을 자산이 하나도 없으면 409.
    """
    asset_ids = params.uuid_list_or_400(payload.asset_ids, field="asset_ids")
    if not asset_ids:
        raise HTTPException(status_code=400, detail="asset_ids 가 비어 있습니다 — 내려받을 자산을 고르십시오")
    if len(asset_ids) > MAX_SELECTION:
        raise HTTPException(
            status_code=400,
            detail=f"한 번에 {MAX_SELECTION}건까지 묶을 수 있습니다(요청 {len(asset_ids)}건)")

    picked = DbManager.read(lambda repo: repo.selection.targets(asset_ids))
    targets = picked["targets"]
    if not targets:
        raise HTTPException(status_code=409, detail="내려받을 수 있는 자산이 없습니다 — 모두 노출 대상이 아닙니다")
    mb, cap = 1024 * 1024, MAX_SELECTION_BYTES // (1024 * 1024)
    if picked["total_bytes"] > MAX_SELECTION_BYTES:       # DB 에 적힌 크기로 먼저 거른다(파일을 건드리기 전)
        raise HTTPException(
            status_code=413, detail=f"용량이 상한을 넘습니다({picked['total_bytes'] // mb}MB > {cap}MB) — 고른 자산을 줄이십시오")
    plan = make_plan(targets)
    if plan.total_bytes > MAX_SELECTION_BYTES:           # 디스크의 실제 크기로 한 번 더 — DB 값이 틀려도 상한을 지킨다
        raise HTTPException(
            status_code=413, detail=f"용량이 상한을 넘습니다({plan.total_bytes // mb}MB > {cap}MB) — 고른 자산을 줄이십시오")

    packed_ids = [t["asset_id"] for t in plan.packed]

    def _audit(repo: Any) -> None:
        """담길 자산마다 한 행 — 개별 다운로드와 같은 낱개로 남겨야 이력이 맞물린다."""
        record_access_many(repo.conn, action="bundle", user_id=principal.user_id,
                           asset_ids=packed_ids, detail={"selection": len(packed_ids)})

    try:
        DbManager.write(_audit)
    except Exception:  # noqa: BLE001 — 감사 실패가 내려받기를 막지 않는다(최선 노력)
        _LOG.warning("선택 묶음 감사 기록 실패(무시): %d건", len(packed_ids))

    return zip_response(
        plan, content_disposition=_content_disposition(f"selection_{len(targets)}.zip"),
        headers={
            # 🔴 zip 은 스트리밍이라 실패가 본문 도중에 드러난다 — 몇 건이 담기고 몇 건이 빠지는지 헤더로 먼저 알려,
            #    화면이 받은 zip 이 온전한지 스스로 맞춰 볼 수 있게 한다. 빠진 수 = 노출 대상이 아니라 뺀 것 + 원본이 없는 것.
            "X-Bundle-Files": str(len(plan.packed)),
            "X-Bundle-Missing": str(len(picked["missing"]) + len(plan.unreadable)),
            "X-Bundle-Bytes": str(plan.total_bytes),
        },
    )
