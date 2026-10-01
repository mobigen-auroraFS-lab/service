"""포탈 인증 설정 — 환경변수 단일 출처.

환경변수
    PORTAL_AUTH_DISABLED   — 1 이면 dev bypass(anonymous 허용)
    PORTAL_AUTH_BACKEND    — 현재 ``local_hs256`` 만
    PORTAL_JWT_SECRET      — HS256 서명 키(dev bypass 시 기본값 허용·운영 전 교체)
    PORTAL_JWT_TTL_SECONDS — 발급 토큰 수명(기본 28800 = 8시간 · 로그인·dev 발급기 공용)
    PORTAL_JWT_ISSUER      — 설정 시 발급·검증에 ``iss`` 핀(미설정이면 단일 secret MVP — iss 미검사).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 32바이트 이상 — HS256 권장 길이에 못 미치면 PyJWT 가 토큰을 만들 때마다 경고를 낸다(개발 전용 기본값 · 운영은 반드시 교체).
_DEFAULT_DEV_SECRET = "dev-portal-jwt-change-in-prod-0000000"

# 토큰 기본 수명(초) — 8시간. 갱신 창구가 없어 이 값이 곧 로그인 주기다.
_TTL_DEFAULT = 28800
_VALID_BACKENDS = frozenset({"local_hs256"})


@dataclass(frozen=True)
class PortalAuthConfig:
    """포탈 API 인증 설정 스냅샷."""

    auth_disabled: bool
    backend: str
    jwt_secret: str
    jwt_ttl_seconds: int
    jwt_issuer: str | None = None  # 설정 시 iss 발급·검증 핀(cross-service 토큰 재사용 차단).


def _env_bool(name: str, default: str = "0") -> bool:
    """환경변수를 불리언으로 읽는다.

    참으로 보는 값은 ``1``·``true``·``yes``·``on`` 뿐이고, **나머지는 전부 거짓**이다 —
    인증 관련 토글이라 애매한 값은 "끔" 쪽으로 접는 편이 안전하다.

    Args:
        name: 환경변수 이름.
        default: 미설정일 때 쓸 문자열 값.

    Returns:
        불리언 값.
    """
    raw = os.getenv(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def load_portal_auth_config() -> PortalAuthConfig:
    """환경변수에서 ``PortalAuthConfig`` 를 읽는다."""
    backend = os.getenv("PORTAL_AUTH_BACKEND", "local_hs256").strip().lower()
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"지원하지 않는 PORTAL_AUTH_BACKEND={backend!r} "
            f"(지원: {sorted(_VALID_BACKENDS)})"
        )
    auth_disabled = _env_bool("PORTAL_AUTH_DISABLED")
    raw_secret = os.getenv("PORTAL_JWT_SECRET", "").strip()
    if auth_disabled:
        # dev bypass — secret 미설정 시 고정 기본값(로컬 스모크·단위 테스트).
        secret = raw_secret or _DEFAULT_DEV_SECRET
    elif not raw_secret:
        # 운영·인증 활성 — 위조 방지를 위해 기동 시점 fail-fast.
        raise ValueError(
            "PORTAL_AUTH_DISABLED=0 인데 PORTAL_JWT_SECRET 미설정 — "
            "운영·인증 활성 환경에서는 서명 키가 필요합니다"
        )
    else:
        secret = raw_secret
    # 🔴 갱신(refresh) 창구를 두지 않기로 했으므로 **수명이 곧 로그인 주기**다. 1시간이던 종전 기본값은
    #    화면을 붙이면 근무 중에 두세 번 로그인 화면으로 튕기는 뜻이었다. 하루 일과를 한 번의 로그인으로
    #    덮도록 8시간으로 둔다. 강제 로그아웃을 두지 않는 대신 계정 상태는 **요청마다** 계정 표에서
    #    읽는다(``deps.with_account``) — 정지하면 이미 나간 토큰도 곧바로 막힌다.
    raw_ttl = os.getenv("PORTAL_JWT_TTL_SECONDS", str(_TTL_DEFAULT)).strip()
    try:
        ttl = max(60, int(raw_ttl))
    except ValueError:
        ttl = _TTL_DEFAULT
    issuer = os.getenv("PORTAL_JWT_ISSUER", "").strip() or None
    return PortalAuthConfig(
        auth_disabled=auth_disabled,
        backend=backend,
        jwt_secret=secret,
        jwt_ttl_seconds=ttl,
        jwt_issuer=issuer,
    )
