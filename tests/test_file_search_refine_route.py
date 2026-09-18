"""099 G3 — `/file-search` 의 결과 내 재검색을 **서버 질의로** 옮긴 것 봉인(DB·엔진 없음).

무엇이 바뀌었나: 091 은 코어가 돌려준 **이번 페이지 행 목록**을 파이썬 ``refine_rows`` 로 걸렀다.
50건을 받아 왔으면 나머지 수천 건은 처음부터 좁히기 대상이 아니었다는 뜻이다. 099 는 좁히기를
**검색 엔진 질의 절**로 내려보낸다 — 엔진이 색인 전체를 보므로 30쪽에 있던 자산도 걸린다(SC-004).

무엇을 봉인하나

① **라우트는 좁히기를 판정하지 않는다** — ``refine`` 을 코어로 넘기기만 한다. 파이썬으로 한 번 더
   거르면 엔진이 맞다고 한 행을 **글자가 다르다는 이유로 버린다**(낱말 단위 ↔ 부분 문자열 불일치).
② **두 경로 모두 넘긴다** — 얕은 페이지(offset)와 이어 읽기(cursor) 중 한쪽만 넘기면 화면이
   스크롤을 시작하는 순간 좁히기가 풀린다.
③ **건수는 모수다**(spec 099 §3-6) — ``scope_total``("지우면 N건") · ``total``("좁히면 M건") ·
   ``refine.shown``(이 쪽에 실린 행 수). 종전 ``refine.page_total`` 은 **이번 페이지의 행 수**라
   모수가 아니었다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app, routes_file_search

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}


def _passthrough_db(callback):
    """``_run_in_db`` 대역 — 가짜 conn 으로 즉시 실행(DB 불필요)."""
    return callback(object())


def _row(asset_id: str, *, file_name: str, summary: str, tags: list[str]) -> dict[str, Any]:
    """코어가 돌려주는 행 한 줄.

    Args:
        asset_id: 자산 id.
        file_name: 표시 파일명.
        summary: 요약.
        tags: 태그들.

    Returns:
        응답 행 dict.
    """
    return {"asset_id": asset_id, "modality": "text", "domain_label": "general",
            "file_name": file_name, "summary": summary, "score": 0.8, "tags": tags,
            "topics": [], "subtopics": [], "topic_pairs": []}


def _found(**over: Any) -> dict[str, Any]:
    """코어 조회 대역 결과(랭킹·커서 공용).

    Args:
        **over: 덮어쓸 키들.

    Returns:
        코어 응답 모양 dict.
    """
    base: dict[str, Any] = {
        "rows": [_row("a1", file_name="김치.txt", summary="요약", tags=["김치"])],
        "total": 3,
        "scope_total": 3,
        "total_capped": False,
        "facets": {"topic": [], "subtopic": [], "tag": [], "modality": []},
        "from": 0, "size": 50, "sort": "relevance", "next_cursor": None,
    }
    base.update(over)
    return base


class TestRefineGoesToTheEngine(unittest.TestCase):
    """좁히기 판정은 코어·엔진 몫이고 라우트는 넘기기만 한다."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, repl in (
            ("service.api._infra._run_in_db", _passthrough_db),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_file_search.fetch_file_meta", lambda *_a, **_k: {}),
            ("src.search.opensearch_sync.get_client", lambda *_a, **_k: object()),
            ("service.api.routes_file_search.get_current_settings",
             lambda: SimpleNamespace(opensearch=SimpleNamespace(index="assets"))),
            ("service.api.routes_file_search.active_embed_channel", lambda: "text"),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def test_파이썬_좁히기를_더_이상_쓰지_않는다(self) -> None:
        """🔴 남겨 두면 엔진이 맞다고 한 행을 라우트가 다시 버린다(매칭 규칙이 서로 다르다)."""
        self.assertFalse(hasattr(routes_file_search, "refine_rows"))
        self.assertFalse(hasattr(routes_file_search, "_refine_fields"))

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_얕은_페이지_경로가_refine_을_코어로_넘긴다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        self.client.get("/file-search", params={"q": "한식", "refine": "배추 김치"})
        self.assertEqual(mock_find.call_args.kwargs["refine"], "배추 김치")

    @patch("service.api.routes_file_search.browse_files")
    def test_이어_읽기_경로도_refine_을_코어로_넘긴다(self, mock_browse) -> None:
        """한쪽만 넘기면 스크롤을 시작하는 순간 좁히기가 풀린다."""
        mock_browse.return_value = _found(sort="created_desc")
        self.client.get("/file-search", params={"refine": "배추 김치"})
        self.assertEqual(mock_browse.call_args.kwargs["refine"], "배추 김치")

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_안_주면_없음으로_넘어간다(self, mock_find, mock_embed) -> None:
        """되돌림의 실질 — 파라미터가 없으면 좁히지 않는다."""
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={"q": "한식"}).json()
        self.assertIsNone(mock_find.call_args.kwargs["refine"])
        self.assertNotIn("refine", body)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_엔진이_준_행을_파이썬이_다시_거르지_않는다(self, mock_find, mock_embed) -> None:
        """🔴 회귀 방지의 핵심.

        엔진은 **낱말**로 맞춘다 — `김치를` 이라는 재검색어가 요약의 `김치` 에 맞는다(조사 제거).
        그런데 파이썬 부분 문자열로 다시 거르면 `김치를` 이라는 글자가 없어 **버려진다**.
        라우트가 한 번 더 거르지 않아야 두 판정이 어긋나지 않는다.
        """
        mock_find.return_value = _found(
            rows=[_row("a9", file_name="한식.txt", summary="김치 담그기", tags=[])], total=1)
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search",
                               params={"q": "한식", "refine": "김치를"}).json()
        self.assertEqual([r["asset_id"] for r in body["items"]], ["a9"])


class TestRefineCounts(unittest.TestCase):
    """건수의 뜻(spec §3-6) — 둘 다 **모수**이고 항상 실린다."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, repl in (
            ("service.api._infra._run_in_db", _passthrough_db),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_file_search.fetch_file_meta", lambda *_a, **_k: {}),
            ("src.search.opensearch_sync.get_client", lambda *_a, **_k: object()),
            ("service.api.routes_file_search.get_current_settings",
             lambda: SimpleNamespace(opensearch=SimpleNamespace(index="assets"))),
            ("service.api.routes_file_search.active_embed_channel", lambda: "text"),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_좁히기_전후_모수와_이_쪽_행수를_따로_낸다(self, mock_find, mock_embed) -> None:
        """"340건 중 12건" 이 무한 스크롤에서도 성립한다 — 둘 다 모수라서다."""
        mock_find.return_value = _found(
            rows=[_row("a1", file_name="가.txt", summary="요약", tags=[]),
                  _row("a2", file_name="나.txt", summary="요약", tags=[])],
            total=12, scope_total=340)
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search",
                               params={"q": "한식", "refine": "배추"}).json()
        self.assertEqual(body["total"], 12)          # 좁힌 뒤 모수
        self.assertEqual(body["scope_total"], 340)   # 지우면 340건
        self.assertEqual(body["refine"]["q"], "배추")
        self.assertEqual(body["refine"]["shown"], 2)  # 이 쪽에 실제로 실린 행 수
        self.assertNotIn("page_total", body["refine"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_좁히기가_없어도_scope_total_이_실린다(self, mock_find, mock_embed) -> None:
        """FR-006 — 항상 있어야 화면이 키 유무로 분기하지 않는다."""
        mock_find.return_value = _found(total=7, scope_total=7)
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={"q": "한식"}).json()
        self.assertEqual(body["scope_total"], 7)
        self.assertEqual(body["total"], 7)

    @patch("service.api.routes_file_search.browse_files")
    def test_이어_읽기_경로도_두_모수를_낸다(self, mock_browse) -> None:
        mock_browse.return_value = _found(total=12, scope_total=340, sort="created_desc")
        body = self.client.get("/file-search", params={"refine": "배추"}).json()
        self.assertEqual((body["total"], body["scope_total"]), (12, 340))


if __name__ == "__main__":
    unittest.main()
