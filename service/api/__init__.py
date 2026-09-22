"""포탈 API 패키지 — FastAPI 앱 조립과 라우터 등록.

**흐름에서의 위치**: 요청이 가장 먼저 닿는 곳이다. 여기서 앱을 만들고, 수명 주기·접근 기록
미들웨어·예외 처리기를 붙인 뒤 라우터 넷을 등록한다. 실제 처리 로직은 각 라우터 모듈에 있다.

``app`` 객체를 소유하는 곳은 **여기 하나**다 — 진입점도 테스트도 이 모듈에서 가져다 쓴다.

⚠️ **라우터 등록 순서를 바꾸지 말 것.** FastAPI 는 먼저 등록된 경로부터 맞춰 보므로, 순서가
바뀌면 어떤 경로가 다른 경로를 가려 404 가 난다.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from service.api import audit, errors, lifespan
from service.api.routes import (
    account,
    assets,
    catalog,
    file_search,
    mm_meta,
    search,
)

# 🔴 관리자·관계 검토 라우터는 **일부러 들이지 않는다**(2026-09-22 보류 결정) — 아래 등록부 주석 참조.
#    from service.api.routes import admin, review
from service.portal.auth import Principal, require_principal
from service.portal.auth.config import load_portal_auth_config
from service.portal.auth.dev_issuer import token_response
from service.portal.auth.schemas import DevTokenRequest

app = FastAPI(title="일반 도메인 포탈 API (010 P1)", lifespan=lifespan.lifespan)

# 접근 기록 미들웨어 — 실제 로직·상태는 ``audit`` 이 갖고, 여기서는 앱에 붙이기만 한다.
app.middleware("http")(audit.access_log_middleware)

# NUL 바이트 차단 — **나중에 등록한 미들웨어가 바깥**이라, 이 검사가 접근 기록보다 먼저 돈다.
# 못 쓸 요청을 가장 앞에서 끊고(DB·감사 어디에도 닿지 않는다), 통과한 것만 아래로 내려보낸다.
app.middleware("http")(errors.reject_nul_bytes)

# 실패 응답 봉투 통일 — 우리 4xx·자동 검증 422·미처리 500 을 모두 같은 모양으로 내보낸다.
# ⚠️ ``Exception`` 처리기는 응답만 대신 만들고 예외는 Starlette 이 다시 올린다(서버 로그 보존).
app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
app.add_exception_handler(RequestValidationError, errors.validation_exception_handler)
app.add_exception_handler(Exception, errors.unhandled_exception_handler)

# 검색 엔진 연결 실패는 503 으로 — 코드 버그(500)와 구분해야 알람을 나눌 수 있다.
# 라이브러리가 없는 환경에서는 이 핸들러를 등록하지 않는다.
if errors.OSConnectionError is not None:
    app.add_exception_handler(errors.OSConnectionError, errors.os_unavailable_handler)


# ── 메타 라우트(health/auth/me) — 앱 수준·소규모라 여기 직접 등록(원래도 맨 앞) ──────────────
@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """헬스 체크(부트스트랩·라우팅 확인용)."""
    return {"status": "ok", "env": lifespan.ENV}


@app.head("/health", include_in_schema=False)
def health_head() -> Response:
    """헬스 체크의 HEAD — 본문 없이 200.

    다른 GET 창구는 HEAD 를 405 로 거절하는데(선언한 메서드만 받는다), 헬스 체크만 예외다.
    HEAD 로 살아 있는지 묻는 헬스체커·업타임 모니터가 있고, 거기서 405 는 "죽었다"로 읽힌다.
    API 문서에는 싣지 않는다 — 같은 창구의 GET 과 뜻이 같다.
    """
    return Response(status_code=200)


@app.post("/auth/token", tags=["meta"])
def auth_token(body: DevTokenRequest) -> dict[str, str | int]:
    """dev JWT 발급 — ``PORTAL_AUTH_DISABLED=1`` 일 때만. 로컬 스모크·Swagger Authorize 용.

    운영(``PORTAL_AUTH_DISABLED=0``)에서는 404 — IdP 연동 전 dev 엔드포인트 노출 방지.
    """
    if not load_portal_auth_config().auth_disabled:
        raise HTTPException(status_code=404, detail="dev 토큰 발급 비활성")
    user_id = (body.user_id or body.username or "dev-user").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="username 또는 user_id 필요")
    return token_response(user_id=user_id)


@app.get("/me", tags=["meta"])
def me(principal: Annotated[Principal, Depends(require_principal)]) -> dict[str, str | None]:
    """지금 요청한 사람 — 화면 머리글(이름)과 권한 표시에 쓴다.

    계정 정보는 인증 의존성이 계정 표에서 **이번 요청에** 읽은 값이다(토큰에 담긴 값이 아니다).
    개발용 발급기 토큰처럼 계정 표에 없는 주체는 세 값이 ``None`` 이다.
    ``display_name`` 이 ``None`` 이면 화면은 ``login_id`` 를 쓴다.
    """
    return {"user_id": principal.user_id, "clearance": principal.clearance,
            "login_id": principal.login_id, "display_name": principal.display_name,
            "role": principal.role}


# ── 라우터 포함(원래 등록 순서: admin GET → review POST → search → assets) ────────────────
# 경로 공간이 겹치지 않아(/admin/*·/search·/assets/*·/topics*) 라우터 간 순서는 매칭에 무관하나,
# 원래 순서를 보존한다. catch-all 라우트 순서는 각 라우터 파일 내부에서 보장(구체 경로 먼저 선언).
# TODO(배포): 앞단 프록시에서 헤더·본문 읽기 제한 시간과 본문 크기 상한을 건다 — `TODO.md` §3.
# ── 다른 오리진에서 부를 수 있게(CORS) ──────────────────────────────────────────
# 화면(Vite dev 서버 등)이 다른 오리진에서 직접 부르면 브라우저가 막는다. 허용할 오리진을
# ``PORTAL_CORS_ORIGINS`` 에 쉼표로 적는다(예: http://localhost:5173,http://127.0.0.1:5173).
# 🔴 비워 두면 **아무 오리진도 허용하지 않는다**(종전 동작) — 운영에서 실수로 전면 개방되지 않게
#    기본값을 열어 두지 않는다. ``*`` 는 자격 증명과 함께 쓸 수 없어 목록으로만 받는다.
_CORS_ORIGINS = [o.strip() for o in os.getenv("PORTAL_CORS_ORIGINS", "").split(",") if o.strip()]
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition", "Content-Range", "X-Bundle-Files",
                        "X-Bundle-Missing"],
    )

# ── 관리자·관계 검토 창구는 **보류**(2026-09-22) ────────────────────────────────
# 당장 쓸 화면이 없고, `/admin/*` 에는 역할 검사(RBAC)가 아직 없다 — 인증만 통과하면 누구나
# 운영 자료(접근 이력·계보·통계)를 보고 관계를 승인·반려할 수 있다. 쓰지 않을 창구를 열어 둘
# 이유가 없어 **등록만** 끊는다.
#
# 🔴 코드는 지우지 않았다 — `service/api/routes/{admin,review}.py` 와 저장소·조회 계층은 그대로
#    있다. 되살리려면 위 import 주석과 아래 두 줄을 풀면 된다(그 전에 RBAC 를 붙일 것).
#    IDD 에서는 구분이 **'보류(창구 닫음)'** 이고, 실측·교차검증은 라우트가 없고 404 인 것을 확인한다.
#
# app.include_router(admin.router)
# app.include_router(review.router)
app.include_router(search.router)
# 파일 검색(시나리오 ③) — 조건으로 좁혀 훑는 창구. 위 /search 와 다른 화면이라 따로 둔다.
app.include_router(file_search.router)
app.include_router(assets.router)
app.include_router(mm_meta.router)
# 목록 창구(관계 종류·태그) — 화면이 "고를 값"을 받아 가는 자리. 검색 결과 칩과 쓰임이 다르다.
app.include_router(catalog.router)
# 계정(가입·로그인) — 로그인 전에 부르므로 인증을 걸지 않는다.
app.include_router(account.public_router)

__all__ = ["app"]
