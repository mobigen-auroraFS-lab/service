"""093 2단계 — 검색 프리셋(``preset``·``meta.tuning``)과 태그 축(``tag``·``meta.tag_facets``) 배선 테스트.

무엇을 봉인하나:
① 프리셋은 **닫힌 이름**이고 지금은 ``default`` 하나다. ``default`` 는 서버 설정값 그대로라 프리셋을
   안 준 요청과 명시한 요청의 응답이 같다. 모르는 이름은 400.
② 적용된 튜닝은 ``meta.tuning`` 에 **이름 + 값 전부**로 실린다(2026-09-07 사용자 결정 — 재현성).
③ ``tag`` 는 정규화·가공 없이 코어 ``SearchFilters.tags`` 로 간다(083 전달물 §1). 태그 패싯은 코어
   ``aggregate_tag_facets`` 산출물 그대로이며 응답에는 ``items``·``has_more`` 만 싣는다.
④ 판단 함수는 전부 코어 참조 — 서비스는 배선만.

DB·OpenSearch 없음. 라우트는 ``TestClient`` + ``search_hybrid`` 대역으로 돈다(설정 미초기화 →
튜닝·패싯 한계는 코어 상수 폴백).
"""

from __future__ import annotations

import inspect
import json
import os
import unittest
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from service.api import app, routes_search
from service.portal import search_presets
from src.config.search_constants import TAG_FACET_MIN_COUNT_DEFAULT, TAG_FACET_TOP_N_DEFAULT
from src.search.search_service import search_hybrid as core_search_hybrid
from src.search.search_tuning import SearchTuning
from src.search.tag_facets import aggregate_tag_facets as core_aggregate_tag_facets

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}


def _passthrough_db(callback):
    """``_run_in_db`` 대역 — 가짜 conn 으로 즉시 실행(DB 불필요)."""
    return callback(object())


def _fake_result() -> dict:
    """``search_hybrid`` 대역 결과 — 행마다 태그 원문을 실어 태그 패싯을 검증할 수 있게."""
    return {
        "query": "김치",
        "results": {
            "text_documents": [
                {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1",
                 "tags": ["김치", "전통음식"]},
                {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2",
                 "tags": ["전통 음식"]},
            ],
            "image": [
                {"id": "a3", "similarity": 0.7, "file_uri": "/x/a3.png", "summary": "s3",
                 "tags": ["김치", "혼자"]},
            ],
        },
        "meta": {},
    }


def _fake_cfg(**over) -> SimpleNamespace:
    """``SearchTuning.from_settings`` 가 읽는 ``cfg.search`` 필드를 가진 가짜 설정."""
    base = {
        "fusion_weights": (0.6, 0.4), "os_cutoff_enabled": True, "os_cutoff_eps": 0.22, "os_cutoff_floor": 0.55,
        "os_result_floor": 0.33, "os_bm25_operator": "or", "os_rerank_enabled": True, "os_rerank_top_r": 7,
        "os_rerank_tau": 0.1, "os_rerank_model": "m", "about_filter_enabled": True,
        "evidence_rescue_enabled": False, "evidence_debug": True, "tag_facet_top_n": 3, "tag_facet_min_count": 1,
    }
    base.update(over)
    return SimpleNamespace(search=SimpleNamespace(**base))


class TestPresets(unittest.TestCase):
    """프리셋 모듈 — 닫힌 어휘 · default = 설정값 그대로 · meta 는 이름 + 값 전부."""

    def test_지금_프리셋은_default_하나다(self) -> None:
        self.assertEqual(search_presets.PRESETS, ("default",))
        self.assertEqual(search_presets.DEFAULT_PRESET, "default")

    def test_default_는_기본_튜닝을_그대로_돌려준다(self) -> None:
        base = SearchTuning(cutoff_eps=0.01)
        self.assertIs(search_presets.resolve_tuning("default", base), base)

    def test_모르는_이름은_예외(self) -> None:
        with self.assertRaises(ValueError):
            search_presets.resolve_tuning("precise", SearchTuning())

    def test_meta_는_이름과_13개_값_전부_JSON_가능(self) -> None:
        tuning = SearchTuning(weights=(0.7, 0.3))
        meta = search_presets.tuning_meta("default", tuning)
        self.assertEqual(set(meta), {"preset"} | {f.name for f in fields(SearchTuning)})
        self.assertEqual(meta["preset"], "default")
        self.assertEqual(meta["weights"], [0.7, 0.3])
        json.dumps(meta)  # 실패하면 예외


class TestRouteHelpers(unittest.TestCase):
    """라우트 보조 함수 — 검증·설정 해소·폴백이 코어 규칙과 같다."""

    def test_parse_preset_은_정규화하고_모르면_400(self) -> None:
        self.assertEqual(routes_search._parse_preset("default"), "default")
        self.assertEqual(routes_search._parse_preset(" DEFAULT "), "default")
        self.assertEqual(routes_search._parse_preset(""), "default")
        with self.assertRaises(HTTPException) as ctx:
            routes_search._parse_preset("bogus")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_base_tuning_은_설정에서_코어와_같은_규칙으로_해소한다(self) -> None:
        cfg = _fake_cfg()
        with patch.object(routes_search, "get_current_settings", return_value=cfg):
            self.assertEqual(routes_search._base_tuning(), SearchTuning.from_settings(cfg))

    def test_base_tuning_설정_미초기화면_상수_기본값(self) -> None:
        with patch.object(routes_search, "get_current_settings", side_effect=RuntimeError("미초기화")):
            self.assertEqual(routes_search._base_tuning(), SearchTuning())

    def test_tag_facet_limits_는_설정값_또는_코어_상수_폴백(self) -> None:
        with patch.object(routes_search, "get_current_settings", return_value=_fake_cfg()):
            self.assertEqual(routes_search._tag_facet_limits(), (3, 1))
        with patch.object(routes_search, "get_current_settings", side_effect=RuntimeError("미초기화")):
            self.assertEqual(
                routes_search._tag_facet_limits(), (TAG_FACET_TOP_N_DEFAULT, TAG_FACET_MIN_COUNT_DEFAULT)
            )

    def test_tag_facets_는_코어_산출물에서_items_has_more_만(self) -> None:
        grouped = {
            "text": [{"tags": ["김치", "전통음식"]}, {"tags": ["전통 음식"]}],
            "image": [{"tags": ["김치", "혼자"]}],
        }
        with patch.object(routes_search, "_tag_facet_limits", return_value=(12, 2)):
            out = routes_search._tag_facets(grouped)
        rows = grouped["text"] + grouped["image"]
        core = core_aggregate_tag_facets(rows, top_n=12, min_count=2)
        self.assertEqual(out, {"items": core["items"], "has_more": core["has_more"]})
        # 김치 2건 · 전통음식/전통 음식 은 한 항목 2건(라벨은 코드포인트 앉은 쪽) · 혼자(1건)는 감춤.
        self.assertEqual(out["items"], [{"label": "김치", "count": 2}, {"label": "전통 음식", "count": 2}])


class TestWiring(unittest.TestCase):
    """판단은 코어 함수를 **그대로** 참조한다."""

    def test_검색과_태그_집계는_코어_함수다(self) -> None:
        self.assertIs(routes_search.search_hybrid, core_search_hybrid)
        self.assertIs(routes_search.aggregate_tag_facets, core_aggregate_tag_facets)

    def test_파라미터_기본값(self) -> None:
        params = inspect.signature(routes_search.search).parameters
        self.assertEqual(params["preset"].default.default, "default")
        self.assertIsNone(params["tag"].default.default)


class TestRoute(unittest.TestCase):
    """``/search`` 라우트 — 대역 검색으로 배선과 응답 모양을 본다."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, repl in (
            ("service.api._infra._run_in_db", _passthrough_db),
            ("service.api.routes_search.fetch_access_tiers", lambda *_a, **_k: {}),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    @patch("service.api.routes_search.search_hybrid")
    def test_기본_요청_meta_에_tuning_과_tag_facets_가_실린다(self, mock_search) -> None:
        mock_search.return_value = _fake_result()
        body = self.client.get("/search", params={"q": "김치", "size": 10}).json()
        tuning = body["meta"]["tuning"]
        self.assertEqual(tuning["preset"], "default")
        self.assertEqual(set(tuning), {"preset"} | {f.name for f in fields(SearchTuning)})
        # 설정 미초기화 → 코어 상수 기본값이 그대로 전달·기록된다(코어 자체 폴백과 같은 값).
        self.assertEqual(mock_search.call_args.kwargs["tuning"], SearchTuning())
        self.assertEqual(tuning["cutoff_floor"], SearchTuning().cutoff_floor)
        self.assertEqual(
            body["meta"]["tag_facets"],
            {"items": [{"label": "김치", "count": 2}, {"label": "전통 음식", "count": 2}], "has_more": False},
        )
        # 행에 태그 원문이 항상 실린다(083 FR-106).
        self.assertEqual(body["results"]["text"][0]["tags"], ["김치", "전통음식"])
        # 필터를 안 줬으니 filters 키도, 코어로 간 필터도 없다(종전과 같다).
        self.assertNotIn("filters", body["meta"])
        self.assertIsNone(mock_search.call_args.kwargs["search_filters"])

    @patch("service.api.routes_search.search_hybrid")
    def test_preset_default_명시는_생략과_같은_응답(self, mock_search) -> None:
        mock_search.return_value = _fake_result()
        a = self.client.get("/search", params={"q": "김치", "size": 10}).text
        mock_search.return_value = _fake_result()
        b = self.client.get("/search", params={"q": "김치", "size": 10, "preset": "default"}).text
        self.assertEqual(a, b)

    @patch("service.api.routes_search.search_hybrid")
    def test_모르는_preset_은_400_이고_검색하지_않는다(self, mock_search) -> None:
        resp = self.client.get("/search", params={"q": "김치", "preset": "precise"})
        self.assertEqual(resp.status_code, 400)
        mock_search.assert_not_called()

    @patch("service.api.routes_search.search_hybrid")
    def test_tag_는_가공_없이_코어_필터로_가고_meta_filters_에_남는다(self, mock_search) -> None:
        mock_search.return_value = _fake_result()
        body = self.client.get(
            "/search", params=[("q", "김치"), ("tag", "전통 음식"), ("tag", " 검은색 ")]
        ).json()
        filters = mock_search.call_args.kwargs["search_filters"]
        # 앞뒤 공백 정리는 코어 파서가 한다(서비스는 손대지 않고 그대로 넘긴다).
        self.assertEqual(filters.tags, ("전통 음식", "검은색"))
        self.assertEqual(body["meta"]["filters"]["tag"], ["전통 음식", "검은색"])

    @patch("service.api.routes_search.search_hybrid")
    def test_좁힌_뒤_태그_패싯도_보이는_행으로_다시_센다(self, mock_search) -> None:
        mock_search.return_value = _fake_result()
        body = self.client.get("/search", params={"q": "김치", "refine": "s1"}).json()
        # s1 행(a1)만 남음 → 태그는 각 1건 → 하한 2 미달로 전부 감춤.
        self.assertEqual(body["meta"]["counts"], {"text": 1, "image": 0})
        self.assertEqual(body["meta"]["tag_facets"], {"items": [], "has_more": False})


if __name__ == "__main__":
    unittest.main()
