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

    def test_이어받기_Range_가_막히지_않는다(self) -> None:
        """종전에는 authorization,range 사전 요청이 400 이라 다른 오리진에서 이어받기가 안 됐다(2026-09-28)."""
        self.assertEqual(200, self._preflight("authorization,range"))
        self.assertEqual(200, self._preflight("authorization,range,if-range"))

    def test_화면이_읽을_응답_헤더를_노출한다(self) -> None:
        r = _client().get("/x", headers={"Origin": ORIGIN})
        exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
        for name in ("content-disposition", "content-range", "accept-ranges",
                     "x-bundle-files", "x-bundle-missing"):
            self.assertIn(name, exposed)

    def test_묶음_창구가_싣는_X_헤더는_모두_노출한다(self) -> None:
        """개체 묶음의 잘림(X-Bundle-Truncated)이 빠져 다른 오리진에서 못 읽었다(2026-09-28)."""
        import pathlib
        import re

        routes = pathlib.Path(__file__).resolve().parents[2] / "service" / "api" / "routes"
        sent = {m for f in routes.glob("*.py") for m in re.findall(r'"(X-[A-Za-z-]+)"', f.read_text())}
        self.assertTrue(sent)
        self.assertEqual(set(), sent - set(CORS_EXPOSE_HEADERS))


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

    def test_모르는_오리진이나_같은_오리진이면_붙이지_않는다(self) -> None:
        self.assertNotIn("access-control-allow-origin", self._get("http://evil.example").headers)
        self.assertNotIn("access-control-allow-origin", self._get(None).headers)


if __name__ == "__main__":
    unittest.main()
