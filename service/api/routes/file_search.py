"""파일 검색 라우트 — 조건으로 좁히고 유사도로 줄 세우고 정확히 센다.

``/search`` 는 패싯도 페이징도 만들 수 없어(관련도 컷을 파이썬에서 계산) 이 창구가 대신한다. ``/search`` 코드는 과제 산출물이라 지우지 않는다.
이 파일은 파라미터 검증 · 질의 임베딩 · 코어 조회 호출 · 응답 조립(권한 가리기 · 크기 · 수정일)만 한다. 세기 · 순위 · 집계는 코어와 검색 엔진 몫이다.
"적힌 숫자 = 누르면 나오는 수" — 칩 건수와 전체 개수를 검색 엔진이 필터와 같은 대상으로 센다.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import errors, params
from service.portal.auth import Principal, require_principal
from service.portal.common.db_manager import DbManager
from service.portal.common.stage_timer import stage
from service.portal.search.extra_facets import AXES as EXTRA_AXES
from service.portal.search.extra_facets import SIZE_BUCKETS, extra_facets
from src.config.search_modalities import VALID_SEARCH_MODALITIES
from src.config.settings import active_embed_channel, get_current_settings
from src.search.cursor import CursorError
from src.search.file_search import (
    ABOUT_BRANCH_DEFAULT,
    FACET_SIZE_DEFAULT,
    RANK_DEPTH_DEFAULT,
    SEARCH_PIPELINE_DEFAULT,
    SEMANTIC_CAP_DEFAULT,
    SEMANTIC_MIN_COSINE_DEFAULT,
    SORT_DEFAULT,
    SORT_DEPTH_DEFAULT,
    SORT_OPTIONS,
    STABLE_SORTS,
    TOTAL_CAP_DEFAULT,
    WORD_OPERATOR_DEFAULT,
    browse_files,
    search_files,
)
from src.search.query_embed import embed_query_for_media_search
from src.search.search_filters import applied_date_bounds, parse_search_filters

router = APIRouter(tags=["search"])

_LOG = logging.getLogger("meta_extract.portal_api")

# 크기 구간 이름 — 칩(``facet-extra``)과 **같은 이름**을 쓴다(화면이 두 벌로 외우지 않게).
SIZE_BUCKET_KEYS = tuple(k for k, _, _ in SIZE_BUCKETS)

# 질의 길이 상한 — 검색 엔진이 낱말마다 절을 만들고 1024개에서 거절한다. 너무 긴 질의는 사용자가 고칠 문제라 422 로 앞에서 끊는다.
_QUERY_MAX_LEN = 300

# 한 페이지 상한. 표로 훑는 화면이라 검색 화면(100)보다 넉넉히 두되, 한 번에 다 받게 하지는 않는다.
_PAGE_SIZE_MAX = 200
# 화면에 보일 칩 수 — 083 태그 패싯 표시 기본(12)과 같은 값으로 맞춘다(축이 달라도 눈에 보이는 양은 같게).
_FACET_SHOW = 12
# 반복 파라미터 한 번에 받을 개수 상한 — 수천 개로 terms 절을 부풀리는 요청을 막는다.
_REPEAT_MAX = 50
# 깊이에 닿았을 때 권하는 정렬(spec §2-5) — 끝까지 넘길 수 있는 **안정** 정렬만 고른다.
_SUGGEST_SORT: tuple[str, ...] = ("created_desc", "name_asc")
# 검색 엔진 연결 실패로 볼 예외들(클라이언트가 없는 환경에서는 빈 튜플).
_OS_CONN_ERRORS: tuple[type[BaseException], ...] = (
    (errors.OSConnectionError,) if errors.OSConnectionError is not None else ()
)


def _cursor_scope(
    *, q: str, refine: str | None, filters: Any, applied_dates: tuple[str | None, str | None]
) -> str:
    """이번 결과 집합을 정의하는 조건 전부를 문자열 하나로 모은다(커서 조건 지문 재료). 코어는 이 문자열의 해시만 커서에 찍어 두었다가 다음 쪽에서 같은지 본다.

    넣은 것: ``q`` · ``refine`` · 칩 필터 5종 · 기간(원문이 아니라 실제 적용값). 모두 결과 집합을 바꾼다.
    뺀 것: ``sort``(코어 커서가 따로 대조한다) · ``limit``/``offset``(집합을 바꾸지 않는다 — 넣으면 쪽 크기를 바꾼 순회가 끊긴다) · 사용자 등급(가리기는 응답 직전에 걸린다. 엔진 조건으로 내려가면 넣어야 한다).
    ⚠️ 필터를 늘리면 여기도 늘린다 — 빠뜨리면 그 조건만 바뀐 커서가 조용히 통과해 자료가 빠진다.

    같은 조건이면 언제나 같은 문자열이다(목록형 조건은 정렬해 담는다).
    """
    def _sorted(values: Any) -> list[str]:
        """반복 조건을 정렬된 문자열 목록으로(순서 차이를 흡수한다)."""
        return sorted(str(v) for v in (values or ()))

    material = {
        "q": q.strip(),
        "refine": (refine or "").strip(),
        "topic": _sorted(getattr(filters, "topics", ())),
        "subtopic": _sorted(getattr(filters, "subtopics", ())),
        "tag": _sorted(getattr(filters, "tags", ())),
        "modality": _sorted(getattr(filters, "modalities", ())),
        "file_ext": _sorted(getattr(filters, "file_exts", ())),
        "created_from": applied_dates[0],
        "created_to": applied_dates[1],
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ext_of(file_name: str) -> str:
    """표시 파일명에서 확장자를 뽑는다(소문자 · 없으면 빈 문자열)."""
    if "." not in file_name or file_name.endswith("."):
        return ""
    return file_name.rsplit(".", 1)[-1].strip().lower()


@router.get("/file-search")
def file_search(
    q: str = Query(
        "",
        max_length=_QUERY_MAX_LEN,
        description=(
            "검색어 — 파일 이름과 내용을 함께 찾는다."
            " **비우면 조건에 맞는 전부**(첫 화면 훑기 · 097). 그때는 관련도가 뜻이 없으므로"
            " 정렬이 relevance 면 created_desc 로 바꾼다"
        ),
    ),
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
    modality: list[str] | None = Query(
        None,
        description=(
            "종류 필터(반복 가능 · text|image|video|audio · 닫힌 어휘라 그 밖의 값은 422)."
            " 멀티모달 검색이 결과를 버킷으로 나눠 보이던 것을 칩 축으로 대신한다 —"
            " 건수가 함께 보이고 눌러 좁힌다"
        ),
    ),
    file_ext: list[str] | None = Query(None, description="확장자 필터(반복 가능)"),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO, UTC)"),
    created_to: str | None = Query(None, description="생성일 상한"),
    refine: str | None = Query(
        None,
        max_length=_QUERY_MAX_LEN,
        description=(
            "결과 내 재검색(좁히기). 공백으로 쪼갠 낱말이 **모두** 든 자산만 남긴다."
            " 🔴 **결과 집합 전체**에 걸린다(099) — 몇 쪽에 있든 걸리며 total 도 함께 줄어든다."
            " 🔴 **낱말 단위**다: `치찌` 로 `김치찌개` 는 걸리지 않고, `김치를` 은 `김치` 에 걸린다."
            " refine 을 바꾸면 집합이 바뀌므로 cursor 는 버리고 처음부터 받아야 한다 —"
            " 099 G7 부터 **서버가 강제한다**(조건이 바뀐 커서는 400)"
        ),
    ),
    size_bucket: str | None = Query(
        None,
        description=("파일 크기 구간: under1 | 1to10 | over10. ⚠️ **아직 거르지 못한다**(501) — "
                     "건수는 /file-search/facet-extra 가 준다"),
    ),
    sort: str = Query(
        SORT_DEFAULT,
        description=(
            "정렬 — relevance(관련도 · 기본) · name_asc/name_desc(이름) ·"
            " created_desc/created_asc(등록일) · updated_desc/updated_asc(수정일) ·"
            " size_desc/size_asc(크기). 관련도 외의 정렬은 더 깊이 넘길 수 있다"
        ),
    ),
    offset: int = Query(0, ge=0, description="페이지 시작 위치(0부터) — 얕은 페이지용. cursor 와 함께 줄 수 없다"),
    cursor: str | None = Query(
        None,
        description=(
            "이어 읽기 표식(책갈피 · 097). 직전 응답의 next_cursor 를 그대로 넘긴다."
            " 🔴 offset 과 달리 **1만 건 벽이 없다** — 무한 스크롤용."
            " relevance 정렬에는 줄 수 없다(점수가 상위 일부만 계산돼 이어받을 기준값이 없다)"
        ),
    ),
    limit: int = Query(50, ge=1, le=_PAGE_SIZE_MAX, description="이 페이지의 행 수"),
    with_facets: bool = Query(
        True,
        description=(
            "칩(facets)을 함께 셀지. 기본 true. 🔴 다음 쪽(cursor · offset)을 받을 때는 false 로 보내라 —"
            " 칩 · 총계는 쪽과 무관해 첫 쪽 것을 그대로 쓰면 되고, 서버는 칩 집계를 건너뛴다(응답 facets 는 {})."
            " total 은 그대로 센다"
        ),
    ),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """조건으로 좁힌 파일을 한 페이지씩 돌려준다 — 전체 개수와 좁히기 칩을 함께.

    ``q`` 를 비우면 조건에 맞는 전부를 훑는다(훑기에서 relevance 면 created_desc 로 바꾼다). 여러 값은 「또는」, 주제와 하위주제는 「그리고」다.
    ``cursor`` 는 직전 응답의 ``next_cursor`` 를 그대로 넘기며 offset 과 함께 주면 400, relevance 정렬에는 줄 수 없고(400), 조건이 하나라도 바뀌면 400 이다.
    ``with_facets`` 가 거짓이면 칩 질의를 건너뛴다(``facets`` 는 ``{}`` · 총계는 센다).

    Returns:
        ``{items, total, scope_total, total_capped, offset, limit, sort, next_cursor, applied, facets, filters, refine?}``.
        ``total`` 은 좁히기 이후, ``scope_total`` 은 이전 결과 집합 크기(둘 다 모수이고 돌려준 개수가 아니다). ``total_capped`` 가 참이면 ``total`` 은 "이 수 이상"이다.
        ``facets`` 는 ``{축: [{key, label, count}]}`` — ``key`` 를 되보내면 그 수만큼 나온다.

    Raises:
        HTTPException: 형식 · 어휘 · 개수 위반 422 · 자리 모순 · 깊이 초과 · 깨진 커서 400 · 엔진 미도달 503.
    """
    # 집합 = 세 갈래의 합집합 중 조건에 맞는 것 — ① 검색어의 모든 형태소가 든 파일 ② 뜻이 가까운 파일(코사인 하한 이상) ③ 개체(about)가 질의 낱말과 같은 파일.
    # 셋 다 검색 엔진이 판정해 개수와 칩을 정확히 센다. 같은 칸에서 여럿 고르면 「또는」, 다른 칸끼리는 「그리고」.
    # 칩 숫자는 그 칩 하나만 골랐을 때 나오는 수다(코어가 축마다 자기 조건을 빼고 센다).
    # 각 행에는 표에 찍을 `file_ext`·`file_size`·`updated_at` 이 항상 있다(`_finish` 가 붙인다).
    # 입력 오류는 전부 422 로 통일한다. 검색어가 비면 조건만으로 훑는 화면이다(커서 경로).
    browsing = not q.strip()
    cursor = params.cursor_or_none(cursor)   # 빈 커서(cursor=)는 커서 없음 — 아래 판단이 갈리지 않게
    if cursor is not None and offset:
        raise HTTPException(
            status_code=400,
            detail="cursor 와 offset 은 함께 줄 수 없습니다 — 어느 자리부터 읽을지 모호합니다",
        )
    # 훑기는 커서 전용 경로라 offset 이 버려진다 — 조용히 버리지 않고 400 으로 알린다.
    if browsing and offset:
        raise HTTPException(
            status_code=400,
            detail=("검색어가 없는 훑기에서는 offset 을 쓸 수 없습니다 — next_cursor 로 이어 읽으십시오"
                    "(offset 은 검색어가 있을 때의 얕은 페이지 전용)"),
        )
    # 대체를 먼저 한다 — 거부를 먼저 두면 자기가 준 커서를 자기가 막는다(첫 쪽은 sort 없이 와 relevance 인데 커서에는 created_desc 가 찍힌다).
    if browsing and sort == SORT_DEFAULT:
        sort = "created_desc"
    # 관련도는 이어받을 기준값이 없다 — 막다른 길이 아니라 정렬을 바꾸라고 안내한다.
    if cursor is not None and sort == SORT_DEFAULT:
        raise HTTPException(
            status_code=400,
            detail=(
                "관련도 정렬은 이어 읽기(cursor)를 지원하지 않습니다 — "
                "created_desc·name_asc 로 바꾸면 끝까지 넘길 수 있습니다"
            ),
        )
    for name, values in (("topic", topic), ("subtopic", subtopic), ("tag", tag),
                         ("modality", modality), ("file_ext", file_ext)):
        if values is not None and len(values) > _REPEAT_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"{name} 은(는) 한 번에 {_REPEAT_MAX}개까지만 받습니다(요청 {len(values)}개)",
            )
    # TODO(코어): 크기로 거르는 것은 엔진 필터에 크기 축이 없어 아직 못 한다 — 자리만 열고 501 로 막는다(조용히 무시하면 잘못된 목록을 보여 준다).
    if size_bucket is not None:
        if size_bucket not in SIZE_BUCKET_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"알 수 없는 크기 구간입니다: {size_bucket!r} (가능: {', '.join(SIZE_BUCKET_KEYS)})")
        raise HTTPException(
            status_code=501,
            detail=("크기 구간으로 거르기는 아직 제공하지 않습니다 — 건수는 "
                    "/file-search/facet-extra?axis=file_size 가 줍니다"))
    if sort not in SORT_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"알 수 없는 정렬입니다: {sort!r} (가능: {', '.join(sorted(SORT_OPTIONS))})",
        )
    # 종류는 닫힌 어휘다 — 모르는 값을 조용히 0건으로 넘기면 오타가 '파일이 없다'로 읽힌다.
    unknown_mods = [m for m in (modality or []) if m not in VALID_SEARCH_MODALITIES]
    if unknown_mods:
        raise HTTPException(
            status_code=422,
            detail=(
                f"알 수 없는 종류입니다: {unknown_mods} "
                f"(가능: {', '.join(VALID_SEARCH_MODALITIES)})"
            ),
        )
    try:
        filters = parse_search_filters(
            file_ext=file_ext,
            created_from=created_from,
            created_to=created_to,
            topic=topic,
            subtopic=subtopic,
            modality=modality,
            tag=tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    # 넘길 수 있는 깊이는 정렬마다 다르다. 깊이에 닿는 것은 오류가 아니라 안내라 200 + depth_limited 로 정렬 전환을 권한다(경계에 걸친 요청은 남은 건수를 준다).
    # 종전 offset 호출은 search_files, 커서 · 훑기는 browse_files 로 나란히 둔다.
    use_cursor = cursor is not None or browsing
    by_field = SORT_OPTIONS[sort] is not None
    depth = SORT_DEPTH_DEFAULT if by_field else RANK_DEPTH_DEFAULT
    depth_limited = not use_cursor and offset + limit > depth
    # 깊이 밖이어도 총계 · 칩은 세야 하므로 첫 쪽 한 건을 떠보고 행은 버린다(코어가 size=0 과 깊이 초과 from_ 을 거부한다).
    beyond_depth = depth_limited and offset >= depth
    page_from, page_size = offset, limit
    if depth_limited:
        page_size = depth - offset
    if beyond_depth:
        page_from, page_size = 0, 1

    from service.api.search_health import get_client

    # 뜻이 집합 판정에 쓰이므로 정렬과 무관하게 임베딩이 필요하다. 임베딩 서버 장애는 검색 엔진 장애와 따로 알린다(코어 임베더는 API 실패를 RuntimeError, 응답 이상을 ValueError 로 올린다).
    # 훑기(검색어 없음)에는 임베딩이 필요 없어 임베딩 서버가 죽어 있어도 첫 화면은 뜬다.
    query_vector: list[float] | None = None
    if not browsing:
        try:
            with stage("embed"):
                query_vector = embed_query_for_media_search(q, channel=active_embed_channel())
        except (RuntimeError, ValueError) as exc:
            _LOG.warning("질의 임베딩 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
            raise HTTPException(status_code=503, detail="임베딩 서버에 연결할 수 없습니다") from exc

    # 기간은 코어가 검색 절에 넣는 적용값으로 한 번만 계산한다 — 지문 재료와 응답 되돌림이 같은 값을 써야 멀쩡한 순회가 끊기지 않는다.
    applied_dates = applied_date_bounds(filters)
    # 커서에 실을 조건 지문 재료 — 조건이 바뀐 커서로 이어 읽으면 오류 없이 자료가 빠지므로 코어가 다음 쪽에서 대조한다.
    scope = _cursor_scope(q=q, refine=refine, filters=filters, applied_dates=applied_dates)
    # 칩을 세지 않을 때는 셀 축을 비워 넘긴다(총계 질의 하나만 돈다).
    facet_axes: dict[str, Any] = {} if with_facets else {"axes": ()}
    try:
        if use_cursor:
            with stage("engine"):       # 요청 로그의 단계별 시간(느린 요청 경고에 실린다)
                found = browse_files(
                    get_client(), get_current_settings().opensearch.index,
                    query=q, query_vector=query_vector, filters=filters,
                    sort=sort, cursor=cursor, size=limit, facet_size=FACET_SIZE_DEFAULT,
                    refine=refine, scope=scope, **facet_axes,
                )
        else:
            with stage("engine"):
                found = search_files(
                    get_client(), get_current_settings().opensearch.index,
                    query=q, query_vector=query_vector, filters=filters,
                    from_=page_from, size=page_size, sort=sort,
                    rank_depth=RANK_DEPTH_DEFAULT, total_cap=TOTAL_CAP_DEFAULT,
                    sort_depth=SORT_DEPTH_DEFAULT, facet_size=FACET_SIZE_DEFAULT,
                    refine=refine, **facet_axes,
                )
    except CursorError as exc:
        # 깨진 커서는 조용히 다른 자리에서 이어 주지 않고 400 으로 알린다.
        raise HTTPException(status_code=400, detail=params.cursor_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _OS_CONN_ERRORS as exc:
        # 엔진 연결 실패만 503 — 빈 결과로 감추면 '자료가 없다'와 '검색이 죽었다'가 같아진다. 그 밖의 예외는 500.
        _LOG.warning("파일 검색 — 검색 엔진 연결 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise HTTPException(status_code=503, detail="검색 엔진에 연결할 수 없습니다") from exc

    def _finish(repo: Any) -> list[dict[str, Any]]:
        """권한 가리기와 파일 메타 붙이기를 한 트랜잭션에서 끝낸다. 각 행에 ``file_ext`` · ``file_size`` · ``updated_at`` · ``created_at`` 이 항상 있다(모르는 값은 0 · ``None``)."""
        # 깊이 밖에서 떠본 한 건은 이 페이지의 결과가 아니다 — 가리기·메타를 붙이기 전에 버린다.
        rows = repo.search.project_rows([] if beyond_depth else found["rows"],
                                        clearance=principal.clearance)
        meta = repo.search.file_meta([r["asset_id"] for r in rows])
        for row in rows:
            got = meta.get(row["asset_id"]) or {}
            row["file_ext"] = _ext_of(str(row.get("file_name") or ""))
            row["file_size"] = int(got.get("file_size") or 0)
            row["updated_at"] = got.get("updated_at")
            row["created_at"] = got.get("created_at")
        return rows

    items: list[dict[str, Any]] = DbManager.read(_finish)

    # 여기서 한 번 더 거르지 않는다 — 좁히기는 이미 엔진 질의 절로 걸렸고, 파이썬 부분 문자열로 다시 거르면 두 판정이 어긋난다(`김치를` / `김치`).
    refine_applied = bool(refine and refine.strip())

    # query 는 싣지 않는다 — IDD IF-ASSET-09 응답 계약에 없다.
    body: dict[str, Any] = {
        "items": items,
        # 두 건수는 엔진이 센 모수다 — scope_total 은 좁히기 이전, total 은 이후.
        "total": found["total"],
        "scope_total": found["scope_total"],
        "total_capped": found["total_capped"],
        # 커서 경로에는 offset 이 없어 책갈피를 준다(깊이 밖에서는 요청한 자리를 돌려준다).
        "offset": offset if beyond_depth else found.get("from", 0),
        "limit": found["size"],
        "sort": found["sort"],
        # 다음 쪽 책갈피. None 이면 더 없다.
        "next_cursor": found.get("next_cursor"),
        # 깊이 경계 안내 — 참이면 이 정렬로는 여기까지이고 suggest_sort 를 함께 준다.
        "depth_limited": depth_limited,
        "depth": depth,
        "suggest_sort": list(_SUGGEST_SORT) if depth_limited else [],
        # 값이 변하는 정렬은 커서가 어긋날 수 있다 — 막지 않고 계약에 경고를 단다.
        "sort_unstable": sort not in STABLE_SORTS and sort != SORT_DEFAULT,
        # 이번 조회에 실제 적용된 값 전부(재현 기록).
        "applied": {
            "word_operator": WORD_OPERATOR_DEFAULT,
            "semantic_min_cosine": SEMANTIC_MIN_COSINE_DEFAULT,
            "semantic_cap": SEMANTIC_CAP_DEFAULT,
            "about_branch": ABOUT_BRANCH_DEFAULT,
            "total_cap": TOTAL_CAP_DEFAULT,
            "rank_depth": RANK_DEPTH_DEFAULT,
            "sort_depth": SORT_DEPTH_DEFAULT,
            "facet_size": FACET_SIZE_DEFAULT,
            "facet_show": _FACET_SHOW,
            "search_pipeline": SEARCH_PIPELINE_DEFAULT,
        },
        # 칩은 상위 몇 개만 보인다 — 코어는 넉넉히 주고 무엇을 보일지는 화면 정책이다.
        "facets": {axis: rows[:_FACET_SHOW] for axis, rows in found["facets"].items()},
        "filters": {
            "topic": list(filters.topics) if filters else [],
            "subtopic": list(filters.subtopics) if filters else [],
            "modality": list(filters.modalities) if filters else [],
            "tag": list(filters.tags) if filters else [],
            "file_ext": list(filters.file_exts) if filters else [],
            # 되돌리는 값은 실제 적용값이다 — 원문을 되돌리면 안 걸린 조건이 걸린 것처럼 보인다.
            "created_from": applied_dates[0],
            "created_to": applied_dates[1],
        },
    }
    if refine_applied:
        # shown 은 이 쪽에 실제로 실린 행 수(권한 가리기 뒤)다.
        body["refine"] = {"q": refine, "shown": len(items)}
    return body



# ── 추천 검색어 ─────────────────────────────────────────────────────────────────
@router.get("/file-search/suggest")
def file_search_suggest(
    q: str = Query(..., min_length=1, max_length=_QUERY_MAX_LEN,
                   description="사용자가 친 글자(부분 일치)"),
    limit: int = Query(10, ge=1, le=50, description="돌려줄 개수(전체 상한)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """검색창 추천 — 주제·하위주제·태그 중 글자가 든 값을 건수 많은 순으로.

    🔴 **DB 에서 읽는다** — 추천은 "지금 결과"가 아니라 "고를 수 있는 값"이고, 검색이 멈춰도 떠야 한다.
    """
    rows = DbManager.read(lambda repo: repo.catalog.suggest(q=q.strip(), limit=limit))
    return {"rows": rows, "total": len(rows)}


def warm_up_search(query: str = "워밍업") -> None:
    """기동 직후 첫 검색이 치르는 초기 비용(임베딩 · 검색 엔진 연결)을 미리 한 번 치른다. 실제 검색과 같은 호출 모양이며 접근 기록에는 남지 않는다. 오류는 그대로 올린다(부르는 쪽이 경고로 삼킨다)."""
    from service.api.search_health import get_client

    vector = embed_query_for_media_search(query, channel=active_embed_channel())
    search_files(
        get_client(), get_current_settings().opensearch.index,
        query=query, query_vector=vector, filters=None,
        from_=0, size=1, sort=SORT_DEFAULT,
        rank_depth=RANK_DEPTH_DEFAULT, total_cap=TOTAL_CAP_DEFAULT,
        sort_depth=SORT_DEPTH_DEFAULT, facet_size=FACET_SIZE_DEFAULT,
        refine=None, axes=(),
    )


# ── 추가 좁히기 칩(형식·크기·기간) ────────────────────────────────────────────────
@router.get("/file-search/facet-extra")
def file_search_facet_extra(
    axis: list[str] = Query(default=[], description=f"셀 축(반복 가능): {' | '.join(EXTRA_AXES)} · 생략=전부"),
    q: str = Query("", max_length=_QUERY_MAX_LEN, description="검색어(빈 값이면 조건에 맞는 전부)"),
    refine: str | None = Query(None, max_length=_QUERY_MAX_LEN, description="결과 내 재검색"),
    topic: list[str] | None = Query(None),
    subtopic: list[str] | None = Query(None),
    tag: list[str] | None = Query(None),
    modality: list[str] | None = Query(None),
    file_ext: list[str] | None = Query(None),
    created_from: str | None = Query(None),
    created_to: str | None = Query(None),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """목록 화면의 **형식·크기·기간 칩 건수** — 코어 칩 네 축(주제·하위주제·태그·종류) 밖.

    파라미터는 ``/file-search`` 와 같은 이름·같은 뜻이고 세는 집합도 같다. 칩은 자기 조건을 뺀 채 센다.
    ``file_size`` 축은 **세기만** 한다(좁히기는 코어 확장 대기 — ``extra_facets`` 의 TODO).

    Raises:
        HTTPException: 모르는 축·필터 형식 오류는 422 · 검색 엔진에 닿지 못하면 503.
    """
    unknown = [a for a in axis if a not in EXTRA_AXES]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"알 수 없는 축입니다: {unknown} (가능: {', '.join(EXTRA_AXES)})")
    # 종류는 /file-search 와 같은 어휘다 — 모르는 값은 같은 422 로 끊는다.
    unknown_mods = [m for m in (modality or []) if m not in VALID_SEARCH_MODALITIES]
    if unknown_mods:
        raise HTTPException(
            status_code=422,
            detail=f"알 수 없는 종류입니다: {unknown_mods} (가능: {', '.join(VALID_SEARCH_MODALITIES)})")
    try:
        filters = parse_search_filters(
            file_ext=file_ext, created_from=created_from, created_to=created_to,
            topic=topic, subtopic=subtopic, modality=modality, tag=tag)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    query = (q or "").strip()
    query_vector: list[float] = []
    if query:
        try:
            with stage("embed"):
                query_vector = embed_query_for_media_search(query, channel=active_embed_channel())
        except Exception as exc:  # noqa: BLE001 — 임베딩 서버 장애를 빈 결과로 감추지 않는다
            _LOG.warning("추가 칩 — 질의 임베딩 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
            raise HTTPException(status_code=503, detail="임베딩 서버에 연결할 수 없습니다") from exc
    # 검색 엔진 클라이언트는 **부를 때** 가져온다(모듈 로딩 시점에 연결을 만들지 않는다 — 훑기 경로와 같은 규칙).
    from service.api.search_health import get_client
    try:
        with stage("engine"):
            return extra_facets(
                get_client(), get_current_settings().opensearch.index,
                query=query, query_vector=query_vector, filters=filters,
                refine=(refine or "").strip() or None, axes=list(axis))
    except _OS_CONN_ERRORS as exc:
        _LOG.warning("추가 칩 — 검색 엔진 연결 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise HTTPException(status_code=503, detail="검색 엔진에 연결할 수 없습니다") from exc
