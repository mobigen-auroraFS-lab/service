"""표 단위 조회 클래스의 바탕 — 열린 커넥션 하나를 감싸고 SQL 을 한곳에 모은다.

커넥션은 **받아서 쓰기만** 한다(열고 닫고 커밋은 ``service.api.db``). HTTP 는 모른다 —
404·400 으로 바꾸는 것은 라우트의 몫이다.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row


class Repository:
    """열린 커넥션 하나를 감싼 조회 묶음의 바탕 클래스."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """여러 행을 dict 목록으로 읽는다(값은 언제나 ``params`` 로 바인딩한다)."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """한 행을 읽는다 — 없으면 ``None``."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """쓰기 한 문장을 실행하고 영향 행 수를 돌려준다(커밋은 호출부).

        🔴 읽기 메서드와 이름을 가른다 — 조회인 줄 알고 쓰기를 부르는 일이 없게.
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
