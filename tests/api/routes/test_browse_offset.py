"""훑기에서 offset 이 조용히 버려지던 것을 봉인한다(2026-09-21 실 서버 실측).

검색어가 없으면 커서 전용 경로로 가는데 그 경로에는 "몇 번째부터"가 없다. ``offset=3`` 도 ``offset=9000``
도 첫 쪽이 왔고 응답의 ``offset`` 은 0 이었다 — 2쪽을 달라고 한 화면이 1쪽을 받고도 알 수 없다.
이제 **400** 으로 끊고 커서를 쓰라고 알린다. 검색어가 있을 때의 얕은 페이지는 그대로 쓴다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db  # noqa: E402
from service.api.routes import file_search as routes_file_search  # noqa: E402

# 설정 미초기화 상태로 도는 단위 테스트다 — 색인 이름·임베딩 채널만 대역으로 준다(다른 라우트 테스트와 같은 관례).
_CFG = SimpleNamespace(opensearch=SimpleNamespace(index="assets"))
_FOUND = {"rows": [], "total": 0, "scope_total": 0, "total_capped": False, "facets": {},
          "from": 0, "size": 3, "sort": "created_desc", "next_cursor": None}


class TestBrowseOffsetRejected(unittest.TestCase):
    """① 훑기(q 없음) + offset → 400. 검색어가 있으면 종전대로 얕은 페이지가 성립한다."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_browse_with_offset_is_400(self) -> None:
        for offset in (1, 3, 9000):
            with self.subTest(offset=offset):
                r = self.client.get("/file-search", params={"limit": 3, "offset": offset})
                self.assertEqual(400, r.status_code, r.text)
                self.assertIn("next_cursor", r.json()["detail"])

    def test_browse_without_offset_is_fine(self) -> None:
        with patch.object(routes_file_search, "browse_files", lambda *a, **k: dict(_FOUND)), \
             patch.object(routes_file_search, "get_current_settings", lambda: _CFG), \
             patch("src.search.opensearch_sync.get_client", lambda *a, **k: object()), \
             patch.object(db, "run_in_db", lambda cb: []):
            self.assertEqual(200, self.client.get("/file-search", params={"limit": 3}).status_code)

    def test_query_path_still_passes_offset_to_core(self) -> None:
        """검색어가 있으면 offset 이 코어 조회로 그대로 간다(얕은 페이지는 그대로 쓴다)."""
        seen: dict = {}

        def fake_search(*_a, **kw):
            seen.update(kw)
            return dict(_FOUND)

        with patch.object(routes_file_search, "search_files", fake_search), \
             patch.object(routes_file_search, "get_current_settings", lambda: _CFG), \
             patch("src.search.opensearch_sync.get_client", lambda *a, **k: object()), \
             patch.object(routes_file_search, "active_embed_channel", lambda: "text"), \
             patch.object(routes_file_search, "embed_query_for_media_search", lambda *a, **k: [0.0] * 8), \
             patch.object(db, "run_in_db", lambda cb: []):
            r = self.client.get("/file-search", params={"q": "김치", "limit": 3, "offset": 3,
                                                        "sort": "created_desc"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(3, seen.get("from_"))


if __name__ == "__main__":
    unittest.main()
