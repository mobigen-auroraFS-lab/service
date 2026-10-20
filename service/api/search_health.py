"""검색 엔진(OpenSearch) 연결 차단기. ``get_client()`` 는 코어 클라이언트를 감싸서, 차단 중이면 호출 전에 ``SearchUnavailable``(→ 503),
호출이 연결 실패하면 차단기에 알린다. 접속 시험은 ``OPENSEARCH_URL`` 의 호스트 · 포트에 TCP 로 2초 안에 붙어 보는 것이다.
"""

from __future__ import annotations

import os
import socket
import threading
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

from service.api.breaker import Breaker

PROBE_TIMEOUT = 2.0
RETRY_AFTER_SECONDS = 5


class SearchUnavailable(RuntimeError):
    """검색 엔진에 닿지 못한다(차단 중) — 화면에는 503 으로 나간다."""


def _probe() -> bool:
    url = os.getenv("OPENSEARCH_URL", "").strip() or "http://localhost:9200"
    parsed = urlparse(url if "//" in url else f"//{url}")
    host, port = parsed.hostname or "localhost", parsed.port or (443 if parsed.scheme == "https" else 9200)
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


BREAKER = Breaker("검색 엔진", lambda: _probe())


def _conn_errors() -> tuple[type[BaseException], ...]:
    try:
        from opensearchpy.exceptions import ConnectionError as OSConnectionError
    except ImportError:
        return ()
    return (OSConnectionError,)


def check() -> None:
    """차단 중이면 곧바로 ``SearchUnavailable`` — 엔진을 부르기 전에 한다."""
    if BREAKER.is_down():
        raise SearchUnavailable("검색 엔진 연결 불가(차단 중)")


class _Guarded:
    """코어 엔진 클라이언트를 감싼다 — 메서드 호출이 연결 실패하면 차단기에 알리고 **같은 예외**를 다시 올린다(기존 503 처리가 그대로 받는다)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            check()
            try:
                return attr(*args, **kwargs)
            except _conn_errors() as exc:
                BREAKER.note_failure(exc)
                raise

        return call


# 코어 클라이언트는 프로세스에서 **하나를 다시 쓴다** — 연결 풀이 요청 사이에 살아 있게(2026-10-21).
# 🔴 종전에는 요청마다 새로 만들어 매번 연결을 새로 맺었다 — 엔진 호출 1회가 67ms, 다시 쓰면 33ms(공용 개발망 실측).
#    묻는 내용 · 결과는 그대로다. 열쇠는 「코어 함수 · 접속 주소」다 — 시험이 코어 함수를 대역으로 바꿔 끼우면 새로 만든다.
_client_lock = threading.Lock()
_client_slot: dict[str, Any] = {"factory": None, "url": None, "client": None}


def get_client() -> Any:
    """코어의 ``get_client()`` 대신 쓴다 — 차단 중이면 곧바로 실패하고, 아니면 보호막을 씌운 클라이언트를 준다."""
    from src.search.opensearch_sync import get_client as core_get_client

    check()
    url = os.getenv("OPENSEARCH_URL", "").strip()
    with _client_lock:
        slot = _client_slot
        if slot["client"] is None or slot["factory"] is not core_get_client or slot["url"] != url:
            slot.update(factory=core_get_client, url=url, client=core_get_client())
        raw = slot["client"]
    return _Guarded(raw)


def close_client() -> None:
    """다시 쓰던 원본 클라이언트의 연결을 닫는다(종료 때). 차단기를 거치지 않고, 닫기 실패가 종료를 막지 않는다."""
    with _client_lock:
        raw = _client_slot["client"]
        _client_slot.update(factory=None, url=None, client=None)
    close = getattr(raw, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
