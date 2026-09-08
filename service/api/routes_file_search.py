"""파일 검색 라우트 — 조건으로 좁히고 유사도로 줄 세우고 정확히 센다(사용 시나리오 ③).

**기존 `/search` 를 손대지 않는다.** 두 화면이 다른 일을 하기 때문이다:
    · `/search`(멀티모달 검색) — 뜻으로 **찾아오기**. 관련도 컷을 통과한 상위만 본다.
    · `/file-search`(이 창구) — 조건으로 **좁혀 훑기**. 전부 세고 페이지로 넘긴다.

**이 파일이 하는 일은 넷뿐이다**: 파라미터 검증, 질의 임베딩, 코어 조회 호출, 응답 조립(권한 가리기·
크기·수정일 붙이기). 세는 규칙·순위·집계는 전부 코어와 검색 엔진 몫이다(093 책무 경계).

🔴 **"적힌 숫자 = 누르면 나오는 수"**(2026-08-26 원칙). 칩 건수와 전체 개수를 **검색 엔진이** 세고, 그
세는 대상이 필터가 걸리는 대상과 같다. 결과 행에서 세던 종전 방식은 화면에 내려온 것만 셌기 때문에
「음악 3」을 눌렀더니 32건이 나오는 결함이 있었다(2026-09-08 실측). 이 창구는 그 구조를 바꿨고,
골든 질의 × 세 축 410건 검사에서 불일치 0 이다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import _infra
from service.portal.asset_file_meta import fetch_file_meta
from service.portal.auth import Principal, require_principal
from src.config.settings import active_embed_channel, get_current_settings
from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers
from src.search.file_search import (
    FACET_SIZE_DEFAULT,
    RANK_DEPTH_DEFAULT,
    SORT_DEFAULT,
    SORT_DEPTH_DEFAULT,
    SORT_OPTIONS,
    TOTAL_CAP_DEFAULT,
    search_files,
)
from src.search.query_embed import embed_query_for_media_search
from src.search.refine import refine_rows
from src.search.search_filters import parse_search_filters

router = APIRouter()

# 한 페이지 상한. 표로 훑는 화면이라 검색 화면(100)보다 넉넉히 두되, 한 번에 다 받게 하지는 않는다.
_PAGE_SIZE_MAX = 200
# 화면에 보일 칩 수 — 083 태그 패싯 표시 기본(12)과 같은 값으로 맞춘다(축이 달라도 눈에 보이는 양은 같게).
_FACET_SHOW = 12


def _clearance_projected(
    conn: Any, rows: list[dict[str, Any]], *, clearance: str
) -> list[dict[str, Any]]:
    """요약에서 **권한이 못 보는 항목을 지운다**(색인·검색은 건드리지 않고 응답 단계에서만).

    권한이 못 미치면 그 키를 행에서 아예 뺀다 — 키의 존재 자체가 "요약이 있다"는 정보이기 때문이다.

    Args:
        conn: DB 커넥션.
        rows: 코어가 준 결과 행. 원본을 바꾸지 않고 새 목록을 만든다.
        clearance: 요청자 권한 등급.

    Returns:
        같은 순서의 새 행 목록. 도메인마다 등급표를 한 번만 조회해 재사용한다.
    """
    tiers: dict[str, dict[str, str]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        domain = str(row.get("domain_label") or "general")
        if domain not in tiers:
            tiers[domain] = fetch_access_tiers(conn, domain)
        summary = row.get("summary") or ""
        masked = project_ext_meta({"summary": summary} if summary else {}, tiers[domain],
                                  domain=domain, clearance=clearance)
        new_row = dict(row)
        if summary and "summary" not in masked:
            new_row.pop("summary", None)
        elif "summary" in masked:
            new_row["summary"] = masked["summary"]
        out.append(new_row)
    return out


def _ext_of(file_name: str) -> str:
    """표시 파일명에서 확장자를 뽑는다(소문자 · 없으면 빈 문자열) — 표의 "종류" 칸.

    Args:
        file_name: 표시용 파일명.

    Returns:
        확장자 또는 빈 문자열.
    """
    if "." not in file_name or file_name.endswith("."):
        return ""
    return file_name.rsplit(".", 1)[-1].strip().lower()


@router.get("/file-search")
def file_search(
    q: str = Query(..., min_length=1, description="검색어 — 파일 이름과 내용을 함께 찾는다"),
    topic: list[str] | None = Query(
        None, description="주제 필터(반복 가능). 여럿이면 그중 하나라도 맞으면 남는다"),
    subtopic: list[str] | None = Query(None, description="하위주제 필터(반복 가능)"),
    tag: list[str] | None = Query(
        None,
        description=(
            "태그 필터(반복 가능 · 화면에 보인 라벨 원문 그대로). 태그끼리는 또는,"
            " 다른 칸과는 그리고 로 걸린다"
        ),
    ),
    file_ext: list[str] | None = Query(None, description="확장자 필터(반복 가능)"),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO, UTC)"),
    created_to: str | None = Query(None, description="생성일 상한"),
    refine: str | None = Query(
        None,
        description=(
            "결과 내 재검색(글자 좁히기). 공백으로 쪼갠 낱말이 **모두** 든 행만 남긴다."
            " 🔴 **이번 페이지 안에서만** 좁힌다 — 서버에 다시 묻지 않으므로 전체 개수는 그대로다"
        ),
    ),
    sort: str = Query(
        SORT_DEFAULT,
        description=(
            "정렬 — relevance(관련도 · 기본) · name_asc/name_desc(이름) ·"
            " created_desc/created_asc(등록일) · updated_desc/updated_asc(수정일) ·"
            " size_desc/size_asc(크기). 관련도 외의 정렬은 더 깊이 넘길 수 있다"
        ),
    ),
    offset: int = Query(0, ge=0, description="페이지 시작 위치(0부터)"),
    limit: int = Query(50, ge=1, le=_PAGE_SIZE_MAX, description="이 페이지의 행 수"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """조건으로 좁힌 파일을 한 페이지씩 돌려준다 — 전체 개수와 좁히기 칩을 함께.

    조건이 겹치는 방식은 검색 엔진 규칙 그대로다. 같은 칸에서 여럿 고르면 **또는**, 다른 칸끼리는
    **그리고**. 조건을 바꾸면 개수와 칩이 함께 다시 계산된다.

    **칩 숫자의 뜻**: 그 칩 **하나만** 골랐을 때 나오는 수(다른 칸 조건은 그대로 적용). 같은 칸에서
    여럿 고르면 「또는」이라 결과는 각 칩 수의 합집합이므로 개별 칩 수보다 크거나 같다. 그래서 고른
    뒤에도 같은 칸의 다른 값이 칩으로 남아 **갈아탈 수 있다**(코어 `build_facet_plan` 이 축마다 자기
    조건을 빼고 센다).

    Args:
        q: 검색어. 빈 값은 422 다(조건만으로 훑는 경로는 이 창구가 아니다).
        topic: 주제 필터(여럿 = 또는).
        subtopic: 하위주제 필터(여럿 = 또는). 주제와는 「그리고」로 걸린다.
        tag: 태그 필터. 정규화·가공 없이 코어로 넘긴다.
        file_ext: 확장자 필터.
        created_from: 생성일 하한.
        created_to: 생성일 상한.
        refine: 이번 페이지를 글자로 좁힐 말.
        sort: 정렬 이름(닫힌 목록 · 모르는 값은 422). 이름·등록일 정렬이면 **질의 임베딩을 만들지
            않는다** — 순서를 필드가 정하므로 뜻이 관여할 이유가 없다(그만큼 빠르다).
        offset: 페이지 시작 위치.
        limit: 이 페이지의 행 수.
        principal: 인증 주체.

    Returns:
        ``{query, items, total, total_capped, offset, limit, sort, facets, filters, refine?}``.
        ``total_capped`` 가 참이면 ``total`` 은 "이 수 이상"이라는 뜻이다(화면이 그렇게 표기한다).
        ``facets`` 는 ``{topic|subtopic|tag: [{key, label, count}]}`` — ``key`` 를 되보내면 그 수만큼
        나온다. 각 행에는 표에 찍을 ``file_ext``·``file_size``·``updated_at`` 이 **항상** 있다.

    Raises:
        HTTPException: 필터 형식·정렬 이름 오류는 422 · 넘길 수 있는 깊이를 넘는 페이지는 400(그 밖은
            순서를 매기지 않았으므로 빈 페이지로 돌려주면 "끝"과 구분되지 않는다) · 검색 엔진 미도달은 503.
    """
    if sort not in SORT_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"알 수 없는 정렬입니다: {sort!r} (가능: {', '.join(sorted(SORT_OPTIONS))})",
        )
    try:
        filters = parse_search_filters(
            file_ext=file_ext,
            created_from=created_from,
            created_to=created_to,
            topic=topic,
            subtopic=subtopic,
            tag=tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    # 넘길 수 있는 깊이는 정렬에 따라 다르다 — 관련도는 이웃 탐색 깊이에, 필드 정렬은 색인 결과창에 걸린다.
    by_field = SORT_OPTIONS[sort] is not None
    depth = SORT_DEPTH_DEFAULT if by_field else RANK_DEPTH_DEFAULT
    if offset + limit > depth:
        hint = "조건을 더 걸어 좁혀 주십시오"
        if not by_field:
            hint = "조건을 더 걸거나 이름·등록일 정렬로 바꿔 주십시오"
        raise HTTPException(
            status_code=400,
            detail=(
                f"이 창구는 {sort} 정렬로 상위 {depth}건까지 넘길 수 있습니다"
                f"(요청 {offset}+{limit}) — {hint}"
            ),
        )

    from src.search.opensearch_sync import get_client

    try:
        # 필드 정렬이면 뜻이 순서에 관여하지 않으므로 **임베딩을 만들지 않는다**(모델 호출을 아낀다).
        query_vector = (
            None if by_field
            else embed_query_for_media_search(q, channel=active_embed_channel())
        )
        found = search_files(
            get_client(), get_current_settings().opensearch.index,
            query=q, query_vector=query_vector, filters=filters,
            from_=offset, size=limit, sort=sort,
            rank_depth=RANK_DEPTH_DEFAULT, total_cap=TOTAL_CAP_DEFAULT,
            sort_depth=SORT_DEPTH_DEFAULT, facet_size=FACET_SIZE_DEFAULT,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # 검색 엔진에 닿지 못한 것을 빈 결과로 감추면 "자료가 없다"와 "검색이 죽었다"가 같아진다.
        _infra._LOG.warning("파일 검색 실패: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="검색 엔진에 연결할 수 없습니다") from exc

    def _finish(conn: Any) -> list[dict[str, Any]]:
        """권한 가리기와 파일 메타 붙이기를 **한 트랜잭션**에서 끝낸다(연결을 두 번 잡지 않게)."""
        rows = _clearance_projected(conn, found["rows"], clearance=principal.clearance)
        meta = fetch_file_meta(conn, [r["asset_id"] for r in rows])
        for row in rows:
            got = meta.get(row["asset_id"]) or {}
            row["file_ext"] = _ext_of(str(row.get("file_name") or ""))
            row["file_size"] = int(got.get("file_size") or 0)
            row["updated_at"] = got.get("updated_at")
            row["created_at"] = got.get("created_at")
        return rows

    items: list[dict[str, Any]] = _infra._run_in_db(_finish)  # type: ignore[assignment]

    # 결과 내 재검색은 **이번 페이지 안에서만** 좁힌다(091 과 같은 규율 — 서버에 다시 묻지 않는다).
    # 그래서 total 은 건드리지 않는다: "전체 340건 중 이 페이지에서 12건" 이 정확한 뜻이다.
    page_total = len(items)
    refine_applied = bool(refine and refine.strip())
    if refine_applied:
        items = refine_rows(items, refine, fields_of=_refine_fields)

    body: dict[str, Any] = {
        "query": q,
        "items": items,
        "total": found["total"],
        "total_capped": found["total_capped"],
        "offset": found["from"],
        "limit": found["size"],
        "sort": found["sort"],
        # 칩은 상위 몇 개만 보인다 — 코어는 넉넉히 주고 무엇을 보일지는 화면 정책이다.
        "facets": {axis: rows[:_FACET_SHOW] for axis, rows in found["facets"].items()},
        "filters": {
            "topic": list(filters.topics) if filters else [],
            "subtopic": list(filters.subtopics) if filters else [],
            "tag": list(filters.tags) if filters else [],
            "file_ext": list(filters.file_exts) if filters else [],
            "created_from": created_from,
            "created_to": created_to,
        },
    }
    if refine_applied:
        body["refine"] = {"q": refine, "page_total": page_total, "shown": len(items)}
    return body


def _refine_fields(row: Any) -> list[str]:
    """결과 내 재검색이 **글자를 찾아볼 필드** — 표에 보이는 값만 본다.

    화면 카드·표에 실제로 보이는 것으로 걸러진다는 계약이 서면 사용자가 결과에 놀라지 않는다
    (091 이 정한 규율 그대로).

    Args:
        row: 응답 행. 없거나 타입이 다른 축은 없는 것으로 본다.

    Returns:
        빈 값을 제외한 문자열 목록(파일명 → 요약 → 태그 순).
    """
    out: list[str] = []
    name = row.get("file_name")
    if isinstance(name, str) and name:
        out.append(name)
    summary = row.get("summary")
    if isinstance(summary, str) and summary:
        out.append(summary)
    tags = row.get("tags")
    if isinstance(tags, (list, tuple)):
        out.extend(t for t in tags if isinstance(t, str) and t)
    return out
