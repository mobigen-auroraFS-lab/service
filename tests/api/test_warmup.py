"""기동 예열 — 뒤에서 돌고, 실패해도 기동을 막지 않으며, 끌 수 있다."""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from service.api import warmup
from service.api.routes import catalog as catalog_routes


class TestWarmup(unittest.TestCase):
    def test_예열_개수가_태그_창구_기본_개수와_같다(self) -> None:
        # 캐시 열쇠에 개수가 들어 있다 — 어긋나면 예열한 항목을 첫 요청이 만나지 못한다.
        self.assertEqual(catalog_routes._TAG_LIMIT_DEFAULT, warmup.TAGS_DEFAULT_LIMIT)

    def test_끌_수_있다(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_WARMUP": "0"}):
            self.assertFalse(warmup.enabled())
            with mock.patch.object(warmup, "warm_once") as once:
                self.assertIsNone(warmup.start())
            once.assert_not_called()
        for raw in ("1", "", "yes"):
            with mock.patch.dict(os.environ, {"PORTAL_WARMUP": raw}):
                self.assertTrue(warmup.enabled(), raw)

    def test_뒤_스레드에서_돈다(self) -> None:
        seen: list[str] = []
        with mock.patch.dict(os.environ, {"PORTAL_WARMUP": "1"}), \
                mock.patch.object(warmup, "warm_once", side_effect=lambda: seen.append(threading.current_thread().name)):
            thread = warmup.start()
            thread.join(5)
        self.assertEqual(["portal-warmup"], seen)
        self.assertTrue(thread.daemon)                 # 종료를 붙잡지 않는다

    def test_태그를_기본_개수로_불러_캐시를_채운다(self) -> None:
        calls = {}

        class _Catalog:
            def tags(self, **kw):
                calls.update(kw)
                return [{"tag": "a", "count": 1}]

        class _Repo:
            catalog = _Catalog()

        with mock.patch.object(warmup.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            self.assertEqual("태그 1건", warmup._warm_tags())
        self.assertEqual({"topics": [], "subtopics": [], "q": None, "limit": 50}, calls)

    def test_검색_예열은_임베딩_뒤_검색_엔진에_한_건을_보낸다(self) -> None:
        from service.api.routes import file_search as route

        order: list[str] = []
        with mock.patch.object(route, "embed_query_for_media_search", side_effect=lambda *a, **k: order.append("embed") or [0.0]), \
                mock.patch.object(route, "active_embed_channel", return_value="text"), \
                mock.patch.object(route, "get_current_settings") as settings, \
                mock.patch("src.search.opensearch_sync.get_client", return_value=object()), \
                mock.patch.object(route, "search_files", side_effect=lambda *a, **k: order.append("search") or {}) as search:
            settings.return_value.opensearch.index = "assets"
            route.warm_up_search()
        self.assertEqual(["embed", "search"], order)
        kw = search.call_args.kwargs
        self.assertEqual((1, 0, ()), (kw["size"], kw["from_"], kw["axes"]))      # 1건 · 첫 쪽 · 칩 안 센다
        self.assertEqual([0.0], kw["query_vector"])

    def test_예열은_단계에_앞서_접속을_시험한다(self) -> None:
        # 죽어 있는 의존에 단계마다 15초씩 매달리지 않게 — 접속 시험이 먼저 차단기를 연다.
        from service.api import db_health, search_health
        order: list[str] = []
        with mock.patch.object(db_health.BREAKER, "startup_check", side_effect=lambda: order.append("db")), \
                mock.patch.object(search_health.BREAKER, "startup_check", side_effect=lambda: order.append("search")), \
                mock.patch.object(warmup, "STEPS", (("x", lambda: order.append("step") or "x"),)):
            warmup.warm_once()
        self.assertEqual(["db", "search", "step"], order)

    def test_단계는_서로_독립이다_하나가_실패해도_다음을_한다(self) -> None:
        ran: list[str] = []

        def boom() -> str:
            raise RuntimeError("임베딩 서버 없음 — 비밀 값")

        def ok() -> str:
            ran.append("검색")
            return "검색"

        with mock.patch.object(warmup, "STEPS", (("태그", boom), ("검색", ok))), \
                self.assertLogs("meta_extract.portal_api", "INFO") as cm:
            warmup.warm_once()                         # 올리면 이 시험이 깨진다
        self.assertEqual(["검색"], ran)
        msgs = [r.getMessage() for r in cm.records]
        self.assertTrue(any("실패(무시) — 태그: RuntimeError" in m for m in msgs))
        self.assertFalse(any("비밀" in m for m in msgs))               # 예외 메시지는 싣지 않는다
        self.assertTrue(any(m.startswith("기동 예열 — 검색 ") for m in msgs))

    def test_모두_성공하면_걸린_시간과_함께_한_줄(self) -> None:
        with mock.patch.object(warmup, "STEPS", (("태그", lambda: "태그 3건"), ("검색", lambda: "검색"))), \
                self.assertLogs("meta_extract.portal_api", "INFO") as cm:
            warmup.warm_once()
        self.assertEqual(1, len(cm.records))
        self.assertRegex(cm.records[0].getMessage(), r"^기동 예열 — 태그 3건 \d+\.\d초 · 검색 \d+\.\d초$")

    def test_모두_실패하면_경고만_남는다(self) -> None:
        def boom() -> str:
            raise ValueError("x")

        with mock.patch.object(warmup, "STEPS", (("태그", boom), ("검색", boom))), \
                self.assertLogs("meta_extract.portal_api", "WARNING") as cm:
            warmup.warm_once()
        self.assertEqual(2, len(cm.records))
        self.assertTrue(all(r.levelname == "WARNING" for r in cm.records))

    def test_기동하면_예열을_부르되_응답은_기다리지_않는다(self) -> None:
        from fastapi.testclient import TestClient

        from service.api import app
        release = threading.Event()
        started = threading.Event()

        def slow_warm() -> None:
            started.set()
            release.wait(5)                           # 예열이 오래 걸려도

        with mock.patch.dict(os.environ, {"PORTAL_WARMUP": "1"}), \
                mock.patch.object(warmup, "warm_once", side_effect=slow_warm):
            with TestClient(app) as client:           # lifespan 이 돈다
                self.assertTrue(started.wait(5))
                self.assertEqual(200, client.get("/health").status_code)   # 기동 · 요청은 막히지 않는다
                release.set()


if __name__ == "__main__":
    unittest.main()
