"""요청 로그(단일 책임: 요청마다 ID 를 매기고 끝에 한 줄을 남긴다).

**하는 일**
  1. 요청 ID 를 정한다 — 앞단(프록시)이 ``X-Request-ID`` 를 보냈고 모양이 맞으면 그대로, 아니면 새로 만든다(12자리 16진수).
     문맥 변수에 실어 그 요청이 남기는 **모든 로그 줄**에 붙이고, 응답 머리에도 ``X-Request-ID`` 로 돌려준다
     (화면 · 문의 때 이 값으로 서버 로그를 찾는다).
  2. 응답이 끝나면 **한 줄**을 남긴다 — ``GET /file-search 200 83.2ms 127.0.0.1``(마지막은 접속한 쪽 주소).
     경로까지만 남긴다. **쿼리 문자열 · 본문 · 토큰은 남기지 않는다**(검색어 · 커서 같은 사용자 입력이 로그에 쌓이지 않게).

**단계별 시간** — 느린 요청 경고와 5xx 줄에는 어디서 시간이 걸렸는지 붙는다: ``(임베딩 120ms · 검색엔진 4,800ms · DB 30ms)``
(``service.portal.common.stage_timer`` — 임베딩 서버 · 검색 엔진 · DB 중 어디가 튀었는지 가른다). ``json`` 형식에는 모든 줄에 ``stages`` 칸으로 실린다.
보통 줄에는 붙이지 않는다(줄이 길어지지 않게).

**수준** — 5xx 는 ERROR(단 501 「아직 제공하지 않음」은 서버 고장이 아니라 WARNING — 자리만 있는 창구를 부를 때마다 오류 알람이 울리지 않게), 기준 시간(``PORTAL_SLOW_REQUEST_MS`` · 기본 3000ms)을 넘긴 요청은 WARNING(``느린 요청``),
헬스 체크(``/health``)는 DEBUG(살아 있는지 묻는 호출이 로그를 덮지 않게), 나머지는 INFO.

**순수 ASGI 미들웨어**로 만들었다 — 흔한 ``@app.middleware("http")`` 방식은 요청마다 태스크를 하나 더 만들고 스트리밍
응답을 버퍼링할 수 있어서, 맨 바깥에 두는 이 층에는 쓰지 않는다. ``service.api`` 가 **가장 바깥**(마지막 등록)에 붙인다 —
그래야 CORS 거절 · 본문 상한 413 · NUL 400 도 같은 ID 와 한 줄을 남긴다.

이 줄은 DB 접근 이력(``audit`` · ``access_log`` 표)과 **별개**다 — 그쪽은 누가 무엇을 봤는지 남기는 감사 자료다.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from starlette.datastructures import MutableHeaders

from service.api.logging_config import new_request_id, request_id_var, valid_request_id
from service.portal.common import stage_timer

REQUEST_ID_HEADER = "X-Request-ID"
SLOW_REQUEST_ENV = "PORTAL_SLOW_REQUEST_MS"
SLOW_REQUEST_DEFAULT_MS = 3000
QUIET_PATHS = frozenset({"/health"})
_PATH_MAX = 200

_LOG = logging.getLogger("meta_extract.portal_api.access")


def slow_request_ms() -> int:
    """느린 요청 기준(ms)을 환경변수에서 읽는다 — 0 이면 끈다. 잘못된 값이면 기본 3000."""
    raw = os.getenv(SLOW_REQUEST_ENV, "").strip()
    if not raw:
        return SLOW_REQUEST_DEFAULT_MS
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning("%s=%r 는 정수가 아니다 — 기본 %d 를 쓴다", SLOW_REQUEST_ENV, raw, SLOW_REQUEST_DEFAULT_MS)
        return SLOW_REQUEST_DEFAULT_MS
    return max(value, 0)


def printable_path(path: str) -> str:
    """로그에 싣기 전에 경로의 제어문자를 ``\\xNN`` 으로 바꾸고 길이를 자른다(줄 위조 방지)."""
    clean = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in path[:_PATH_MAX])
    return clean + "…" if len(path) > _PATH_MAX else clean


def _incoming_request_id(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            return valid_request_id(value.decode("latin-1"))
    return None


class RequestLogMiddleware:
    """요청 ID 를 정하고, 응답이 끝나면 한 줄을 남긴다(HTTP 만 · 그 밖의 종류는 그대로 통과)."""

    def __init__(self, app: Any, slow_ms: int | None = None) -> None:
        self.app = app
        self.slow_ms = slow_request_ms() if slow_ms is None else slow_ms

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _incoming_request_id(scope) or new_request_id()
        scope["request_id"] = rid          # 미처리 예외 처리기는 이 층 **바깥**이라 scope 로 ID 를 읽는다(``errors``)
        token = request_id_var.set(rid)
        stage_token = stage_timer.begin()
        started = time.perf_counter()
        status = {"code": 500}             # 응답을 시작하기 전에 예외가 오르면 500 으로 센다

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            self._log(scope, status["code"], (time.perf_counter() - started) * 1000, stage_timer.snapshot())
            stage_timer.end(stage_token)
            request_id_var.reset(token)

    def _log(self, scope: dict[str, Any], code: int, ms: float, stages: dict[str, float] | None = None) -> None:
        method = scope.get("method", "-")
        path = printable_path(scope.get("path", ""))
        client = (scope.get("client") or ("-", 0))[0]
        slow = bool(self.slow_ms) and ms >= self.slow_ms
        if code >= 500 and code != 501:
            level = logging.ERROR
        elif code == 501 or slow:
            level = logging.WARNING
        elif slow:
            level = logging.WARNING
        elif path in QUIET_PATHS:
            level = logging.DEBUG
        else:
            level = logging.INFO
        if not _LOG.isEnabledFor(level):
            return
        stages = stages or {}
        # 느린 요청 · 5xx 에만 단계별 시간을 문장으로 붙인다(보통 줄은 짧게). 구조화 칸은 항상 싣는다.
        detail = f" ({stage_timer.describe(stages)})" if stages and (slow or code >= 500) else ""
        extra = {"method": method, "path": path, "status": code, "duration_ms": round(ms, 1), "client": client}
        if stages:
            extra["stages"] = {k: round(v, 1) for k, v in stages.items()}
        _LOG.log(
            level, "%s%s %s %d %.1fms %s%s", "느린 요청 " if slow else "", method, path, code, ms, client, detail,
            extra=extra,
        )
