"""DB 커넥션 풀과 트랜잭션 실행 통로(단일 책임: 데이터베이스 접근 수단).

**흐름에서의 위치**: 라우터가 DB 를 쓰려면 반드시 여기를 거친다. 조회(``run_in_db``)와
쓰기(``run_in_db_write``)가 **유일한 통로**이고, 테스트는 이 둘만 갈아끼우면 DB 없이 돈다.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from service.portal.common.stage_timer import stage

_LOG = logging.getLogger("meta_extract.portal_api")

# DB 접근 객체는 **앱 수명 동안 하나만** 둔다 — 요청마다 만들면 연결 풀이 매번 새로 생겼다 사라져
# **풀을 생성·즉시 파괴**해 풀링 이득이 0(매 요청 TCP+auth 재수행·고부하 시 커넥션 폭증)이었다.
# 지연 생성(최초 요청)·프로세스 공유·lifespan 종료 시 close. 생성 경합은 락으로 차단.
_SINGLETON: object | None = None
_LOCK = threading.Lock()


# ── 커넥션 풀 크기 ───────────────────────────────────────────────────────────────
# ⚠️ **스레드풀과 짝을 맞춰야 한다.** 이 앱의 라우트는 전부 동기 ``def`` 라 Starlette 이
# AnyIO 스레드풀(기본 40개)에서 돌린다. 반면 코어 ``PostgresConfig`` 의 풀 기본값은 **max 10**
# 이고 환경변수로 열려 있지 않다 — 동시 요청이 10을 넘으면 남은 스레드가 커넥션을 기다리다
# ``PoolTimeout`` 으로 떨어진다. 부하가 걸려야 드러나는 종류의 고장이라 평소엔 보이지 않는다.
# 🔴 [2026-09-28] 그래서 기동할 때 스레드 상한을 풀 상한에 맞춘다(``align_thread_limit_to_pool``).
#
# 기본값을 두지 않는다 — 미설정이면 코어 기본값을 그대로 써서 **종전 동작이 바뀌지 않는다**.
# 값을 정하는 것은 배포 결정이다: **워커 수 × max ≤ PostgreSQL max_connections** 를 지켜야
# 하므로 여기서 임의로 올리면 같은 DB 를 쓰는 다른 서비스의 커넥션을 빼앗는다.
POOL_MIN_ENV = "PORTAL_DB_POOL_MIN"
POOL_MAX_ENV = "PORTAL_DB_POOL_MAX"


def pool_size_override() -> tuple[int | None, int | None]:
    """풀 크기 환경변수를 읽는다 — 미설정·형식 오류면 ``None``(코어 기본값 유지).

    Returns:
        ``(min, max)``. 설정됐고 1 이상일 때만 값이고, 그 밖은 ``None``.

    Note:
        잘못된 값에 기동을 막지 않는다 — 풀 크기는 성능 손잡이지 정확성 조건이 아니다.
        대신 경고를 남겨 오타가 조용히 묻히지 않게 한다.
    """
    sizes: list[int | None] = []
    for name in (POOL_MIN_ENV, POOL_MAX_ENV):
        raw = os.getenv(name, "").strip()
        if not raw:
            sizes.append(None)
            continue
        try:
            value = int(raw)
        except ValueError:
            _LOG.warning("%s 값이 정수가 아님 — 무시하고 코어 기본값 사용: %r", name, raw)
            sizes.append(None)
            continue
        if value < 1:
            _LOG.warning("%s 는 1 이상이어야 함 — 무시: %d", name, value)
            sizes.append(None)
            continue
        sizes.append(value)
    return sizes[0], sizes[1]


def new_db() -> object:
    """DB 접근 객체를 만든다 — 풀 크기 환경변수가 있을 때만 그 항목을 덮어쓴다.

    크기를 주지 않으면 ``PostgresUtil()`` 을 **인자 없이** 만든다. 접속 정보 해석(DSN 우선 →
    개별 환경변수)을 코어에 그대로 맡기기 위해서다 — 같은 규칙을 여기 복제하면 두 벌이 된다.

    크기를 줄 때만 코어와 **같은 우선순위**로 설정을 조립하고 풀 항목만 바꾼다. ``dsn`` 과
    ``config`` 를 함께 넘기면 접속 문자열은 ``dsn`` 이, 풀 크기는 ``config`` 가 정한다
    (``_build_conninfo`` 는 dsn 을 그대로 쓰고 ``open_pool`` 은 config 를 읽는다).

    Returns:
        아직 풀을 열지 않은 접근 객체.
    """
    from src.database.postgres_util import PostgresConfig, PostgresUtil

    min_size, max_size = pool_size_override()
    if min_size is None and max_size is None:
        return PostgresUtil()  # 종전 경로 그대로

    dsn = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_DSN") or "").strip()
    config = PostgresConfig.from_env()
    if min_size is not None:
        config.min_pool_size = min_size
    if max_size is not None:
        config.max_pool_size = max_size
    # min > max 같은 모순은 코어 검증이 기동 시점에 막는다 — 여기서 조용히 보정하지 않는다.
    return PostgresUtil(config=config, dsn=dsn or None)


THREAD_LIMIT_ENV = "PORTAL_THREAD_LIMIT"


def align_thread_limit_to_pool() -> None:
    """동기 핸들러 동시 실행 상한(스레드풀)을 커넥션 풀 상한에 **맞춘다**(기동 시 한 번).

    🔴 [2026-09-28] 종전에는 경고만 했다(스레드 40 > 풀 10). 둘이 어긋나면 동시 요청이 풀을 넘는 순간
    남은 스레드가 커넥션을 기다리다 ``PoolTimeout`` 으로 **실패**한다 — 부하를 걸어야 드러나는 고장이다.
    요청마다 계정 상태를 DB 에서 읽으므로 거의 모든 요청이 커넥션을 쓴다. 스레드 상한을 풀과 같게 두면
    넘치는 요청은 실패하지 않고 **스레드 앞에서 줄을 선다**.

    ``PORTAL_THREAD_LIMIT`` 를 주면 그 값을 쓴다(배포가 정한 값 · 풀보다 크면 종전처럼 경고만).
    어떤 경우에도 기동을 막지 않는다 — 못 읽으면 조용히 넘어간다.
    """
    try:
        from anyio.to_thread import current_default_thread_limiter

        from src.database.postgres_util import PostgresConfig

        limiter = current_default_thread_limiter()
        threads = int(limiter.total_tokens)
        _, max_size = pool_size_override()
        if max_size is None:
            max_size = PostgresConfig().max_pool_size
        raw = os.getenv(THREAD_LIMIT_ENV, "").strip()
        wanted = int(raw) if raw.isdigit() and int(raw) > 0 else None
    except Exception:  # noqa: BLE001 — 관측·조정용이다. 못 읽으면 조용히 넘어간다(기동을 막지 않는다).
        return
    if wanted is not None:
        limiter.total_tokens = wanted
        if wanted > max_size:
            _LOG.warning(
                "%s=%d 이 DB 커넥션 풀 상한(%d)보다 큽니다 — 동시 요청이 %d 를 넘으면 커넥션 대기가 "
                "쌓입니다(워커 수 × 풀 상한 ≤ PostgreSQL max_connections).",
                THREAD_LIMIT_ENV, wanted, max_size, max_size)
        return
    if threads > max_size:
        limiter.total_tokens = max_size
        _LOG.info("스레드 상한 %d → %d (DB 풀 상한에 맞춤 · %s 로 풀을 키우면 함께 오른다)", threads, max_size, POOL_MAX_ENV)

def get_db() -> object:
    """앱 전체가 공유하는 DB 접근 객체를 돌려준다(첫 호출 때 연결 풀이 한 번 열린다)."""
    global _SINGLETON
    # 이중 검사 락(double-checked locking): 락 밖 첫 검사로 이미 생성된 정상 경로의 락 경합을 피하고,
    # 락 안에서 다시 검사해 경쟁 스레드가 풀을 중복 생성(2개)하지 않게 한다.
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                db = new_db()
                db.open_pool()
                _SINGLETON = db
    return _SINGLETON


def close_db() -> None:
    """lifespan 종료 훅 — 싱글턴 풀을 닫고 초기화한다(재기동·테스트 격리용)."""
    global _SINGLETON
    if _SINGLETON is not None:
        try:
            _SINGLETON.close()
        except Exception:  # noqa: BLE001 — 종료 경로 best-effort(닫힘 실패가 셧다운을 막지 않게)
            _LOG.warning("DB 풀 close 실패(무시)", exc_info=True)
        _SINGLETON = None


def run_in_db(callback: Callable[[object], object]) -> object:
    """PostgresUtil 조회 트랜잭션에서 ``callback(conn)`` 을 실행하는 단일 seam.

    상세/다운로드/묶음 핸들러의 DB 접근은 모두 이 함수를 거친다(테스트는 이 함수를 patch 로
    대체해 DB 없이 단위 검증). 요청마다 풀을 만들지 않고 앱 수명 객체를 재사용한다.

    Args:
        callback: 커넥션을 받아 조회를 수행하는 함수. **쓰기를 하면 안 된다** — 이 경로는
            재시도 가능한 것으로 표시돼 있어, 쓰기가 섞이면 중복 반영될 수 있다.

    Returns:
        ``callback`` 의 반환값.
    """
    with stage("db"):       # 요청 로그에 단계별 시간으로 남는다(풀 대기 + 질의)
        return get_db().execute_in_transaction(callback, idempotent=True)


def run_in_db_write(callback: Callable[[object], object]) -> object:
    """조회 seam(``run_in_db``)과 분리한 write 트랜잭션(``idempotent=False``·commit) 공유 seam.

    원본 자산 payload·스키마는 무변경이나, 두 부류의 거버넌스 write 가 이 seam 을 공유한다:
    (1) 미들웨어가 남기는 접근 기록(한 행씩 추가·실패해도 요청을 깨지 않음),
    (2) 관계 검토 결정(``bulk_review``/``revise_edge``/``promote_relation_kind`` — graph_edge
    status·relation_kind status 전이 + relation 감사). 테스트는 이 함수를 patch 한다.

    Args:
        callback: 커넥션을 받아 쓰기를 수행하는 함수.

    Returns:
        ``callback`` 의 반환값. 실패하면 트랜잭션이 통째로 롤백된다.
    """
    with stage("db"):
        return get_db().execute_in_transaction(callback, idempotent=False)
