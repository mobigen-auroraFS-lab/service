"""개체(멀티모달 메타) 화면 라우트 — 얇게. 조립은 ``service/portal/mm_meta.py``, 읽기는 코어 seam.

이 파일은 파라미터 검증 · 트랜잭션 안에서 조립 함수 호출 · HTTP 상태 코드만 한다(세는 규칙 · 노출 기준 · 정렬은 코어, 응답 키 · 상한 · 문구는 정형 계층).
개체를 좁히는 축은 종류(타입)와 갈래(개체 라벨)이고 둘 다 개체에 붙어 있어 좁혀도 개체가 쪼개지지 않는다.
옛 별칭 ``/mm-meta/types`` 는 지웠다(칩 창구 ``/mm-meta/facets`` 로 흡수).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from service.api import params
from service.api.bundle_response import (
    BundleSlot,
    bundle_headers,
    content_disposition,
    make_plan,
    zip_head_response,
    zip_response,
)
from service.portal import mm_meta
from service.portal.auth import get_bundle_principal, require_principal
from service.portal.common.db_manager import DbManager
from src.mm_meta.rules import MIN_BUNDLE_SIZE
from src.search.cursor import CursorError

# 접두사를 두지 않는 이유 — 목록 경로가 ``/mm-meta`` 자체라 prefix 와 빈 경로가 만나면
# 끝 슬래시 취급이 미묘해진다. 이득은 표기뿐이라 정확성을 택했다. 인증만 라우터에 건다.
router = APIRouter(tags=["entity"], dependencies=[Depends(require_principal)])

# 묶음(zip) 내려받기 — 헤더 **또는** 묶음 링크(``?link=``)로 인증한다(``get_bundle_principal`` · 링크는 그 요청 대상에만 통한다).
bundle_router = APIRouter(tags=["entity"], dependencies=[Depends(get_bundle_principal)])
# 화면에 그대로 뜨는 문구라 사용자 말로 쓴다(웹 점검 #36).
_ENTITY_404 = "찾는 개체가 없습니다."
_NOTHING_TO_ZIP = "내려받을 수 있는 파일이 없습니다."
_LINK_DESC = "묶음 링크(`POST /bundle-links` 로 받은 것). 헤더 대신 이 링크로 인증한다 — 그 요청 대상(경로 + 조건)에만 통하고 곧 만료된다"

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


def _parse_modalities(raw: str | None) -> list[str]:
    """고른 자료 유형 — 알 수 없는 값은 422(고칠 수 있는 입력 오류). 문자열이 아니면(라우트 함수를 직접 부를 때의 기본값) 고른 것이 없다."""
    if not isinstance(raw, str):
        return []
    try:
        return mm_meta.parse_modalities(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _parse_names(raw: str | None) -> list[str]:
    """쉼표로 이은 이름 목록을 파싱한다(빈 값 · 공백 제거). ``None`` · 빈 값이면 빈 목록."""
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
    modalities: str | None = Query(
        None,
        description=(
            "자료 유형 — text · image · video · audio 를 쉼표로 이어 보내면 **모두** 가진 개체만 남는다(AND)."
            " 개체 항목의 ``modalities`` 배열 기준이다. 종류 · 갈래 · 검색어와 함께 걸린다."
            " 🔴 조건이라 바뀌면 커서는 무효(400). 알 수 없는 값은 422"
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
    """개체 목록 · 검색 — 구성 자산 수 내림차순(언제나 DB 정렬). 검색도 좁히기도 서버가 한다.

    찾아오기(``q``)와 재검색(``refine``)은 한 구조다 — 둘 다 엔진에 낱말을 던져 매칭 개체 집합을 얻고 결과는 그 교집합이며, 좁힌 결과는 항상 좁히기 전의 부분집합이다.
    ``q`` · ``refine`` · 종류 · 갈래가 바뀌면 화면은 커서를 버리고 처음부터 받아야 한다 — 커서에 조건 지문이 있어 조건이 바뀐 커서는 400 이다.
    이름을 정확히 친 개체는 맨 앞에 온다(부분 일치는 앞세우지 않는다).

    Returns:
        ``{items, total, scope_total, next_cursor}``(+ ``refine`` 을 준 요청에만 ``refine``). ``total``(좁히기 이후) · ``scope_total``(이전)은 돌려준 개수가 아니라 조건에 맞는 전부(모수)다.
        항목의 ``confirmed_count`` 는 연결된 자료 수(확인 전 포함)이고 화면 표기는 「연결된 N건」이다(정렬 · 커서 키라 이름은 그대로).
        ``next_cursor`` 가 ``None`` 이면 마지막 쪽이다. 검색 경로(``q``/``refine`` 중 하나라도 준 요청)의 항목에는 ``by_text`` · ``by_semantic`` · ``match_reason`` 이 더 실린다
        (뜻으로 걸린 개체는 카드에 검색어가 없어 근거가 필요하다 — 불린이 실질이고 문구는 표시용). 검색이 없는 목록에는 이 키가 없다.

    Raises:
        HTTPException: 커서가 깨졌거나 정렬 · 조건이 어긋나면 400 · 집합 판정 실패(엔진 · 임베딩 연결 실패 · 되돌림 백엔드)는 503(전체 목록으로 되돌리지 않는다).
    """
    picked_areas = _parse_names(areas)
    picked_mods = _parse_modalities(modalities)
    # 커서에 실을 **조건 지문 재료**(099 G7) — 이번 결과 집합을 정의하는 것 전부를 한 문자열로.
    #   같은 값을 되읽기와 다음 커서 발급에 함께 쓴다(두 곳이 갈라지면 서버가 준 커서를 서버가
    #   거부한다). 무엇이 재료이고 무엇을 뺐는지는 `mm_meta.entity_cursor_scope` 주석에 있다.
    cursor = params.cursor_or_none(cursor)   # 빈 커서(cursor=)는 커서 없음(첫 쪽) — "커서가 비었다" 400 이 아니다
    scope_material = mm_meta.entity_cursor_scope(
        q=q, refine=refine, entity_type=entity_type, areas=picked_areas,
        min_bundle_size=_MIN_BUNDLE_SIZE, modalities=picked_mods)
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
        """이 쪽의 행과 두 모수를 한 트랜잭션에서 읽는다(세 값이 서로 다른 시점을 말하지 않게) — ``(목록, 좁히기 이후 모수, 이전 모수)``."""
        # 자료 유형 조건은 검색 집합에 **교집합**으로 얹는다(None = 걸지 않음 — 빈 집합과 섞지 않는다).
        mod_allow = repo.entity.modality_allow(picked_mods)
        uid_allow = mm_meta.intersect_allow(scope.uid_allow, mod_allow)
        scope_allow = mm_meta.intersect_allow(scope.scope_allow, mod_allow)
        # 쪽과 좁히기 이후 모수는 코어 한 번에 받는다(코어 spec 113 — 노출 개체 집합을 한 번만 만든다).
        page, after = repo.entity.page_with_total(
            entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, limit=limit,
            after_tier=after_tier, after_count=after_count,
            after_uid=after_uid, after_type=after_type,
            uid_allow=uid_allow, uid_first=uid_first, uid_semantic=uid_semantic,
        )
        # 좁히기가 없으면 두 모수가 **같은 값**이다 — 같은 수를 두 번 묻지 않는다. 있으면 허용 목록이 달라 따로 센다.
        before = after if not scope.refined else repo.entity.total(
            entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, uid_allow=scope_allow,
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
    entity_type: str | None = Query(None, description="지금 고른 종류 — 갈래 · 자료 유형 건수를 이 안으로 좁힌다(종류 칩은 종류만 바꾼 수)"),
    areas: str | None = Query(None, description="지금 고른 갈래들(쉼표) — 갈래 건수를 더 좁힌다"),
    modalities: str | None = Query(
        None,
        description=(
            "지금 고른 자료 유형들(text · image · video · audio 쉼표) — 세 축의 칩 건수를 모두 이 안으로 좁힌다(칩 숫자 = 그 칩을 눌렀을 때 나올 수)."
            " 알 수 없는 값은 422"
        ),
    ),
) -> dict[str, Any]:
    """좁히기 칩 세 축의 건수 — 종류 · 갈래 · 자료 유형. 건수는 노출 임계를 통과한 개체 수이고 목록과 같은 임계를 쓴다. 0건 축도 싣는다(감출지는 화면이 정한다). ``{vocab, types, areas, modalities, min_members, scoped_by}``."""
    picked_mods = _parse_modalities(modalities)
    return DbManager.read(
        lambda repo: repo.entity.facets(
            entity_type=entity_type, areas=_parse_names(areas),
            min_bundle_size=_MIN_BUNDLE_SIZE, modalities=picked_mods,
        )
    )


@router.get("/mm-meta/{entity_type}/{entity_uid}")
def mm_meta_card(
    entity_type: str,
    entity_uid: str,
) -> dict[str, Any]:
    """개체 카드 — 모달리티별 구성 자산과 자료 성격(크로스모달 응집이 존재 이유라 모달리티별 그룹으로 돌려준다). 개체가 없으면 404, 빈 개체는 200(``total`` 0).

    ``confirmed_count`` 는 ``total`` 과 같은 값(연결된 자료 수 · 확인 전 포함)이다. 자산마다 ``status`` 가 소속 확인 여부(``active`` 확인 · ``proposed`` 확인 전)다.
    """
    card = DbManager.read(
        lambda repo: repo.entity.card(entity_type=entity_type, entity_uid=entity_uid))
    if card is None:
        raise HTTPException(status_code=404, detail=_ENTITY_404)
    return card  # type: ignore[return-value]


# ── 묶음(zip) — 임시 파일 없이 만들면서 흘려 보낸다(``service/portal/asset/bundle_stream.py``) ──────────────
def _entities_bundle(entity_type: str | None, areas: str | None, labels: str | None, exclude_video: bool, *, head: bool) -> Response:
    picked = _parse_names(areas) or _parse_names(labels)
    # 조건 없는 전체 묶음은 확인 단계만 수 분이 걸린다(웹 점검 #4 · #48) — 그 단계부터 동시 묶음 자리 안에서 한다(HEAD 포함).
    with BundleSlot() as slot:
        rows: list[dict[str, Any]] = DbManager.read(
            lambda repo: repo.entity.zip_rows(
                entity_type=entity_type, areas=picked,
                min_bundle_size=_MIN_BUNDLE_SIZE, exclude_video=exclude_video,
            )
        )
        if not rows:
            raise HTTPException(status_code=404, detail="이 조건에 해당하는 자료가 없습니다")
        targets = [
            {"asset_id": r["asset_id"], "fs_path": r["fs_path"], "file_name": r["file_name"]}
            for r in rows if r["fs_path"]
        ]
        if not targets:
            raise HTTPException(status_code=409, detail=_NOTHING_TO_ZIP)
        plan = make_plan(targets)
        # 한글 이름은 filename* 로 싣는다(웹 점검 #41 — 어느 묶음이든 「mmmeta_N files.zip」 꼴이라 받은 파일끼리 구별이 안 됐다). ASCII 이름은 옛 브라우저용.
        disposition = content_disposition(
            mm_meta.zip_display_name(["개체", entity_type or "전체", *picked]),
            ascii_name=mm_meta.ascii_zip_name([entity_type or "all", *picked], fallback="meta", count=len(plan.packed)))
        if head:
            return zip_head_response(content_disposition=disposition, headers=bundle_headers(plan))
        return zip_response(plan, content_disposition=disposition, headers=bundle_headers(plan), slot=slot)


@bundle_router.get("/mm-meta/bundle")
def download_entities_bundle(
    entity_type: str | None = Query(None, description="종류(타입) 필터"),
    areas: str | None = Query(None, description="갈래(개체 라벨) — 모두 가진 개체만(AND · 쉼표 구분)"),
    labels: str | None = Query(
        None,
        description="⚠️ 옛 이름 — `areas` 와 같은 뜻이다. 프론트가 옮겨 갈 때까지 함께 받는다",
        deprecated=True,
    ),
    exclude_video: bool = Query(False, description="영상 제외(용량이 크게 준다)"),
    link: str | None = Query(None, description=_LINK_DESC),
) -> Response:
    """지금 좁힌 개체들의 구성 자산 전부를 한 zip 으로 내려준다(종류 · 갈래 축은 목록과 같다 — 옛 이름 ``labels`` 도 받는다).

    검색어(``q``) · ``refine`` · ``limit`` 은 받지 않아 검색어로 좁힌 목록에서 부르면 전량이 대상이 된다. 확인 전(proposed) 소속까지 담는다.
    좁힌 결과가 비면 404 · 경로를 아는 파일이 하나도 없으면 409. 용량 상한은 두지 않는다(2026-10-13 · 동시 묶음 수 제한만 걸린다). 헤더 대신 묶음 링크(``link``)로도 받는다.
    """
    return _entities_bundle(entity_type, areas, labels, exclude_video, head=False)


@bundle_router.head("/mm-meta/bundle", include_in_schema=False)
def entities_bundle_head(
    entity_type: str | None = None, areas: str | None = None, labels: str | None = None, exclude_video: bool = False,
) -> Response:
    """본문 없이 같은 머리 — 링크를 열기 전에 404 · 409 · 담길 수를 확인한다(접근 기록 없음 · 확인하는 동안 동시 묶음 자리를 잡는다 — 꽉 차면 503)."""
    return _entities_bundle(entity_type, areas, labels, exclude_video, head=True)


def _card_bundle(entity_type: str, entity_uid: str, *, head: bool) -> Response:
    with BundleSlot() as slot:      # 대상 조회 · 파일 확인부터 동시 묶음 자리 안에서 한다(HEAD 포함)
        result = DbManager.read(
            lambda repo: repo.entity.card_zip_targets(entity_type=entity_type, entity_uid=entity_uid))
        if result is None:
            raise HTTPException(status_code=404, detail=_ENTITY_404)
        targets, name = result  # type: ignore[misc]
        if not targets:
            raise HTTPException(status_code=409, detail=_NOTHING_TO_ZIP)
        plan = make_plan(targets)
        # 「작품_기생충.zip」처럼 한글 이름은 filename* 로 싣는다(웹 점검 #41). ASCII 이름은 옛 브라우저용.
        disposition = content_disposition(
            mm_meta.zip_display_name([entity_type, name]),
            ascii_name=mm_meta.ascii_zip_name([f"mm_meta_{entity_type}", name], fallback="mm_meta", count=len(plan.packed)))
        if head:
            return zip_head_response(content_disposition=disposition, headers=bundle_headers(plan))
        return zip_response(plan, content_disposition=disposition, headers=bundle_headers(plan), slot=slot)


@bundle_router.get("/mm-meta/{entity_type}/{entity_uid}/bundle")
def download_card_bundle(entity_type: str, entity_uid: str, link: str | None = Query(None, description=_LINK_DESC)) -> Response:
    """개체 카드의 구성 자산을 한 zip 으로 내려준다(건수 상한 없음). 개체가 없으면 404, 담을 파일이 없으면 409. 헤더 대신 묶음 링크(``link``)로도 받는다."""
    return _card_bundle(entity_type, entity_uid, head=False)


@bundle_router.head("/mm-meta/{entity_type}/{entity_uid}/bundle", include_in_schema=False)
def card_bundle_head(entity_type: str, entity_uid: str) -> Response:
    """본문 없이 같은 머리 — 링크를 열기 전에 404 · 409 · 담길 수를 확인한다(접근 기록 없음 · 확인하는 동안 동시 묶음 자리를 잡는다 — 꽉 차면 503)."""
    return _card_bundle(entity_type, entity_uid, head=True)
