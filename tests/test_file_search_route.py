"""096 — `/file-search` 라우트 계약 테스트(복수 선택 · 정렬 · 깊이 · 칩 상한).

무엇을 봉인하나:
① **주제·하위주제를 여럿 그대로 코어로** 넘긴다 — 종전에는 첫 값만 넘겼다(같은 칸 = 또는).
② **정렬은 닫힌 목록**이고 모르는 값은 422 다. 응답이 적용된 정렬을 되돌려 준다(화면이 그걸 보고 그린다).
③ **이름·등록일 정렬이면 질의 임베딩을 만들지 않는다** — 순서를 필드가 정하므로 뜻이 관여할 이유가 없다.
   모델 호출을 아끼는 것이 목적이라, 안 부르는지를 직접 본다.
④ **넘길 수 있는 깊이가 정렬마다 다르다** — 관련도는 이웃 탐색 깊이, 필드 정렬은 색인 결과창.
⑤ 되돌려 주는 ``filters`` 는 **실제 적용값**이다(공백·중복이 정리된 값이 정본).

DB·OpenSearch·임베딩 모델 없음 — 코어 조회를 대역으로 세운다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app, routes_file_search
from src.config.search_modalities import VALID_SEARCH_MODALITIES
from src.search.file_search import (
    ABOUT_BRANCH_DEFAULT,
    RANK_DEPTH_DEFAULT,
    SEMANTIC_MIN_COSINE_DEFAULT,
    SORT_DEPTH_DEFAULT,
    SORT_OPTIONS,
    WORD_OPERATOR_DEFAULT,
)
from src.search.file_search import (
    search_files as core_search_files,
)

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}


def _passthrough_db(callback):
    """``_run_in_db`` 대역 — 가짜 conn 으로 즉시 실행(DB 불필요)."""
    return callback(object())


def _found(**over: Any) -> dict[str, Any]:
    """코어 ``search_files`` 대역 결과."""
    base: dict[str, Any] = {
        "rows": [{"asset_id": "a1", "modality": "text", "domain_label": "general",
                  "file_name": "김치.txt", "summary": "요약", "score": 0.8,
                  "tags": ["김치"], "topics": ["음식·요리"], "subtopics": ["한식"],
                  "topic_pairs": []}],
        "total": 3,
        "total_capped": False,
        "facets": {"topic": [{"key": "음식·요리", "label": "음식·요리", "count": 3}],
                   "subtopic": [], "tag": [], "modality": []},
        "from": 0,
        "size": 50,
        "sort": "relevance",
    }
    base.update(over)
    return base


class TestFileSearchRoute(unittest.TestCase):
    """라우트 배선 — 대역 조회로 계약만 본다."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, repl in (
            ("service.api._infra._run_in_db", _passthrough_db),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_file_search.fetch_file_meta", lambda *_a, **_k: {}),
            ("src.search.opensearch_sync.get_client", lambda *_a, **_k: object()),
            # 설정 미초기화 상태로 도는 단위 테스트다 — 색인 이름·임베딩 채널만 대역으로 준다.
            ("service.api.routes_file_search.get_current_settings",
             lambda: SimpleNamespace(opensearch=SimpleNamespace(index="assets"))),
            ("service.api.routes_file_search.active_embed_channel", lambda: "text"),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def test_코어_함수를_그대로_참조한다(self) -> None:
        # 서비스가 자기 사본을 들고 있으면 코어 수정이 화면에 반영되지 않는다.
        self.assertIs(routes_file_search.search_files, core_search_files)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_주제_하위주제를_여럿_그대로_넘긴다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={
            "q": "김치", "topic": ["음악", "미술"], "subtopic": ["가수", "화가"],
        }).json()
        filters = mock_find.call_args.kwargs["filters"]
        self.assertEqual(filters.topics, ("음악", "미술"))
        self.assertEqual(filters.subtopics, ("가수", "화가"))
        # 되돌려 주는 값도 실제 적용값이다.
        self.assertEqual(body["filters"]["topic"], ["음악", "미술"])
        self.assertEqual(body["filters"]["subtopic"], ["가수", "화가"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_적용값이_정리된_뒤_되돌려진다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={
            "q": "김치", "topic": ["음악", " 음악 ", "  ", "미술"],
        }).json()
        self.assertEqual(body["filters"]["topic"], ["음악", "미술"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_기본_정렬은_관련도이고_응답이_되돌려_준다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={"q": "김치"}).json()
        self.assertEqual(mock_find.call_args.kwargs["sort"], "relevance")
        self.assertEqual(body["sort"], "relevance")

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_모든_정렬이_코어로_전달된다(self, mock_find, mock_embed) -> None:
        mock_embed.return_value = [0.0]
        for name in SORT_OPTIONS:
            mock_find.return_value = _found(sort=name)
            body = self.client.get("/file-search", params={"q": "김치", "sort": name}).json()
            self.assertEqual(mock_find.call_args.kwargs["sort"], name)
            self.assertEqual(body["sort"], name)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_어느_정렬이든_임베딩을_만든다(self, mock_find, mock_embed) -> None:
        # 🔴 뜻이 **집합 판정**에 쓰이므로(코사인 하한 이상이면 글자가 안 겹쳐도 집합에 든다)
        #    정렬과 무관하게 임베딩이 필요하다. 정렬에 따라 개수가 달라지면 화면이 거짓말을 한다.
        mock_embed.return_value = [0.3]
        for name in SORT_OPTIONS:
            mock_find.return_value = _found(sort=name)
            self.client.get("/file-search", params={"q": "김치", "sort": name})
            self.assertEqual(mock_find.call_args.kwargs["query_vector"], [0.3], name)
        self.assertEqual(mock_embed.call_count, len(SORT_OPTIONS))

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_관련도_정렬은_임베딩을_만든다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.1]
        self.client.get("/file-search", params={"q": "김치"})
        mock_embed.assert_called_once()
        self.assertEqual(mock_find.call_args.kwargs["query_vector"], [0.1])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_종류_필터가_여럿_그대로_넘어간다(self, mock_find, mock_embed) -> None:
        # 멀티모달 검색이 결과를 버킷으로 나눠 보이던 것을 칩 축으로 대신한다.
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={
            "q": "김치", "modality": ["text", "video"]}).json()
        self.assertEqual(mock_find.call_args.kwargs["filters"].modalities, ("text", "video"))
        self.assertEqual(body["filters"]["modality"], ["text", "video"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_적용값을_전부_되돌려_준다(self, mock_find, mock_embed) -> None:
        # 헌법 3조 재현성 — 서버 설정이 나중에 바뀌어도 이 응답이 왜 이렇게 나왔는지 알 수 있어야 한다.
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        applied = self.client.get("/file-search", params={"q": "김치"}).json()["applied"]
        self.assertEqual(applied["word_operator"], WORD_OPERATOR_DEFAULT)
        self.assertEqual(applied["semantic_min_cosine"], SEMANTIC_MIN_COSINE_DEFAULT)
        self.assertIs(applied["about_branch"], ABOUT_BRANCH_DEFAULT)
        self.assertEqual(applied["rank_depth"], RANK_DEPTH_DEFAULT)
        self.assertEqual(applied["sort_depth"], SORT_DEPTH_DEFAULT)
        # 값이 하나라도 빠지면 재현이 안 된다 — 키 집합을 못 박는다.
        self.assertEqual(set(applied), {
            "word_operator", "semantic_min_cosine", "semantic_cap", "about_branch",
            "total_cap", "rank_depth", "sort_depth", "facet_size", "facet_show",
            "search_pipeline"})

    def test_공백만인_검색어는_422(self) -> None:
        # 입력 오류는 전부 422 로 통일 — 종전엔 코어 ValueError 경로로 400 이 되어 어긋났다.
        resp = self.client.get("/file-search", params={"q": "   "})
        self.assertEqual(resp.status_code, 422)

    def test_반복_파라미터_개수_상한(self) -> None:
        many = [f"주제{i}" for i in range(routes_file_search._REPEAT_MAX + 1)]
        resp = self.client.get("/file-search", params={"q": "김치", "topic": many})
        self.assertEqual(resp.status_code, 422)
        self.assertIn(str(routes_file_search._REPEAT_MAX), resp.json()["detail"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_날짜_되돌림은_실제_적용값이다(self, mock_find, mock_embed) -> None:
        # 원문은 공백·시각을 포함할 수 있는데 적용은 날짜까지만 — 원문을 되돌리면 "안 걸린 조건이
        # 걸린 것처럼" 보인다(리뷰 2026-09-09).
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        body = self.client.get("/file-search", params={
            "q": "김치", "created_from": "2026-01-01T15:30:00+09:00", "created_to": "   "}).json()
        self.assertEqual(body["filters"]["created_from"], "2026-01-01")
        self.assertIsNone(body["filters"]["created_to"], "공백은 필터가 아니므로 None 이어야 한다")

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    def test_임베딩_실패는_임베딩_503(self, mock_embed) -> None:
        # 임베딩 서버 장애와 검색 엔진 장애는 다른 원인 — 문구로 구분한다.
        mock_embed.side_effect = RuntimeError("임베딩 API 호출 실패")
        resp = self.client.get("/file-search", params={"q": "김치"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("임베딩", resp.json()["detail"])

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_코드_결함은_503으로_둔갑하지_않는다(self, mock_find, mock_embed) -> None:
        # 연결 실패만 503 — 그 밖의 예외는 그대로 올라가 전역 핸들러가 500 으로 구분한다
        # (_infra.os_unavailable_handler 가 정한 원칙 · 리뷰 2026-09-09).
        mock_embed.return_value = [0.0]
        mock_find.side_effect = KeyError("코드 결함 흉내")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/file-search", params={"q": "김치"})
        self.assertEqual(resp.status_code, 500)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_권한이_못_보는_요약은_키가_빠진다(self, mock_find, mock_embed) -> None:
        # 🔴 등급표를 빈 dict 로 대역하면 미등록 키 통과 규칙 때문에 가리기가 켜져도 꺼져도 통과한다.
        #    실제 등급을 넣어야 가리기가 검증된다(리뷰 2026-09-09). regulated 는 최상위 등급이라
        #    인증 꺼짐 모드의 개발 principal 은 반드시 그 아래다.
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        with patch("service.portal.access_project.fetch_access_tiers",
                   return_value={"summary": "regulated"}):
            body = self.client.get("/file-search", params={"q": "김치"}).json()
        self.assertNotIn("summary", body["items"][0], "권한이 못 보는 요약이 그대로 나갔다")
        self.assertEqual(body["items"][0]["asset_id"], "a1", "가리기가 행 자체를 지우면 안 된다")
        # 등급표가 비면(미등록) 종전처럼 통과한다 — 대역 방식에 따라 결과가 갈림을 함께 봉인.
        body = self.client.get("/file-search", params={"q": "김치"}).json()
        self.assertEqual(body["items"][0]["summary"], "요약")

    def test_모르는_종류는_422(self) -> None:
        # 닫힌 어휘다. 조용히 0건으로 넘기면 오타(`문서`·`텍스트`)를 "그런 파일이 없다"로 읽게 된다.
        resp = self.client.get("/file-search", params={"q": "김치", "modality": "문서"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("종류", resp.json()["detail"])
        for good in VALID_SEARCH_MODALITIES:
            self.assertIn(good, resp.json()["detail"], "허용값을 알려 주지 않는다")

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_허용된_종류는_통과한다(self, mock_find, mock_embed) -> None:
        mock_find.return_value = _found()
        mock_embed.return_value = [0.0]
        for good in VALID_SEARCH_MODALITIES:
            resp = self.client.get("/file-search", params={"q": "김치", "modality": good})
            self.assertEqual(resp.status_code, 200, good)

    def test_모르는_정렬은_422(self) -> None:
        resp = self.client.get("/file-search", params={"q": "김치", "sort": "크기순"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("정렬", resp.json()["detail"])

    def test_깊이_한계는_정렬마다_다르다(self) -> None:
        # 관련도는 이웃 탐색 깊이에서 막히고, 필드 정렬은 그보다 깊이 넘길 수 있다.
        deep = self.client.get("/file-search", params={
            "q": "김치", "offset": RANK_DEPTH_DEFAULT, "limit": 10})
        self.assertEqual(deep.status_code, 400)
        self.assertIn("이름·등록일 정렬", deep.json()["detail"])
        with patch("service.api.routes_file_search.search_files") as mock_find, \
             patch("service.api.routes_file_search.embed_query_for_media_search",
                   return_value=[0.1]):
            mock_find.return_value = _found(sort="name_asc", **{"from": RANK_DEPTH_DEFAULT})
            ok = self.client.get("/file-search", params={
                "q": "김치", "sort": "name_asc", "offset": RANK_DEPTH_DEFAULT, "limit": 10})
        self.assertEqual(ok.status_code, 200)
        too_deep = self.client.get("/file-search", params={
            "q": "김치", "sort": "name_asc", "offset": SORT_DEPTH_DEFAULT, "limit": 10})
        self.assertEqual(too_deep.status_code, 400)

    @patch("service.api.routes_file_search.embed_query_for_media_search")
    @patch("service.api.routes_file_search.search_files")
    def test_칩은_화면_상한까지만_실린다(self, mock_find, mock_embed) -> None:
        mock_embed.return_value = [0.0]
        many = [{"key": f"t{i}", "label": f"t{i}", "count": 30 - i} for i in range(24)]
        mock_find.return_value = _found(facets={"topic": many, "subtopic": [], "tag": []})
        body = self.client.get("/file-search", params={"q": "김치"}).json()
        self.assertEqual(len(body["facets"]["topic"]), routes_file_search._FACET_SHOW)
        # 순서는 코어가 준 그대로(건수 내림차순)여야 한다 — 자르기만 한다.
        self.assertEqual(body["facets"]["topic"][0]["key"], "t0")


if __name__ == "__main__":
    unittest.main()
