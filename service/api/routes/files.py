"""파일 제공 창구 중 **아직 자리만 있는 것**(501) — 썸네일 하나.

🔴 [2026-10-01] 원본 다운로드 · 원문(``routes/assets.py``)과 묶음 zip 네 가지(고른 자산 · 관계 · 개체 목록 · 개체 카드 — ``routes/assets.py`` · ``routes/mm_meta.py``)는
**구현했다** — 원본은 DB 에 적힌 경로에 있다고 보고 읽고, 묶음은 임시 파일 없이 만들면서 흘려 보낸다(``service/portal/asset/bundle_stream.py``).
남은 썸네일은 이미지 처리 라이브러리(``opencv``)를 되살려야 해서 자리만 둔다(`TODO.md` [협의] 파일 제공).

경로와 파라미터는 남겨 둔다 — 화면이 이 이름으로 코딩할 수 있고,
API 문서(``/docs``)에 계약이 보인다. 부르면 **501** 로 분명히 막는다(404 로 두면 "경로를 틀렸다"로 읽힌다).

  · 인증은 다른 창구와 같다 — 라우터에 걸려 있어 운영에서 토큰이 없으면 501 보다 401 이 먼저다.
  · 파라미터 형식 검증(FastAPI)도 먼저다 — 요청 본문이 틀리면 422.
  · 구현은 지웠다(2026-09-28) — 지우기 직전 구현은 git ``9d11d29``. 다시 만들 때는
    ``service/portal/asset/__init__.py`` 의 「원본 파일 전제」를 따른다.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query

from service.portal.auth import require_principal

router = APIRouter(tags=["files"], dependencies=[Depends(require_principal)])

NOT_READY_DETAIL = "파일 제공은 아직 없습니다 — 제공 방식을 정한 뒤 엽니다"
_NOT_READY = {501: {"description": "아직 제공하지 않음 — 파일 제공 방식 협의 대기(자리만 있다)"}}


def _not_ready() -> NoReturn:
    raise HTTPException(status_code=501, detail=NOT_READY_DETAIL)


@router.get("/assets/{asset_id}/thumbnail", responses=_NOT_READY)
def asset_thumbnail(
    asset_id: str,
    size: str = Query("card",
                      description="크기 프리셋: card(320·목록/hover 기본) | detail(640·상세 히어로) · 대소문자 무관"),
) -> None:
    """이미지 · 영상의 축소 미리보기 — **준비 중(501)**."""
    _not_ready()
