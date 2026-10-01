"""DB 장애 차단기(단일 책임: DB 가 죽어 있을 때 요청이 매달리지 않고 곧바로 503 으로 끝나게 한다).

**왜 필요한가** — DB 에 닿지 않으면 코어 풀이 커넥션을 기다리다(30초) 포기하고, 코어가 그것을 3번 다시 시도한다 → 요청 하나가 최대 90초 매달린다
(2026-10-02 DB 접속 거부 때 실제로 겪었다). 동기 라우트는 스레드풀(DB 풀 크기에 맞춘 10개)에서 도는데 그 10개가 전부 묶이면 ``/health`` 까지 답하지 못한다.
거기에 같은 연결 실패 경고와 긴 트레이스백이 요청마다 쌓여 진짜 원인이 묻힌다.

**하는 일**
  1. 풀 대기 시간을 줄인다(``PORTAL_DB_WAIT_SECONDS`` · 기본 5초 — 코어 기본 30초).
  2. 연결 실패가 나면 **정말 죽었는지** 짧게(3초) 접속해 본다. 죽었으면 차단기를 연다 — 그 뒤 요청은 DB 를 기다리지 않고 곧바로 ``DatabaseUnavailable``(→ 503 + ``Retry-After``).
     접속되면(풀이 꽉 찬 것일 뿐) 차단기는 열지 않고 그 요청만 503 으로 끝낸다(과부하 차단).
  3. 차단 중에는 뒤에서 2초마다 접속을 시도해 살아나면 차단기를 닫는다. 어떤 이유로 시도가 안 돼도 30초 뒤에는 닫는다(영구히 막히지 않게).
  4. 상태가 바뀔 때만 한 줄 남긴다(``DB 연결 불가 — 차단`` · ``DB 연결 복구``).

연결 실패와 다른 DB 오류(검증 · 제약 · 질의 취소)는 구분한다 — 후자는 그대로 올라가 종전처럼 처리된다.
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
MAX_OPEN_SECONDS = 30.0        # 접속 시도가 안 되는 경우의 안전장치 — 이만큼 지나면 차단기를 닫고 실제로 다시 해 본다

# 연결 자체가 안 된 것을 뜻하는 SQLSTATE — 08(연결 예외) · 57P01~03(서버 종료 중).
_CONNECTION_SQLSTATES = ("08",)
_SHUTDOWN_SQLSTATES = ("57P01", "57P02", "57P03")


class DatabaseUnavailable(RuntimeError):
    """DB 에 닿지 못했다 — 화면에는 503(잠시 뒤 다시 시도)으로 나간다."""


def wait_seconds() -> float:
    """풀에서 커넥션을 기다릴 시간 — ``PORTAL_DB_WAIT_SECONDS``(0 이하 · 형식 오류면 기본)."""
    raw = os.getenv(WAIT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_WAIT_SECONDS
    except ValueError:
        _LOG.warning("%s=%r 는 숫자가 아니다 — 기본 %.0f초를 쓴다", WAIT_ENV, raw, DEFAULT_WAIT_SECONDS)
        return DEFAULT_WAIT_SECONDS
    return value if value > 0 else DEFAULT_WAIT_SECONDS


def is_connection_failure(exc: BaseException | None) -> bool:
    """예외(또는 그 원인 사슬)가 「DB 에 연결하지 못함」인가. 질의 취소 · 제약 위반 같은 오류는 아니다."""
    seen = 0
    while exc is not None and seen < 8:
        name = type(exc).__name__
        if name in ("PoolTimeout", "PoolClosed", "TooManyRequests"):
            return True
        try:
            import psycopg
        except ImportError:  # 미설치 환경 방어
            return False
        if isinstance(exc, psycopg.OperationalError):
            state = getattr(exc, "sqlstate", None)
            if state is None or state.startswith(_CONNECTION_SQLSTATES) or state in _SHUTDOWN_SQLSTATES:
                return True            # sqlstate 가 없는 OperationalError 는 서버에 닿기도 전의 실패(연결 거부 · 시간 초과)다
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
        if not is_connection_failure(exc):
            raise
        BREAKER.note_failure(exc)
        raise DatabaseUnavailable(f"DB 연결 실패: {type(exc).__name__}") from exc
