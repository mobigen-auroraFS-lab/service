"""질의 임베딩 재사용 — 목록과 추가 칩이 같은 질의를 동시에 보내도 임베딩 서버 호출은 한 번이다(웹 점검 #46)."""

from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from service.portal.search import query_vector
from service.portal.search.query_vector import embed_once


class _Embedder:
    """부른 횟수를 세는 임베딩 대역 — ``gate`` 를 주면 그것이 열릴 때까지 계산을 붙잡는다."""

    def __init__(self, gate: threading.Event | None = None, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.gate = gate
        self.fail = fail

    def __call__(self, query: str, *, channel: str) -> list[float]:
        self.calls.append((channel, query))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        return [float(len(query)), 1.0]


class TestEmbedOnce(unittest.TestCase):
    def setUp(self) -> None:
        query_vector.clear()
        self.addCleanup(query_vector.clear)
        env = mock.patch.dict(os.environ, {query_vector.CACHE_ENV: "60"})
        env.start()
        self.addCleanup(env.stop)

    def test_같은_질의는_수명_안에서_한_번만_부른다(self) -> None:
        embed = _Embedder()
        first = embed_once(embed, "김치", channel="text")
        first.append(99.0)                       # 받은 쪽이 고쳐도 들고 있는 값은 그대로다
        self.assertEqual([2.0, 1.0], embed_once(embed, "김치", channel="text"))
        self.assertEqual(1, len(embed.calls))

    def test_채널이나_질의가_다르면_따로_부른다(self) -> None:
        embed = _Embedder()
        embed_once(embed, "김치", channel="text")
        embed_once(embed, "김치", channel="st_api")
        embed_once(embed, " 김치", channel="text")      # 넘긴 값 그대로가 열쇠 — 다듬지 않는다
        self.assertEqual(3, len(embed.calls))

    def test_0_이면_끈다(self) -> None:
        embed = _Embedder()
        with mock.patch.dict(os.environ, {query_vector.CACHE_ENV: "0"}):
            embed_once(embed, "김치", channel="text")
            embed_once(embed, "김치", channel="text")
        self.assertEqual(2, len(embed.calls))

    def test_수명이_지나면_다시_부른다(self) -> None:
        embed = _Embedder()
        with mock.patch.object(query_vector.time, "monotonic", side_effect=[0.0, 61.0, 61.0]):
            embed_once(embed, "김치", channel="text")
            embed_once(embed, "김치", channel="text")
        self.assertEqual(2, len(embed.calls))

    def test_계산_중인_같은_질의는_기다렸다_받는다(self) -> None:
        """🔴 화면은 두 요청을 거의 동시에 보낸다 — 끝난 것만 재사용하면 둘 다 계산한다."""
        gate = threading.Event()
        embed = _Embedder(gate=gate)
        got: list[list[float]] = []
        first = threading.Thread(target=lambda: got.append(embed_once(embed, "불국사", channel="text")))
        first.start()
        self.assertTrue(embed.started.wait(5))
        second = threading.Thread(target=lambda: got.append(embed_once(embed, "불국사", channel="text")))
        second.start()
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(1, len(embed.calls))
        self.assertEqual([[3.0, 1.0], [3.0, 1.0]], got)

    def test_실패는_들고_있지_않고_기다리던_요청도_같은_실패를_받는다(self) -> None:
        gate = threading.Event()
        embed = _Embedder(gate=gate, fail=RuntimeError("임베딩 서버 다운"))
        errors: list[BaseException] = []

        def call() -> None:
            try:
                embed_once(embed, "불국사", channel="text")
            except RuntimeError as exc:
                errors.append(exc)

        first = threading.Thread(target=call)
        first.start()
        self.assertTrue(embed.started.wait(5))
        second = threading.Thread(target=call)
        second.start()
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(2, len(errors))
        self.assertEqual(1, len(embed.calls))
        embed.fail = None
        embed_once(embed, "불국사", channel="text")       # 실패는 남기지 않았으니 다시 계산한다
        self.assertEqual(2, len(embed.calls))


class TestRoutesShareVector(unittest.TestCase):
    """목록과 추가 칩이 같은 질의면 임베딩 서버 호출은 한 번이다."""

    def setUp(self) -> None:
        query_vector.clear()
        self.addCleanup(query_vector.clear)
        self.embed = _Embedder()
        found = {"rows": [], "total": 0, "scope_total": 0, "total_capped": False, "facets": {},
                 "from": 0, "size": 50, "sort": "relevance"}
        for target, repl in (
            ("os.environ", {query_vector.CACHE_ENV: "60", "PORTAL_AUTH_DISABLED": "1",
                            "PORTAL_JWT_SECRET": "test-secret"}),
        ):
            p = mock.patch.dict(target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (
            ("service.api.db.run_in_db", lambda cb: cb(object())),
            ("service.portal.repositories.search_repo.fetch_file_meta", lambda *_a, **_k: {}),
            ("src.search.opensearch_sync.get_client", lambda *_a, **_k: object()),
            ("service.api.routes.file_search.get_current_settings",
             lambda: SimpleNamespace(opensearch=SimpleNamespace(index="assets"))),
            ("service.api.routes.file_search.active_embed_channel", lambda: "text"),
            ("service.api.routes.file_search.embed_query_for_media_search", self.embed),
            ("service.api.routes.file_search.search_files", lambda *_a, **_k: dict(found)),
            ("service.api.routes.file_search.extra_facets",
             lambda *_a, **_k: {"axes": {}, "total": 0, "as_of": "2026-10-21T00:00:00+00:00"}),
        ):
            p = mock.patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        from service.api import app
        self.client = TestClient(app)

    def test_목록과_추가_칩은_같은_벡터를_쓴다(self) -> None:
        self.assertEqual(200, self.client.get("/file-search", params={"q": "다보탑"}).status_code)
        self.assertEqual(200, self.client.get("/file-search/facet-extra", params={"q": "다보탑"}).status_code)
        self.assertEqual([("text", "다보탑")], self.embed.calls)


if __name__ == "__main__":
    unittest.main()
