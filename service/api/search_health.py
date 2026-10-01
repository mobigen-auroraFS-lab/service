"""검색 엔진(OpenSearch) 연결 차단기(단일 책임: 검색 엔진이 죽어 있을 때 요청이 매달리지 않고 곧바로 503 으로 끝나게 한다).

엔진 클라이언트가 연결 실패를 한 번 겪을 때마다 요청이 수 초씩 기다린다(실측: 4초) — 동기 라우트가 스레드풀(10개)을 다 잡으면 다른 화면까지 늦어진다.
``get_client()`` 는 코어 클라이언트를 **감싸서** 돌려준다: 차단 중이면 곧바로 ``SearchUnavailable``(→ 503 + ``Retry-After``), 호출이 연결 실패하면 차단기에 알린다.
접속 시험은 ``OPENSEARCH_URL`` 의 호스트 · 포트에 TCP 로 2초 안에 붙어 보는 것이다(인증 · 색인 상태는 보지 않는다).
"""

from __future__ import annotations

import os
import socket
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


def get_client() -> Any:
    """코어의 ``get_client()`` 대신 쓴다 — 차단 중이면 곧바로 실패하고, 아니면 보호막을 씌운 클라이언트를 준다."""
    from src.search.opensearch_sync import get_client as core_get_client

    check()
    return _Guarded(core_get_client())
