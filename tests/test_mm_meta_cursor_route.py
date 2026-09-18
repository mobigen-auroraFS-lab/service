"""099 G2 — ``/mm-meta`` 개체 목록 **커서 순회 + 모수 총계** 라우트 계약(DB·검색엔진·임베딩 없음).

무엇을 봉인하나:
① **커서로 이어 읽는다** — 직전 응답의 ``next_cursor`` 를 그대로 되보내면 겹치지도 빠지지도 않고 이어진다.
   쪽 경계를 **동점 무더기 가운데**(같은 ``confirmed_count``)에 일부러 두어, 표기 키 tiebreak 이
   실제로 무더기를 가르는지 본다(``<`` 만 쓰면 통째로 잃고 ``<=`` 면 통째로 중복된다).
② **마지막 쪽은 ``next_cursor`` 가 ``None``** — 이번 쪽이 덜 찼으면 더 없다는 뜻이다. 빈 쪽을 한 번 더
   받으러 가지 않게 하는 097 과 같은 규율.
③ **깨진·어긋난 커서는 400**(500 아님) — 주소창의 커서 한 글자를 고친 것뿐인데 "서버 오류"가 뜨면 안 된다.
④ **``total``·``scope_total`` 은 모수**다 — 돌려준 개수가 아니다(200개만 받아 놓고 "200건"이라 적던 오해).
⑤ **커서 없는 기존 호출은 그대로** 동작한다(회귀).

⚠️ **099 G5 로 바뀐 것 둘**(G2 가 임시로 못 박았던 자리):
   - ``cursor`` + ``q`` 400 제약이 **풀렸다** — 정렬이 언제나 DB(구성 자산 수)라 이어 읽을 자리가 있다.
   - 좁히기(``refine``)가 **서버 질의**로 올라가 ``total`` 이 쪽 안의 수가 아니라 **모수**가 됐다.
   통합 집합 구조 자체(교집합·건수·엔진 실패)는 `test_mm_meta_set_search.py` 가 본다.
"""

from __future__ import annotations

import inspect
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app, routes_mm_meta
from src.search.cursor import encode_cursor
from src.search.entity_search_os import EntityMatchSet

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}

# 노출 개체 모수 대역 — 목록이 한 쪽만 돌려줘도 총계는 이 값이어야 한다(실측 dev 822 와 같은 뜻).
_TOTAL = 822


def _item(uid: str, count: int) -> dict[str, Any]:
    """정형된 목록 항목 대역(``shape_list_item`` 이 낸 모양)."""
    return {
        "entity_type": "장소", "entity_uid": uid, "name": uid, "source": "auto",
        "description": None, "confirmed_count": count, "total_count": count,
        "modalities": ["text"], "keywords": [uid], "topics": [], "forms": [], "areas": [],
    }


# 동점이 3건씩 뭉친 10건 — 쪽 크기 2·3 으로 끊으면 경계가 무더기 **가운데**에 걸린다.
_TABLE: list[dict[str, Any]] = sorted(
    (_item(f"e{i:02d}", 10 - i // 3) for i in range(10)),
    key=lambda r: (-int(r["confirmed_count"]), str(r["entity_uid"])),
)


def _allowed(uid_allow: set[tuple[str, str]] | None) -> list[dict[str, Any]]:
    """화이트리스트를 적용한 행들(정렬 순서 유지 · SQL 과 같은 규칙).

    Args:
        uid_allow: 개체 화이트리스트. 🔴 ``None`` = 조건 없음(전체) · 빈 집합 = **0건**.

    Returns:
        조건에 맞는 행 목록.
    """
    if uid_allow is None:
        return _TABLE
    return [r for r in _TABLE if (r["entity_type"], r["entity_uid"]) in uid_allow]


def _fake_fetch_list(
    _conn: object,
    *,
    entity_type: str | None = None,
    areas: list[str] | None = None,
    min_bundle_size: int = 0,
    limit: int = 200,
    after_tier: int | None = None,
    after_count: int | None = None,
    after_uid: str | None = None,
    after_type: str | None = None,
    uid_allow: set[tuple[str, str]] | None = None,
    uid_first: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """코어 keyset 목록 대역 — 실제 SQL 과 **같은 조건**으로 이어 읽는다.

    정렬은 **4단**(우선 티어 → 구성 자산 수 → 표기 키 → 종류)이라 책갈피도 네 값이다.
    마지막 단이 종류인 이유: 개체의 자연키가 (종류, 표기) 둘이라 표기만으로는 자리가 하나로
    정해지지 않는다(2026-09-18). 이 파일의 시험은 우선 대상을 쓰지 않으므로 티어는 늘 0 이다.

    Args:
        _conn: 커넥션 자리(쓰지 않는다).
        entity_type: 종류 필터(대역은 쓰지 않는다).
        areas: 갈래 필터(대역은 쓰지 않는다).
        min_bundle_size: 노출 임계(대역은 쓰지 않는다).
        limit: 이 쪽의 행 수.
        after_tier: 직전 쪽 마지막 개체의 우선 티어(099 G7).
        after_count: 직전 쪽 마지막 개체의 구성 자산 수.
        after_uid: 직전 쪽 마지막 개체의 표기 키.
        after_type: 직전 쪽 마지막 개체의 종류(표기까지 같은 자리를 가른다).
        uid_allow: 찾아오기·좁히기가 정한 개체 화이트리스트(099 G5).
        uid_first: 맨 앞에 세울 개체 집합(099 G7 · 순서만 바꾼다).

    Returns:
        정렬(티어 ↓ → 수 ↓ → 표기 키 ↑ → 종류 ↑) 기준 다음 ``limit`` 행.

    Raises:
        ValueError: 책갈피를 일부만 준 경우(코어와 같은 계약).
    """
    book = (after_tier, after_count, after_uid, after_type)
    if any(v is not None for v in book) and any(v is None for v in book):
        raise ValueError("이어읽기 책갈피는 네 값을 함께 줘야 한다")
    first = uid_first or set()
    rows = sorted(
        _allowed(uid_allow),
        key=lambda r: (-(1 if (r["entity_type"], r["entity_uid"]) in first else 0),
                       -int(r["confirmed_count"]), str(r["entity_uid"]),
                       str(r["entity_type"])))
    if after_count is not None:
        def _tier(row: dict[str, Any]) -> int:
            """행의 우선 티어(1=앞세운 개체 · 0=나머지).

            Args:
                row: 목록 행.

            Returns:
                티어 값.
            """
            return 1 if (row["entity_type"], row["entity_uid"]) in first else 0

        rows = [
            r for r in rows
            if _tier(r) < int(after_tier)
            or (_tier(r) == int(after_tier)
                and (int(r["confirmed_count"]) < int(after_count)
                     or (int(r["confirmed_count"]) == int(after_count)
                         and (str(r["entity_uid"]) > str(after_uid)
                              or (str(r["entity_uid"]) == str(after_uid)
                                  and str(r["entity_type"]) > str(after_type))))))
        ]
    return [dict(r) for r in rows[: int(limit)]]


def _cfg() -> SimpleNamespace:
    """``get_current_settings`` 대역 — 정형 계층이 읽는 필드만."""
    return SimpleNamespace(
        mm_meta=SimpleNamespace(search_backend="opensearch", semantic_gate_eps=0.15,
                                semantic_gate_enabled=True),
        embed=SimpleNamespace(api_model="bge-m3"),
    )


class _RouteCase(unittest.TestCase):
    """대역을 세운 라우트 호출 기반 케이스(DB 없음)."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.total_kwargs: dict[str, Any] = {}
        self.total_calls = 0
        self.engine_calls: list[str] = []

        def _fake_fetch_total(_conn: object, **kw: Any) -> int:
            """노출 개체 **모수** 대역 — 목록이 한 쪽만 줘도 이 값이 총계다.

            화이트리스트가 걸리면 그 안에서 센다(목록과 같은 조건이어야 "N건 중 M건"이 참말이 된다).
            대역 표는 10건뿐이라, 조건 없는 모수만 실측값(822)으로 흉내 낸다.
            """
            self.total_calls += 1
            self.total_kwargs.update(kw)
            allow = kw.get("uid_allow")
            return _TOTAL if allow is None else len(_allowed(allow))

        def _fake_match(_client: object, _index: str, **kw: Any) -> EntityMatchSet:
            """엔진 집합 판정 대역 — 표기 키에 질의 글자가 든 개체를 돌려준다(099 G5).

            실제 판정은 형태소(낱말) 단위이지만, 여기서 보는 것은 **집합이 어디에 쓰이는가**라
            대역은 단순 포함으로 충분하다. 매칭 규칙 자체는 코어 단위가 본다.
            """
            q = str(kw.get("query") or "")
            self.engine_calls.append(q)
            keys = frozenset((r["entity_type"], r["entity_uid"])
                             for r in _TABLE if q in r["entity_uid"])
            # 대역도 코어와 **같은 모양**을 돌려준다 — 갈래는 "전부 글자로 걸렸다"로 둔다
            # (「걸린 이유」 자체는 `test_mm_meta_match_reason.py` 가 본다).
            return EntityMatchSet(keys=keys, text_keys=keys, semantic_keys=frozenset(),
                                  semantic_gate_passed=False)

        for target, repl in (
            ("service.api._infra._run_in_db", lambda fn: fn(object())),
            ("service.api.routes_mm_meta.mm_meta.fetch_total", _fake_fetch_total),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_mm_meta.mm_meta.fetch_list", _fake_fetch_list),
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

    def _get(self, **params: Any) -> Any:
        """``/mm-meta`` 를 부르고 200 을 확인한 뒤 본문을 돌려준다."""
        resp = self.client.get("/mm-meta", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _drain(self, *, limit: int, **params: Any) -> list[str]:
        """커서를 따라 끝까지 훑어 표기 키를 순서대로 모은다."""
        uids: list[str] = []
        cursor: str | None = None
        for _ in range(50):  # 무한 루프 방지(10건을 쪽 크기 2~3 으로 훑는다)
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}), **params)
            uids.extend(i["entity_uid"] for i in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                return uids
        self.fail("커서가 끝나지 않는다 — 마지막 쪽에서 None 이 나와야 한다")
        return uids


class TestCursorPaging(_RouteCase):
    """T010 — ``cursor`` 를 받고 ``next_cursor`` 를 돌려준다."""

    def test_꽉_찬_쪽은_다음_커서를_준다(self) -> None:
        body = self._get(limit=3)
        self.assertEqual([i["entity_uid"] for i in body["items"]], ["e00", "e01", "e02"])
        self.assertTrue(body["next_cursor"], "꽉 찬 쪽이면 이어 읽을 커서를 줘야 한다")

    def test_커서로_이어_읽으면_동점_무더기_가운데서도_이어진다(self) -> None:
        first = self._get(limit=2)
        self.assertEqual([i["entity_uid"] for i in first["items"]], ["e00", "e01"])
        second = self._get(limit=2, cursor=first["next_cursor"])
        # e01·e02 는 같은 confirmed_count 다 — 표기 키로 갈라야 e02 가 살아남는다.
        self.assertEqual([i["entity_uid"] for i in second["items"]], ["e02", "e03"])

    def test_마지막_쪽은_다음_커서가_없다(self) -> None:
        body = self._get(limit=20)
        self.assertEqual(len(body["items"]), len(_TABLE))
        self.assertIsNone(body["next_cursor"], "덜 찬 쪽은 마지막 쪽이다")

    def test_커서_없는_응답에도_키가_있다(self) -> None:
        # 화면이 키 유무로 분기하지 않게 응답 모양을 한 가지로 유지한다.
        self.assertIn("next_cursor", self._get(limit=20))

    def test_깨진_커서는_400_이다(self) -> None:
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": "!!!깨짐!!!"})
        self.assertEqual(resp.status_code, 400, resp.text)

    def _scope(self) -> str:
        """조건 없는 목록 요청의 지문 재료(099 G7) — 위조 토큰도 여기까지는 맞춰야 한다.

        Returns:
            라우트가 만드는 것과 같은 지문 재료 문자열.
        """
        return routes_mm_meta.mm_meta.entity_cursor_scope(
            q=None, refine=None, entity_type=None, areas=[],
            min_bundle_size=routes_mm_meta._MIN_BUNDLE_SIZE)

    def test_다른_정렬로_만든_커서는_400_이다(self) -> None:
        token = encode_cursor("name_asc", [1, "e00"], scope=self._scope())
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": token})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_정렬값_개수가_다른_커서는_400_이다(self) -> None:
        # 구버전·위조 토큰. 통과시키면 반쪽 책갈피가 되어 첫 쪽을 다시 읽는다(중복).
        token = encode_cursor(routes_mm_meta.mm_meta.ENTITY_CURSOR_SORT, ["e00"],
                              scope=self._scope())
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": token})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_커서와_검색어를_함께_줄_수_있다(self) -> None:
        # 099 G5(T022a) — G2 가 임시로 막아 둔 제약을 푼다. 검색이 **집합 판정**이 되어 정렬이 언제나
        # DB(구성 자산 수)라, "그 다음부터"를 가리킬 자리가 생겼다.
        first = self._get(limit=3, q="e0")
        self.assertEqual([i["entity_uid"] for i in first["items"]], ["e00", "e01", "e02"])
        second = self._get(limit=3, q="e0", cursor=first["next_cursor"])
        self.assertEqual([i["entity_uid"] for i in second["items"]], ["e03", "e04", "e05"])


class TestParentTotals(_RouteCase):
    """T011 — ``total``·``scope_total`` 은 **모수**이고 항상 실린다."""

    def test_총계는_돌려준_개수가_아니라_모수다(self) -> None:
        body = self._get(limit=3)
        self.assertEqual(len(body["items"]), 3)
        # 🔴 종전에는 len(items) 라 화면에 "3건"(실측 화면에서는 200건)이 찍혔다.
        self.assertEqual(body["total"], _TOTAL)

    def test_scope_total_은_refine_이_없어도_실린다(self) -> None:
        body = self._get(limit=3)
        self.assertEqual(body["scope_total"], _TOTAL)

    def test_모수는_목록과_같은_조건으로_센다(self) -> None:
        # 조건이 다르면 "822건 중 3건"의 822 가 목록과 다른 모수를 말하게 된다.
        self._get(limit=3, entity_type="장소", areas="여행·명소,자연")
        self.assertEqual(self.total_kwargs["entity_type"], "장소")
        self.assertEqual(self.total_kwargs["areas"], ["여행·명소", "자연"])
        self.assertEqual(self.total_kwargs["min_bundle_size"], routes_mm_meta._MIN_BUNDLE_SIZE)

    def test_쪽을_넘겨도_총계는_그대로다(self) -> None:
        first = self._get(limit=3)
        second = self._get(limit=3, cursor=first["next_cursor"])
        self.assertEqual(second["total"], _TOTAL)
        self.assertEqual(second["scope_total"], _TOTAL)

    def test_좁히기가_걸리면_모수가_scope_total_이고_total_도_모수다(self) -> None:
        body = self._get(limit=10, refine="e00")
        self.assertEqual([i["entity_uid"] for i in body["items"]], ["e00"])
        self.assertEqual(body["scope_total"], _TOTAL, "지우면 몇 건인지가 scope_total 이다")
        # 🔴 099 G5 — 좁히기가 서버 질의로 올라가 total 이 **모수**가 됐다(종전에는 이 쪽 안에서
        #    좁힌 수라, 쪽을 넘기면 같은 좁히기인데 다른 수가 찍혔다).
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["refine"], "e00")

    def test_검색어가_있어도_총계는_모수다(self) -> None:
        # 🔴 099 G5 — 검색이 집합 판정이 되어 "조건에 맞는 전부"를 셀 수 있다(G2 까지는 돌려준
        #    개수였다). 한 쪽만 받아도 total 은 찾은 전부를 말한다.
        body = self._get(limit=3, q="e0")
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(body["total"], 10, "표기 키에 e0 가 든 개체 전부")
        self.assertEqual(body["scope_total"], 10, "좁히기가 없으면 둘이 같다")
        self.assertEqual(self.total_kwargs["uid_allow"],
                         {("장소", f"e{i:02d}") for i in range(10)},
                         "총계도 목록과 **같은 화이트리스트**로 센다")


class TestDrainAndRegression(_RouteCase):
    """T013 — 완주(중복 0·누락 0) · 순서 재현 · 커서 없는 기존 호출 회귀."""

    def test_커서로_완주하면_한_번에_받은_목록과_같다(self) -> None:
        whole = [i["entity_uid"] for i in self._get(limit=len(_TABLE))["items"]]
        self.assertEqual(self._drain(limit=2), whole, "쪽을 나눠도 같은 목록이어야 한다")
        self.assertEqual(self._drain(limit=3), whole, "쪽 크기를 바꿔도 같아야 한다")

    def test_완주_결과에_중복이_없다(self) -> None:
        walked = self._drain(limit=2)
        self.assertEqual(len(set(walked)), len(walked))
        self.assertEqual(len(walked), len(_TABLE), "누락도 없다")

    def test_두_번_순회해도_순서가_같다(self) -> None:
        # 헌법 3조(결정 재현성) — 같은 입력이면 같은 순서다.
        self.assertEqual(self._drain(limit=3), self._drain(limit=3))

    def test_좁히기가_걸려도_꼬리_개체까지_닿는다(self) -> None:
        # 🔴 종전에는 좁히기가 파이썬이라 1쪽에서 0건이 되면 커서가 끊겨 마지막 쪽의 e09 에 영영
        #    닿지 못했다. 이제는 SQL 이 좁히므로 첫 쪽에 바로 나온다(099 G5).
        self.assertEqual(self._drain(limit=2, refine="e09"), ["e09"])
        self.assertEqual(self.engine_calls, ["e09"], "좁히기도 엔진 집합 판정을 거친다")

    def test_커서_없는_기존_호출이_그대로_동작한다(self) -> None:
        # 회귀 — 프론트가 종전처럼 limit 만 주고 부르는 경우.
        body = self._get(limit=200)
        self.assertEqual([i["entity_uid"] for i in body["items"]],
                         [r["entity_uid"] for r in _TABLE])
        self.assertIsNone(body["next_cursor"])
        self.assertEqual(set(body) - {"items", "total", "scope_total", "next_cursor"}, set())

    def test_좁히기_응답에_적용한_글자가_되돌아온다(self) -> None:
        body = self._get(limit=200, refine="e01")
        self.assertEqual(body["refine"], "e01")
        self.assertEqual([i["entity_uid"] for i in body["items"]], ["e01"])


class TestLimitMeaning(unittest.TestCase):
    """T012 — 커서가 생겨 ``limit`` 의 뜻이 「전체 상한」에서 「한 쪽 크기」로 바뀐다."""

    def _limit_param(self) -> Any:
        """``limit`` 의 선언(Query 객체)을 돌려준다."""
        return inspect.signature(routes_mm_meta.list_mm_meta).parameters["limit"].default

    def test_설명이_한_쪽_크기를_말한다(self) -> None:
        # 문구가 옛 뜻("집계 상한")에 머물면 프론트가 limit 을 키워 전량을 받으려 한다 — 커서가 있으니
        # 더 볼 때는 쪽을 넘기면 된다.
        desc = str(self._limit_param().description)
        self.assertIn("한 쪽", desc)
        self.assertNotIn("집계 상한", desc)

    def test_범위와_기본값은_그대로다(self) -> None:
        # 🔴 동작 보존 — limit 없이 부르던 화면이 종전과 같은 쪽 크기를 받아야 한다.
        param = self._limit_param()
        bounds = {type(m).__name__: getattr(m, k) for m in param.metadata
                  for k in ("ge", "le") if hasattr(m, k)}
        self.assertEqual(bounds, {"Ge": 1, "Le": 500})
        self.assertEqual(param.default, 200)


if __name__ == "__main__":
    unittest.main()
