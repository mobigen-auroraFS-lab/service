"""요청 한 건 안에서 **단계별로 걸린 시간**을 모은다(단일 책임: 재기만 한다 — 남기는 것은 ``request_log`` 가 한다).

**왜 필요한가** — 검색이 가끔 5초씩 튀는데(2026-10-01 · 재현되지 않음) 요청 전체 시간만으로는 원인을 가를 수 없다. 임베딩 서버인지,
검색 엔진인지, DB 인지가 갈려야 어디를 볼지 정해진다. 그래서 느린 요청 경고에 단계별 시간을 함께 싣는다.

**쓰는 법** — 재고 싶은 구간을 ``with stage("embed"):`` 로 감싼다. 요청 밖(기동 · 시험)에서는 아무것도 하지 않는다(비용 0).
같은 이름을 여러 번 재면 더한다. 동기 라우트는 스레드풀에서 도는데 문맥 변수는 그리로 복사되고, 담는 그릇(dict)은 **같은 객체**라
스레드 안에서 더한 값이 요청 로그 쪽에도 보인다.

단계 이름: ``embed``(질의 임베딩) · ``engine``(검색 엔진) · ``search``(임베딩 + 엔진을 한 번에 부르는 창구) · ``db``(DB 연결 대기 + 질의).
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

_STAGES: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar("stage_ms", default=None)

# 로그에 보일 이름과 순서
LABELS: dict[str, str] = {"embed": "임베딩", "engine": "검색엔진", "search": "검색(임베딩+엔진)", "db": "DB"}


def begin() -> contextvars.Token:
    """이 요청의 단계 시간을 담을 빈 그릇을 문맥에 둔다. 돌려받은 토큰으로 ``end`` 한다."""
    return _STAGES.set({})


def end(token: contextvars.Token) -> None:
    """``begin`` 으로 둔 그릇을 문맥에서 거둔다."""
    _STAGES.reset(token)


def snapshot() -> dict[str, float]:
    """지금까지 모인 단계 시간(ms)의 복사본. 요청 밖이면 빈 dict."""
    current = _STAGES.get()
    return dict(current) if current else {}


@contextmanager
def stage(name: str) -> Iterator[None]:
    """감싼 구간의 걸린 시간을 ``name`` 에 더한다(요청 밖이면 아무것도 하지 않는다). 예외가 나도 잰 만큼은 남긴다."""
    bucket = _STAGES.get()
    if bucket is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        bucket[name] = bucket.get(name, 0.0) + (time.perf_counter() - started) * 1000


def describe(stages: dict[str, float]) -> str:
    """로그에 붙일 한 줄 — ``임베딩 120ms · 검색엔진 4,800ms · DB 30ms``(고정 순서 · 모르는 이름은 뒤에 이름 그대로)."""
    order = [k for k in LABELS if k in stages] + [k for k in stages if k not in LABELS]
    return " · ".join(f"{LABELS.get(k, k)} {stages[k]:,.0f}ms" for k in order)
