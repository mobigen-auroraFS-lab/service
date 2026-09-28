"""개체(멀티모달 메타) 화면 라우트 — 얇게. 조립은 ``service/portal/mm_meta.py``, 읽기는 코어 seam.

**이 파일이 하는 일은 셋뿐이다**: 파라미터 검증, 트랜잭션 안에서 조립 함수 호출, HTTP 상태 코드. 세는 규칙·노출 기준·정렬 같은 판단은 하나도 여기 없다(코어) — 그리고 응답 키 이름과
상한·문구는 정형 계층에 있다(백엔드).

**화면 개념 두 축**(spec 087): 개체를 좁히는 축은 **종류**(타입)와 **갈래**(개체에 붙은 분류 라벨)다.
둘 다 개체에 붙어 있어 좁혀도 개체가 쪼개지지 않는다. 자산에 붙는 라벨로 좁히던 옛 화면은 한 개체가
갈래마다 나뉘어 보였다(실측: 한 지명이 여섯 건인데 갈래별로 다섯·하나·하나로 갈라졌다).

**라우트 순서**: ``/mm-meta/facets`` 는 세그먼트가 하나라
``/mm-meta/{entity_type}/{entity_uid}``(둘)와 겹치지 않는다 — 선언 순서에 의존하지 않는다.

🔴 **옛 별칭 ``/mm-meta/types`` 는 지웠다**(2026-09-28) — 칩 창구(``/mm-meta/facets``)에 흡수된 뒤 이름만
남아 있었고 화면은 쓰지 않는다(IDD IF-ENTITY-03 보류).

🔴 **개체 묶음 zip 창구는 없다**(2026-09-28 삭제) — ``/mm-meta/bundle``·``/mm-meta/{type}/{uid}/bundle`` 은
파일 제공 방식을 다른 쪽과 협의한 뒤 다시 설계한다(`TODO.md` · IDD IF-ENTITY-04·06 보류).
되살릴 때 원본 전제는 ``service/portal/asset/__init__.py`` 의 「원본 파일 전제」를 따른다(묶음 대상도 원본이 바뀌고 사라질 수 있다).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import params
from service.portal import mm_meta
from service.portal.auth import require_principal
from service.portal.common.db_manager import DbManager
from src.mm_meta.rules import MIN_BUNDLE_SIZE
from src.search.cursor import CursorError

# 접두사를 두지 않는 이유 — 목록 경로가 ``/mm-meta`` 자체라 prefix 와 빈 경로가 만나면
# 끝 슬래시 취급이 미묘해진다. 이득은 표기뿐이라 정확성을 택했다. 인증만 라우터에 건다.
router = APIRouter(tags=["entity"], dependencies=[Depends(require_principal)])

# 질의 길이 상한 — 검색 엔진이 낱말마다 절(clause)을 만들고 **1024개**에서 거절한다
# (실측 2026-09-21: 2,000자 질의가 OpenSearch RequestError 로 터져 **500** 이 났다).
# 입력이 너무 긴 것은 **사용자가 고칠 문제**라 422 로 앞에서 끊는다 — 상한은 넉넉하되
# 질의와 좁히기를 합쳐도 절 수가 한계에 닿지 않는 값이다.
_QUERY_MAX_LEN = 300

# 노출 임계 — 구성 자산이 이 수 미만인 개체는 목록·칩에서 **모두** 감춘다.
# 🔴 목록과 칩이 **같은 값**을 써야 한다: 칩에 "다섯"이라 적혀 있는데 눌러서 세 건이 나오면 사용자가
#    숫자를 믿지 않게 된다("적힌 숫자 = 누르면 나오는 수" 원칙 · 2026-09-08 판정으로 통일).
#    값 자체는 코어에서 읽는다 — 판정 배치가 대상을 고를 때 같은 값을 써야 판정과 노출이 안 어긋난다.
_MIN_BUNDLE_SIZE = MIN_BUNDLE_SIZE


def _parse_names(raw: str | None) -> list[str]:
    """쉼표로 이은 이름 목록을 파싱한다(빈 값·공백 제거).

    Args:
        raw: ``"한식,영화"`` 꼴 문자열. ``None``·빈 값이면 조건 없음을 뜻한다.

    Returns:
        이름 목록. 조건이 없으면 빈 목록.
    """
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


@router.get("/mm-meta")
def list_mm_meta(
    q: str | None = Query(
        None,
        max_length=_QUERY_MAX_LEN,
        description=(
            "검색어(낱말 단위) — 이름·근거 키워드·설명·구성 자료를 본다. 글자가 겹치지 않아도"
            " 뜻이 가까우면 함께 걸린다(의미 검색 · 게이트를 넘겼을 때). 미지정이면 전체 목록"
        ),
    ),
    entity_type: str | None = Query(None, description="종류(타입) 필터. 미지정이면 전체"),
    areas: str | None = Query(
        None,
        description=(
            "갈래(개체에 붙은 분류 라벨) — 쉼표로 이어 보내면 **모두** 가진 개체만 남는다(AND)."
            " 자산에 붙은 라벨이 아니라 개체에 붙은 라벨이라 좁혀도 개체가 쪼개지지 않는다."
        ),
    ),
    refine: str | None = Query(
        None,
        max_length=_QUERY_MAX_LEN,
        description=(
            "결과 내 재검색(낱말 좁히기 · 099). 공백으로 쪼갠 낱말이 **모두** 걸린 개체만 남긴다"
            "(이름·근거 키워드·설명·구성 자료 대상). 🔴 **이번 쪽이 아니라 결과 집합 전체**를 서버가"
            " 다시 좁힌다 — 상위 200 밖 개체도 좁히기로 닿는다. 질의(q)는 바꾸지 않으므로 결과는"
            " 언제나 좁히기 전 결과의 부분집합이다"
        ),
    ),
    cursor: str | None = Query(
        None,
        description=(
            "이어 읽기 표식(책갈피 · 099). 직전 응답의 next_cursor 를 그대로 넘기면 그 다음부터 잇는다."
            " 검색어(q)·재검색(refine)과 함께 써도 된다 — 정렬이 언제나 구성 자산 수라 이어받을 자리가 있다."
            " 🔴 조건(q·refine·종류·갈래)을 하나라도 고치면 **커서는 무효**이며 서버가 400 으로 끊는다"
            " — 처음부터 다시 받아야 한다. 종전에는 막지 못해 조건이 바뀐 커서가 통과했고 자료가"
            " 조용히 빠졌다(099 G7)"
        ),
    ),
    limit: int = Query(
        200, ge=1, le=500,
        description=(
            "한 쪽에 담을 개체 수(구성 자산 수 상위). 더 보려면 next_cursor 로 다음 쪽을 받는다 —"
            " 상한을 키워 전량을 한 번에 받을 이유가 없다."
            " 검색어(q)가 있어도 뜻이 같다 — 검색·재검색은 서버가 집합으로 좁히고 쪽은 그 안에서 센다"
        ),
    ),
) -> dict[str, Any]:
    """개체 목록·검색 — 구성 자산 수 내림차순(언제나 DB 정렬).

    화면의 카드 그리드가 쓴다. **검색도 좁히기도 서버가 한다** — 화면이 전량을 받아 브라우저에서
    거르는 방식은 "목록 전체가 이미 손에 있다"를 전제하므로 규모가 커지면 성립하지 않는다.

    **찾아오기(q)와 재검색(refine)은 한 구조다**(099 §3-2a): 둘 다 엔진에 낱말을 던져 **매칭 개체
    집합**을 얻고, 결과는 그 교집합이다. 정렬은 언제나 DB(구성 자산 수)이므로 **커서가 q 유무와
    무관하게 성립**한다. refine 은 질의를 바꾸지 않으므로(집합 필터) 좁힌 결과는 언제나 좁히기 전의
    부분집합이다 — 좁혔는데 없던 개체가 나타나는 일이 없다.

    🔴 **프론트 계약**: ``q``·``refine``·종류·갈래가 바뀌면 결과 집합이 통째로 달라지므로 화면은
    **커서를 버리고 처음부터** 받아야 한다. 099 G7 부터 **서버가 강제한다** — 커서에 조건 지문이
    함께 들어 있어, 조건이 바뀐 커서는 **400** 이다(종전에는 200 으로 이어 주어 자료가 조용히 빠졌다).

    🔴 **이름을 정확히 친 개체는 맨 앞**에 온다(099 G7). 종전에는 구성 자산 수 순뿐이라 `숭례문` 이
    7위, `경포대` 가 15위였다 — 이름을 아는 사람에게 큰 개체부터 보여 준 셈이다. 부분 일치는
    앞세우지 않는다(순위가 뒤집힌 이유를 설명할 수 없게 된다).

    Args:
        q: 검색어. 앞뒤 공백은 무시하고 빈 문자열은 미지정과 같다.
        entity_type: 종류 필터.
        areas: 갈래 이름들(쉼표 구분 · AND).
        refine: 결과 내 재검색 낱말들(결과 집합 **전체**에 적용).
        limit: 한 쪽에 보일 개체 수.
        cursor: 이어 읽기 표식(직전 응답의 ``next_cursor``). ``q``·``refine`` 과 함께 쓸 수 있다.

    Returns:
        ``{items, total, scope_total, next_cursor}``. ``refine`` 을 준 요청에만 ``refine`` 이 더
        실린다. ``total``(좁히기 **이후**)·``scope_total``(좁히기 **이전** · "지우면 N건")은 경로와
        무관하게 **모수**다 — 돌려준 개수가 아니라 조건에 맞는 전부. ``next_cursor`` 가 ``None``
        이면 마지막 쪽이다. **검색 경로**(``q``·``refine`` 중 하나라도 준 요청)의 항목에는
        ``by_text``·``by_semantic``·``match_reason`` 이 **더** 실린다(2026-09-17 결정) — 뜻(kNN)으로
        걸린 개체는 카드에 검색어가 한 자도 없어서(`왕실 무덤`→`영릉`) 근거를 못 보이면 사용자가
        "검색이 고장났나"로 읽기 때문이다(089·090·092 가 만든 설명 가능성). 🔴 **불린이 실질이고
        문구는 표시용**이다 — 화면이 ``match_reason`` 을 파싱해 층을 가르면 문구를 고칠 때 조용히
        깨진다. 검색이 없는 목록 응답에는 이 키가 **없다**(걸린 이유 자체가 없다).

    Raises:
        HTTPException: 커서가 깨졌거나 정렬·**조건**이 어긋나면 400 · 집합 판정에 실패하면 503
            (엔진·임베딩 연결 실패 · 되돌림 백엔드). 🔴 **전체 목록으로 되돌리지 않는다**.
    """
    picked_areas = _parse_names(areas)
    # 커서에 실을 **조건 지문 재료**(099 G7) — 이번 결과 집합을 정의하는 것 전부를 한 문자열로.
    #   같은 값을 되읽기와 다음 커서 발급에 함께 쓴다(두 곳이 갈라지면 서버가 준 커서를 서버가
    #   거부한다). 무엇이 재료이고 무엇을 뺐는지는 `mm_meta.entity_cursor_scope` 주석에 있다.
    cursor = params.cursor_or_none(cursor)   # 빈 커서(cursor=)는 커서 없음(첫 쪽) — "커서가 비었다" 400 이 아니다
    scope_material = mm_meta.entity_cursor_scope(
        q=q, refine=refine, entity_type=entity_type, areas=picked_areas,
        min_bundle_size=_MIN_BUNDLE_SIZE)
    after_tier: int | None = None
    after_count: int | None = None
    after_uid: str | None = None
    after_type: str | None = None
    if cursor is not None:
        try:
            after_tier, after_count, after_uid, after_type = mm_meta.decode_entity_cursor(
                cursor, scope=scope_material)
        except CursorError as exc:
            # 입력 오류이지 서버 오류가 아니다 — **조용히 다른 자리에서 이어 주지 않는다**.
            #   조건이 바뀐 커서도 여기서 끊긴다(실측 2026-09-17: q 를 바꾼 채 이어 읽자 1쪽에
            #   있던 개체들이 통째로 빠졌다 — 오류가 없어 화면은 그것을 알 수 없었다).
            #   문구의 파이썬 진단(예외 원문 · 입력 repr)은 떼고 할 일을 붙인다(``params.cursor_detail``).
            raise HTTPException(status_code=400, detail=params.cursor_detail(exc)) from exc
    # 🔴 집합 판정이 **DB 읽기보다 먼저**다 — 화이트리스트를 SQL 에 얹어야 상한 밖 개체도 검색·좁히기로
    #    닿는다(2026-09-15 실측: 「숭례문」이 색인에 있는데 상위 200 을 먼저 자르는 바람에 0건이었다).
    #    엔진 왕복이라 DB 트랜잭션 **밖**에서 한다(커넥션을 쥔 채 네트워크를 기다리지 않게).
    try:
        scope = mm_meta.search_and_refine(q=q, refine=refine)
    except mm_meta.EntitySearchUnavailable as exc:
        # ⛔ 여기서 "필터 없음"으로 되돌리면 검색했는데 전량이 나가고, 빈 집합으로 접으면 "자료가 없다"와
        #    "검색이 죽었다"가 같아진다. 둘 다 사용자를 속이므로 끊는다(파일 검색과 같은 규율).
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # **이름을 정확히 친 개체**는 맨 앞에 세운다(099 G7 · 결함 B). 실측에서 `숭례문` 은 7위,
    #   `경포대` 는 15위였다 — 정렬이 구성 자산 수뿐이라 큰 개체가 늘 위로 왔기 때문이다.
    #   🔴 판정은 여기(화면 정책)서 하고, 순서를 만드는 것은 코어 SQL 이다(093 경계).
    uid_first = mm_meta.name_first_keys(q=q, refine=refine, keys=scope.uid_allow)
    # **뜻으로 상위**인 개체도 앞자리로 승급시킨다(2026-09-21). 이름이 하나도 안 맞는 개념 질의
    #   (「남자 배우」)에서는 전원이 티어 0 이 되어 구성 자산 수가 순서를 지배했다 — kNN 1등이
    #   하정우인데 화면 1위는 채원빈이었다. 관련도로 **정렬**하는 것이 아니라 상위를 **승급**시키는
    #   것이라 커서 계약은 그대로다(티어는 이미 커서에 실려 있다).
    uid_semantic = mm_meta.semantic_first_keys(scope.semantic_ranked)

    def _read(repo: Any) -> tuple[list[dict[str, Any]], int, int]:
        """이 쪽의 행과 두 모수를 **한 트랜잭션**에서 읽는다(세 값이 서로 다른 시점을 말하지 않게).

        Args:
            repo: 저장소 묶음(``DbManager.read`` 가 넘긴다 — 셋이 같은 커넥션을 쓴다).

        Returns:
            ``(이 쪽의 목록, 좁히기 이후 모수, 좁히기 이전 모수)``.
        """
        page = repo.entity.page(
            entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, limit=limit,
            after_tier=after_tier, after_count=after_count,
            after_uid=after_uid, after_type=after_type,
            uid_allow=scope.uid_allow, uid_first=uid_first, uid_semantic=uid_semantic,
        )
        after = repo.entity.total(
            entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, uid_allow=scope.uid_allow,
        )
        # 좁히기가 없으면 두 모수가 **같은 값**이다 — 같은 수를 두 번 묻지 않는다.
        before = after if not scope.refined else repo.entity.total(
            entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, uid_allow=scope.scope_allow,
        )
        return page, after, before

    rows, total, scope_total = DbManager.read(_read)
    # 다음 책갈피는 **DB 가 준 쪽 그대로**에서 만든다. 좁히기는 이미 SQL 이 적용했으므로 여기서 행이
    # 더 줄어들 일이 없다(파이썬이 다시 거르면 걸러진 꼬리를 다음 쪽이 건너뛰어 누락이 났다).
    body: dict[str, Any] = {
        # 「걸린 이유」는 **쪽을 다 고른 뒤** 얹는다 — 행을 더하거나 빼지 않는 표시용 정보라 위
        # ``next_cursor`` 재료(DB 가 준 쪽 그대로)에 영향을 주지 않는다.
        "items": mm_meta.attach_match_reason(rows, scope=scope),
        "total": total,
        "scope_total": scope_total,
        "next_cursor": mm_meta.next_entity_cursor(
            rows, page_size=limit, scope=scope_material,
            uid_first=uid_first, uid_semantic=uid_semantic),
    }
    if scope.refined:
        body["refine"] = refine
    return body


@router.get("/mm-meta/facets")
def mm_meta_facets(
    entity_type: str | None = Query(None, description="지금 고른 종류 — 갈래 건수를 이 안으로 좁힌다"),
    areas: str | None = Query(None, description="지금 고른 갈래들(쉼표) — 갈래 건수를 더 좁힌다"),
) -> dict[str, Any]:
    """좁히기 칩 두 축의 건수 — 종류와 갈래.

    건수는 **노출 임계를 통과한 개체 수**이고 목록과 같은 임계를 쓴다. 0건 축도 응답에 실린다 —
    0 은 "이 갈래엔 아직 자료가 없다"는 신호라 API 가 지우지 않고, 감출지는 화면이 정한다.

    세는 범위가 축마다 다른 이유는 정형 계층 docstring 에 적어 두었다(종류는 갈아타는 축이라 조건을
    적용하지 않고, 갈래는 좁히는 축이라 적용한다).

    Args:
        entity_type: 지금 고른 종류.
        areas: 지금 고른 갈래들(쉼표 구분).

    Returns:
        ``{vocab, types, areas, min_members, scoped_by}``.
    """
    return DbManager.read(
        lambda repo: repo.entity.facets(
            entity_type=entity_type, areas=_parse_names(areas),
            min_bundle_size=_MIN_BUNDLE_SIZE,
        )
    )


@router.get("/mm-meta/{entity_type}/{entity_uid}")
def mm_meta_card(
    entity_type: str,
    entity_uid: str,
) -> dict[str, Any]:
    """개체 카드 — 모달리티별 구성 자산과 자료 성격.

    이 기능의 존재 이유가 크로스모달 응집(글·그림·영상·소리가 한 묶음)이라 반환도 모달리티별 그룹이다.

    Args:
        entity_type: 개체 종류(닫힌 어휘).
        entity_uid: 표기 키(원표기를 줘도 코어가 정규화해 흡수한다).

    Returns:
        묶음 + ``confirmed_count``·``description``·자산별 ``forms``·``form_counts``.

    Raises:
        HTTPException: 개체 자체가 없으면 404. **빈 개체는 404 가 아니다**(200·``total`` 0) —
            "등록했는데 안 보인다"와 "주소가 틀렸다"를 같은 응답으로 만들지 않는다.
    """
    card = DbManager.read(
        lambda repo: repo.entity.card(entity_type=entity_type, entity_uid=entity_uid))
    if card is None:
        raise HTTPException(status_code=404, detail="해당 멀티모달 메타가 없다")
    return card  # type: ignore[return-value]
