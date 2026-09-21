"""DB 모의기 — psycopg 커넥션/커서를 흉내 낸다(단일 책임: 조회 결과 공급).

응답 형식을 검증하려면 라우트만 봐서는 안 된다 — 응답의 모양은 portal·core 조회 함수가 만든다.
그래서 결과를 가짜로 끼워 넣는 대신 **커넥션 자체를 가짜로** 만들어 진짜 조립 코드를 태운다.

기본은 **빈 결과**다. 목록·집계 엔드포인트는 그것만으로 응답 봉투(키 집합)를 완전하게 만들어 내므로
행 데이터를 지어내지 않고도 형식을 대조할 수 있다. 행이 있어야 하는 경로만 픽스처를 준다.

⚠️ SQL 을 **실행하지 않는다** — SQL 의 정확성은 검증 대상이 아니다. 검증 대상은 "조회 결과가
주어졌을 때 만들어지는 응답의 모양"이다.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

Fixture = tuple[re.Pattern[str], list[dict[str, Any]]]


def fixture(pattern: str, rows: list[dict[str, Any]]) -> Fixture:
    """SQL 정규식 ↔ 결과 행을 묶는다.

    ⚠️ 정규식은 **좁게** 건다. 느슨하면 다른 쿼리에 물려 엉뚱한 칸 수로 언패킹 오류가 난다
    (실제로 8칸 픽스처가 9칸 SELECT 에 걸려 터진 적이 있다 — 그건 제품 결함이 아니라 픽스처 결함이다).
    """
    return (re.compile(pattern, re.I | re.S), rows)


class FakeCursor:
    """psycopg 커서 표면 중 조회 계층이 실제로 쓰는 것만 흉내 낸다."""

    def __init__(self, fixtures: list[Fixture], dict_mode: bool, log: list[str]) -> None:
        self._fx, self._dict, self._log = fixtures, dict_mode, log
        self._rows: list[dict[str, Any]] = []
        self._agg_cols = 0
        self._is_count = False
        self._count_keys: list[str] = ["count"]
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(str(sql).split())
        self._log.append(text[:200])
        # 집계 쿼리는 행이 없어도 **한 행**을 낸다. 칸 수는 count(...) 등장 횟수로 센다 —
        # FILTER 로 여러 칸을 한 번에 세는 쿼리가 있어서 1칸 고정이면 호출부의
        # zip(strict=True) 가드에 걸린다(그 가드는 정상 동작이다).
        self._agg_cols = len(re.findall(r"\bcount\s*\(", text, re.I))
        self._is_count = self._agg_cols > 0 and bool(re.match(r"SELECT\s+count", text, re.I))
        # dict 모드의 빈 집계 행은 SQL 이 붙인 **별칭**을 키로 쓴다(``COUNT(*) AS n`` → ``{"n": 0}``).
        # 별칭이 없을 때만 psycopg 기본 칸 이름(``count``)으로 둔다.
        self._count_keys = re.findall(r"\bcount\s*\([^)]*\)\s+AS\s+(\w+)", text, re.I) or ["count"]
        self._rows = []
        for pat, rows in self._fx:
            if pat.search(text):
                self._rows = rows
                break
        self.rowcount = len(self._rows)

    def fetchone(self) -> Any:
        if self._rows:
            return self._shape(self._rows[0])
        if self._is_count:
            if self._dict:
                return dict.fromkeys(self._count_keys, 0)
            return tuple([0] * max(1, self._agg_cols))
        return None

    def fetchall(self) -> list[Any]:
        return [self._shape(r) for r in self._rows]

    def _shape(self, row: dict[str, Any]) -> Any:
        return dict(row) if self._dict else tuple(row.values())

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConn:
    """조회 전용 가짜 커넥션. 쓰기도 받아들이지만 아무것도 남기지 않는다."""

    def __init__(self, fixtures: list[Fixture] | None = None) -> None:
        self.fixtures = fixtures or []
        self.sql_log: list[str] = []

    def cursor(self, row_factory: Any = None, **_: Any) -> FakeCursor:
        return FakeCursor(self.fixtures, row_factory is not None, self.sql_log)

    @contextmanager
    def transaction(self):
        """psycopg 중첩 트랜잭션(SAVEPOINT) 자리 — 감사 기록이 이걸 쓴다."""
        yield self

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass
