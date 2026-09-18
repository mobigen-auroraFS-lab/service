"""099 후속 — 검색 결과 항목의 **「걸린 이유」**(2026-09-17 사용자 결정 · DB·엔진·임베딩 없음).

무엇을 봉인하나

① **뜻(kNN)으로 걸린 결과는 화면 어디에도 검색어가 보이지 않는다.** `왕실 무덤` 으로 찾으면
   `영릉` 이 나오는데 그 카드에는 "왕실 무덤" 이라는 글자가 한 자도 없다 → 근거를 함께 싣지 않으면
   사용자는 "왜 이게 나오지, 검색이 고장났나"로 읽는다. 089·090·092 가 공들여 만든 설명 가능성이고,
   G5 가 집합 판정으로 갈아타며 잃었던 것을 되살린다.
② **불린이 실질이고 문구는 표시용**이다 — 화면은 ``by_text``·``by_semantic`` 으로 갈라 보고,
   ``match_reason`` 은 그대로 찍기만 한다. 문구를 파싱해 층을 가르면 문구를 고칠 때 조용히 깨진다.
③ **문구는 코어 상수 그대로**다(``REASON_TEXT_MATCH``·``REASON_SEMANTIC``). 백엔드가 문구를 새로
   만들면 같은 사실이 화면마다 다르게 적힌다.
④ **검색 경로 전용**이다 — 검색어도 좁히기도 없는 목록에는 이 키들을 싣지 않는다(걸린 이유가
   없는데 "글자 일치"라고 적을 수는 없다).
⑤ **``q`` 와 ``refine`` 이 둘 다 있을 때의 합침 규칙**(보수적) — ``by_text`` 는 **모든** 질의에서
   글자로 걸렸을 때만 참(교집합), ``by_semantic`` 은 **어느 한 질의에서라도** 뜻으로 걸리면 참
   (합집합). 근거는 ``mm_meta.search_and_refine`` 주석에 있다.

⚠️ 매칭 규칙·게이트·임계는 이 파일의 관심사가 아니다(코어 단위가 본다). 여기서 보는 것은
**이미 계산된 갈래를 화면까지 나르는가** 하나다 — 엔진 왕복은 늘지 않는다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app
from service.portal import mm_meta
from src.mm_meta.entity_search import REASON_SEMANTIC, REASON_TEXT_MATCH
from src.search.entity_search_os import EntityMatchSet

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}
_TYPE = "장소"


def _key(uid: str) -> tuple[str, str]:
    """개체 키 ``(종류, 표기 키)``.

    Args:
        uid: 표기 키.

    Returns:
        개체 자연키 두 값.
    """
    return (_TYPE, uid)


def _row(uid: str, count: int) -> dict[str, Any]:
    """코어 ``list_entities`` 행 대역 하나.

    Args:
        uid: 표기 키(이름으로도 쓴다).
        count: 구성 자산 수(정렬 자리).

    Returns:
        코어 목록 행 모양의 dict.
    """
    return {
        "entity_type": _TYPE, "entity_uid": uid, "node_id": uid, "name": uid,
        "source": "auto", "description": None, "confirmed_count": count, "total_count": count,
        "modalities": ["text"], "keywords": [uid], "topics": [], "forms": [], "areas": [],
    }


# 정렬(구성 자산 수 내림차순 → 표기 키 오름차순) 그대로의 순서.
_TABLE = [_row("영릉", 9), _row("숭례문", 8), _row("석굴암", 7)]


def _cfg() -> SimpleNamespace:
    """``get_current_settings`` 대역 — 정형 계층이 읽는 필드만."""
    return SimpleNamespace(
        mm_meta=SimpleNamespace(search_backend="opensearch", semantic_gate_eps=0.15,
                                semantic_gate_enabled=True),
        embed=SimpleNamespace(api_model="bge-m3"),
    )


def _match(text: set[tuple[str, str]], semantic: set[tuple[str, str]] = frozenset()
           ) -> EntityMatchSet:
    """코어 집합 판정 결과 대역(갈래 둘을 직접 정한다).

    Args:
        text: ① 낱말(BM25) 갈래로 걸린 키들.
        semantic: ② 의미(kNN) 갈래로 걸린 키들. 비우면 게이트에 막힌 것과 같은 모양이다.

    Returns:
        ``EntityMatchSet`` — ``keys`` 는 코어와 같이 두 갈래의 합집합.
    """
    return EntityMatchSet(keys=frozenset(text) | frozenset(semantic),
                          text_keys=frozenset(text), semantic_keys=frozenset(semantic),
                          semantic_gate_passed=bool(semantic))


class TestReasonWording(unittest.TestCase):
    """③ 문구는 **코어 상수 그대로** — 백엔드에 사본 문자열이 없다."""

    def test_문구를_코어에서_그대로_가져온다(self) -> None:
        self.assertIs(mm_meta.REASON_TEXT_MATCH, REASON_TEXT_MATCH)
        self.assertIs(mm_meta.REASON_SEMANTIC, REASON_SEMANTIC)


class TestAttachMatchReason(unittest.TestCase):
    """① ② ④ 순수 조립 — 항목에 갈래를 얹는다(검색 경로 전용)."""

    def _items(self) -> list[dict[str, Any]]:
        """정형된 목록 항목 세 개."""
        return [mm_meta.shape_list_item(r) for r in _TABLE]

    def test_검색이_없으면_키를_싣지_않는다(self) -> None:
        """④ 걸린 이유가 없는데 "글자 일치"라고 적을 수는 없다(목록 경로는 종전 그대로)."""
        scope = mm_meta.EntityScope(uid_allow=None, scope_allow=None, refined=False)
        for item in mm_meta.attach_match_reason(self._items(), scope=scope):
            for absent in ("match_reason", "by_text", "by_semantic"):
                self.assertNotIn(absent, item)

    def test_글자로_걸린_항목은_글자_일치다(self) -> None:
        scope = mm_meta.EntityScope(
            uid_allow={_key("영릉")}, scope_allow={_key("영릉")}, refined=False,
            text_keys=frozenset({_key("영릉")}), semantic_keys=frozenset())
        [item] = mm_meta.attach_match_reason([self._items()[0]], scope=scope)
        self.assertTrue(item["by_text"])
        self.assertFalse(item["by_semantic"])
        self.assertEqual(item["match_reason"], REASON_TEXT_MATCH)

    def test_뜻으로만_걸린_항목은_뜻이_가까움이다(self) -> None:
        """🔴 이것이 이 기능의 존재 이유다 — 카드에 검색어가 없는 바로 그 경우."""
        scope = mm_meta.EntityScope(
            uid_allow={_key("영릉")}, scope_allow={_key("영릉")}, refined=False,
            text_keys=frozenset(), semantic_keys=frozenset({_key("영릉")}))
        [item] = mm_meta.attach_match_reason([self._items()[0]], scope=scope)
        self.assertFalse(item["by_text"])
        self.assertTrue(item["by_semantic"])
        self.assertEqual(item["match_reason"], REASON_SEMANTIC)

    def test_둘_다면_글자를_앞세우되_뜻도_함께_남긴다(self) -> None:
        """② 불린이 실질이다 — 문구는 하나를 골라야 하지만 사실은 둘 다 참일 수 있다."""
        scope = mm_meta.EntityScope(
            uid_allow={_key("영릉")}, scope_allow={_key("영릉")}, refined=False,
            text_keys=frozenset({_key("영릉")}), semantic_keys=frozenset({_key("영릉")}))
        [item] = mm_meta.attach_match_reason([self._items()[0]], scope=scope)
        self.assertTrue(item["by_text"])
        self.assertTrue(item["by_semantic"])
        self.assertEqual(item["match_reason"], REASON_TEXT_MATCH, "더 확실한 근거를 앞세운다")

    def test_항목마다_따로_판정한다(self) -> None:
        """한 쪽 안에 글자로 걸린 것과 뜻으로 걸린 것이 섞여 있는 것이 정상이다."""
        scope = mm_meta.EntityScope(
            uid_allow={_key("영릉"), _key("숭례문")}, scope_allow=None, refined=False,
            text_keys=frozenset({_key("숭례문")}), semantic_keys=frozenset({_key("영릉")}))
        got = {i["entity_uid"]: i["match_reason"]
               for i in mm_meta.attach_match_reason(self._items()[:2], scope=scope)}
        self.assertEqual(got, {"영릉": REASON_SEMANTIC, "숭례문": REASON_TEXT_MATCH})

    def test_원본_항목을_바꾸지_않는다(self) -> None:
        """순수 조립 — 같은 목록을 두 번 얹어도 결과가 달라지지 않는다."""
        items = self._items()[:1]
        scope = mm_meta.EntityScope(
            uid_allow={_key("영릉")}, scope_allow=None, refined=False,
            text_keys=frozenset({_key("영릉")}), semantic_keys=frozenset())
        mm_meta.attach_match_reason(items, scope=scope)
        self.assertNotIn("match_reason", items[0])

    def test_둘_중_하나는_언제나_참이다(self) -> None:
        """결과 집합에 든 개체는 **어느 갈래로든** 걸려서 들어온 것이다(둘 다 거짓이면 모순).

        집합이 두 갈래의 합집합이라 성립한다 — 어느 쪽에도 없으면 애초에 결과에 없다.
        """
        scope = mm_meta.EntityScope(
            uid_allow={_key(r["entity_uid"]) for r in _TABLE}, scope_allow=None, refined=False,
            text_keys=frozenset({_key("숭례문")}),
            semantic_keys=frozenset({_key("영릉"), _key("석굴암")}))
        for item in mm_meta.attach_match_reason(self._items(), scope=scope):
            self.assertTrue(item["by_text"] or item["by_semantic"], item["entity_uid"])


class TestScopeMerge(unittest.TestCase):
    """⑤ ``q`` + ``refine`` 의 갈래 합침 — **보수적**으로 고른다."""

    def setUp(self) -> None:
        self.answers: dict[str, EntityMatchSet] = {}
        for target, repl in (
            ("service.portal.mm_meta.match_entity_keys",
             lambda *_a, **kw: self.answers[str(kw["query"])]),
            ("service.portal.mm_meta.embed_query_for_media_search", lambda *_a, **_k: [0.0]),
            ("service.portal.mm_meta.active_embed_channel", lambda: "st_api"),
            ("service.portal.mm_meta.get_current_settings", _cfg),
            ("src.search.opensearch_sync.get_client", lambda: object()),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)

    def test_양쪽_다_글자로_걸려야_글자_일치다(self) -> None:
        self.answers = {"조선": _match({_key("영릉")}), "왕릉": _match({_key("영릉")})}
        scope = mm_meta.search_and_refine(q="조선", refine="왕릉")
        self.assertIn(_key("영릉"), scope.text_keys)
        self.assertNotIn(_key("영릉"), scope.semantic_keys)

    def test_한쪽이라도_뜻이면_뜻으로_본다(self) -> None:
        """🔴 보수적 선택 — 한 질의라도 뜻으로 걸렸으면 그 낱말은 카드에 없다.
        "글자 일치"라고 적으면 거짓말이 되므로 설명이 필요한 쪽으로 기운다."""
        self.answers = {"조선": _match({_key("영릉")}),
                        "왕실 무덤": _match(set(), {_key("영릉")})}
        scope = mm_meta.search_and_refine(q="조선", refine="왕실 무덤")
        self.assertNotIn(_key("영릉"), scope.text_keys)
        self.assertIn(_key("영릉"), scope.semantic_keys)

    def test_한쪽만_물었으면_그_갈래_그대로다(self) -> None:
        self.answers = {"왕실 무덤": _match({_key("숭례문")}, {_key("영릉")})}
        scope = mm_meta.search_and_refine(q="왕실 무덤", refine=None)
        self.assertEqual(scope.text_keys, frozenset({_key("숭례문")}))
        self.assertEqual(scope.semantic_keys, frozenset({_key("영릉")}))

    def test_안_물었으면_갈래도_비어_있다(self) -> None:
        scope = mm_meta.search_and_refine(q=None, refine=None)
        self.assertEqual(scope.text_keys, frozenset())
        self.assertEqual(scope.semantic_keys, frozenset())


class TestRouteCarriesReason(unittest.TestCase):
    """① ④ 라우트까지 — 검색 응답 항목에 실리고, 목록 응답에는 없다."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.answers: dict[str, EntityMatchSet] = {}
        self.engine_calls: list[str] = []

        def _fake_match(*_a: Any, **kw: Any) -> EntityMatchSet:
            """엔진 집합 판정 대역 — 질의별로 미리 정한 갈래를 돌려준다."""
            self.engine_calls.append(str(kw["query"]))
            return self.answers[str(kw["query"])]

        def _fake_list(_conn: object, **kw: Any) -> list[dict[str, Any]]:
            """코어 ``list_entities`` 대역 — 화이트리스트와 책갈피를 SQL 과 같은 규칙으로 흉내 낸다."""
            allow = kw.get("uid_allow")
            rows = [r for r in _TABLE
                    if allow is None or (r["entity_type"], r["entity_uid"]) in allow]
            after_count, after_uid = kw.get("after_count"), kw.get("after_uid")
            if after_count is not None:
                rows = [r for r in rows
                        if int(r["confirmed_count"]) < int(after_count)
                        or (int(r["confirmed_count"]) == int(after_count)
                            and str(r["entity_uid"]) > str(after_uid))]
            return [dict(r) for r in rows[: int(kw.get("limit") or 200)]]

        for target, repl in (
            ("service.api._infra._run_in_db", lambda fn: fn(object())),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.portal.mm_meta.list_entities", _fake_list),
            ("service.portal.mm_meta.count_entities", lambda _conn, **_kw: 3),
            ("service.portal.mm_meta.match_entity_keys", _fake_match),
            ("service.portal.mm_meta.embed_query_for_media_search", lambda *_a, **_k: [0.0]),
            ("service.portal.mm_meta.active_embed_channel", lambda: "st_api"),
            ("service.portal.mm_meta.get_current_settings", _cfg),
            ("src.search.opensearch_sync.get_client", lambda: object()),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def _get(self, **params: Any) -> dict[str, Any]:
        """``/mm-meta`` 를 부르고 200 을 확인한 뒤 본문을 돌려준다."""
        resp = self.client.get("/mm-meta", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_뜻으로_걸린_항목에_근거가_실린다(self) -> None:
        """🔴 `왕실 무덤` → `영릉`. 카드에 검색어가 없으니 이 세 키가 화면의 유일한 설명이다."""
        self.answers = {"왕실 무덤": _match(set(), {_key("영릉")})}
        [item] = self._get(q="왕실 무덤", limit=200)["items"]
        self.assertEqual(item["entity_uid"], "영릉")
        self.assertFalse(item["by_text"])
        self.assertTrue(item["by_semantic"])
        self.assertEqual(item["match_reason"], REASON_SEMANTIC)

    def test_글자로_걸린_항목도_근거가_실린다(self) -> None:
        self.answers = {"숭례문": _match({_key("숭례문")})}
        [item] = self._get(q="숭례문", limit=200)["items"]
        self.assertTrue(item["by_text"])
        self.assertEqual(item["match_reason"], REASON_TEXT_MATCH)

    def test_좁히기만_있어도_근거가_실린다(self) -> None:
        """좁히기도 같은 엔진 판정이라 갈래가 있다(099 §3-2a — 한 구조)."""
        self.answers = {"왕실 무덤": _match(set(), {_key("영릉")})}
        [item] = self._get(refine="왕실 무덤", limit=200)["items"]
        self.assertTrue(item["by_semantic"])

    def test_검색이_없는_목록에는_실리지_않는다(self) -> None:
        """④ 검색 경로 전용 — 목록 경로 항목은 종전 모양 그대로다."""
        body = self._get(limit=200)
        self.assertEqual(len(body["items"]), 3)
        for item in body["items"]:
            for absent in ("match_reason", "by_text", "by_semantic"):
                self.assertNotIn(absent, item)
        self.assertEqual(self.engine_calls, [], "안 물었으면 엔진도 부르지 않는다")

    def test_근거를_실으려고_질의를_더_던지지_않는다(self) -> None:
        """이미 계산된 갈래를 나를 뿐이다 — 질의당 한 번씩 그대로."""
        self.answers = {"조선": _match({_key("영릉")}), "왕실 무덤": _match(set(), {_key("영릉")})}
        self._get(q="조선", refine="왕실 무덤", limit=200)
        self.assertEqual(self.engine_calls, ["조선", "왕실 무덤"])

    def test_커서로_이어_읽어도_근거가_실린다(self) -> None:
        """쪽이 넘어가도 같은 판정 집합을 보므로 근거가 사라지지 않는다."""
        self.answers = {"왕실": _match({_key("숭례문")}, {_key("영릉")})}
        first = self._get(q="왕실", limit=1)
        self.assertEqual(first["items"][0]["match_reason"], REASON_SEMANTIC)
        second = self._get(q="왕실", limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["items"][0]["entity_uid"], "숭례문")
        self.assertEqual(second["items"][0]["match_reason"], REASON_TEXT_MATCH)


if __name__ == "__main__":
    unittest.main()
