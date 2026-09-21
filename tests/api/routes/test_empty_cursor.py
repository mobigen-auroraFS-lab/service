"""빈 커서(``cursor=``)는 **커서 없음**(첫 쪽)이다 — 창구 안·창구 사이에서 판단이 갈리지 않게 봉인한다.

실측(2026-09-21)에서 같은 빈 커서가 경로마다 다르게 읽혔다.

| 요청 | 종전 | 이제 |
|---|---|---|
| ``/file-search?cursor=`` (훑기) | 200 — 없음으로 봄 | 200 |
| ``/file-search?q=김치&offset=2&cursor=`` | **400** cursor·offset 동시 | 200(얕은 페이지) |
| ``/file-search?q=김치&cursor=`` (관련도 정렬 기본) | **400** 관련도는 cursor 불가 | 200 |
| ``/mm-meta?cursor=`` | **400** 커서가 비었다 | 200 |

화면은 흔히 첫 쪽에도 ``cursor=`` 를 빈 값으로 붙인다 — 그러면 검색 첫 쪽이 통째로 400 이었다.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db, params  # noqa: E402
from service.api.routes import file_search as routes_file_search  # noqa: E402
from tests.conformance import probes  # noqa: E402
from tests.conformance.simulator import FakeConn  # noqa: E402

_CFG = SimpleNamespace(opensearch=SimpleNamespace(index="assets"))
_FOUND = {"rows": [], "total": 0, "scope_total": 0, "total_capped": False, "facets": {},
          "from": 0, "size": 3, "sort": "relevance", "next_cursor": None}


class TestCursorOrNone(unittest.TestCase):
    """입구 정리 함수 — 빈 값·공백뿐이면 None, 그 밖은 앞뒤 공백만 뗀다."""

    def test_blank_values(self) -> None:
        for raw in (None, "", "   ", "\t"):
            with self.subTest(raw=raw):
                self.assertIsNone(params.cursor_or_none(raw))

    def test_real_cursor_is_kept(self) -> None:
        self.assertEqual("eyJhIjoxfQ", params.cursor_or_none("  eyJhIjoxfQ "))


class TestFileSearchEmptyCursor(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.seen: dict = {}

    def _search(self, **query):
        def fake_search(*_a, **kw):
            self.seen.update(kw)
            return dict(_FOUND)
        with patch.object(routes_file_search, "search_files", fake_search), \
             patch.object(routes_file_search, "browse_files", lambda *a, **k: dict(_FOUND)), \
             patch.object(routes_file_search, "get_current_settings", lambda: _CFG), \
             patch("src.search.opensearch_sync.get_client", lambda *a, **k: object()), \
             patch.object(routes_file_search, "active_embed_channel", lambda: "text"), \
             patch.object(routes_file_search, "embed_query_for_media_search", lambda *a, **k: [0.0] * 8), \
             patch.object(db, "run_in_db", lambda cb: []):
            return self.client.get("/file-search", params=query)

    def test_query_offset_with_empty_cursor_is_shallow_page(self) -> None:
        """🔴 종전 400(cursor·offset 동시) — 빈 커서는 커서가 아니다."""
        for blank in ("", "   "):
            with self.subTest(cursor=repr(blank)):
                r = self._search(q="김치", offset=2, limit=3, sort="created_desc", cursor=blank)
                self.assertEqual(200, r.status_code, r.text)
                self.assertEqual(2, self.seen.get("from_"))

    def test_relevance_first_page_with_empty_cursor(self) -> None:
        """🔴 종전 400(관련도 정렬은 cursor 불가) — 검색 첫 쪽이 통째로 실패했다."""
        r = self._search(q="김치", limit=3, cursor="")
        self.assertEqual(200, r.status_code, r.text)

    def test_browse_with_empty_cursor_still_first_page(self) -> None:
        self.assertEqual(200, self._search(limit=3, cursor="").status_code)

    def test_real_cursor_still_conflicts_with_offset(self) -> None:
        """정리는 빈 값만 — 진짜 커서와 offset 을 함께 주면 여전히 400 이다."""
        r = self._search(q="김치", offset=2, limit=3, sort="created_desc", cursor="abc")
        self.assertEqual(400, r.status_code, r.text)


class TestEntityListEmptyCursor(unittest.TestCase):
    def test_empty_cursor_is_first_page(self) -> None:
        """🔴 종전 400(커서가 비었다)."""
        client = TestClient(app)
        with contextlib.ExitStack() as st:
            for pt in (*probes.settings_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found())))):
                st.enter_context(pt)
            for blank in ("", "  "):
                with self.subTest(cursor=repr(blank)):
                    r = client.get("/mm-meta", params={"limit": 2, "cursor": blank})
                    self.assertEqual(200, r.status_code, r.text)

    def test_broken_cursor_is_still_400(self) -> None:
        client = TestClient(app)
        with contextlib.ExitStack() as st:
            for pt in (*probes.settings_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found())))):
                st.enter_context(pt)
            self.assertEqual(400, client.get("/mm-meta", params={"cursor": "abc"}).status_code)


if __name__ == "__main__":
    unittest.main()
