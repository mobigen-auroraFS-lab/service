"""CORS 허용 헤더 — 화면이 실제로 보내는 헤더가 사전 요청(preflight)에서 막히지 않아야 한다.

앱의 CORS 미들웨어는 ``PORTAL_CORS_ORIGINS`` 가 있을 때만 붙으므로, 여기서는 앱이 쓰는 **같은 목록**
(``CORS_ALLOW_HEADERS``·``CORS_EXPOSE_HEADERS``)으로 미들웨어를 세워 브라우저의 사전 요청을 흉내 낸다.
"""

from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from service.api import CORS_ALLOW_HEADERS, CORS_EXPOSE_HEADERS

ORIGIN = "http://localhost:5173"


def _client() -> TestClient:
    app = Starlette(routes=[Route("/x", lambda _r: PlainTextResponse("ok"))])
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_credentials=True,
                       allow_methods=["GET", "POST"], allow_headers=list(CORS_ALLOW_HEADERS),
                       expose_headers=list(CORS_EXPOSE_HEADERS))
    return TestClient(app)


class TestCorsHeaders(unittest.TestCase):
    def _preflight(self, headers: str) -> int:
        return _client().options("/x", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": headers}).status_code

    def test_인증_헤더만(self) -> None:
        self.assertEqual(200, self._preflight("authorization"))

    def test_JSON_본문_요청(self) -> None:
        self.assertEqual(200, self._preflight("authorization,content-type"))

    def test_파일_전송_헤더는_열지_않는다(self) -> None:
        """다운로드 창구는 자리만 있다(2026-09-28) — 이어받기 헤더도, 파일용 노출 헤더도 두지 않는다."""
        self.assertEqual(400, self._preflight("authorization,range"))
        self.assertNotIn("Content-Disposition", CORS_EXPOSE_HEADERS)

    def test_노출_헤더는_요청_ID_하나(self) -> None:
        """화면이 오류를 문의할 때 서버 로그를 찾는 열쇠(``request_log``)."""
        self.assertEqual(("X-Request-ID",), CORS_EXPOSE_HEADERS)
        r = _client().get("/x", headers={"Origin": ORIGIN})
        self.assertEqual("X-Request-ID", r.headers["access-control-expose-headers"])


class TestServerErrorCors(unittest.TestCase):
    """미처리 예외(500)도 허용 오리진이면 CORS 헤더가 붙어야 화면이 봉투를 읽는다(2026-09-28 재현)."""

    def _get(self, origin: str | None):
        from unittest import mock

        from starlette.requests import Request

        from service.api import errors

        scope = {"type": "http", "method": "GET", "path": "/x", "query_string": b"",
                 "headers": [(b"origin", origin.encode())] if origin else []}
        with mock.patch.object(errors, "CORS_ORIGINS", frozenset({ORIGIN})):
            import asyncio
            return asyncio.run(errors.unhandled_exception_handler(Request(scope), RuntimeError("x")))

    def test_허용_오리진이면_헤더를_붙인다(self) -> None:
        r = self._get(ORIGIN)
        self.assertEqual(500, r.status_code)
        self.assertEqual(ORIGIN, r.headers["access-control-allow-origin"])
        self.assertEqual("true", r.headers["access-control-allow-credentials"])
        self.assertEqual("X-Request-ID", r.headers["access-control-expose-headers"])

    def test_모르는_오리진이나_같은_오리진이면_붙이지_않는다(self) -> None:
        self.assertNotIn("access-control-allow-origin", self._get("http://evil.example").headers)
        self.assertNotIn("access-control-allow-origin", self._get(None).headers)


if __name__ == "__main__":
    unittest.main()
