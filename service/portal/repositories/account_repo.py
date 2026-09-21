"""계정 표(``portal_user``) 조회·쓰기 — 로그인·가입이 쓰는 자리.

🔴 아이디는 **대소문자를 가리지 않는다**(`lower(login_id)` 유일 인덱스) — 조회도 같은 규칙으로 한다.
   조회만 대소문자를 가리면 "가입은 막히는데 로그인은 안 되는" 아이디가 생긴다.
"""

from __future__ import annotations

from typing import Any

from service.portal.common.repository import Repository

_FIND_SQL = """
SELECT user_id, login_id, display_name, password_hash, status, role
FROM portal_user
WHERE lower(login_id) = lower(%s)
LIMIT 1
"""

_EXISTS_SQL = "SELECT 1 FROM portal_user WHERE lower(login_id) = lower(%s) LIMIT 1"

_INSERT_SQL = """
INSERT INTO portal_user (user_id, login_id, display_name, password_hash)
VALUES (%s, %s, %s, %s)
"""

_TOUCH_LOGIN_SQL = "UPDATE portal_user SET last_login_at = now() WHERE user_id = %s"

_REHASH_SQL = "UPDATE portal_user SET password_hash = %s, updated_at = now() WHERE user_id = %s"


class AccountRepository(Repository):
    """계정 표 접근."""

    def find_by_login_id(self, login_id: str) -> dict[str, Any] | None:
        return self.one(_FIND_SQL, (login_id,))

    def exists(self, login_id: str) -> bool:
        return self.one(_EXISTS_SQL, (login_id,)) is not None

    def create(self, *, user_id: str, login_id: str, display_name: str | None,
               password_hash: str) -> None:
        """계정 한 줄을 넣는다 — 아이디 중복이면 유일 인덱스가 거절한다(호출부가 409)."""
        self.execute(_INSERT_SQL, (user_id, login_id, display_name, password_hash))

    def touch_login(self, user_id: str) -> None:
        self.execute(_TOUCH_LOGIN_SQL, (user_id,))

    def update_password_hash(self, *, user_id: str, password_hash: str) -> None:
        """해시 설정이 세진 뒤 로그인 성공 시 다시 저장한다(사용자는 모르는 채로 강해진다)."""
        self.execute(_REHASH_SQL, (password_hash, user_id))
