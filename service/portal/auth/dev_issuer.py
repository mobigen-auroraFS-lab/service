"""토큰 발급 — 로그인(``POST /auth/login``)과 dev 발급기(``POST /auth/token``)가 함께 쓴다.

서명 방식·유효 기간·발급자 핀은 **한 곳**에서 정한다. 두 벌로 두면 한쪽만 고쳐져 검증을 통과하지
못하는 토큰이 생긴다. 다른 점은 **주체를 어떻게 정하느냐**뿐이다 — 로그인은 계정 표에서 확인한
``user_id``, dev 발급기는 요청이 적어 보낸 문자열.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from service.portal.auth.config import load_portal_auth_config


def issue_access_token(*, user_id: str) -> str:
    """토큰을 발급한다 — 검증 쪽과 비밀값·알고리즘을 맞춰 만든다.

    Args:
        user_id: 토큰 주체. 검증 뒤 이 값이 요청자 식별자가 된다.

    Returns:
        서명된 토큰 문자열. 유효 기간과 발급자 핀은 설정에서 읽는다.
    """
    cfg = load_portal_auth_config()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,  # ``claims_to_principal`` 이 user_id 로 읽음
        "iat": now,
        "exp": now + timedelta(seconds=cfg.jwt_ttl_seconds),
        # 토큰에 역할·등급을 담지 않는다 — 검증을 통과하면 코드가 기본 등급을 부여한다.
    }
    if cfg.jwt_issuer:
        # issuer 핀이 켜져 있으면 발급 토큰도 iss 를 박아 자체 검증을 통과시킨다.
        payload["iss"] = cfg.jwt_issuer
    return jwt.encode(payload, cfg.jwt_secret, algorithm="HS256")


def issue_dev_token(*, user_id: str) -> str:
    """dev 발급기(``POST /auth/token``)용 — 비밀번호를 보지 않고 주체를 그대로 믿는다."""
    return issue_access_token(user_id=user_id)
