"""요청 본문 크기 상한 — 넘는 본문은 앱에 닿기 전에 413 으로 끊는다.

**왜 필요한가(실측 2026-09-21)**: 상한이 없어 5MB 본문도 끝까지 받아 파싱했다. 응답에 되돌리지 않게는
막았지만(봉투가 값을 통째로 싣지 않는다) 수신 · 파싱 비용은 그대로라, 큰 본문을 되풀이해 보내면 서버를
묶을 수 있었다. 지금 본문을 받는 창구는 계정(가입 · 로그인)과 개발용 토큰뿐이고 모두 짧은 JSON 이다.

**어떻게 막나** — ASGI 입구에서 본다.
  · ``Content-Length`` 가 있으면 그 값만 보고 넘으면 곧바로 413(본문을 한 바이트도 읽지 않는다).
  · 없으면(청크 전송) 상한까지 먼저 받아 보고, 넘으면 413 — 앱에는 그 본문이 닿지 않는다.
    (받는 도중 예외로 끊으면 FastAPI 가 400 「본문 해석 실패」로 바꿔 버려 먼저 받는다.)

앞단 프록시(``client_max_body_size`` 등)에서도 막는 것이 맞다 — 이 상한은 프록시가 없거나 설정이 빠졌을 때의
마지막 선이다. 상한은 ``PORTAL_MAX_BODY_BYTES`` 로 바꾼다(기본 1MiB · 0 이하나 형식 오류면 기본값).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from service.api.errors import envelope

_LOG = logging.getLogger("meta_extract.portal_api")

MAX_BODY_ENV = "PORTAL_MAX_BODY_BYTES"
MAX_BODY_DEFAULT = 1024 * 1024


def max_body_bytes() -> int:
    """본문 상한(바이트)을 환경변수에서 읽는다 — 없거나 잘못되면 기본 1MiB.

    Returns:
        1 이상의 정수.
    """
    raw = os.getenv(MAX_BODY_ENV, "").strip()
    try:
        value = int(raw) if raw else MAX_BODY_DEFAULT
    except ValueError:
        _LOG.warning("%s=%r 는 정수가 아니다 — 기본 %d 바이트를 쓴다", MAX_BODY_ENV, raw, MAX_BODY_DEFAULT)
        return MAX_BODY_DEFAULT
    return value if value > 0 else MAX_BODY_DEFAULT


def _detail(limit: int) -> str:
    return f"요청 본문이 너무 큽니다(상한 {limit:,}바이트) — 보내는 값을 줄이십시오"


class BodyLimitMiddleware:
    """HTTP 요청 본문이 상한을 넘으면 413 공용 봉투로 답한다(그 밖의 요청은 그대로 통과)."""

    def __init__(self, app: Any, limit: int | None = None) -> None:
        self.app = app
        self.limit = limit if limit is not None else max_body_bytes()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            # 길이를 밝혔으면 그 값으로 판단한다 — 밝힌 길이와 실제 본문이 다른 요청은 ASGI 서버(h11)가 끊는다.
            try:
                declared = int(length)
            except ValueError:
                declared = 0
            if declared > self.limit:
                await self._reject(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if b"chunked" not in headers.get(b"transfer-encoding", b"").lower():
            await self.app(scope, receive, send)          # 본문이 없는 요청(GET 등)
            return
        # 🔴 길이를 밝히지 않은 청크 전송 — 상한까지 **먼저 다 받아 본 뒤** 앱에 다시 넘긴다.
        #    받는 도중에 예외로 끊으면 FastAPI 가 본문 읽기 오류를 400(본문 해석 실패)으로 바꿔 버린다.
        #    지금 본문은 짧은 JSON 뿐이라 먼저 받아도 부담이 없다(상한 1MiB).
        chunks: list[bytes] = []
        seen = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":          # 받는 도중 끊김 — 앱에 그대로 알린다
                chunks.append(b"")
                break
            body = message.get("body", b"")
            seen += len(body)
            if seen > self.limit:
                await self._reject(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay_receive() -> dict[str, Any]:
            if replay:
                return replay.pop()
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        _LOG.warning("본문 상한 초과(413): %s %s", scope.get("method"), scope.get("path"))
        response = envelope(status_code=413, detail=_detail(self.limit))
        await response(scope, receive, send)
