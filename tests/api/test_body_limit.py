"""요청 본문 크기 상한 — 넘는 본문은 앱에 닿기 전에 413 공용 봉투로 끊는다(2026-09-28)."""

from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from service.api.body_limit import MAX_BODY_DEFAULT, BodyLimitMiddleware, max_body_bytes

LIMIT = 100
REACHED: list[int] = []


async def echo(request: Request) -> JSONResponse:
    body = await request.body()
    REACHED.append(len(body))
    return JSONResponse({"len": len(body)})


def _client() -> TestClient:
    app = Starlette(routes=[Route("/x", echo, methods=["POST", "GET"])])
    app.add_middleware(BodyLimitMiddleware, limit=LIMIT)
    return TestClient(app)


class TestBodyLimit(unittest.TestCase):
    def setUp(self) -> None:
        REACHED.clear()

    def test_길이를_밝힌_큰_본문은_앱에_닿기_전에_413(self) -> None:
        r = _client().post("/x", content=b"a" * (LIMIT + 1))
        self.assertEqual(413, r.status_code)
        self.assertIn("상한 100바이트", r.json()["detail"])
        self.assertEqual([], REACHED)

    def test_청크로_보낸_큰_본문도_413(self) -> None:
        r = _client().post("/x", content=iter([b"a" * 60, b"a" * 60]))
        self.assertEqual(413, r.status_code)
        self.assertEqual([], REACHED)

    def test_상한_안의_본문은_그대로_닿는다(self) -> None:
        c = _client()
        self.assertEqual(LIMIT, c.post("/x", content=b"a" * LIMIT).json()["len"])
        self.assertEqual(80, c.post("/x", content=iter([b"a" * 40, b"a" * 40])).json()["len"])
        self.assertEqual(200, c.get("/x").status_code)

    def test_환경변수가_잘못되면_기본값(self) -> None:
        from unittest import mock
        for raw, want in (("", MAX_BODY_DEFAULT), ("abc", MAX_BODY_DEFAULT), ("0", MAX_BODY_DEFAULT),
                          ("2048", 2048)):
            with mock.patch.dict("os.environ", {"PORTAL_MAX_BODY_BYTES": raw}):
                self.assertEqual(want, max_body_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
