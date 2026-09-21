"""검색 창구의 **장애와 입력 오류**를 올바른 코드로 답하는지 봉인한다(2026-09-21 실 서버 실측).

① **임베딩 서버가 죽으면 ``/search`` 만 500 이었다.** 같은 상황에서 ``/file-search``·``/mm-meta`` 는
   503 이다. 500 은 "코드 결함" 신호라 운영자가 엉뚱한 곳을 본다. 이제 503 으로 구분한다 —
   🔴 다만 **임베딩 사유일 때만** 이고, 그 밖의 ``RuntimeError`` 는 그대로 500 이다(결함을 장애로
   위장하지 않는다).
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
from service.api.routes import mm_meta as routes_mm_meta  # noqa: E402
from service.api.routes import search as routes_search  # noqa: E402

# 설정 미초기화 상태로 도는 단위 테스트다 — 색인 이름·임베딩 채널만 대역으로 준다(다른 라우트 테스트와 같은 관례).
_CFG = SimpleNamespace(opensearch=SimpleNamespace(index="assets"))
_FOUND = {"rows": [], "total": 0, "scope_total": 0, "total_capped": False, "facets": {},
          "from": 0, "size": 3, "sort": "created_desc", "next_cursor": None}


class TestSearchEmbeddingOutage(unittest.TestCase):
    """② 임베딩 장애는 503 · 그 밖의 RuntimeError 는 500."""

    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_embedding_failure_is_503(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("임베딩 API 호출 실패(3회): http://embed/v1/embeddings")

        with patch.object(routes_search, "search_hybrid", boom):
            r = self.client.get("/search", params={"q": "김치"})
        self.assertEqual(503, r.status_code, r.text)
        self.assertIn("임베딩", r.json()["detail"])

    def test_other_runtime_error_stays_500(self) -> None:
        """설정 미초기화 같은 코드·배포 결함은 장애로 위장하지 않는다."""
        def boom(*_a, **_k):
            raise RuntimeError("settings가 초기화되지 않았습니다")

        with patch.object(routes_search, "search_hybrid", boom):
            r = self.client.get("/search", params={"q": "김치"})
        self.assertEqual(500, r.status_code, r.text)


class TestQueryLengthCap(unittest.TestCase):
    """③ **아주 긴 질의가 500 이었다.** 검색 엔진이 낱말마다 절을 만들고 1024개에서 거절하는데
    (`maxClauseCount`), 그 오류가 그대로 새어 나갔다(실측 2026-09-21: 2,000자 질의). 입력이 너무 긴 것은
    사용자가 고칠 문제라 **422** 로 앞에서 끊는다 — 엔진까지 가지 않는다.

    ④ **공백뿐인 질의**는 `/search` 에서 0건짜리 200 이었다. 묻지 않은 것을 "자료가 없다"로 보이면
    안 되므로 422 로 통일한다(`/file-search` 가 같은 이유로 2026-09-09 에 정리됐다).
    """

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_long_query_is_422_everywhere(self) -> None:
        long_q = "김" * (routes_file_search._QUERY_MAX_LEN + 1)
        for path, params in (("/file-search", {"q": long_q}), ("/file-search", {"refine": long_q}),
                             ("/search", {"q": long_q}), ("/mm-meta", {"q": long_q}),
                             ("/mm-meta", {"refine": long_q})):
            with self.subTest(path=path, param=next(iter(params))):
                r = self.client.get(path, params=params)
                self.assertEqual(422, r.status_code, f"{path} {r.text[:120]}")

    def test_cap_is_shared_by_all_three(self) -> None:
        """상한은 세 창구가 같은 값을 쓴다 — 한 곳만 고쳐져 어긋나지 않게."""
        self.assertEqual(routes_file_search._QUERY_MAX_LEN, routes_search._QUERY_MAX_LEN)
        self.assertEqual(routes_file_search._QUERY_MAX_LEN, routes_mm_meta._QUERY_MAX_LEN)

    def test_blank_query_on_search_is_422(self) -> None:
        r = self.client.get("/search", params={"q": "   "})
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("검색어", r.json()["detail"])

    def test_blank_query_on_file_search_is_browse(self) -> None:
        """파일 검색은 공백을 **훑기**로 본다 — 두 창구의 계약이 다르다(의도)."""
        with patch.object(routes_file_search, "browse_files", lambda *a, **k: dict(_FOUND)), \
             patch.object(routes_file_search, "get_current_settings", lambda: _CFG), \
             patch("src.search.opensearch_sync.get_client", lambda *a, **k: object()), \
             patch.object(db, "run_in_db", lambda cb: []):
            self.assertEqual(200, self.client.get("/file-search", params={"q": "   "}).status_code)


if __name__ == "__main__":
    unittest.main()
