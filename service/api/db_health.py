"""DB 장애 차단기 — DB 가 죽어 있을 때 요청이 풀 대기(30초 × 코어 재시도 3번)에 매달리지 않고 곧바로 503 으로 끝나게 한다.

풀 대기를 줄이고(``PORTAL_DB_WAIT_SECONDS`` · 기본 5초), 연결 실패 시 3초 접속 시험으로 죽었는지 가려 차단기를 연다. 접속이 되면(풀이 꽉 찬 것) 그 요청만 503 이다.
질의 시간 제한(``PORTAL_DB_STATEMENT_TIMEOUT_MS``)을 넘긴 취소는 ``DatabaseSlow`` 로 올리고 차단기는 열지 않는다. 연결 실패가 아닌 DB 오류는 그대로 올라간다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from service.api.breaker import Breaker

_LOG = logging.getLogger("meta_extract.portal_api")

WAIT_ENV = "PORTAL_DB_WAIT_SECONDS"
DEFAULT_WAIT_SECONDS = 5.0
RETRY_AFTER_SECONDS = 5
PROBE_INTERVAL = 2.0
PROBE_CONNECT_TIMEOUT = 3
MAX_OPEN_SECONDS = 30.0

# 연결 자체가 안 된 것을 뜻하는 SQLSTATE — 08(연결 예외) · 57P01~03(서버 종료 중).
_CONNECTION_SQLSTATES = ("08",)
_SHUTDOWN_SQLSTATES = ("57P01", "57P02", "57P03")


class DatabaseUnavailable(RuntimeError):
    """DB 에 닿지 못했다 — 화면에는 503(잠시 뒤 다시 시도)으로 나간다."""


class DatabaseSlow(DatabaseUnavailable):
    """DB 가 질의 시간 제한(``PORTAL_DB_STATEMENT_TIMEOUT_MS``) 안에 답하지 못했다 — 접속은 되므로 차단기는 열지 않는다."""


def wait_seconds() -> float:
    """풀에서 커넥션을 기다릴 시간 — ``PORTAL_DB_WAIT_SECONDS``(0 이하 · 형식 오류면 기본)."""
    raw = os.getenv(WAIT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_WAIT_SECONDS
    except ValueError:
        _LOG.warning("%s=%r 는 숫자가 아니다 — 기본 %.0f초를 쓴다", WAIT_ENV, raw, DEFAULT_WAIT_SECONDS)
        return DEFAULT_WAIT_SECONDS
    return value if value > 0 else DEFAULT_WAIT_SECONDS


def _is_statement_timeout(exc: BaseException) -> bool:
    """질의 시간 제한으로 DB 가 질의를 취소한 것인가(SQLSTATE 57014 — 잠금 대기 취소 · 사용자 취소와 같은 코드라 메시지로 가른다)."""
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(exc, psycopg.errors.QueryCanceled) and "statement timeout" in str(exc).lower()


def is_connection_failure(exc: BaseException | None) -> bool:
    """예외(또는 그 원인 사슬)가 「DB 에 연결하지 못함」인가. 질의 취소 · 제약 위반 같은 오류는 아니다."""
    seen = 0
    while exc is not None and seen < 8:
        name = type(exc).__name__
        if name in ("PoolTimeout", "PoolClosed", "TooManyRequests"):
            return True
        try:
            import psycopg
        except ImportError:
            return False
        if isinstance(exc, psycopg.OperationalError):
            state = getattr(exc, "sqlstate", None)
            if state is None or state.startswith(_CONNECTION_SQLSTATES) or state in _SHUTDOWN_SQLSTATES:
                return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _probe() -> bool:
    """새 접속을 짧게 시도해 본다 — 풀과 별개로, 3초 안에 되면 DB 는 살아 있다."""
    try:
        import psycopg

        from service.api import db

        conninfo = db.get_db()._build_conninfo()  # noqa: SLF001 — 코어의 접속 문자열을 그대로 쓴다(복제하지 않는다)
        with psycopg.connect(conninfo, connect_timeout=PROBE_CONNECT_TIMEOUT):
            return True
    except Exception:  # noqa: BLE001 — 접속 시도의 모든 실패는 「아직 안 된다」다
        return False


BREAKER = Breaker("DB", lambda: _probe(), interval=PROBE_INTERVAL, max_open=MAX_OPEN_SECONDS)     # 시험이 _probe 를 바꿔 끼울 수 있게 늦게 부른다
is_down = BREAKER.is_down
reset = BREAKER.reset


def guard[T](work: Callable[[], T]) -> T:
    """``work()`` 를 실행한다. 차단 중이면 DB 를 부르지 않고, 연결 실패면 ``DatabaseUnavailable`` 로 바꿔 올린다."""
    if BREAKER.is_down():
        raise DatabaseUnavailable("DB 연결 불가(차단 중)")
    try:
        return work()
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        if _is_statement_timeout(exc):
            raise DatabaseSlow("DB 질의 시간 초과") from exc
        if not is_connection_failure(exc):
            raise
        BREAKER.note_failure(exc)
        raise DatabaseUnavailable(f"DB 연결 실패: {type(exc).__name__}") from exc
