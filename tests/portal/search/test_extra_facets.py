"""추가 좁히기 칩(형식·크기·기간) — **세는 집합이 검색 결과와 같아야** 한다.

여기서 지키는 것 둘.
  ① 질의 절은 코어의 집합 절(``browse_scope_clause``)을 그대로 쓴다 — 새로 짜면 칩 숫자와
     클릭 결과가 갈린다.
  ② 칩은 **자기 조건을 뺀 채** 센다 — 형식 칩에 형식 필터가 걸리면 고른 값만 남아 칩이 접힌다.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from service.portal.search.extra_facets import (
    AXES,
    DATE_PRESETS,
    FIELD,
    SIZE_BUCKETS,
    _items,
    build_axis_body,
    extra_facets,
)
from src.search.search_filters import parse_search_filters

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _body(axis: str, **kw):
    return build_axis_body(axis, query=kw.pop("query", ""), semantic_ids=kw.pop("semantic_ids", []),
                           filters=kw.pop("filters", None), refine=kw.pop("refine", None), now=NOW)


class TestAxisBody(unittest.TestCase):
    def test_모르는_축은_거절한다(self) -> None:
        with self.assertRaises(ValueError):
            _body("bogus")

    def test_형식_축은_확장자를_센다(self) -> None:
        aggs = _body("file_ext")["aggs"]
        self.assertEqual(FIELD["file_ext"], aggs["file_ext"]["terms"]["field"])

    def test_형식_축은_형식_필터를_뺀다(self) -> None:
        """🔴 빼지 않으면 고른 확장자만 남아 칩이 하나로 접힌다."""
        filters = parse_search_filters(file_ext=["txt"], topic=["역사·문화유산"])
        body = _body("file_ext", filters=filters)
        dumped = str(body["query"])
        self.assertNotIn("txt", dumped)
        self.assertIn("역사·문화유산", dumped)      # 다른 축 조건은 그대로 걸린다

    def test_기간_축은_기간_필터를_뺀다(self) -> None:
        filters = parse_search_filters(created_from="2026-01-01", created_to="2026-02-01")
        body = _body("date_preset", filters=filters)
        self.assertNotIn("2026-01-01", str(body["query"]))

    def test_크기_축은_세_구간이다(self) -> None:
        ranges = _body("file_size")["aggs"]["file_size"]["range"]["ranges"]
        self.assertEqual([k for k, _, _ in SIZE_BUCKETS], [r["key"] for r in ranges])

    def test_기간_축은_프리셋_수만큼_구간을_만든다(self) -> None:
        ranges = _body("date_preset")["aggs"]["date_preset"]["date_range"]["ranges"]
        self.assertEqual([str(d) for d in DATE_PRESETS], [r["key"] for r in ranges])
        # 기준 시각에서 거슬러 올라간 **자정**이 경계다(하루를 반 토막 내지 않게).
        self.assertTrue(all(r["from"].endswith("00:00:00+00:00") for r in ranges))

    def test_행은_받지_않는다(self) -> None:
        """건수만 필요하다 — 행까지 받으면 목록 질의를 한 번 더 하는 셈이다."""
        self.assertEqual(0, _body("file_ext")["size"])


class TestIndexFieldNames(unittest.TestCase):
    """🔴 [2026-09-21] 집계 대상 필드 이름은 **코어가 거를 때 쓰는 이름과 같아야** 한다.

    처음에 최상위 `file_ext`·`created_at` 로 집계했더니 오류 없이 **빈 칩**이 나왔다(색인은 그 둘을
    `filter_kw`·`filter_date` 안에 둔다). 이름이 틀려도 조용하기 때문에, 코어의 필터 절에서 이름을
    되읽어 대조한다 — 코어가 이름을 바꾸면 이 시험이 먼저 깨진다.
    """

    def _clause_fields(self, **kw) -> set[str]:
        from src.search.search_filters import filters_to_opensearch_bool
        out: set[str] = set()
        for clause in filters_to_opensearch_bool(parse_search_filters(**kw)):
            for body in clause.values():
                out.update(body)
        return out

    def test_형식_필드가_코어와_같다(self) -> None:
        self.assertIn(FIELD["file_ext"], self._clause_fields(file_ext=["txt"]))

    def test_기간_필드가_코어와_같다(self) -> None:
        self.assertIn(FIELD["date_preset"], self._clause_fields(created_from="2026-01-01"))

    def test_축마다_필드를_정해_뒀다(self) -> None:
        self.assertEqual(set(AXES), set(FIELD))


class TestItems(unittest.TestCase):
    def test_형식_버킷을_편다(self) -> None:
        agg = {"buckets": [{"key": "txt", "doc_count": 3}, {"key": "jpg", "doc_count": 1}]}
        self.assertEqual([{"key": "txt", "count": 3}, {"key": "jpg", "count": 1}],
                         _items("file_ext", agg))

    def test_구간_버킷은_고정_순서로_편다(self) -> None:
        """🔴 응답에 없는 구간도 0 으로 채운다 — 화면이 칩을 그렸다 지웠다 하지 않게."""
        agg = {"buckets": {"under1": {"doc_count": 2}}}
        self.assertEqual([{"key": "under1", "count": 2}, {"key": "1to10", "count": 0},
                          {"key": "over10", "count": 0}], _items("file_size", agg))

    def test_집계가_없으면_빈_목록(self) -> None:
        self.assertEqual([], _items("file_ext", None))


_ONE = {"hits": {"total": {"value": 7}},
        "aggregations": {"file_ext": {"buckets": [{"key": "txt", "doc_count": 7}]}}}


class _FakeClient:
    """몇 번 왕복하는지, 무엇을 세는지 본다(``msearch`` 한 번 · 의미 질의는 검색어가 있을 때만)."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []      # msearch 로 보낸 축 본문들(헤더 제외)
        self.calls: list[str] = []        # 부른 메서드 순서

    def search(self, *, index: str, body: dict) -> dict:
        self.calls.append("search")
        return {"hits": {"hits": []}}

    def msearch(self, *, body: list) -> dict:
        self.calls.append("msearch")
        self.bodies.extend(body[1::2])    # 짝수 자리는 {"index": ...} 헤더
        return {"responses": [dict(_ONE) for _ in body[1::2]]}


class TestExtraFacets(unittest.TestCase):
    def test_축을_지정하지_않으면_전부_센다(self) -> None:
        client = _FakeClient()
        out = extra_facets(client, "assets", query="", query_vector=[], filters=None,
                           refine=None, axes=[], now=NOW)
        self.assertEqual(len(AXES), len(client.bodies))
        self.assertEqual(sorted(AXES), sorted(out["axes"]))
        self.assertEqual(7, out["total"])

    def test_축이_여럿이어도_왕복은_한_번(self) -> None:
        """칩은 필터를 바꿀 때마다 다시 센다 — 축 수만큼 왕복하면 그대로 체감이 된다."""
        client = _FakeClient()
        extra_facets(client, "assets", query="", query_vector=[], filters=None,
                     refine=None, axes=list(AXES), now=NOW)
        self.assertEqual(["msearch"], client.calls)

    def test_훑기에는_의미_질의를_하지_않는다(self) -> None:
        """검색어가 없으면 뜻으로 걸 것이 없다 — 질의 하나를 아낀다."""
        client = _FakeClient()
        extra_facets(client, "assets", query="", query_vector=[0.1], filters=None,
                     refine=None, axes=["file_ext"], now=NOW)
        self.assertEqual(1, len(client.bodies))
        self.assertEqual(["msearch"], client.calls)

    def test_검색어가_있으면_의미_질의_뒤에_한_번_묶어_보낸다(self) -> None:
        client = _FakeClient()
        extra_facets(client, "assets", query="김치", query_vector=[0.1], filters=None,
                     refine=None, axes=list(AXES), now=NOW)
        self.assertEqual(["search", "msearch"], client.calls)

    def test_부분_실패를_0건으로_감추지_않는다(self) -> None:
        """msearch 는 실패한 질의를 예외가 아니라 응답 안 error 로 준다 — 빈 칩이 정상처럼 보이면 안 된다."""
        class _Broken(_FakeClient):
            def msearch(self, *, body: list) -> dict:
                return {"responses": [dict(_ONE), {"error": {"type": "search_phase_execution"}},
                                      dict(_ONE)]}

        with self.assertRaises(RuntimeError):
            extra_facets(_Broken(), "assets", query="", query_vector=[], filters=None,
                         refine=None, axes=list(AXES), now=NOW)

    def test_응답_수가_모자라면_짝을_맞춰_읽지_않는다(self) -> None:
        class _Short(_FakeClient):
            def msearch(self, *, body: list) -> dict:
                return {"responses": [dict(_ONE)]}

        with self.assertRaises(RuntimeError):
            extra_facets(_Short(), "assets", query="", query_vector=[], filters=None,
                         refine=None, axes=list(AXES), now=NOW)


class _ByFilterClient(_FakeClient):
    """본문에 걸린 필터 수만큼 건수를 줄여 돌려준다 — 어느 본문의 건수가 total 로 나갔는지 가린다."""

    def msearch(self, *, body: list) -> dict:
        self.calls.append("msearch")
        bodies = body[1::2]
        self.bodies.extend(bodies)

        def _count(b: dict) -> int:
            clauses = b["query"]["bool"].get("filter") or []
            return 100 - 10 * len(clauses if isinstance(clauses, list) else [clauses])

        return {"responses": [{"hits": {"total": {"value": _count(b)}}, "aggregations": {}}
                              for b in bodies]}


class TestTotalIsWholeSet(unittest.TestCase):
    """total 은 조건을 하나도 빼지 않은 집합의 크기다(= /file-search 의 total · 2026-09-23 결함 수정).

    종전에는 마지막 축의 건수를 썼다 — 기간 축은 기간 조건을 빼고 세므로, 기간을 건 화면에
    기간을 뺀 수가 total 로 나갔다.
    """

    FILTERS = parse_search_filters(file_ext=["jpg"], created_from="2026-09-01", created_to=None,
                                   topic=None, subtopic=None, modality=None, tag=None)

    def _full_count(self) -> int:
        client = _ByFilterClient()
        return extra_facets(client, "assets", query="", query_vector=[], filters=self.FILTERS,
                            refine=None, axes=["file_size"], now=NOW)["total"]

    def test_기간_축이_마지막이어도_전체_건수다(self) -> None:
        client = _ByFilterClient()
        out = extra_facets(client, "assets", query="", query_vector=[], filters=self.FILTERS,
                           refine=None, axes=["file_ext", "file_size", "date_preset"], now=NOW)
        self.assertEqual(self._full_count(), out["total"])
        self.assertEqual(3, len(client.bodies))      # 크기 축이 전체 집합을 세므로 더 싣지 않는다

    def test_조건을_빼는_축만_물으면_전체를_세는_본문을_하나_더_싣는다(self) -> None:
        client = _ByFilterClient()
        out = extra_facets(client, "assets", query="", query_vector=[], filters=self.FILTERS,
                           refine=None, axes=["date_preset"], now=NOW)
        self.assertEqual(self._full_count(), out["total"])
        self.assertEqual(["date_preset"], list(out["axes"]))   # 덧붙인 본문은 축으로 내보내지 않는다
        self.assertEqual(2, len(client.bodies))
        self.assertEqual(["msearch"], client.calls)            # 그래도 왕복은 한 번

    def test_빠지는_조건이_없으면_더_싣지_않는다(self) -> None:
        client = _ByFilterClient()
        extra_facets(client, "assets", query="", query_vector=[], filters=None,
                     refine=None, axes=["date_preset"], now=NOW)
        self.assertEqual(1, len(client.bodies))


if __name__ == "__main__":
    unittest.main()
