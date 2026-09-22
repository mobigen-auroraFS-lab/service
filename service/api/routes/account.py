"""계정 — 회원가입 · 아이디 중복 확인 · 로그인.

운영 모드에서는 dev 발급기(``POST /auth/token``)가 404 라 **토큰을 받을 길이 이 창구뿐**이다.

결정(2026-08-18 회의 · 코어 초안 스키마)
  · 약관 동의·이메일은 받지 않는다. 가입 즉시 ``active`` — 승인 절차를 두지 않는다.
  · 아이디는 **대소문자를 가리지 않는다**(`lower(login_id)` 유일) — 대소문자만 다른 아이디는 같은 것이다.
  · 토큰에는 역할·등급을 담지 않는다. 클레임은 주체·발급·만료뿐이고 권한은 요청마다 읽는다.
  · 비밀번호는 argon2id 해시만 저장한다(``service.portal.auth.passwords``).

🔴 로그인 실패 문구는 **한 가지**다 — "아이디가 없다"와 "비밀번호가 틀렸다"를 가르면 어떤 아이디가
   존재하는지 알려 주는 셈이다.

TODO: 즐겨찾기(`GET /favorites` · `PUT|DELETE /favorites/{asset_id}`)는 **만들지 않는다**(2026-09-21
      결정). 나중에 열 때를 위해 계약만 아래 주석으로 남겨 둔다 — 표가 필요하다(`TODO.md`).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from service.portal.auth.deps import SUSPENDED_DETAIL
from service.portal.auth.dev_issuer import token_response
from service.portal.auth.passwords import hash_password, needs_rehash, verify_password
from service.portal.common.db_manager import DbManager
from src.database.ids import uuid7

_LOG = logging.getLogger(__name__)

# 계정 창구는 **로그인 전에** 부른다 — 인증을 걸지 않는다.
public_router = APIRouter(tags=["account"])

_LOGIN_FAIL = "아이디 또는 비밀번호가 올바르지 않습니다"


class SignupRequest(BaseModel):
    """회원가입 — 약관·이메일은 받지 않는다."""

    login_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    """로그인 — 성공하면 dev 발급기와 **같은 모양**의 토큰을 준다."""

    login_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1)


@public_router.post("/auth/signup", status_code=201)
def signup(payload: SignupRequest) -> dict[str, Any]:
    """계정을 만든다(가입 즉시 ``active`` · 토큰은 함께 주지 않는다).

    🔴 중복은 **DB 의 유일 인덱스**로 판정한다 — 미리 세어 보고 넣으면 두 요청이 동시에 통과한다.

    Raises:
        HTTPException: 아이디 중복 409 · 빈 값·길이 초과 422.
    """
    login_id = payload.login_id.strip()
    if not login_id or not payload.password.strip():
        raise HTTPException(status_code=422, detail="login_id·password 는 공백일 수 없습니다")
    display_name = (payload.display_name or "").strip() or None
    user_id = str(uuid7())
    encoded = hash_password(payload.password)

    def _create(repo: Any) -> None:
        repo.account.create(user_id=user_id, login_id=login_id,
                            display_name=display_name, password_hash=encoded)

    try:
        DbManager.write(_create)
    except pg_errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"이미 쓰는 아이디입니다: {login_id!r}") from exc
    return {"user_id": user_id, "login_id": login_id, "status": "active"}


@public_router.get("/auth/login-id/availability")
def login_id_availability(
    login_id: str = Query(..., min_length=1, max_length=50, description="쓸 수 있는지 볼 아이디"),
) -> dict[str, Any]:
    """아이디를 쓸 수 있는지 미리 본다.

    ⚠️ **확정이 아니다** — 보는 사이에 남이 먼저 가입할 수 있다. 최종 판정은 가입 시 409 다.
    """
    taken = DbManager.read(lambda repo: repo.account.exists(login_id.strip()))
    return {"login_id": login_id.strip(), "available": not taken}


@public_router.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    """아이디·비밀번호로 토큰을 받는다.

    성공하면 마지막 로그인 시각을 갱신하고, 해시 설정이 세졌으면 **조용히 다시 저장한다**
    (사용자는 모르는 채로 강해진다).

    Raises:
        HTTPException: 아이디·비밀번호 불일치 401 · 정지된 계정 401.
    """
    login_id = payload.login_id.strip()
    row = DbManager.read(lambda repo: repo.account.find_by_login_id(login_id))
    # 🔴 아이디가 없어도 **해시 대조를 건너뛰지 않는다** — 응답 시간 차이로 존재 여부가 드러난다.
    encoded = str(row["password_hash"]) if row else hash_password("not-a-real-password")
    ok = verify_password(payload.password, encoded)
    if not row or not ok:
        raise HTTPException(status_code=401, detail=_LOGIN_FAIL)
    if str(row["status"]) != "active":
        raise HTTPException(status_code=401, detail=SUSPENDED_DETAIL)

    user_id = str(row["user_id"])
    rehash = hash_password(payload.password) if needs_rehash(encoded) else None

    def _after_login(repo: Any) -> None:
        repo.account.touch_login(user_id)
        if rehash:
            repo.account.update_password_hash(user_id=user_id, password_hash=rehash)

    try:
        DbManager.write(_after_login)
    except Exception:  # noqa: BLE001 — 기록 실패가 로그인을 막지 않는다(최선 노력)
        _LOG.warning("로그인 뒤 기록 실패(무시): user_id=%s", user_id)
    return token_response(user_id=user_id)


# ── 즐겨찾기 — 만들지 않는다(2026-09-21 결정) ─────────────────────────────────────
# 나중에 열 때 쓸 계약만 남긴다. 표(`favorite(user_id, asset_id, created_at)`)가 먼저 있어야 한다.
#
# @router.get("/favorites")        → {rows: [{asset_id, file_name, modality, created_at}], total}
# @router.put("/favorites/{asset_id}")    → {asset_id, favorited: true}   · 멱등 · 없는 자산 404
# @router.delete("/favorites/{asset_id}") → {asset_id, favorited: false}  · 멱등
#
# 라우터에는 인증이 필요하다(주체별 목록이므로):
#     router = APIRouter(tags=["account"], dependencies=[Depends(require_principal)])
