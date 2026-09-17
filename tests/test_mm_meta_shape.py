"""095 — 개체 화면 **정형 계층** 단위 테스트(DB·OpenSearch·임베딩 없음).

무엇을 봉인하나:
① 판단은 전부 코어 함수를 **그대로** 참조한다(서비스가 사본을 만들지 않는다 · 093 규칙).
② 화면 정책은 여기 있다 — 근거 키워드 상위 몇 개·짧은 것부터, 응답 키 이름, 상한·파일명.
③ 찾아오기·좁히기의 **집합 판정**(099 G5): 둘 다 엔진을 거쳐 개체 키 집합이 되고, 판정하지 못하면
   **되돌리지 않고 끊는다**(전체도 빈 결과도 아니다).
④ 좁히기 칩의 세는 범위가 축마다 다르다(종류는 조건 없음 · 갈래는 종류+고른 갈래 적용).
⑤ 카드는 "없는 개체"(None→404)와 "빈 개체"(total 0)를 가른다.

⚠️ **③ 은 099 G5 에서 뒤집힌 계약이다.** 종전에는 엔진이 죽으면 파이썬 문자열 매칭 결과로 되돌렸다
(`narrow_entities`·`refine_rows`). 새 구조에서 같은 되돌림은 화이트리스트가 ``None``(=필터 없음)이
되어 "검색했는데 전체가 나온다"가 되므로 금지다 — 라우트 계약은 `test_mm_meta_set_search.py` 가,
정형 계층 함수 계약은 이 파일이 본다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from service.api import routes_assets, routes_mm_meta
from service.portal import mm_meta
from src.mm_classify.read import label_names_of_assets as core_label_names
from src.mm_meta.rules import MIN_BUNDLE_SIZE
from src.relations import graph_query as gq
from src.search.entity_search_os import match_entity_keys as core_match_entity_keys
from src.search.facets import aggregate_facets as core_aggregate_facets


def _row(**over) -> dict:
    """코어 ``list_entities`` 행 대역(모든 키가 채워진 상태)."""
    base = {
        "entity_type": "장소", "entity_uid": "제주도", "node_id": "7", "name": "제주도",
        "source": "auto", "description": None, "confirmed_count": 14, "total_count": 14,
        "modalities": ["image", "text"], "keywords": ["제주도", "제주 해녀", "제주 감귤 농장"],
        "topics": ["여행", "자연"], "forms": ["기록·자료"], "areas": ["여행·명소"],
    }
    base.update(over)
    return base


def _cfg(backend: str = "opensearch") -> SimpleNamespace:
    """``get_current_settings`` 대역 — 정형 계층이 읽는 필드만."""
    return SimpleNamespace(
        mm_meta=SimpleNamespace(search_backend=backend, semantic_gate_eps=0.15,
                                semantic_gate_enabled=True),
        embed=SimpleNamespace(api_model="bge-m3"),
    )


class TestWiring(unittest.TestCase):
    """판단은 코어 함수를 그대로 쓴다 — 서비스에 사본이 없다."""

    def test_core_functions_are_referenced_as_is(self) -> None:
        # 찾아오기·좁히기 판정은 코어 집합 함수 하나가 한다(099 G5 — 두 일이 한 경로로 접혔다).
        self.assertIs(mm_meta.match_entity_keys, core_match_entity_keys)
        self.assertIs(mm_meta.aggregate_facets, core_aggregate_facets)
        self.assertIs(mm_meta.label_names_of_assets, core_label_names)
        self.assertIs(mm_meta.list_entities, gq.list_entities)
        self.assertIs(mm_meta.count_entities, gq.count_entities)
        self.assertIs(mm_meta.count_entities_by_type, gq.count_entities_by_type)
        self.assertIs(mm_meta.count_entities_by_area, gq.count_entities_by_area)
        self.assertIs(mm_meta.assets_of_entities, gq.assets_of_entities)
        self.assertIs(mm_meta.mm_meta_bundle, gq.mm_meta_bundle)
        self.assertIs(routes_assets.mm_meta_of_asset, gq.mm_meta_of_asset)

    def test_threshold_comes_from_core_and_is_one_value(self) -> None:
        # 목록·칩·다운로드가 같은 값을 쓴다("적힌 숫자 = 누르면 나오는 수" · 2026-09-08 판정).
        self.assertEqual(routes_mm_meta._MIN_BUNDLE_SIZE, MIN_BUNDLE_SIZE)

    def test_removed_parameters_are_gone_and_areas_added(self) -> None:
        import inspect

        params = inspect.signature(routes_mm_meta.list_mm_meta).parameters
        for gone in ("labels", "label_axis", "semantic"):
            self.assertNotIn(gone, params, f"{gone} 는 제거된 파라미터다")
        self.assertIn("areas", params)


class TestListShaping(unittest.TestCase):
    """목록 항목 — 화면 정책이 여기 있다."""

    def test_keywords_are_short_first_and_capped(self) -> None:
        row = _row(keywords=[f"kw{i}" * (i + 1) for i in range(10)])
        item = mm_meta.shape_list_item(row)
        self.assertEqual(len(item["keywords"]), mm_meta.KEYWORD_TOP_N)
        lengths = [len(k) for k in item["keywords"]]
        self.assertEqual(lengths, sorted(lengths))  # 짧은 것부터

    def test_representative_keyword_comes_first(self) -> None:
        item = mm_meta.shape_list_item(_row())
        self.assertEqual(item["keywords"][0], "제주도")  # 긴 표현보다 대표 표기가 앞

    def test_node_id_is_not_exposed(self) -> None:
        self.assertNotIn("node_id", mm_meta.shape_list_item(_row()))

    def test_response_keys_are_fixed(self) -> None:
        self.assertEqual(
            set(mm_meta.shape_list_item(_row())),
            {"entity_type", "entity_uid", "name", "source", "description", "confirmed_count",
             "total_count", "modalities", "keywords", "topics", "forms", "areas"},
        )

    def test_shaping_does_not_mutate_core_row(self) -> None:
        row = _row()
        before = dict(row)
        mm_meta.shape_list_item(row)
        self.assertEqual(row, before)


class TestFormSkills(unittest.TestCase):
    """형식 축 스킬 — 기본은 하나, 환경변수로 바꾼다."""

    def test_default_is_single_skill(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mm_meta.FORM_SKILLS_ENV, None)
            self.assertEqual(mm_meta.form_skill_codes(), mm_meta.DEFAULT_FORM_SKILL_CODES)

    def test_env_overrides_and_trims(self) -> None:
        with patch.dict(os.environ, {mm_meta.FORM_SKILLS_ENV: " content_form , food_content ,"}):
            self.assertEqual(mm_meta.form_skill_codes(), ("content_form", "food_content"))

    def test_blank_env_falls_back(self) -> None:
        with patch.dict(os.environ, {mm_meta.FORM_SKILLS_ENV: "  ,  "}):
            self.assertEqual(mm_meta.form_skill_codes(), mm_meta.DEFAULT_FORM_SKILL_CODES)


class TestFormCounts(unittest.TestCase):
    """형식 집계 — 코어 세는 규칙 사용 · 단위는 자산 하나."""

    def test_counts_by_asset_and_sorts(self) -> None:
        out = mm_meta._form_counts({
            "a1": ["인터뷰", "리뷰·해석"], "a2": ["인터뷰"], "a3": ["레시피"],
        })
        self.assertEqual(out, [{"name": "인터뷰", "count": 2}, {"name": "레시피", "count": 1},
                               {"name": "리뷰·해석", "count": 1}])

    def test_empty_is_empty(self) -> None:
        self.assertEqual(mm_meta._form_counts({}), [])


class TestSetJudgement(unittest.TestCase):
    """099 G5 — 찾아오기(q)·좁히기(refine)의 **집합 판정**(정형 계층 함수 계약).

    🔴 여기서 보는 것은 "무엇을 돌려주나"가 아니라 **무엇으로 돌려주나**다. ``None``(필터 없음)과
    빈 집합(0건)을 섞으면 "검색했는데 전체가 나온다"가 되기 때문이다 — 파이썬에서는 둘 다 거짓값이라
    ``if not keys`` 한 줄이 그 사고를 만든다.
    """

    def setUp(self) -> None:
        self.engine = MagicMock(return_value={("장소", "제주도")})
        for target, repl in (
            ("service.portal.mm_meta.match_entity_keys", self.engine),
            ("service.portal.mm_meta.embed_query_for_media_search", lambda *_a, **_k: [0.0]),
            ("service.portal.mm_meta.active_embed_channel", lambda: "st_api"),
            ("service.portal.mm_meta.get_current_settings", _cfg),
            ("src.search.opensearch_sync.get_client", lambda: object()),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)

    def test_아무것도_안_물어보면_필터가_없다(self) -> None:
        scope = mm_meta.search_and_refine(q=None, refine=None)
        self.assertIsNone(scope.uid_allow, "빈 집합이면 첫 화면이 통째로 0건이 된다")
        self.assertIsNone(scope.scope_allow)
        self.assertFalse(scope.refined)
        self.engine.assert_not_called()

    def test_공백뿐인_값은_안_물어본_것이다(self) -> None:
        scope = mm_meta.search_and_refine(q="   ", refine="\t")
        self.assertIsNone(scope.uid_allow)
        self.engine.assert_not_called()

    def test_q_만_있으면_그_집합이_둘_다다(self) -> None:
        scope = mm_meta.search_and_refine(q="제주", refine=None)
        self.assertEqual(scope.uid_allow, {("장소", "제주도")})
        self.assertEqual(scope.scope_allow, {("장소", "제주도")}, "좁히기 이전 집합도 같은 값")
        self.assertFalse(scope.refined)

    def test_refine_만_있으면_좁히기_이전은_전체다(self) -> None:
        scope = mm_meta.search_and_refine(q=None, refine="해녀")
        self.assertEqual(scope.uid_allow, {("장소", "제주도")})
        self.assertIsNone(scope.scope_allow, "찾아온 적이 없으니 '지우면 N건'의 답은 전체다")
        self.assertTrue(scope.refined)

    def test_둘_다_있으면_교집합이다(self) -> None:
        self.engine.side_effect = lambda *_a, **kw: (
            {("장소", "제주도"), ("장소", "서울특별시")} if kw["query"] == "섬"
            else {("장소", "제주도"), ("인물", "해녀")}
        )
        scope = mm_meta.search_and_refine(q="섬", refine="해녀")
        self.assertEqual(scope.uid_allow, {("장소", "제주도")})
        self.assertEqual(scope.scope_allow, {("장소", "제주도"), ("장소", "서울특별시")})
        self.assertTrue(scope.refined)

    def test_매칭이_없으면_빈_집합이다(self) -> None:
        self.engine.return_value = set()
        scope = mm_meta.search_and_refine(q="없는낱말", refine=None)
        self.assertEqual(scope.uid_allow, set())
        self.assertIsNotNone(scope.uid_allow, "🔴 None 으로 접으면 전체가 나간다")

    def test_질의마다_엔진을_한_번씩만_부른다(self) -> None:
        mm_meta.search_and_refine(q="섬", refine="해녀")
        self.assertEqual([c.kwargs["query"] for c in self.engine.call_args_list], ["섬", "해녀"])

    def test_엔진_연결_실패는_되돌리지_않고_끊는다(self) -> None:
        # 🔴 종전에는 문자열 결과로 되돌렸다 — 새 구조에서 그 되돌림은 '전체 노출'이 된다.
        try:
            from opensearchpy.exceptions import ConnectionError as OSConnectionError
        except ImportError:  # pragma: no cover - 라이브러리 미설치 환경
            self.skipTest("opensearchpy 미설치")
        self.engine.side_effect = OSConnectionError("N/A", "conn refused", None)
        with self.assertRaises(mm_meta.EntitySearchUnavailable):
            mm_meta.search_and_refine(q="제주", refine=None)

    def test_임베딩_실패도_끊고_문구가_다르다(self) -> None:
        with patch("service.portal.mm_meta.embed_query_for_media_search",
                   side_effect=RuntimeError("임베딩 API 호출 실패")):
            with self.assertRaises(mm_meta.EntitySearchUnavailable) as caught:
                mm_meta.search_and_refine(q="제주", refine=None)
        self.assertIn("임베딩", str(caught.exception))

    def test_코드_결함은_삼키지_않는다(self) -> None:
        # 연결 실패만 골라 잡는다 — 결함까지 삼키면 "엔진 장애"로 둔갑해 운영자가 엉뚱한 곳을 본다.
        self.engine.side_effect = KeyError("코드 결함 흉내")
        with self.assertRaises(KeyError):
            mm_meta.search_and_refine(q="제주", refine=None)

    def test_되돌림_백엔드는_집합_판정을_하지_않는다(self) -> None:
        with patch("service.portal.mm_meta.get_current_settings", lambda: _cfg("pg")):
            with self.assertRaises(mm_meta.EntitySearchUnavailable) as caught:
                mm_meta.search_and_refine(q="제주", refine=None)
        self.assertIn("pg", str(caught.exception))
        self.engine.assert_not_called()

    def test_엔진_호출에_질의_벡터가_함께_간다(self) -> None:
        # 🔴 벡터를 빠뜨리면 의미(kNN) 갈래가 조용히 사라진다(`발효`→김치를 못 찾는다).
        mm_meta.search_and_refine(q="발효", refine=None)
        self.assertEqual(self.engine.call_args.kwargs["query_vector"], [0.0])


class TestFacetsShaping(unittest.TestCase):
    """좁히기 칩 — 축마다 세는 범위가 다르고 0건도 싣는다."""

    def _run(self, *, entity_type=None, areas=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "name": "기본 어휘", "version": 3,
            "types": [{"name": "장소", "definition": "정의", "not": "경계"},
                      {"name": "인물", "definition": "정의2", "not": ""}],
        }
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn.cursor.return_value = cur
        with (
            patch.object(mm_meta, "count_entities_by_type", return_value={"장소": 40}) as m_type,
            patch.object(mm_meta, "count_entities_by_area", return_value=[
                {"name": "여행·명소", "skill": "장소", "skill_code": "place_heritage", "count": 12},
                {"name": "음료", "skill": "음식", "skill_code": "food_content", "count": 0},
            ]) as m_area,
        ):
            out = mm_meta.fetch_facets(conn, entity_type=entity_type, areas=areas,
                                       min_bundle_size=3)
        return out, m_type, m_area

    def test_type_axis_gets_no_conditions(self) -> None:
        # 종류는 갈아타는 축 — 갈래를 고른 상태에서도 조건 없이 센다(안 그러면 갈아탈 칩이 사라진다).
        _out, m_type, _m_area = self._run(entity_type="음식", areas=["한식"])
        self.assertEqual(m_type.call_args.kwargs, {"min_bundle_size": 3})

    def test_area_axis_applies_type_and_picked(self) -> None:
        _out, _m_type, m_area = self._run(entity_type="음식", areas=["한식"])
        self.assertEqual(m_area.call_args.kwargs["entity_type"], "음식")
        self.assertEqual(m_area.call_args.kwargs["area_names"], ["한식"])

    def test_zero_count_area_is_kept(self) -> None:
        out, _t, _a = self._run()
        self.assertEqual(out["areas"][1], {"name": "음료", "skill": "음식",
                                           "skill_code": "food_content", "entities": 0})

    def test_types_follow_vocab_order_and_zero_fill(self) -> None:
        out, _t, _a = self._run()
        self.assertEqual([t["name"] for t in out["types"]], ["장소", "인물"])  # 어휘 순서
        self.assertEqual(out["types"][0]["entities"], 40)
        self.assertEqual(out["types"][1]["entities"], 0)  # 개체 없는 종류도 0 으로
        self.assertEqual(out["types"][0]["boundary"], "경계")  # JSON 의 not → boundary
        self.assertEqual(out["vocab"], {"name": "기본 어휘", "version": 3})

    def test_scope_and_threshold_are_reported(self) -> None:
        out, _t, _a = self._run(entity_type="장소", areas=["여행·명소"])
        self.assertEqual(out["scoped_by"], {"entity_type": "장소", "areas": ["여행·명소"]})
        self.assertEqual(out["min_members"], 3)

    def test_missing_vocab_row_gives_empty_not_preset(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn.cursor.return_value = cur
        with (
            patch.object(mm_meta, "count_entities_by_type", return_value={}),
            patch.object(mm_meta, "count_entities_by_area", return_value=[]),
        ):
            out = mm_meta.fetch_facets(conn, entity_type=None, areas=None, min_bundle_size=3)
        self.assertIsNone(out["vocab"])
        self.assertEqual(out["types"], [])


class TestCard(unittest.TestCase):
    """카드 — 없는 개체와 빈 개체를 가른다 · 라벨은 한 번에 읽는다."""

    def test_absent_entity_is_none(self) -> None:
        with patch.object(mm_meta, "mm_meta_bundle", return_value=None):
            self.assertIsNone(mm_meta.fetch_card(object(), entity_type="장소", entity_uid="x"))

    def test_empty_entity_is_total_zero(self) -> None:
        bundle = {"entity_type": "장소", "entity_uid": "x", "name": "x", "source": "user",
                  "description": None, "total": 0, "modalities": []}
        with (
            patch.object(mm_meta, "mm_meta_bundle", return_value=bundle),
            patch.object(mm_meta, "label_names_of_assets", return_value={}) as m_lab,
        ):
            card = mm_meta.fetch_card(object(), entity_type="장소", entity_uid="x")
        self.assertEqual((card["total"], card["confirmed_count"]), (0, 0))
        self.assertEqual(card["form_counts"], [])
        self.assertEqual(m_lab.call_args.args[1], [])  # 자산이 없으면 빈 목록으로 부른다

    def test_labels_are_read_once_for_all_assets(self) -> None:
        bundle = {
            "entity_type": "장소", "entity_uid": "제주도", "name": "제주도", "source": "auto",
            "description": "섬", "total": 3,
            "modalities": [
                {"modality": "text", "count": 2, "assets": [{"asset_id": "a1"}, {"asset_id": "a2"}]},
                {"modality": "image", "count": 1, "assets": [{"asset_id": "a1"}]},
            ],
        }
        with (
            patch.object(mm_meta, "mm_meta_bundle", return_value=bundle),
            patch.object(mm_meta, "label_names_of_assets",
                         return_value={"a1": ["기록·자료"]}) as m_lab,
        ):
            card = mm_meta.fetch_card(object(), entity_type="장소", entity_uid="제주도")
        self.assertEqual(m_lab.call_count, 1)
        self.assertEqual(m_lab.call_args.args[1], ["a1", "a2"])  # 중복 제거
        self.assertEqual(card["modalities"][0]["assets"][0]["forms"], ["기록·자료"])
        self.assertEqual(card["modalities"][0]["assets"][1]["forms"], [])  # 라벨 없으면 빈 목록
        self.assertEqual(card["form_counts"], [{"name": "기록·자료", "count": 1}])
        self.assertEqual(card["description"], "섬")


class TestZipHelpers(unittest.TestCase):
    """다운로드 정책 — 상한·잘림·파일명."""

    def test_card_targets_truncate_and_report(self) -> None:
        ids = [f"a{i}" for i in range(mm_meta.CARD_BUNDLE_MAX_ASSETS + 5)]
        bundle = {"name": "제주도", "modalities": [
            {"assets": [{"asset_id": i} for i in ids]}]}
        with (
            patch.object(mm_meta, "mm_meta_bundle", return_value=bundle),
            patch.object(mm_meta, "fetch_asset_paths",
                         side_effect=lambda _c, a: {i: f"/x/{i}.txt" for i in a}),
        ):
            targets, name, truncated = mm_meta.card_zip_targets(
                object(), entity_type="장소", entity_uid="제주도")
        self.assertEqual(len(targets), mm_meta.CARD_BUNDLE_MAX_ASSETS)
        self.assertTrue(truncated)
        self.assertEqual(name, "제주도")

    def test_assets_without_path_are_dropped(self) -> None:
        bundle = {"name": "n", "modalities": [{"assets": [{"asset_id": "a1"}, {"asset_id": "a2"}]}]}
        with (
            patch.object(mm_meta, "mm_meta_bundle", return_value=bundle),
            patch.object(mm_meta, "fetch_asset_paths", return_value={"a1": "/x/a1.txt"}),
        ):
            targets, _n, truncated = mm_meta.card_zip_targets(
                object(), entity_type="장소", entity_uid="n")
        self.assertEqual([t["asset_id"] for t in targets], ["a1"])
        self.assertFalse(truncated)

    def test_zip_name_is_ascii_only(self) -> None:
        self.assertEqual(mm_meta.ascii_zip_name(["장소", "제주도"], fallback="meta", count=3),
                         "meta_3files.zip")
        self.assertEqual(mm_meta.ascii_zip_name(["place", "jeju"], fallback="meta", count=2),
                         "placejeju_2files.zip")

    def test_parse_names_splits_and_trims(self) -> None:
        self.assertEqual(routes_mm_meta._parse_names(" 한식 , 영화 ,, "), ["한식", "영화"])
        self.assertEqual(routes_mm_meta._parse_names(None), [])
        self.assertEqual(routes_mm_meta._parse_names(""), [])


class TestRouteReadScope(unittest.TestCase):
    """🔴 찾아오기·좁히기는 **SQL 이 한다** — 라우트는 한 쪽만 읽는다(099 G5).

    2026-09-15 실측: 「숭례문」이 개체 색인에 있는데 화면에서 0건이었다. 목록 상한 200 을 먼저 자르고
    그 안에서 찾았기 때문이다. G5 전에는 그 상한을 10,000 으로 키워 우회했지만(모수를 통째로 읽고
    파이썬이 골랐다), 이제는 **찾아온 개체 키 집합을 화이트리스트로 SQL 에 얹어** 한 쪽만 읽는다 —
    꼬리 개체는 DB 가 고르므로 쪽 크기와 무관하게 닿는다(SC-002 는 `test_mm_meta_set_search.py`).
    """

    def _call(self, **params):
        """라우트를 부르고 ``fetch_list`` 가 받은 인자를 돌려준다.

        Args:
            **params: 라우트 파라미터(빠진 것은 기본값으로 채운다).

        Returns:
            ``(fetch_list 가 받은 kwargs, 응답 본문)``.
        """
        from service.api import routes_mm_meta as mod
        seen: dict[str, object] = {}
        # 꼬리 개체 하나만 든 대역 — 화이트리스트가 SQL 로 갔는지·쪽 크기가 얼마인지를 본다.
        tail = mm_meta.shape_list_item(_row(entity_uid="숭례문", name="숭례문", confirmed_count=1))

        def fake_fetch(_conn, **kw):
            """목록 대역 — 인자를 기록하고 화이트리스트에 맞는 행만 돌려준다."""
            seen.update(kw)
            allow = kw.get("uid_allow")
            if allow is None:
                return [tail]
            return [tail] if ("장소", "숭례문") in allow else []

        args = {"q": None, "entity_type": None, "areas": None, "refine": None,
                "cursor": None, "limit": 200, **params}
        with (
            patch.object(mod._infra, "_run_in_db", lambda fn: fn(object())),
            patch.object(mod.mm_meta, "fetch_list", fake_fetch),
            patch.object(mod.mm_meta, "fetch_total", lambda _conn, **kw: 1),
            patch.object(mod.mm_meta, "search_and_refine",
                         lambda **kw: mm_meta.EntityScope(
                             uid_allow={("장소", "숭례문")} if (kw["q"] or kw["refine"]) else None,
                             scope_allow={("장소", "숭례문")} if kw["q"] else None,
                             refined=bool(kw["refine"]))),
        ):
            body = mod.list_mm_meta(**args, principal=SimpleNamespace(clearance="authorized"))
        return seen, body

    def test_검색어가_있어도_한_쪽만_읽는다(self) -> None:
        seen, _body = self._call(q="숭례문", limit=200)
        self.assertEqual(seen["limit"], 200, "모수를 통째로 읽던 우회(10,000)는 사라졌다")

    def test_찾아온_집합이_화이트리스트로_SQL_에_간다(self) -> None:
        seen, body = self._call(q="숭례문", limit=3)
        self.assertEqual(seen["uid_allow"], {("장소", "숭례문")})
        self.assertEqual([i["entity_uid"] for i in body["items"]], ["숭례문"],
                         "꼬리 개체라도 찾으면 나와야 한다")

    def test_검색어가_없으면_화이트리스트도_없다(self) -> None:
        seen, _body = self._call(limit=200)
        self.assertIsNone(seen["uid_allow"], "🔴 빈 집합이면 첫 화면이 0건이 된다")
        self.assertEqual(seen["limit"], 200)

    def test_좁히기만_줘도_SQL_이_좁힌다(self) -> None:
        seen, body = self._call(refine="숭례문", limit=3)
        self.assertEqual(seen["uid_allow"], {("장소", "숭례문")})
        self.assertEqual(body["refine"], "숭례문")


if __name__ == "__main__":
    unittest.main()
