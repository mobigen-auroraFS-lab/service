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
# 검색어가 있을 때 훑는 모수의 상한. 목록 상한(`limit`)과 다른 값이다 — 저쪽은 **화면에 몇 개를
#   보일까**이고 이쪽은 **어디까지 뒤져 볼까**다. 코어 `list_entities` 가 상한을 필수로 받으므로
#   무제한 대신 넉넉한 값을 준다(노출 임계가 모수를 이미 크게 줄여 실제 행은 훨씬 적다).
_SEARCH_SCOPE_MAX = 10_000


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
    q: str | None = Query(None, description="검색어 — 이름·근거 키워드·설명을 본다(미지정=전체 목록)"),
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
            "결과 내 재검색(글자 좁히기). 공백으로 쪼갠 낱말이 **모두** 들어 있는 개체만 남긴다"
            "(이름·근거 키워드·설명 대상). 서버에 다시 묻지 않고 **이번 결과 안에서만** 좁힌다."
        ),
    ),
    cursor: str | None = Query(
        None,
        description=(
            "이어 읽기 표식(책갈피 · 099). 직전 응답의 next_cursor 를 그대로 넘기면 그 다음부터 잇는다."
            " 🔴 검색어(q)와는 함께 줄 수 없다 — 개체 검색은 아직 상위 몇 개를 고르는 순위 경로라"
            " 이어받을 자리가 없다"
        ),
    ),
    limit: int = Query(
        200, ge=1, le=500,
        description=(
            "한 쪽에 담을 개체 수(구성 자산 수 상위). 더 보려면 next_cursor 로 다음 쪽을 받는다 —"
            " 상한을 키워 전량을 한 번에 받을 이유가 없다."
            " 검색어(q)가 있을 때만 옛 뜻 그대로 **찾은 뒤 자르는 상한**이다"
        ),
    ),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """개체 목록 — 구성 자산 수 내림차순.

    화면의 카드 그리드가 쓴다. **검색은 서버가 한다** — 화면이 전량을 받아 브라우저에서 거르는 방식은
    "목록 전체가 이미 손에 있다"를 전제하므로 규모가 커지면 성립하지 않는다. 찾는 범위는 셋이다:
    이름·근거 키워드·설명. 이름만으로는 닿지 못하는 개체가 있어서다('해녀'로 제주도를 찾는 식).

    Args:
        q: 검색어. 앞뒤 공백은 무시하고 빈 문자열은 미지정과 같다.
        entity_type: 종류 필터.
        areas: 갈래 이름들(쉼표 구분 · AND).
        refine: 결과 내 재검색 글자.
        limit: 한 쪽에 보일 개체 수.
        cursor: 이어 읽기 표식(직전 응답의 ``next_cursor``). ``q`` 와 함께 주면 400.
        principal: 인증 주체.

    Returns:
        ``{items, total, scope_total, next_cursor}``. ``refine`` 을 준 요청에만 ``refine`` 이 더
        실린다. 목록 경로(``q`` 없음)의 ``total``·``scope_total`` 은 **모수**다 — 돌려준 개수가 아니라
        조건에 맞는 전부(화면의 "N건 중 M건"). ``next_cursor`` 가 ``None`` 이면 마지막 쪽이다(더 없다).
        각 항목의 ``confirmed_count`` 는 화면에서 **"확인된 N건"** 으로 표기한다(완전성을 약속하지 않는다).

    Raises:
        HTTPException: 커서가 깨졌거나 정렬이 어긋나면 400 · ``cursor`` 와 ``q`` 를 함께 주면 400.
    """
    has_query = bool(q and q.strip())
    # 🔴 커서와 검색어를 함께 받지 않는다. 개체 검색은 아직 **순위**(상위 몇 개)라 "그 다음부터"를
    #    가리킬 자리가 없다 — 조용히 무시하면 화면이 같은 쪽을 계속 받으며 끝나지 않는다.
    #    검색을 집합 판정으로 바꾼 뒤에 열린다(099 G4).
    if cursor is not None and has_query:
        raise HTTPException(
            status_code=400,
            detail="cursor 와 q 는 함께 줄 수 없습니다 — 개체 검색은 아직 이어 읽기를 지원하지 않습니다",
        )
    after_count: int | None = None
    after_uid: str | None = None
    if cursor is not None:
        try:
            after_count, after_uid = mm_meta.decode_entity_cursor(cursor)
        except CursorError as exc:
            # 입력 오류이지 서버 오류가 아니다 — **조용히 다른 자리에서 이어 주지 않는다**.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    picked_areas = _parse_names(areas)
    # 🔴 검색어가 있으면 **상한 밖까지** 모수를 가져온다 — 먼저 잘라 놓고 그 안에서 찾으면
    #    상위 N 밖의 개체는 검색으로도 닿을 수 없다(2026-09-15 실측: 「숭례문」이 색인에 있는데 0건).
    #    상한은 `search_and_refine` 이 **찾은 뒤에** 적용한다. 모수 자체는 노출 임계
    #    (`_MIN_BUNDLE_SIZE`)가 이미 줄여 놓아 무거워지지 않는다.
    scope = _SEARCH_SCOPE_MAX if has_query else limit

    def _read(conn: Any) -> tuple[list[dict[str, Any]], int | None]:
        """이 쪽의 행과 모수를 **한 트랜잭션**에서 읽는다(두 값이 서로 다른 시점을 말하지 않게).

        Args:
            conn: DB 커넥션(``_run_in_db`` 가 넘긴다).

        Returns:
            ``(이 쪽의 목록, 모수)``. 검색 경로에서는 모수를 세지 않으므로 뒤 값이 ``None`` 이다.
        """
        page = mm_meta.fetch_list(
            conn, entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE, limit=scope,
            after_count=after_count, after_uid=after_uid,
        )
        # ⛔ 검색 경로는 모수를 세지 않는다 — 개체 검색이 아직 **순위**(상위 몇 개)라 "조건에 맞는
        #    전부"가 몇 개인지 셀 방법이 없다. 목록과 다른 수를 내보내면 화면이 거짓말을 한다(099 G4).
        parent = None if has_query else mm_meta.fetch_total(
            conn, entity_type=entity_type, areas=picked_areas,
            min_bundle_size=_MIN_BUNDLE_SIZE,
        )
        return page, parent

    rows, parent_total = _infra._run_in_db(_read)  # type: ignore[misc]
    # 🔴 다음 책갈피는 **DB 가 준 쪽 그대로**에서 만든다 — 글자 좁히기로 걸러낸 뒤의 행으로 만들면
    #    걸러진 꼬리를 다음 쪽이 건너뛴다(누락). 검색 경로는 아직 커서가 없어 언제나 None 이다.
    next_cursor = None if has_query else mm_meta.next_entity_cursor(rows, page_size=limit)
    items, scope_total = mm_meta.search_and_refine(
        rows, q=q, refine=refine, run_in_db=_infra._run_in_db,  # type: ignore[arg-type]
        limit=limit,
    )
    refined = bool(refine and refine.strip())
    # 건수의 뜻(plan 099 §1-⑥): `scope_total` = 좁히기 **이전** 집합 크기 · `total` = 좁히기 **이후**.
    #   - 목록 경로는 둘 다 **모수**다. 한 쪽만 돌려줘도 "전체 몇 건"을 정확히 말한다.
    #   - ⚠️ 좁히기(`refine`)가 걸린 목록의 `total` 은 아직 **이 쪽 안에서 좁힌 수**다. 좁히기가
    #     파이썬에서 쪽 단위로 돌기 때문이며, 서버 질의로 옮기는 것은 099 G5 다. 그때 모수가 된다.
    #   - 검색 경로(`q`)는 **현행 그대로** — `total` 은 돌려준 개수, `scope_total` 은 상한으로 자른 뒤
    #     좁히기 전 개수다(모수 아님 · G4 에서 집합 판정으로 바뀌면 모수가 된다).
    if parent_total is not None:
        scope_total = parent_total
        total = len(items) if refined else parent_total
    else:
        total = len(items)
    body: dict[str, Any] = {
        "items": items, "total": total, "scope_total": scope_total, "next_cursor": next_cursor,
    }
    if refined:
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
