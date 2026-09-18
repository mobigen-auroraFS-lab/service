"""099 G7 — `/file-search` 커서에 **조건 지문**을 실어, 조건이 바뀐 커서를 거부한다.

무엇을 막나(실측 2026-09-17 · 실 OpenSearch): 전체 훑기에서 받은 커서를 ``modality=text`` 요청에
그대로 넣자 서버가 **200** 으로 답했고, 정상 1쪽의 첫 건이 통째로 빠졌다. 계약 주석에는 "조건이
바뀌면 cursor 는 버리고 처음부터"라고 **적혀만** 있었고 서버가 강제하지 않았다 — 프론트가 실수하면
오류 없이 **자료가 빠진다**. 도서관 비유로, 요리책에 꽂아 둔 책갈피를 역사책에 끼우고 "여기서부터
읽으세요"라고 답해 준 셈이다.

여기서 보는 것은 **배선**이다: 라우트가 "이번 결과 집합을 정의하는 것 전부"를 모아 코어로 넘기는가.
지문의 규약(만들기·대조·옛 토큰 거부)은 코어 단위(`tests/test_cursor*.py` · 코어 레포)가 본다.
그래서 대역 ``browse_files`` 는 **실제 코어 커서 함수**로 커서를 만들고 되읽는다 — 대역이 검사를
흉내내면 "배선이 맞는지"를 증명하지 못한다.

🔴 **과잉 거부도 결함이다**: 쪽 크기(limit)만 바꾸거나 칩을 고른 **순서**만 다른 요청은 결과 집합이
같으므로 이어 읽혀야 한다. 멀쩡한 순회가 400 으로 끊기면 무한 스크롤이 중간에 멈춘다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app
from src.search.cursor import decode_cursor, encode_cursor

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}

# 파일 정렬은 (정렬 필드, asset_id) 두 값이다 — 코어 ``SORT_OPTIONS`` 의 길이와 같은 값.
_ARITY = 2


def _passthrough_db(callback):
    """``_run_in_db`` 대역 — 가짜 conn 으로 즉시 실행(DB 불필요).

    Args:
        callback: 트랜잭션 안에서 돌 함수.

    Returns:
        콜백 반환값.
    """
    return callback(object())


class _ScopeRecorder:
    """``browse_files`` 대역 — **실제 코어 커서**로 되읽고 다음 커서를 만든다.

    대역이 하는 일은 둘뿐이다: ① 라우트가 준 ``scope`` 로 커서를 검증(어긋나면 코어가
    ``CursorError`` → 라우트가 400) ② 같은 ``scope`` 로 다음 커서를 발급.
    """

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def __call__(self, _client: Any, _index: str, *, sort: str, cursor: str | None = None,
                 scope: str, **_kw: Any) -> dict[str, Any]:
        """한 쪽을 훑은 척한다.

        Args:
            _client: 엔진 클라이언트(쓰지 않는다).
            _index: 색인 이름(쓰지 않는다).
            sort: 이번 요청의 정렬 이름.
            cursor: 이어 읽기 커서(없으면 첫 쪽).
            scope: 라우트가 모은 조건 지문 재료 — **이 테스트의 관심사**.
            **_kw: 나머지 인자(질의·필터·좁히기 등).

        Returns:
            라우트가 읽는 키만 채운 응답.

        Raises:
            CursorError: 커서의 지문·정렬·개수가 이번 요청과 어긋날 때(코어가 올린다).
        """
        self.scopes.append(scope)
        if cursor:
            decode_cursor(cursor, expect_sort=sort, expect_arity=_ARITY, expect_scope=scope)
        return {
            "rows": [{"asset_id": "a1", "modality": "text", "domain_label": "general",
                      "file_name": "김치.txt", "summary": "요약", "score": 0.0,
                      "tags": [], "topics": [], "subtopics": [], "topic_pairs": []}],
            "total": 1, "scope_total": 1, "total_capped": False,
            "facets": {"topic": [], "subtopic": [], "tag": [], "modality": []},
            "next_cursor": encode_cursor(sort, ["2026-09-11", "a1"], scope=scope),
            "size": 1, "sort": sort,
        }


class _Case(unittest.TestCase):
    """대역을 세운 라우트 호출 기반 케이스(DB·엔진·임베딩 없음)."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.browse = _ScopeRecorder()
        for target, repl in (
            ("service.api._infra._run_in_db", _passthrough_db),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_file_search.fetch_file_meta", lambda *_a, **_k: {}),
            ("src.search.opensearch_sync.get_client", lambda *_a, **_k: object()),
            ("service.api.routes_file_search.get_current_settings",
             lambda: SimpleNamespace(opensearch=SimpleNamespace(index="assets"))),
            ("service.api.routes_file_search.active_embed_channel", lambda: "text"),
            ("service.api.routes_file_search.embed_query_for_media_search",
             lambda *_a, **_k: [0.0]),
            ("service.api.routes_file_search.browse_files", self.browse),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def _scope_of(self, **params: Any) -> str:
        """그 조건으로 라우트가 만든 **지문 재료**를 그대로 받아 온다.

        ⚠️ 왜 깨진 커서를 한 번 던지나: 파일 창구는 검색어가 있으면 첫 쪽을 offset 경로
        (``search_files``)로 내보내 **책갈피를 주지 않는다**(097 설계 ①). 커서 경로를 타게 하려면
        커서가 있어야 하므로, 아무 값이나 넣어 대역까지 흘려보내고 **기록만** 가져온다(응답은 400).
        지문 재료를 테스트가 손으로 베끼면 라우트를 고칠 때 이 파일만 옛 규칙으로 남는다.

        Args:
            **params: 질의 파라미터.

        Returns:
            라우트가 코어에 넘긴 ``scope`` 문자열.
        """
        self.client.get("/file-search", params={"limit": 1, "cursor": "!!!깨짐!!!", **params})
        return self.browse.scopes[-1]

    def _first_cursor(self, **params: Any) -> str:
        """그 조건에서 **유효한** 커서를 만든다(정렬값은 아무 자리나 — 여기서 보는 것은 지문이다).

        Args:
            **params: 질의 파라미터(정렬을 생략하면 ``created_desc``).

        Returns:
            커서 문자열.
        """
        return encode_cursor(params.get("sort", "created_desc"), ["2026-09-11", "a1"],
                             scope=self._scope_of(**params))

    def _status(self, cursor: str, **params: Any) -> int:
        """커서를 끼워 다시 부른 뒤 상태 코드만 돌려준다.

        Args:
            cursor: 앞서 받은 커서.
            **params: 이번 요청의 질의 파라미터.

        Returns:
            HTTP 상태 코드.
        """
        return self.client.get(
            "/file-search", params={"limit": 1, "cursor": cursor, **params}).status_code


class 조건이_바뀐_커서는_거부(_Case):
    """🔴 실측 결함 A — 조용히 이어 주면 자료가 빠진다."""

    def test_칩_필터가_늘면_400(self) -> None:
        """실측 그대로: 전체 훑기 커서 → ``modality=text`` 요청."""
        token = self._first_cursor(sort="created_desc")
        self.assertEqual(self._status(token, sort="created_desc", modality="text"), 400)

    def test_검색어가_바뀌면_400(self) -> None:
        token = self._first_cursor(q="사찰", sort="created_desc")
        self.assertEqual(self._status(token, q="석탑", sort="created_desc"), 400)

    def test_좁히기가_바뀌면_400(self) -> None:
        token = self._first_cursor(q="한복", refine="배추", sort="created_desc")
        self.assertEqual(self._status(token, q="한복", refine="김치", sort="created_desc"), 400)

    def test_좁히기를_지우면_400(self) -> None:
        """집합이 **넓어지는** 쪽도 위험은 같다 — 이어 읽으면 앞부분을 통째로 건너뛴다."""
        token = self._first_cursor(q="한복", refine="배추", sort="created_desc")
        self.assertEqual(self._status(token, q="한복", sort="created_desc"), 400)

    def test_기간_조건이_바뀌면_400(self) -> None:
        token = self._first_cursor(sort="created_desc", created_from="2026-01-01")
        self.assertEqual(self._status(token, sort="created_desc", created_from="2026-02-01"), 400)

    def test_주제_칩이_바뀌면_400(self) -> None:
        token = self._first_cursor(sort="created_desc", topic="음식·요리")
        self.assertEqual(self._status(token, sort="created_desc", topic="여행·명소"), 400)


class 같은_집합이면_이어_읽는다(_Case):
    """🔴 과잉 거부 방지 — 결과 집합이 같은 요청은 끊기면 안 된다."""

    def test_같은_조건이면_200(self) -> None:
        token = self._first_cursor(q="한복", sort="created_desc", modality="text")
        self.assertEqual(self._status(token, q="한복", sort="created_desc", modality="text"), 200)

    def test_훑기_응답이_준_커서로_그대로_이어_읽는다(self) -> None:
        """🔴 서버가 준 책갈피를 서버가 되받지 못하면 무한 스크롤이 첫 쪽에서 멈춘다."""
        first = self.client.get("/file-search",
                                params={"limit": 1, "sort": "created_desc", "modality": "text"})
        self.assertEqual(first.status_code, 200, first.text)
        token = first.json()["next_cursor"]
        self.assertTrue(token, "꽉 찬 쪽이면 책갈피를 줘야 한다")
        self.assertEqual(self._status(token, sort="created_desc", modality="text"), 200)

    def test_쪽_크기만_달라도_200(self) -> None:
        """``limit`` 은 **한 번에 몇 줄 볼지**일 뿐 결과 집합을 바꾸지 않는다."""
        token = self._first_cursor(sort="created_desc")
        resp = self.client.get("/file-search",
                               params={"limit": 50, "cursor": token, "sort": "created_desc"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_칩을_고른_순서만_다르면_200(self) -> None:
        """같은 칸에서 고른 값들은 「또는」이라 순서가 결과를 바꾸지 않는다."""
        first = self.client.get("/file-search", params=[
            ("limit", "1"), ("sort", "created_desc"), ("topic", "음식·요리"), ("topic", "여행·명소")])
        self.assertEqual(first.status_code, 200, first.text)
        token = first.json()["next_cursor"]
        second = self.client.get("/file-search", params=[
            ("limit", "1"), ("sort", "created_desc"), ("cursor", token),
            ("topic", "여행·명소"), ("topic", "음식·요리")])
        self.assertEqual(second.status_code, 200, second.text)

    def test_검색어_앞뒤_공백은_같은_조건이다(self) -> None:
        token = self._first_cursor(q="한복", sort="created_desc")
        self.assertEqual(self._status(token, q="  한복 ", sort="created_desc"), 200)


class 지문_재료(_Case):
    """지문이 **무엇으로** 만들어지는지 — 재료가 빠지면 위 거부가 성립하지 않는다."""

    def test_조건이_다르면_지문_재료도_다르다(self) -> None:
        self.client.get("/file-search", params={"limit": 1, "sort": "created_desc"})
        self.client.get("/file-search",
                        params={"limit": 1, "sort": "created_desc", "modality": "text"})
        self.assertNotEqual(self.browse.scopes[0], self.browse.scopes[1])

    def test_지문_재료에_검색어와_좁히기가_들어_있다(self) -> None:
        """무엇이 재료인지 알 수 없으면 다음 사람이 필터를 늘릴 때 빠뜨린다."""
        scope = self._scope_of(q="한복", refine="배추", sort="created_desc")
        self.assertIn("한복", scope)
        self.assertIn("배추", scope)

    def test_쪽_크기는_지문_재료가_아니다(self) -> None:
        self.client.get("/file-search", params={"limit": 1, "sort": "created_desc"})
        self.client.get("/file-search", params={"limit": 50, "sort": "created_desc"})
        self.assertEqual(self.browse.scopes[0], self.browse.scopes[1])


if __name__ == "__main__":
    unittest.main()
