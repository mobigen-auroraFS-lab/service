"""개체(멀티모달 메타) 화면 라우트 — 얇게. 조립은 ``service/portal/mm_meta.py``, 읽기는 코어 seam.

**이 파일이 하는 일은 넷뿐이다**: 파라미터 검증, 트랜잭션 안에서 조립 함수 호출, HTTP 상태 코드,
다운로드 헤더. 세는 규칙·노출 기준·정렬 같은 판단은 하나도 여기 없다(코어) — 그리고 응답 키 이름과
상한·문구는 정형 계층에 있다(백엔드).

**화면 개념 두 축**(spec 087): 개체를 좁히는 축은 **종류**(타입)와 **갈래**(개체에 붙은 분류 라벨)다.
둘 다 개체에 붙어 있어 좁혀도 개체가 쪼개지지 않는다. 자산에 붙는 라벨로 좁히던 옛 화면은 한 개체가
갈래마다 나뉘어 보였다(실측: 한 지명이 여섯 건인데 갈래별로 다섯·하나·하나로 갈라졌다).

**라우트 순서**: ``/mm-meta/facets``·``/mm-meta/types``·``/mm-meta/bundle`` 은 세그먼트가 하나라
``/mm-meta/{entity_type}/{entity_uid}``(둘)와 겹치지 않는다 — 선언 순서에 의존하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from service.api import _infra
from service.portal import mm_meta
from service.portal.auth import Principal, require_principal
from service.portal.download import build_bundle_zip_stream
from src.mm_meta.rules import MIN_BUNDLE_SIZE
from src.search.cursor import CursorError

router = APIRouter()

# zip 전송 조각 크기 — 자산 다운로드와 같은 값(같은 이유: 묶음이 커도 서버 메모리가 일정하다).
_STREAM_CHUNK = 64 * 1024

# 노출 임계 — 구성 자산이 이 수 미만인 개체는 목록·칩·다운로드에서 **모두** 감춘다.
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


def _zip_response(
    targets: list[dict[str, Any]], *, file_name: str, extra_headers: dict[str, str]
) -> StreamingResponse:
    """zip 을 조각내어 흘려보내는 응답을 만든다.

    파일 읽기는 DB 트랜잭션 **밖**에서 일어나고 조각으로 흐르므로, 묶음이 아무리 커도 서버 메모리
    사용량이 일정하다. 경로가 없거나 사라진 파일은 zip 안 목록 파일에 남는다(부분 zip 계약).

    Args:
        targets: ``{asset_id, fs_path, file_name}`` 목록.
        file_name: 내려줄 zip 이름(ASCII).
        extra_headers: 함께 실을 헤더(담긴 수·잘림·용량 등).

    Returns:
        ``application/zip`` 스트리밍 응답.
    """
    zip_stream = build_bundle_zip_stream(targets)

    def _iter_zip() -> Iterator[bytes]:
        """zip 을 조각내어 흘려보낸다."""
        while True:
            chunk = zip_stream.read(_STREAM_CHUNK)
            if not chunk:
                break
            yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{file_name}"', **extra_headers}
    return StreamingResponse(
        _iter_zip(), media_type="application/zip", headers=headers,
        background=BackgroundTask(zip_stream.close),
    )


@router.get("/mm-meta")
def list_mm_meta(
    q: str | None = Query(
        None,
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
    principal: Annotated[Principal, Depends(require_principal)] = ...,
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
        principal: 인증 주체.

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
    scope_material = mm_meta.entity_cursor_scope(
        q=q, refine=refine, entity_type=entity_type, areas=picked_areas,
        min_bundle_size=_MIN_BUNDLE_SIZE)
    after_tier: int | None = None
    after_count: int | None = None
    after_uid: str | None = None
    if cursor is not None:
        try:
            after_tier, after_count, after_uid = mm_meta.decode_entity_cursor(
                cursor, scope=scope_material)
        except CursorError as exc:
            # 입력 오류이지 서버 오류가 아니다 — **조용히 다른 자리에서 이어 주지 않는다**.
            #   조건이 바뀐 커서도 여기서 끊긴다(실측 2026-09-17: q 를 바꾼 채 이어 읽자 1쪽에
            #   있던 개체들이 통째로 빠졌다 — 오류가 없어 화면은 그것을 알 수 없었다).
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    def _read(conn: Any) -> tuple[list[dict[str, Any]], int, int]:
        """이 쪽의 행과 두 모수를 **한 트랜잭션**에서 읽는다(세 값이 서로 다른 시점을 말하지 않게).

        Args:
            conn: DB 커넥션(``_run_in_db`` 가 넘긴다).

        Returns:
            ``(이 쪽의 목록, 좁히기 이후 모수, 좁히기 이전 모수)``.
        """
        page = mm_meta.fetch_list(
            conn, entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, limit=limit,
            after_tier=after_tier, after_count=after_count, after_uid=after_uid,
            uid_allow=scope.uid_allow, uid_first=uid_first,
        )
        after = mm_meta.fetch_total(
            conn, entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, uid_allow=scope.uid_allow,
        )
        # 좁히기가 없으면 두 모수가 **같은 값**이다 — 같은 수를 두 번 묻지 않는다.
        before = after if not scope.refined else mm_meta.fetch_total(
            conn, entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, uid_allow=scope.scope_allow,
        )
        return page, after, before

    rows, total, scope_total = _infra._run_in_db(_read)  # type: ignore[misc]
    # 다음 책갈피는 **DB 가 준 쪽 그대로**에서 만든다. 좁히기는 이미 SQL 이 적용했으므로 여기서 행이
    # 더 줄어들 일이 없다(파이썬이 다시 거르면 걸러진 꼬리를 다음 쪽이 건너뛰어 누락이 났다).
    body: dict[str, Any] = {
        # 「걸린 이유」는 **쪽을 다 고른 뒤** 얹는다 — 행을 더하거나 빼지 않는 표시용 정보라 위
        # ``next_cursor`` 재료(DB 가 준 쪽 그대로)에 영향을 주지 않는다.
        "items": mm_meta.attach_match_reason(rows, scope=scope),
        "total": total,
        "scope_total": scope_total,
        "next_cursor": mm_meta.next_entity_cursor(
            rows, page_size=limit, scope=scope_material, uid_first=uid_first),
    }
    if scope.refined:
        body["refine"] = refine
    return body


@router.get("/mm-meta/facets")
def mm_meta_facets(
    entity_type: str | None = Query(None, description="지금 고른 종류 — 갈래 건수를 이 안으로 좁힌다"),
    areas: str | None = Query(None, description="지금 고른 갈래들(쉼표) — 갈래 건수를 더 좁힌다"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """좁히기 칩 두 축의 건수 — 종류와 갈래.

    건수는 **노출 임계를 통과한 개체 수**이고 목록과 같은 임계를 쓴다. 0건 축도 응답에 실린다 —
    0 은 "이 갈래엔 아직 자료가 없다"는 신호라 API 가 지우지 않고, 감출지는 화면이 정한다.

    세는 범위가 축마다 다른 이유는 정형 계층 docstring 에 적어 두었다(종류는 갈아타는 축이라 조건을
    적용하지 않고, 갈래는 좁히는 축이라 적용한다).

    Args:
        entity_type: 지금 고른 종류.
        areas: 지금 고른 갈래들(쉼표 구분).
        principal: 인증 주체.

    Returns:
        ``{vocab, types, areas, min_members, scoped_by}``.
    """
    return _infra._run_in_db(  # type: ignore[return-value]
        lambda conn: mm_meta.fetch_facets(
            conn, entity_type=entity_type, areas=_parse_names(areas),
            min_bundle_size=_MIN_BUNDLE_SIZE,
        )
    )


@router.get("/mm-meta/types")
def mm_meta_types(
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """타입 어휘와 종류별 개체 수 — ``/mm-meta/facets`` 의 **부분 응답 별칭**.

    이 창구는 위 칩 창구에 흡수됐다(2026-09-08 판정). 프론트가 옮겨 갈 때까지 이름만 살려 둔다 —
    옛 전화번호를 착신 전환해 두는 것과 같다. 기한은 정하지 않았다.

    Args:
        principal: 인증 주체.

    Returns:
        ``{vocab, types}`` — 칩 창구 응답에서 갈래 축을 뺀 것. 어휘 행이 없으면 ``vocab`` 은 ``None``,
        ``types`` 는 빈 목록이다(코드 프리셋으로 채우지 않는다 — 화면이 폴백하면 "등록 안 했는데 왜
        보이나"를 조사하게 된다).
    """
    facets = _infra._run_in_db(
        lambda conn: mm_meta.fetch_facets(
            conn, entity_type=None, areas=None, min_bundle_size=_MIN_BUNDLE_SIZE,
        )
    )
    return {"vocab": facets["vocab"], "types": facets["types"]}  # type: ignore[index]


@router.get("/mm-meta/bundle")
def download_entities_bundle(
    entity_type: str | None = Query(None, description="종류(타입) 필터"),
    areas: str | None = Query(None, description="갈래(개체 라벨) — 모두 가진 개체만(AND · 쉼표 구분)"),
    labels: str | None = Query(
        None,
        description="⚠️ 옛 이름 — `areas` 와 같은 뜻이다. 프론트가 옮겨 갈 때까지 함께 받는다",
        deprecated=True,
    ),
    exclude_video: bool = Query(False, description="영상 제외(용량이 크게 준다)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> Response:
    """지금 좁힌 **개체들의 구성 자산 전부**를 한 zip 으로 내려준다.

    화면의 좁히기 축과 다운로드 축이 같아야 "지금 보고 있는 것을 받는다"가 성립한다.

    옛 이름 ``labels`` 를 함께 받는 이유: 이름만 바꾸고 무시하면 프론트가 계속 그것을 보내면서
    **좁혀지지 않은 전량**을 내려받게 된다. 목록 창구의 옛 ``labels``(자산 라벨)와는 뜻이 달라 거기서는
    별칭을 두지 않는다.

    Args:
        entity_type: 종류 필터.
        areas: 갈래 이름들(쉼표 구분).
        labels: ``areas`` 의 옛 이름(둘 다 오면 ``areas`` 우선).
        exclude_video: 영상 제외 여부.
        principal: 인증 주체.

    Returns:
        zip 스트리밍 응답. 헤더에 담긴 파일 수와 총 용량을 함께 싣는다.

    Raises:
        HTTPException: 좁힌 결과가 비면 404 · 용량 상한 초과면 413 · 경로를 아는 파일이 하나도 없으면 409.
    """
    picked = _parse_names(areas) or _parse_names(labels)
    rows: list[dict[str, Any]] = _infra._run_in_db(  # type: ignore[assignment]
        lambda conn: mm_meta.entities_zip_rows(
            conn, entity_type=entity_type, areas=picked,
            min_bundle_size=_MIN_BUNDLE_SIZE, exclude_video=exclude_video,
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="이 조건에 해당하는 자료가 없습니다")
    total = sum(int(r["file_size"]) for r in rows)
    if total > mm_meta.ENTITIES_BUNDLE_MAX_BYTES:
        mb = 1024 * 1024
        raise HTTPException(
            status_code=413,
            detail=(
                f"용량이 상한을 넘습니다({total // mb}MB > "
                f"{mm_meta.ENTITIES_BUNDLE_MAX_BYTES // mb}MB) — 더 좁히거나 영상을 제외하세요"
            ),
        )
    targets = [
        {"asset_id": r["asset_id"], "fs_path": r["fs_path"], "file_name": r["file_name"]}
        for r in rows if r["fs_path"]
    ]
    if not targets:
        raise HTTPException(status_code=409, detail="내려받을 파일 경로가 없다")
    return _zip_response(
        targets,
        file_name=mm_meta.ascii_zip_name(
            [entity_type or "all", *picked], fallback="meta", count=len(targets)),
        extra_headers={"X-Bundle-Count": str(len(targets)), "X-Bundle-Bytes": str(total)},
    )


@router.get("/mm-meta/{entity_type}/{entity_uid}")
def mm_meta_card(
    entity_type: str,
    entity_uid: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """개체 카드 — 모달리티별 구성 자산과 자료 성격.

    이 기능의 존재 이유가 크로스모달 응집(글·그림·영상·소리가 한 묶음)이라 반환도 모달리티별 그룹이다.

    Args:
        entity_type: 개체 종류(닫힌 어휘).
        entity_uid: 표기 키(원표기를 줘도 코어가 정규화해 흡수한다).
        principal: 인증 주체.

    Returns:
        묶음 + ``confirmed_count``·``description``·자산별 ``forms``·``form_counts``.

    Raises:
        HTTPException: 개체 자체가 없으면 404. **빈 개체는 404 가 아니다**(200·``total`` 0) —
            "등록했는데 안 보인다"와 "주소가 틀렸다"를 같은 응답으로 만들지 않는다.
    """
    card = _infra._run_in_db(
        lambda conn: mm_meta.fetch_card(conn, entity_type=entity_type, entity_uid=entity_uid)
    )
    if card is None:
        raise HTTPException(status_code=404, detail="해당 멀티모달 메타가 없다")
    return card  # type: ignore[return-value]


@router.get("/mm-meta/{entity_type}/{entity_uid}/bundle")
def download_card_bundle(
    entity_type: str,
    entity_uid: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> Response:
    """개체 카드의 구성 자산을 한 zip 으로 내려준다.

    Args:
        entity_type: 개체 종류.
        entity_uid: 표기 키.
        principal: 인증 주체.

    Returns:
        zip 스트리밍 응답. 상한을 넘겨 잘렸으면 헤더로도 알린다(조용히 자르지 않는다).

    Raises:
        HTTPException: 개체가 없으면 404 · 구성 자산이 없거나 전부 경로 미상이면 409(빈 zip 을 주면
            사용자가 "받았는데 비었다"를 오류로 오해한다).
    """
    result = _infra._run_in_db(
        lambda conn: mm_meta.card_zip_targets(
            conn, entity_type=entity_type, entity_uid=entity_uid)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="해당 멀티모달 메타가 없다")
    targets, name, truncated = result  # type: ignore[misc]
    if not targets:
        raise HTTPException(
            status_code=409, detail="내려받을 파일이 없다(구성 자산 없음 또는 경로 미상)")
    headers = {"X-Bundle-Count": str(len(targets))}
    if truncated:
        headers["X-Bundle-Truncated"] = str(mm_meta.CARD_BUNDLE_MAX_ASSETS)
    return _zip_response(
        targets,
        file_name=mm_meta.ascii_zip_name(
            [f"mm_meta_{entity_type}", name], fallback="mm_meta", count=len(targets)),
        extra_headers=headers,
    )
