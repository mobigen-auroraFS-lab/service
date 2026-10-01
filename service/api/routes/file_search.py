"""파일 검색 라우트 — 조건으로 좁히고 유사도로 줄 세우고 정확히 센다(사용 시나리오 ③).

🔴 **이 창구는 `/search`(멀티모달 검색)를 대체하기 위한 것이다**(2026-09-09 사용자 확인).
`/search` 는 관련도 컷을 파이썬에서 계산하므로 **패싯도 페이징도 만들 수 없다** — 검색 엔진이 집합을
셀 수 없기 때문이다. 이 창구는 그 둘을 얻기 위해 만들었고, 집합을 엔진이 판정하는 조건으로 확정했다.

대체 대조표(무엇이 무엇을 대신하나)는 코어 `src/search/file_search.py` 모듈 docstring 에 있다.
⚠️ **`/search` 코드는 지우지 않는다** — 과제 산출물 8건의 구현체이고 KPI F-4.3 에 완료로 기록돼 있다.
화면을 이 창구로 옮기는 것과 코드를 지우는 것은 다르다. 라우터 등재도 당장 내리지 않는다.

**이 파일이 하는 일은 넷뿐이다**: 파라미터 검증, 질의 임베딩, 코어 조회 호출, 응답 조립(권한 가리기·
크기·수정일 붙이기). 세는 규칙·순위·집계는 전부 코어와 검색 엔진 몫이다(093 책무 경계).

🔴 **"적힌 숫자 = 누르면 나오는 수"**(2026-08-26 원칙). 칩 건수와 전체 개수를 **검색 엔진이** 세고, 그
세는 대상이 필터가 걸리는 대상과 같다. 결과 행에서 세던 종전 방식은 화면에 내려온 것만 셌기 때문에
「음악 3」을 눌렀더니 32건이 나오는 결함이 있었다(2026-09-08 실측). 이 창구는 그 구조를 바꿨고,
골든 질의 × 세 축 410건 검사에서 불일치 0 이다.
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

# 질의 길이 상한 — 검색 엔진이 낱말마다 절(clause)을 만들고 **1024개**에서 거절한다
# (실측 2026-09-21: 2,000자 질의가 OpenSearch RequestError 로 터져 **500** 이 났다).
# 입력이 너무 긴 것은 **사용자가 고칠 문제**라 422 로 앞에서 끊는다 — 상한은 넉넉하되
# 질의와 좁히기를 합쳐도 절 수가 한계에 닿지 않는 값이다.
_QUERY_MAX_LEN = 300

# 한 페이지 상한. 표로 훑는 화면이라 검색 화면(100)보다 넉넉히 두되, 한 번에 다 받게 하지는 않는다.
_PAGE_SIZE_MAX = 200
# 화면에 보일 칩 수 — 083 태그 패싯 표시 기본(12)과 같은 값으로 맞춘다(축이 달라도 눈에 보이는 양은 같게).
_FACET_SHOW = 12
# 반복 파라미터(주제·태그 등) 한 번에 받을 개수 상한. 화면은 칩을 눌러 몇 개 고르는 정도라 50 이면
# 넉넉하고, 수천 개를 보내 ``terms`` 절을 부풀리는 요청을 여기서 막는다(엔진 한계 65,536 보다 훨씬 앞).
_REPEAT_MAX = 50
# 깊이에 닿았을 때 권하는 정렬(spec §2-5) — 끝까지 넘길 수 있는 **안정** 정렬만 고른다.
_SUGGEST_SORT: tuple[str, ...] = ("created_desc", "name_asc")
# 검색 엔진 **연결** 실패로 볼 예외들. 클라이언트가 안 깔린 환경(순수 단위 테스트)에서는 빈 튜플이라
# ``except ()`` 가 아무것도 잡지 않는다 — 그 환경에는 잡을 연결 실패도 없다.
_OS_CONN_ERRORS: tuple[type[BaseException], ...] = (
    (errors.OSConnectionError,) if errors.OSConnectionError is not None else ()
)


def _cursor_scope(
    *, q: str, refine: str | None, filters: Any, applied_dates: tuple[str | None, str | None]
) -> str:
    """이번 **결과 집합을 정의하는 것 전부**를 문자열 하나로 모은다(커서 조건 지문 재료 · 099 G7).

    왜 백엔드가 만드나: 코어는 화면 파라미터를 알면 안 된다(093 책무 경계). 무엇이 "이번 조회"를
    정하는지는 화면이 늘리면 함께 늘어나는 목록이라, 그 목록을 아는 쪽이 모아서 넘긴다. 코어는 이
    문자열의 **지문(해시)** 만 커서에 찍어 두었다가 다음 쪽에서 같은지 본다 — 책갈피에 책 제목 대신
    도장을 찍어 두고, 다음에 꺼낼 때 도장이 같은지만 보는 것과 같다.

    🔴 **무엇을 넣었나와 그 근거**(빠뜨리면 그만큼 검사가 헐거워진다):

    | 재료 | 왜 |
    |---|---|
    | ``q`` | 결과 집합의 세 갈래(낱말·뜻·개체)를 모두 정한다. 실측 결함 A 의 직접 원인 |
    | ``refine`` | 099 G3 부터 **엔진 질의 절**이라 집합을 줄인다(쪽 안 거르기가 아니다) |
    | 칩 필터 5종(주제·하위주제·태그·종류·확장자) | 전부 ``filter`` 절이라 집합을 바꾼다 |
    | 기간(적용값) | 같은 이유. 원문이 아니라 **실제 적용값**을 쓴다 — 원문은 공백·시각을 포함할 수 있는데 적용은 날짜까지라, 원문을 쓰면 같은 조건이 다른 지문이 된다 |

    **일부러 뺀 것과 근거**:

    - ``sort`` — 코어 커서가 **이미 따로** 대조한다(``expect_sort``). 넣으면 같은 검사를 두 겹으로
      하게 되고, 오류 문구가 "정렬이 다르다" 대신 "조건이 다르다"로 뭉개져 원인을 가린다.
    - ``limit``/``offset`` — 한 번에 몇 줄 볼지는 결과 집합을 바꾸지 않는다. 넣으면 쪽 크기를 바꾼
      멀쩡한 순회가 400 으로 끊긴다(과잉 거부도 결함이다).
    - 사용자 등급(권한 가리기) — 집합은 엔진이 정하고 가리기는 **응답 직전**에 걸린다. 지문에 넣어도
      막을 수 있는 사고가 없다. ⚠️ 가리기가 엔진 조건으로 내려가는 날에는 재료로 올려야 한다.

    ⚠️ **필터를 늘리면 여기도 늘려야 한다.** 빠뜨리면 그 조건만 바뀐 커서가 조용히 통과해 자료가
    빠진다 — 그것이 099 G7 이 고친 바로 그 결함이다.

    Args:
        q: 검색어 원문(앞뒤 공백은 결과를 바꾸지 않으므로 떼고 쓴다).
        refine: 결과 내 재검색어. ``None``·공백뿐이면 "좁히지 않음"과 같은 값으로 접는다.
        filters: ``parse_search_filters`` 가 만든 선필터(``None`` 이면 조건 없음).
        applied_dates: 실제 적용된 ``(생성일 하한, 상한)`` — 응답에 되돌리는 값과 **같은 계산**.

    Returns:
        같은 조건이면 언제나 같은 문자열(헌법 3조). 목록형 조건은 **정렬해** 담는다 — 같은 칸에서
        고른 칩들은 「또는」이라 순서가 결과를 바꾸지 않기 때문이다(순서만 다른 요청을 끊지 않는다).
    """
    def _sorted(values: Any) -> list[str]:
        """반복 조건을 정렬된 문자열 목록으로(순서 차이를 흡수한다).

        Args:
            values: 필터 튜플(``None`` 가능).

        Returns:
            정렬된 문자열 목록.
        """
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

    Args:
        q: 검색어. 비우면 조건에 맞는 **전부**를 훑는다(097 · 첫 화면 전량 목록).
        topic: 주제 필터(여럿 = 또는).
        subtopic: 하위주제 필터(여럿 = 또는). 주제와는 「그리고」로 걸린다.
        tag: 태그 필터. 정규화·가공 없이 코어로 넘긴다.
        modality: 종류 필터(여럿 = 또는). 닫힌 어휘라 모르는 값은 422 다.
        file_ext: 확장자 필터.
        created_from: 생성일 하한.
        created_to: 생성일 상한.
        refine: 결과 집합을 좁힐 말(낱말 단위 · 낱말끼리 「그리고」). 파일명·요약·태그를 본다.
        sort: 정렬 이름(닫힌 목록 · 모르는 값은 422). 어느 정렬이든 집합은 같고 순서만 바뀐다.
            훑기에서 relevance 면 created_desc 로 갈아 끼운다(관련도가 뜻이 없으므로).
        offset: 페이지 시작 위치(얕은 페이지용). cursor 와 함께 주면 400.
        cursor: 이어 읽기 표식. 직전 응답의 ``next_cursor`` 를 그대로 넘긴다 — offset 과 달리 1만 건
            벽이 없다. relevance 정렬에는 줄 수 없다(이어받을 점수 기준값이 없다 · 400).
            🔴 **조건이 하나라도 바뀌면 400** 이다(099 G7) — 커서에 조건 지문이 함께 들어 있다.
            종전에는 막지 못해, 전체 훑기 커서를 ``modality=text`` 에 쓰면 첫 건이 통째로 빠졌다.
        limit: 이 페이지의 행 수.
        with_facets: 칩을 함께 셀지. 거짓이면 코어에 셀 축을 비워 넘겨 칩 질의를 건너뛴다(``facets`` 는 ``{}`` · 총계는 센다).
        principal: 인증 주체.

    Returns:
        ``{items, total, scope_total, total_capped, offset, limit, sort, next_cursor,
        applied, facets, filters, refine?}``. ``applied`` 는 이번 조회에 쓰인 값 전부(재현성 기록).
        ``total`` 은 좁히기 **이후**, ``scope_total`` 은 좁히기 **이전** 결과 집합 크기이며 **둘 다
        모수**다(돌려준 개수가 아니다 · spec 099 §3-6). refine 이 없으면 둘이 같다.
        ``total_capped`` 가 참이면 ``total`` 은 "이 수 이상"이고, ``next_cursor`` 가 ``None`` 이면 더 없다.
        ``facets`` 는 ``{축: [{key, label, count}]}`` — ``key`` 를 되보내면 그 수만큼 나온다.

    Raises:
        HTTPException: 형식·어휘·개수 위반은 422 · 자리 모순·깊이 초과·깨진 커서는 400 · 엔진 미도달 503.

    설계 배경: `docs/설계_변경이력.md` 2026-09-15 (1)
    """
    # 조건이 겹치는 방식은 검색 엔진 규칙 그대로다 — 같은 칸에서 여럿 고르면 「또는」, 다른 칸끼리는
    #   「그리고」. 조건을 바꾸면 개수와 칩이 함께 다시 계산된다.
    # 집합의 뜻(096): 세 갈래의 합집합 중 조건에 맞는 것 —
    #   ① 검색어의 모든 형태소가 든 파일(단어 절은 멀티모달 검색과 같은 것)
    #   ② 뜻이 아주 가까운 파일(코사인 하한 이상 · 상위 k개가 아니라 하한이라 경계가 있다)
    #   ③ 개체(about)가 질의 낱말과 완전히 같은 파일(글자도 뜻도 못 잡은 것을 개체로 잡는다).
    #   세 갈래 모두 검색 엔진이 판정하는 조건이라 개수와 칩을 엔진이 정확히 센다.
    # 칩 숫자의 뜻: 그 칩 하나만 골랐을 때 나오는 수(다른 칸 조건은 그대로 적용). 같은 칸에서 여럿
    #   고르면 「또는」이라 결과는 각 칩 수의 합집합이므로 개별 칩 수보다 크거나 같다. 그래서 고른
    #   뒤에도 같은 칸의 다른 값이 칩으로 남아 갈아탈 수 있다(코어 `build_facet_plan` 이 축마다 자기
    #   조건을 빼고 센다).
    # 각 행에는 표에 찍을 `file_ext`·`file_size`·`updated_at` 이 항상 있다(`_finish` 가 붙인다).
    # 입력 오류는 전부 **422** 로 통일한다(리뷰 2026-09-09 — 공백만인 q 가 코어 ValueError 경로로
    # 400 이 되어 다른 입력 오류와 어긋났다). 프론트가 상태 코드 하나로 "입력을 고쳐 달라"를 판단한다.
    # 🔴 검색어가 비면 **조건만으로 훑는 화면**이다(097). 종전에는 422 로 막았다 — 첫 화면 전량
    #    목록을 낼 창구가 없었기 때문이다. 이제는 커서 경로가 그것을 맡는다.
    browsing = not q.strip()
    cursor = params.cursor_or_none(cursor)   # 빈 커서(cursor=)는 커서 없음 — 아래 판단이 갈리지 않게
    if cursor is not None and offset:
        raise HTTPException(
            status_code=400,
            detail="cursor 와 offset 은 함께 줄 수 없습니다 — 어느 자리부터 읽을지 모호합니다",
        )
    # 🔴 훑기(검색어 없음)는 **커서 전용 경로**다(아래 ``use_cursor``) — 그 경로에는 "몇 번째부터"가
    #    없어 offset 이 버려진다. 조용히 버리면 2쪽을 달라고 한 화면이 1쪽을 받고도 알 수 없다
    #    (실측 2026-09-21: offset=3·9000 이 전부 첫 쪽이었고 응답 offset 은 0 이었다). 끊어서 알린다.
    if browsing and offset:
        raise HTTPException(
            status_code=400,
            detail=("검색어가 없는 훑기에서는 offset 을 쓸 수 없습니다 — next_cursor 로 이어 읽으십시오"
                    "(offset 은 검색어가 있을 때의 얕은 페이지 전용)"),
        )
    # 🔴 **대체를 먼저** 한다 — 순서가 반대면 첫 화면이 이어 읽기를 못 한다(실측 결함 2026-09-15).
    #    첫 쪽은 sort 없이 오므로 relevance 가 기본인데, 응답의 커서에는 실제 쓰인 created_desc 가
    #    찍힌다. 다음 쪽도 sort 없이 오니, 대체보다 거부를 먼저 두면 **자기가 준 커서를 자기가 막는다.**
    if browsing and sort == SORT_DEFAULT:
        sort = "created_desc"
    # 관련도는 점수가 상위 rank_depth 개만 계산돼 **이어받을 기준값이 없다**(097 §2-5).
    #   막다른 길이 아니라 갈림길로 안내한다 — 정렬을 바꾸면 끝까지 넘길 수 있다.
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
    # TODO(코어): 크기로 **거르는 것**은 검색 엔진 필터에 크기 축이 없어 아직 못 한다. 파라미터 자리를
    #   먼저 열어 두고(화면이 이 이름으로 코딩할 수 있게) 지금은 501 로 분명히 막는다 — 조용히
    #   무시하면 화면은 걸린 줄 알고 잘못된 목록을 보여 준다(`TODO.md` · `extra_facets` 의 TODO).
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
    # 종류는 **닫힌 어휘**다(코어 정본). 모르는 값을 조용히 0건으로 넘기면 오타(`문서`·`텍스트`)를
    # "그런 파일이 없다"로 읽게 된다 — `/search` 도 같은 자리에서 거부한다(``routes/search.py``).
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

    # 넘길 수 있는 깊이는 정렬에 따라 다르다 — 관련도는 이웃 탐색 깊이에, 필드 정렬은 색인 결과창에 걸린다.
    # 🔴 깊이에 닿는 것은 **오류가 아니라 안내**다(spec §2-5). 400 으로 끊으면 화면이 「끝」과
    #    「고장」을 구분하지 못한다 — 200 으로 주고 `depth_limited` 로 알려 정렬 전환을 권한다.
    # 경계에 걸친 요청(990+50)은 남은 10건을 마저 준다 — 통째로 막으면 볼 수 있는 것을 못 본다.
    # 🔴 두 경로를 **나란히** 둔다(097 설계 ①). 종전 offset 호출은 그대로 `search_files` 로 가고,
    #    커서·훑기만 `browse_files` 로 간다 — 되돌림 경로를 남기고 기존 화면을 흔들지 않는다.
    use_cursor = cursor is not None or browsing
    by_field = SORT_OPTIONS[sort] is not None
    depth = SORT_DEPTH_DEFAULT if by_field else RANK_DEPTH_DEFAULT
    depth_limited = not use_cursor and offset + limit > depth
    # 깊이 **밖**(offset 이 이미 한계)이면 줄 행이 없다. 그래도 총계·칩은 세야 하므로(화면이
    #   "전체 N건 중 여기까지"를 쓴다) 첫 쪽 한 건만 떠보고 행은 버린다 — 총계·칩은 페이지와
    #   무관하다. 코어는 `size=0` 과 깊이 초과 `from_` 을 둘 다 거부하므로 이렇게 우회한다.
    beyond_depth = depth_limited and offset >= depth
    page_from, page_size = offset, limit
    if depth_limited:
        page_size = depth - offset
    if beyond_depth:
        page_from, page_size = 0, 1

    from src.search.opensearch_sync import get_client

    # 🔴 정렬과 무관하게 임베딩이 필요하다 — 뜻이 **집합 판정**에 쓰이기 때문이다(코사인 하한
    #    이상이면 글자가 안 겹쳐도 집합에 든다). 정렬에 따라 개수가 달라지면 화면이 거짓말을 한다.
    # 임베딩 서버 장애는 검색 엔진 장애와 **다른 원인**이라 따로 알린다(리뷰 2026-09-09 — 종전에는
    # 모든 예외가 "검색 엔진 연결 실패"로 뭉개져 운영자가 엉뚱한 곳을 봤다). 코어 임베더는 API
    # 실패를 RuntimeError, 응답 이상을 ValueError 로 올린다.
    # 훑기(검색어 없음)에는 임베딩이 필요 없다 — 뜻으로 걸 질의가 없다. 임베딩 서버가 죽어 있어도
    #   첫 화면은 떠야 한다(전량 목록은 조건만으로 정해진다).
    query_vector: list[float] | None = None
    if not browsing:
        try:
            with stage("embed"):
                query_vector = embed_query_for_media_search(q, channel=active_embed_channel())
        except (RuntimeError, ValueError) as exc:
            _LOG.warning("질의 임베딩 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
            raise HTTPException(status_code=503, detail="임베딩 서버에 연결할 수 없습니다") from exc

    # 기간은 원문이 아니라 코어가 검색 절에 넣는 **적용값**으로 한 번만 계산한다. 아래 지문 재료와
    #   응답 되돌림(``filters.created_*``)이 **같은 값**을 써야 한다 — 갈라지면 "화면에 보이는 조건"과
    #   "커서가 기억하는 조건"이 어긋나 멀쩡한 순회가 400 으로 끊긴다.
    applied_dates = applied_date_bounds(filters)
    # 커서에 실을 **조건 지문 재료**(099 G7). 여기서 한 번 만들어 코어에 넘기면, 코어가 다음 쪽에서
    #   같은 조건인지 대조한다 — 조건이 바뀐 커서로 이어 읽으면 오류 없이 자료가 빠지기 때문이다
    #   (실측 2026-09-17: 전체 훑기 커서를 modality=text 에 쓰자 첫 건이 통째로 누락).
    scope = _cursor_scope(q=q, refine=refine, filters=filters, applied_dates=applied_dates)
    # [2026-09-28] 칩을 세지 않을 때는 코어에 **셀 축을 비워** 넘긴다 — 총계 질의 하나만 돈다(칩 질의 생략).
    #   다음 쪽마다 칩 · 총계를 다시 세던 부하를 화면이 끌 수 있게 한다. 기본(true)은 종전과 같다.
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
        # 커서가 깨졌거나 정렬이 어긋났다 — **조용히 다른 자리에서 이어 주지 않는다**(097 §2-3).
        #   문구의 파이썬 진단(예외 원문 · 입력 repr)은 떼고 할 일을 붙인다(``params.cursor_detail``).
        raise HTTPException(status_code=400, detail=params.cursor_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _OS_CONN_ERRORS as exc:
        # 검색 엔진에 닿지 못한 것을 빈 결과로 감추면 "자료가 없다"와 "검색이 죽었다"가 같아진다.
        # **연결 실패만** 503 으로 — 그 밖의 예외(코드 결함)는 그대로 올려 전역 핸들러가 500 으로
        # 구분한다(`errors.os_unavailable_handler` 가 정한 원칙: 코드 버그 500 과 구분·운영 알람용).
        _LOG.warning("파일 검색 — 검색 엔진 연결 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise HTTPException(status_code=503, detail="검색 엔진에 연결할 수 없습니다") from exc

    def _finish(repo: Any) -> list[dict[str, Any]]:
        """권한 가리기와 파일 메타 붙이기를 **한 트랜잭션**에서 끝낸다(연결을 두 번 잡지 않게).

        Args:
            repo: 저장소 묶음(``DbManager.read`` 가 넘긴다).

        Returns:
            코어가 준 순서 그대로의 행 목록. 각 행에 표에 찍을 ``file_ext``·``file_size``·
            ``updated_at``·``created_at`` 이 **항상** 있다(모르는 값은 0·``None``) — 프론트가
            키 유무로 분기하지 않게.
        """
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

    # 🔴 여기서 한 번 더 거르지 않는다(099 G3). 좁히기는 이미 **검색 엔진 질의 절**로 걸렸고,
    #   엔진은 낱말(형태소) 단위로 맞춘다. 파이썬 부분 문자열로 다시 거르면 두 판정이 어긋나
    #   엔진이 맞다고 한 행을 버린다 — 예를 들어 `김치를` 은 색인의 `김치` 에 맞지만(조사 제거)
    #   요약 글자에는 `김치를` 이 없어 탈락한다. 판정은 **한 곳에서 한 번만** 한다.
    refine_applied = bool(refine and refine.strip())

    # ⚠️ ``query`` 를 싣지 않는다 — IDD IF-ASSET-09 응답 계약에 없는 필드였다(``/search`` 는
    #    계약에 있어 싣는다). 되돌림이 필요하면 IDD 에 먼저 올리고 여기에 더한다.
    body: dict[str, Any] = {
        "items": items,
        # 🔴 두 건수 모두 **엔진이 센 모수**다(spec 099 §3-6) — 돌려준 개수가 아니다.
        #   `scope_total` = 좁히기 이전("지우면 N건") · `total` = 좁히기 이후("좁히면 M건").
        #   무한 스크롤에서는 "화면에 보이는 수"가 계속 늘어 고정 기준이 못 되므로, 091 이 쓰던
        #   "이 페이지에서 몇 건" 대신 모수 두 개로 말한다.
        "total": found["total"],
        "scope_total": found["scope_total"],
        "total_capped": found["total_capped"],
        # 커서 경로에는 "몇 번째부터"가 없다 — 그 자리에 책갈피를 준다.
        #   깊이 밖에서는 코어에 0 을 떠봤으므로 **요청한 자리**를 그대로 돌려준다.
        "offset": offset if beyond_depth else found.get("from", 0),
        "limit": found["size"],
        "sort": found["sort"],
        # 다음 쪽 책갈피. `None` 이면 **더 없다**(화면이 스크롤을 멈춘다).
        #   offset 경로에서는 주지 않는다(그쪽은 offset 으로 넘긴다).
        "next_cursor": found.get("next_cursor"),
        # 깊이 경계 안내(spec §2-5) — 참이면 이 정렬로는 여기까지다. 오류가 아니라 갈림길이라
        #   `suggest_sort` 로 끝까지 볼 수 있는 정렬을 함께 준다.
        "depth_limited": depth_limited,
        "depth": depth,
        "suggest_sort": list(_SUGGEST_SORT) if depth_limited else [],
        # 🔴 값이 변하는 정렬은 커서가 어긋날 수 있다(spec §2-8). 막지 않는 것이 결정이고,
        #   대신 계약에 경고를 단다 — 정확해야 하는 순회는 등록 시각 정렬을 쓴다.
        "sort_unstable": sort not in STABLE_SORTS and sort != SORT_DEFAULT,
        # 이번 조회에 실제 적용된 값 전부 — 서버 설정이 나중에 바뀌어도 이 응답이 왜 이렇게 나왔는지
        # 재현할 수 있다(헌법 3조 · `/search` 의 `meta.tuning` 과 같은 취지).
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
            # 되돌리는 값은 **실제 적용값**이다 — 원문은 공백·시각을 포함할 수 있는데 적용은 날짜까지만
            # 이라, 원문을 되돌리면 "안 걸린 조건이 걸린 것처럼" 보인다(리뷰 2026-09-09).
            "created_from": applied_dates[0],
            "created_to": applied_dates[1],
        },
    }
    if refine_applied:
        # ``shown`` 은 **이 쪽에 실제로 실린 행 수**(권한 가리기 뒤)다 — 모수는 위 두 값이 말한다.
        #   종전 ``page_total``(이번 페이지의 좁히기 전 행 수)은 모수가 아니라 없앴다.
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
    """기동 직후 첫 검색이 치르는 초기 비용을 **미리 한 번** 치른다 — 임베딩 모델 · 서버 연결, 검색 엔진 연결과 캐시.

    왜 필요한가: 서버를 갈아 끼운 직후 첫 ``/file-search`` 가 32초, ``/search`` 가 8초 걸렸다(사내 k8s 실측 2026-10-01 ·
    두 번째부터는 정상). 준비 확인(readiness)이 ``/health`` 만 봐서 데우기 전에 사용자 요청을 받는다.
    검색 한 건(1건 · 칩 없음)을 **실제 검색과 같은 호출 모양**으로 보낸다 — 접근 기록(``access_log``)에는 남지 않는다(HTTP 를 거치지 않는다).

    Args:
        query: 데울 때 쓸 검색어(뜻은 없다).

    Raises:
        임베딩 · 검색 엔진 오류는 그대로 올린다 — 부르는 쪽(``service.api.warmup``)이 경고로 삼킨다.
    """
    from src.search.opensearch_sync import get_client

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
    # 종류는 /file-search 와 **같은 이름·같은 뜻**이다 — 모르는 값을 0건 칩으로 넘기지 않고 같은 422 로 끊는다
    #   (종전에는 여기만 검사가 없어, 목록은 422 인데 칩은 조용히 전부 0 이었다 · 2026-09-23 대조).
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
    from src.search.opensearch_sync import get_client
    try:
        with stage("engine"):
            return extra_facets(
                get_client(), get_current_settings().opensearch.index,
                query=query, query_vector=query_vector, filters=filters,
                refine=(refine or "").strip() or None, axes=list(axis))
    except _OS_CONN_ERRORS as exc:
        _LOG.warning("추가 칩 — 검색 엔진 연결 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise HTTPException(status_code=503, detail="검색 엔진에 연결할 수 없습니다") from exc
