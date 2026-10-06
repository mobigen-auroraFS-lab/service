"""요청마다 도는 인증 의존성 — 토큰 파싱·검증 후 요청 주체를 만든다.

**흐름에서의 위치**: 라우트가 실행되기 **전에** 돈다. 여기서 만든 요청 주체의 권한 등급으로
라우트가 응답 필드를 가린다. 등급을 실제로 적용하는 일은 이 모듈 밖(투영 계층)에서 한다.

표준 Bearer 방식이라 API 문서 화면의 Authorize 버튼과 그대로 맞물린다.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from service.portal.auth.config import load_portal_auth_config
from service.portal.auth.principal import ANONYMOUS, Principal, claims_to_principal
from service.portal.auth.verifier import get_token_verifier

# 정지된 계정 문구 — 로그인(``POST /auth/login``)과 요청마다의 확인이 **같은 말**을 한다.
SUSPENDED_DETAIL = "정지된 계정입니다 — 관리자에게 문의하십시오"

# auto_error=False — PORTAL_AUTH_DISABLED=1 일 때 토큰 없이 anonymous 허용(401 아님).
portal_bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "JWT access token. ``POST /auth/token`` 으로 발급. "
        "값에는 **토큰 문자열만** 입력(Bearer 접두사 불필요)."
    ),
)


def authenticate_token(token: str) -> Principal:
    """토큰을 검증해 요청 주체로 바꾼다.

    Args:
        token: Authorization 헤더에서 뽑은 토큰 문자열.

    Returns:
        검증된 요청 주체.

    Raises:
        HTTPException: 검증 실패·필수 클레임 누락 시 401. **어느 항목이 틀렸는지 알려주지
            않는다** — 세부 사유를 노출하면 토큰을 맞춰 보는 데 단서가 된다.
    """
    try:
        claims = get_token_verifier().verify(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰") from exc
    try:
        return claims_to_principal(claims)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def get_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(portal_bearer_scheme)
    ] = None,
) -> Principal:
    """요청 주체를 만든다 — 라우트가 이 값의 권한 등급으로 응답 필드를 가린다.

    Args:
        credentials: Bearer 자격 증명. 인증을 끈 환경에서는 없을 수 있다.

    Returns:
        요청 주체. **인증을 끈 환경에서 토큰이 없으면 익명 주체**(공개 등급)를 돌려주고,
        토큰이 있으면 켜짐/꺼짐과 무관하게 검증한다.

    Raises:
        HTTPException: 인증이 켜져 있는데 토큰이 없거나 검증에 실패하면 401.
    """
    cfg = load_portal_auth_config()
    token = credentials.credentials if credentials else None
    if cfg.auth_disabled:
        if token:
            return with_account(authenticate_token(token), auth_disabled=True)
        return ANONYMOUS
    if not token:
        raise HTTPException(status_code=401, detail="인증 필요")
    return with_account(authenticate_token(token), auth_disabled=False)


def with_account(principal: Principal, *, auth_disabled: bool) -> Principal:
    """토큰 주체가 **지금도** 쓸 수 있는 계정인지 계정 표에서 확인하고, 계정 정보를 채운다.

    갱신 창구·강제 로그아웃을 두지 않는 대신 상태를 **요청마다** 읽는다 — 토큰에 담으면 계정을
    정지해도 이미 나간 토큰은 만료(8시간)까지 그대로 통한다. 캐시하지 않는 것도 같은 이유다.
    비용은 요청당 PK 조회 한 번이다.

    Args:
        principal: 서명·만료 검증을 통과한 주체.
        auth_disabled: 개발 모드인지. 개발용 발급기(``/auth/token``)는 계정 표에 없는 임의 주체로
            토큰을 만들므로, **개발 모드에서만** 표에 없는 주체를 그대로 통과시킨다.

    Returns:
        계정 정보(login_id·display_name·role)를 채운 주체. 표에 없는 개발용 주체는 그대로.

    Raises:
        HTTPException: 401 — 정지된 계정, 또는 운영 모드에서 계정 표에 없는 주체(지워진 계정).
            후자는 "유효하지 않은 토큰"과 같은 문구다 — 계정이 있었는지를 알려 줄 까닭이 없다.
    """
    # 늦은 import — 인증 모듈은 DB 계층보다 먼저 읽히는 자리라 위에서 들이면 순환이 생긴다.
    from service.api.params import is_uuid
    from service.portal.common.db_manager import DbManager

    # 계정 표의 키는 UUID 다. UUID 가 아닌 주체(개발용 `dev-user` 등)는 묻지 않는다 — 물으면
    # DB 가 형식 오류를 낸다.
    row = (DbManager.read(lambda repo: repo.account.find_by_user_id(principal.user_id))
           if is_uuid(principal.user_id) else None)
    if row is None:
        if auth_disabled:
            return principal
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")
    if str(row["status"]) != "active":
        raise HTTPException(status_code=401, detail=SUSPENDED_DETAIL)
    return replace(principal, login_id=row["login_id"], display_name=row["display_name"],
                   role=row["role"])


def require_principal(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """보호 라우트에 공통으로 다는 의존성 — 검색·상세·다운로드·묶음이 함께 쓴다.

    ``get_principal`` 결과를 그대로 넘긴다. 별도 이름을 둔 이유는 "이 라우트는 인증이 필요하다"를
    선언으로 드러내기 위해서다.
    """
    return principal


def get_download_principal(
    request: Request,
    asset_id: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(portal_bearer_scheme)] = None,
) -> Principal:
    """내려받기 전용 의존성 — ``Authorization`` 헤더 **또는** 다운로드 링크(``?link=``)로 인증한다.

    헤더가 있으면 평소와 같다(링크는 보지 않는다). 헤더가 없고 링크가 있으면 링크를 검증해 그 사용자로 취급한다 —
    이 자산의 내려받기에만 통하며, 계정 상태는 여기서도 다시 확인한다(정지된 계정은 막힌다).
    둘 다 없으면 평소처럼 401(인증을 끈 환경이면 익명).
    """
    link = request.query_params.get("link")
    if credentials is None and link:
        from service.portal.auth.download_link import verify_link_token

        user_id = verify_link_token(link, asset_id)
        principal = claims_to_principal({"sub": user_id})
        return with_account(principal, auth_disabled=load_portal_auth_config().auth_disabled)
    return get_principal(credentials)
