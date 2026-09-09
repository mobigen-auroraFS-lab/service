"""095 — 개체 화면 **정형 계층** 단위 테스트(DB·OpenSearch·임베딩 없음).

무엇을 봉인하나:
① 판단은 전부 코어 함수를 **그대로** 참조한다(서비스가 사본을 만들지 않는다 · 093 규칙).
② 화면 정책은 여기 있다 — 근거 키워드 상위 몇 개·짧은 것부터, 응답 키 이름, 상한·파일명.
③ 검색 경로 분기와 **되돌림**: 검색 엔진·임베딩이 실패해도 문자열 결과는 나간다.
④ 좁히기 칩의 세는 범위가 축마다 다르다(종류는 조건 없음 · 갈래는 종류+고른 갈래 적용).
⑤ 카드는 "없는 개체"(None→404)와 "빈 개체"(total 0)를 가른다.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from service.api import routes_assets, routes_mm_meta
from service.portal import mm_meta
from src.mm_classify.read import label_names_of_assets as core_label_names
from src.mm_meta.entity_search import entity_refine_fields as core_refine_fields
from src.mm_meta.entity_search import narrow_entities as core_narrow
from src.mm_meta.rules import MIN_BUNDLE_SIZE
from src.relations import graph_query as gq
from src.search.facets import aggregate_facets as core_aggregate_facets
from src.search.refine import refine_rows as core_refine_rows


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
        self.assertIs(mm_meta.narrow_entities, core_narrow)
        self.assertIs(mm_meta.refine_rows, core_refine_rows)
        self.assertIs(mm_meta.entity_refine_fields, core_refine_fields)
        self.assertIs(mm_meta.aggregate_facets, core_aggregate_facets)
        self.assertIs(mm_meta.label_names_of_assets, core_label_names)
        self.assertIs(mm_meta.list_entities, gq.list_entities)
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


class TestSearchPaths(unittest.TestCase):
    """검색 경로 분기와 되돌림."""

    def setUp(self) -> None:
        self.items = [dict(_row()), dict(_row(entity_uid="서울특별시", name="서울특별시",
                                              keywords=["서울"], confirmed_count=10))]

    def test_no_query_returns_all_with_reason_key(self) -> None:
        with patch.object(mm_meta, "get_current_settings", return_value=_cfg()):
            out, scope = mm_meta.search_and_refine(
                self.items, q=None, refine=None, run_in_db=lambda fn: fn(object()))
        self.assertEqual(len(out), 2)
        self.assertEqual(scope, 2)
        self.assertTrue(all("match_reason" in r for r in out))  # 응답 모양은 한 가지

    def test_opensearch_failure_falls_back_to_string_hits(self) -> None:
        # 엔진이 죽어도 문자열로 되던 것은 나간다.
        with (
            patch.object(mm_meta, "get_current_settings", return_value=_cfg("opensearch")),
            patch.object(mm_meta, "active_embed_channel", side_effect=RuntimeError("죽음")),
        ):
            out, _scope = mm_meta.search_and_refine(
                self.items, q="제주", refine=None, run_in_db=lambda fn: fn(object()))
        self.assertEqual([r["entity_uid"] for r in out], ["제주도"])

    def test_semantic_failure_falls_back_to_string_hits(self) -> None:
        with (
            patch.object(mm_meta, "get_current_settings", return_value=_cfg("pg")),
            patch.object(mm_meta, "active_embed_channel", side_effect=RuntimeError("죽음")),
        ):
            out, _scope = mm_meta.search_and_refine(
                self.items, q="제주", refine=None, run_in_db=lambda fn: fn(object()))
        self.assertEqual([r["entity_uid"] for r in out], ["제주도"])

    def test_opensearch_hits_replace_list_and_carry_booleans(self) -> None:
        hits = [{"entity_type": "장소", "entity_uid": "서울특별시", "by_text": False,
                 "by_semantic": True, "cosine": 0.42}]
        with (
            patch.object(mm_meta, "get_current_settings", return_value=_cfg("opensearch")),
            patch.object(mm_meta, "active_embed_channel", return_value="st_api"),
            patch.object(mm_meta, "embed_query_for_media_search", return_value=[0.0]),
            patch.object(mm_meta, "search_entities_hybrid", return_value=hits),
            patch("src.search.opensearch_sync.get_client", return_value=object()),
        ):
            out, _scope = mm_meta.search_and_refine(
                self.items, q="수도", refine=None, run_in_db=lambda fn: fn(object()))
        self.assertEqual([r["entity_uid"] for r in out], ["서울특별시"])
        self.assertIs(out[0]["by_semantic"], True)
        self.assertIs(out[0]["by_text"], False)
        self.assertIn("0.42", out[0]["match_reason"])

    def test_unknown_entity_in_hits_is_dropped(self) -> None:
        # 색인이 낡아 목록에 없는 개체가 오면 버린다 — 목록이 정본이다.
        hits = [{"entity_type": "장소", "entity_uid": "없는곳", "by_text": True}]
        with (
            patch.object(mm_meta, "get_current_settings", return_value=_cfg("opensearch")),
            patch.object(mm_meta, "active_embed_channel", return_value="st_api"),
            patch.object(mm_meta, "embed_query_for_media_search", return_value=[0.0]),
            patch.object(mm_meta, "search_entities_hybrid", return_value=hits),
            patch("src.search.opensearch_sync.get_client", return_value=object()),
        ):
            out, _scope = mm_meta.search_and_refine(
                self.items, q="x", refine=None, run_in_db=lambda fn: fn(object()))
        self.assertEqual(out, [])

    def test_refine_narrows_after_search_and_reports_scope(self) -> None:
        with patch.object(mm_meta, "get_current_settings", return_value=_cfg()):
            out, scope = mm_meta.search_and_refine(
                self.items, q=None, refine="서울", run_in_db=lambda fn: fn(object()))
        self.assertEqual([r["entity_uid"] for r in out], ["서울특별시"])
        self.assertEqual(scope, 2)  # 좁히기 전 건수 — 화면이 "지우면 N건" 에 쓴다


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


if __name__ == "__main__":
    unittest.main()
