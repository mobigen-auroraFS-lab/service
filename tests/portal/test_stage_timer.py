"""단계별 시간 — 요청 안에서만 재고, 같은 이름은 더하고, 스레드를 건너도 보이며, 느린 요청 경고에 실린다."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from service.api.request_log import RequestLogMiddleware
from service.portal.common import stage_timer
from service.portal.common.stage_timer import describe, stage

ACCESS = "meta_extract.portal_api.access"


class TestStageTimer(unittest.TestCase):
    def test_요청_밖에서는_아무것도_하지_않는다(self) -> None:
        with stage("db"):
            pass
        self.assertEqual({}, stage_timer.snapshot())

    def test_같은_이름은_더한다(self) -> None:
        token = stage_timer.begin()
        try:
            with stage("db"):
                time.sleep(0.01)
            with stage("db"):
                time.sleep(0.01)
            snap = stage_timer.snapshot()
        finally:
            stage_timer.end(token)
        self.assertGreaterEqual(snap["db"], 18)
        self.assertEqual({"db"}, set(snap))

    def test_예외가_나도_잰_만큼은_남긴다(self) -> None:
        token = stage_timer.begin()
        try:
            with self.assertRaises(RuntimeError), stage("embed"):
                time.sleep(0.01)
                raise RuntimeError("x")
            self.assertGreaterEqual(stage_timer.snapshot()["embed"], 9)
        finally:
            stage_timer.end(token)

    def test_스레드풀로_가도_같은_그릇에_쌓인다(self) -> None:
        async def go() -> dict[str, float]:
            token = stage_timer.begin()
            try:
                def work() -> None:
                    with stage("engine"):
                        time.sleep(0.01)
                await run_in_threadpool(work)
                return stage_timer.snapshot()
            finally:
                stage_timer.end(token)

        self.assertGreaterEqual(asyncio.run(go())["engine"], 9)

    def test_거두면_다음_요청에_섞이지_않는다(self) -> None:
        token = stage_timer.begin()
        with stage("db"):
            pass
        stage_timer.end(token)
        self.assertEqual({}, stage_timer.snapshot())

    def test_로그용_문장은_고정_순서다(self) -> None:
        text = describe({"db": 30.0, "engine": 4800.0, "embed": 120.4})
        self.assertEqual("임베딩 120ms · 검색엔진 4,800ms · DB 30ms", text)
        self.assertEqual("검색(임베딩+엔진) 700ms · 뭔가 5ms", describe({"search": 700.0, "뭔가": 5.0}))


def _client(slow_ms: int) -> TestClient:
    def route(_r):
        with stage("embed"):
            time.sleep(0.005)
        with stage("db"):
            time.sleep(0.005)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/x", route)])
    app.add_middleware(RequestLogMiddleware, slow_ms=slow_ms)
    return TestClient(app)


class TestStagesInRequestLog(unittest.TestCase):
    def test_느린_요청_경고에_단계별_시간이_붙는다(self) -> None:
        with mock.patch("service.api.request_log.time") as clock:
            clock.perf_counter.side_effect = [0.0, 5.0]
            with self.assertLogs(ACCESS, "INFO") as cm:
                _client(slow_ms=1000).get("/x")
        rec = cm.records[0]
        self.assertEqual("WARNING", rec.levelname)
        self.assertRegex(rec.getMessage(), r"^느린 요청 GET /x 200 5000\.0ms \S+ \(임베딩 \d+ms · DB \d+ms\)$")

    def test_보통_줄에는_문장으로_붙이지_않는다_구조화_칸에는_싣는다(self) -> None:
        with self.assertLogs(ACCESS, "INFO") as cm:
            _client(slow_ms=0).get("/x")
        rec = cm.records[0]
        self.assertNotIn("임베딩", rec.getMessage())              # 줄이 길어지지 않게
        self.assertEqual({"embed", "db"}, set(rec.stages))       # JSON 형식에는 실린다
        self.assertGreaterEqual(rec.stages["embed"], 4)

    def test_단계가_없는_요청은_칸도_문장도_없다(self) -> None:
        def route(_r):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/x", route)])
        app.add_middleware(RequestLogMiddleware, slow_ms=0)
        with self.assertLogs(ACCESS, "INFO") as cm:
            TestClient(app).get("/x")
        self.assertFalse(hasattr(cm.records[0], "stages"))


if __name__ == "__main__":
    unittest.main()
