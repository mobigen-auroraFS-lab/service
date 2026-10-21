"""질의 임베딩 재사용 — 같은 (채널, 질의)의 벡터를 잠깐 들고, 계산 중인 같은 질의는 기다렸다 받는다.

**왜**: 화면은 검색할 때 목록(``/file-search``)과 추가 칩(``/file-search/facet-extra``)을 **동시에** 부른다. 두 요청이 같은
질의를 각각 임베딩하면 임베딩 서버 부하가 두 배이고 느린 쪽이 화면을 붙잡는다(웹 점검 #46 · 300자 질의 1.0~1.35초).
두 요청이 거의 같은 순간에 오므로 「끝난 것 재사용」만으로는 부족하다 — 먼저 온 요청이 계산하는 동안 뒤 요청은
**그 결과를 기다렸다 받는다**.

임베딩은 같은 입력이면 같은 결과라 재사용해도 검색 결과는 바뀌지 않는다. 열쇠는 임베딩 함수에 넘긴 값 그대로다
(앞뒤 공백을 다듬지 않는다 — 다듬은 값의 벡터는 다른 값일 수 있다). 실패는 들고 있지 않는다(기다리던 요청도 같은 실패를 받는다).

⚠️ 프로세스(워커)마다 따로 든다 — 워커가 여럿이면 두 요청이 다른 워커에 닿을 때는 효과가 없다.
환경변수: ``PORTAL_QUERY_EMBED_CACHE_SECONDS``(기본 60초 · 0 이면 끈다 — 매번 임베딩 함수를 그대로 부른다).
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future

CACHE_ENV = "PORTAL_QUERY_EMBED_CACHE_SECONDS"
CACHE_DEFAULT = 60
_CACHE_MAX = 256          # 들고 있을 질의 수 — 넘으면 가장 오래 안 쓴 것부터 버린다

_LOCK = threading.Lock()
_DONE: OrderedDict[tuple[str, str], tuple[float, list[float]]] = OrderedDict()
_PENDING: dict[tuple[str, str], Future[list[float]]] = {}


def cache_seconds() -> int:
    """재사용 수명(초). 없거나 잘못되면 기본값, 0 이하면 끔(0)."""
    raw = os.getenv(CACHE_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else CACHE_DEFAULT
    except ValueError:
        return CACHE_DEFAULT


def clear() -> None:
    """들고 있는 벡터를 비운다(시험 · 임베딩 모델을 바꾼 직후). 계산 중인 것은 그대로 끝난다."""
    with _LOCK:
        _DONE.clear()


def embed_once(embed: Callable[..., list[float]], query: str, *, channel: str) -> list[float]:
    """``embed(query, channel=channel)`` 을 부르되, 같은 (채널, 질의)는 수명 동안 한 번만 부른다.

    Args:
        embed: 임베딩 함수(코어 ``embed_query_for_media_search``). 부르는 쪽이 넘긴다 — 라우트 모듈의 이름을
            그대로 넘기면 시험이 그 이름을 대역으로 바꿀 수 있다.
        query: 질의(넘긴 값 그대로가 열쇠다).
        channel: 임베딩 채널(다른 채널의 벡터는 다른 값이다).

    Returns:
        질의 벡터(새 목록 — 받은 쪽이 고쳐도 들고 있는 값은 그대로다).

    Raises:
        Exception: 임베딩 함수가 올린 것 그대로. 계산을 기다리던 요청도 같은 예외를 받는다.
    """
    ttl = cache_seconds()
    if not ttl:
        return embed(query, channel=channel)
    key = (channel, query)
    with _LOCK:
        hit = _DONE.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            _DONE.move_to_end(key)
            return list(hit[1])
        pending = _PENDING.get(key)
        if pending is None:
            pending = Future()
            _PENDING[key] = pending
            owner = True
        else:
            owner = False
    if not owner:
        # 같은 질의를 다른 요청이 계산 중이다 — 기다렸다 받는다(시간 제한은 임베딩 함수의 것이 그대로 걸린다).
        return list(pending.result())
    try:
        vector = list(embed(query, channel=channel))
    except Exception as exc:
        with _LOCK:
            _PENDING.pop(key, None)
        pending.set_exception(exc)
        raise
    with _LOCK:
        _PENDING.pop(key, None)
        _DONE[key] = (time.monotonic(), vector)
        _DONE.move_to_end(key)
        while len(_DONE) > _CACHE_MAX:
            _DONE.popitem(last=False)
    pending.set_result(vector)
    return list(vector)
