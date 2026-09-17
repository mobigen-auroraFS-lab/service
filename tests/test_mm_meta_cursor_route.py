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

🔴 이번 범위(G2)는 **검색어 없는 목록 경로와 총계**뿐이다. ``q`` 가 있는 경로는 현행 그대로이며
   그 사실 자체를 테스트로 못 박는다(G4 에서 열린다).
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


def _fake_fetch_list(
    _conn: object,
    *,
    entity_type: str | None = None,
    areas: list[str] | None = None,
    min_bundle_size: int = 0,
    limit: int = 200,
    after_count: int | None = None,
    after_uid: str | None = None,
) -> list[dict[str, Any]]:
    """코어 keyset 목록 대역 — 실제 SQL 과 **같은 조건**으로 이어 읽는다.

    Args:
        _conn: 커넥션 자리(쓰지 않는다).
        entity_type: 종류 필터(대역은 쓰지 않는다).
        areas: 갈래 필터(대역은 쓰지 않는다).
        min_bundle_size: 노출 임계(대역은 쓰지 않는다).
        limit: 이 쪽의 행 수.
        after_count: 직전 쪽 마지막 개체의 구성 자산 수.
        after_uid: 직전 쪽 마지막 개체의 표기 키.

    Returns:
        정렬(수 내림차순 → 표기 키 오름차순) 기준 다음 ``limit`` 행.

    Raises:
        ValueError: 책갈피를 반쪽만 준 경우(코어와 같은 계약).
    """
    if (after_count is None) != (after_uid is None):
        raise ValueError("이어읽기 책갈피는 after_count·after_uid 를 함께 줘야 한다")
    rows = _TABLE
    if after_count is not None:
        rows = [
            r for r in rows
            if int(r["confirmed_count"]) < int(after_count)
            or (int(r["confirmed_count"]) == int(after_count)
                and str(r["entity_uid"]) > str(after_uid))
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

        def _fake_fetch_total(_conn: object, **kw: Any) -> int:
            """노출 개체 **모수** 대역 — 목록이 한 쪽만 줘도 이 값이 총계다."""
            self.total_calls += 1
            self.total_kwargs.update(kw)
            return _TOTAL

        for target, repl in (
            ("service.api._infra._run_in_db", lambda fn: fn(object())),
            ("service.api.routes_mm_meta.mm_meta.fetch_total", _fake_fetch_total),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.api.routes_mm_meta.mm_meta.fetch_list", _fake_fetch_list),
            ("service.portal.mm_meta.get_current_settings", _cfg),
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

    def test_다른_정렬로_만든_커서는_400_이다(self) -> None:
        token = encode_cursor("name_asc", [1, "e00"])
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": token})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_정렬값_개수가_다른_커서는_400_이다(self) -> None:
        # 구버전·위조 토큰. 통과시키면 반쪽 책갈피가 되어 첫 쪽을 다시 읽는다(중복).
        token = encode_cursor(routes_mm_meta.mm_meta.ENTITY_CURSOR_SORT, ["e00"])
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": token})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_커서와_검색어를_함께_주면_400_이다(self) -> None:
        # 개체 검색은 아직 순위(top_n)라 이어 읽을 자리가 없다 — G4 뒤에 열린다.
        first = self._get(limit=3)
        resp = self.client.get(
            "/mm-meta", params={"limit": 3, "q": "제주", "cursor": first["next_cursor"]})
        self.assertEqual(resp.status_code, 400, resp.text)


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

    def test_좁히기가_걸리면_모수가_scope_total_이고_total_은_줄어든다(self) -> None:
        body = self._get(limit=10, refine="e00")
        self.assertEqual([i["entity_uid"] for i in body["items"]], ["e00"])
        self.assertEqual(body["scope_total"], _TOTAL, "지우면 몇 건인지가 scope_total 이다")
        # ⚠️ 좁히기가 걸린 total 은 아직 **이 쪽 안에서 좁힌 수**다 — 좁히기가 파이썬에서 쪽 단위로
        #    돌기 때문이고, 서버 질의로 옮기면(099 G5) 모수가 된다. 지금 뜻을 못 박아 둔다.
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["refine"], "e00")

    def test_검색어가_있으면_현행대로_돌려준_개수를_센다(self) -> None:
        # ⛔ 개체 검색은 아직 상위 몇 개를 고르는 순위 경로라 모수를 낼 수 없다(099 G4 에서 연다).
        #    그때까지는 **모수 조회를 아예 하지 않는다** — 목록과 다른 수를 내보내면 화면이 거짓말을 한다.
        with patch(
            "service.api.routes_mm_meta.mm_meta.search_and_refine",
            lambda rows, **kw: ([dict(rows[0])], 7),
        ):
            body = self._get(limit=3, q="제주")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["scope_total"], 7)
        self.assertEqual(self.total_calls, 0, "검색 경로는 모수를 세지 않는다(G4 전)")


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
        # 🔴 다음 책갈피를 **좁힌 뒤**의 행으로 만들면 1쪽에서 0건이 되어 커서가 끊기고, 마지막 쪽에
        #    있는 e09 에는 영영 닿지 못한다. DB 가 준 쪽 기준이어야 한다.
        self.assertEqual(self._drain(limit=2, refine="e09"), ["e09"])

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
