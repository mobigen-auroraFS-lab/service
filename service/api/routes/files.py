"""파일 제공 창구 — **자리만 있다**(모두 501). 원본 · 미리보기 · 원문 · 묶음 zip.

파일을 어떻게 내줄지(원본 위치 · 접근 방식 · 바뀌고 사라지는 원본 처리)는 다른 쪽과 협의한 뒤 정한다
(`TODO.md` [협의] 파일 제공). 그때까지 경로와 파라미터는 남겨 둔다 — 화면이 이 이름으로 코딩할 수 있고,
API 문서(``/docs``)에 계약이 보인다. 부르면 **501** 로 분명히 막는다(404 로 두면 "경로를 틀렸다"로 읽힌다).

  · 인증은 다른 창구와 같다 — 라우터에 걸려 있어 운영에서 토큰이 없으면 501 보다 401 이 먼저다.
  · 파라미터 형식 검증(FastAPI)도 먼저다 — 요청 본문이 틀리면 422.
  · 구현은 지웠다(2026-09-28) — 지우기 직전 구현은 git ``9d11d29``. 다시 만들 때는
    ``service/portal/asset/__init__.py`` 의 「원본 파일 전제」를 따른다.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from service.portal.auth import require_principal

router = APIRouter(tags=["files"], dependencies=[Depends(require_principal)])

NOT_READY_DETAIL = "파일 제공은 아직 없습니다 — 제공 방식을 정한 뒤 엽니다"
_NOT_READY = {501: {"description": "아직 제공하지 않음 — 파일 제공 방식 협의 대기(자리만 있다)"}}


def _not_ready() -> NoReturn:
    raise HTTPException(status_code=501, detail=NOT_READY_DETAIL)


@router.get("/assets/{asset_id}/download", responses=_NOT_READY)
def download(asset_id: str) -> None:
    """자산 원본 내려받기 — **준비 중(501)**. 구간 요청(``Range`` · 이어받기 · 영상 탐색)을 받을 자리다."""
    _not_ready()


@router.get("/assets/{asset_id}/thumbnail", responses=_NOT_READY)
def asset_thumbnail(
    asset_id: str,
    size: str = Query("card",
                      description="크기 프리셋: card(320·목록/hover 기본) | detail(640·상세 히어로) · 대소문자 무관"),
) -> None:
    """이미지 · 영상의 축소 미리보기 — **준비 중(501)**."""
    _not_ready()


@router.get("/assets/{asset_id}/content", responses=_NOT_READY)
def asset_content(asset_id: str) -> None:
    """자산의 글자 내용(문서 본문 · 받아쓰기) — **준비 중(501)**. 상세 화면의 원문 영역용."""
    _not_ready()


@router.get("/assets/{asset_id}/bundle", responses=_NOT_READY)
def bundle(asset_id: str) -> None:
    """기준 자산과 직접 연결된 자산들을 한 zip 으로 — **준비 중(501)**."""
    _not_ready()


class SelectionBundleRequest(BaseModel):
    """화면에서 고른 자산 목록 — '전체 선택' 같은 암묵 대상은 받지 않는다."""

    asset_ids: list[str]


@router.post("/assets/bundle", responses=_NOT_READY)
def selection_bundle(payload: SelectionBundleRequest) -> None:
    """고른 자산들을 한 zip 으로(목록 화면의 일괄 내려받기) — **준비 중(501)**."""
    _not_ready()


@router.get("/mm-meta/bundle", responses=_NOT_READY)
def download_entities_bundle(
    entity_type: str | None = Query(None, description="종류(타입) 필터"),
    areas: str | None = Query(None, description="갈래(개체 라벨) — 모두 가진 개체만(AND · 쉼표 구분)"),
    exclude_video: bool = Query(False, description="영상 제외(용량이 크게 준다)"),
) -> None:
    """지금 좁힌 개체들의 구성 자산 전부를 한 zip 으로 — **준비 중(501)**. 좁히기 축은 목록(``/mm-meta``)과 같다."""
    _not_ready()


@router.get("/mm-meta/{entity_type}/{entity_uid}/bundle", responses=_NOT_READY)
def download_card_bundle(entity_type: str, entity_uid: str) -> None:
    """개체 카드의 구성 자산을 한 zip 으로 — **준비 중(501)**."""
    _not_ready()
