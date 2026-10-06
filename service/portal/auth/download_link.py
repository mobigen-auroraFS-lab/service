"""다운로드 링크 — 헤더 없이 받을 수 있는 짧은 수명의 주소를 만들고 검증한다(``<a href>`` · ``<video src>`` 용).

토큰은 접속 토큰과 다른 키(비밀키에서 파생)로 서명하므로 링크로 다른 창구를 열 수 없고, 한 자산의 내려받기(``scope=dl``)에만 통한다. 수명 기본 300초(30~3600).
받는 시점에 계정 상태를 다시 확인한다. 서명 · 만료 · 자산 불일치는 같은 401 문장이다. 링크는 URL 에 들어가므로 프록시 로그에 남을 수 있어 수명이 짧다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException

from service.portal.auth.config import load_portal_auth_config

LINK_TTL_ENV = "PORTAL_DOWNLOAD_LINK_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 300
SCOPE = "dl"
_INVALID = "유효하지 않거나 만료된 링크입니다 — 새 링크를 받으십시오"


def ttl_seconds() -> int:
    raw = os.getenv(LINK_TTL_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        value = DEFAULT_TTL_SECONDS
    return max(30, min(3600, value))


def _key() -> str:
    """링크 서명 키 — 접속 토큰 키에서 **파생**한다(같은 키를 쓰면 링크가 접속 토큰으로도 통한다)."""
    secret = load_portal_auth_config().jwt_secret
    return hmac.new(secret.encode("utf-8"), b"portal-download-link/v1", hashlib.sha256).hexdigest()


def issue_link_token(*, user_id: str, asset_id: str) -> tuple[str, int]:
    """(링크 토큰, 남은 초). 토큰에는 주체 · 자산 · 범위 · 만료만 든다."""
    ttl = ttl_seconds()
    now = datetime.now(UTC)
    token = jwt.encode({"sub": user_id, "aid": asset_id, "scope": SCOPE, "iat": now, "exp": now + timedelta(seconds=ttl)}, _key(), algorithm="HS256")
    return token, ttl


def verify_link_token(token: str, asset_id: str) -> str:
    """링크가 이 자산에 대해 유효하면 주체(user_id)를 돌려준다. 아니면 401(이유는 알리지 않는다)."""
    try:
        claims = jwt.decode(token, _key(), algorithms=["HS256"], options={"require": ["exp", "sub", "aid", "scope"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=_INVALID) from exc
    if claims.get("scope") != SCOPE or claims.get("aid") != asset_id or not claims.get("sub"):
        raise HTTPException(status_code=401, detail=_INVALID)
    return str(claims["sub"])


def peek_user(token: str) -> str | None:
    """접근 기록용 — 서명이 맞으면 주체를 돌려준다(자산 일치는 보지 않는다 · 실패는 ``None``)."""
    try:
        claims = jwt.decode(token, _key(), algorithms=["HS256"], options={"require": ["exp", "sub"]})
        return str(claims["sub"])
    except Exception:
        return None
