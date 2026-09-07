"""093 1단계 — 백엔드 사본을 코어 정본 참조로 바꿔도 **값·순서·동작이 전과 같다**. DB·네트워크 없음.

바꾼 것 다섯 가지와, 각각 "전과 같다"를 어떻게 확인하는지:
  ① 계보 활동명 ``relations.proposed.v1`` → 코어 ``LineageActivity``       — 값 동일
  ② 관계 종류 상태 ``("active", "inactive")`` → 코어 ``RelationKindStatus`` — 값·순서 동일(400 문구 순서)
  ③ 노출 등급 순위표 ``{"strong": 0, "weak": 1}`` → 코어 ``tier_rank``      — 강<약<모름 순위 동일
  ④ 버킷→모달리티 표 → 코어 ``BUCKET_TO_MODALITY``                          — 표 내용 동일
  ⑤ 자산 id 접두 정규식 + 점수 정화 → 코어 ``strip_asset_id_prefix``·``safe_float`` — 표본 입력 동일
"""
from __future__ import annotations

import math
import re
import unittest

from service.api import routes_admin
from service.portal import asset_detail, asset_stats, lineage_query, search_group
from src.config.search_modalities import BUCKET_TO_MODALITY
from src.database.lineage_activity import LineageActivity
from src.domain.status_vocab import RelationKindStatus
from src.relations.approval_policy import tier_rank


class TestLineageActivity(unittest.TestCase):
    def test_관계_제안_활동명이_전과_같다(self) -> None:
        self.assertEqual(asset_stats._RELATION_PROPOSED_ACTIVITY, "relations.proposed.v1")
        self.assertIs(asset_stats._RELATION_PROPOSED_ACTIVITY, LineageActivity.RELATIONS_PROPOSED)
        # lineage_query 는 asset_stats 의 같은 이름을 import 한다 — 두 집계가 같은 값을 본다.
        self.assertIs(lineage_query._RELATION_PROPOSED_ACTIVITY, asset_stats._RELATION_PROPOSED_ACTIVITY)


class TestRelationKindStatuses(unittest.TestCase):
    def test_값과_순서가_전과_같다(self) -> None:
        self.assertEqual(routes_admin._RELATION_KIND_STATUSES, ("active", "inactive"))
        self.assertEqual(list(routes_admin._RELATION_KIND_STATUSES), [s.value for s in RelationKindStatus])


class TestTierRank(unittest.TestCase):
    """종전 사본 ``_TIER_RANK = {"strong": 0, "weak": 1}`` · 모르는 값은 ``len`` (=2) 이었다."""

    _LEGACY = {"strong": 0, "weak": 1}

    def test_순위가_전과_같다(self) -> None:
        for t in ("strong", "weak", "", "None", "unknown"):
            with self.subTest(tier=t):
                self.assertEqual(tier_rank(t), self._LEGACY.get(t, len(self._LEGACY)))

    def test_병합_정렬이_전과_같다(self) -> None:
        # 고신뢰 약칸(0.9 weak)이 저신뢰 강칸(0.6 strong)을 밀어내지 않는다 — 등급이 신뢰도보다 앞선다.
        edges = [
            {"asset_id": "b", "kind_code": "same_domain", "confidence": 0.9, "tier": "weak",
             "edge_id": "e1", "direction": None, "is_symmetric": True, "topic": None, "reason": None,
             "folded_kind_codes": [], "file_name": "b.txt", "modality": "text"},
            {"asset_id": "a", "kind_code": "references", "confidence": 0.6, "tier": "strong",
             "edge_id": "e2", "direction": None, "is_symmetric": False, "topic": None, "reason": None,
             "folded_kind_codes": [], "file_name": "a.txt", "modality": "text"},
            {"asset_id": "c", "kind_code": "references", "confidence": 0.7, "tier": "",
             "edge_id": "e3", "direction": None, "is_symmetric": False, "topic": None, "reason": None,
             "folded_kind_codes": [], "file_name": "c.txt", "modality": "text"},
        ]
        merged = asset_detail._merge_relations_by_asset(edges)
        self.assertEqual([m["asset_id"] for m in merged], ["a", "b", "c"])
        self.assertEqual([m["tier"] for m in merged], ["strong", "weak", ""])


class TestBucketToModality(unittest.TestCase):
    _LEGACY = {"text_documents": "text", "audio": "audio", "image": "image", "video": "video"}

    def test_표가_전과_같다(self) -> None:
        self.assertEqual(dict(BUCKET_TO_MODALITY), self._LEGACY)

    def test_group_ranked_가_같은_모달리티_이름을_낸다(self) -> None:
        result = {"results": {"text_documents": [{"id": "x", "similarity": 0.5}], "video": [{"id": "y", "similarity": 0.4}],
                              "meta": []}}
        grouped = search_group.group_ranked(result, limit_per_modality=5)
        self.assertEqual(set(grouped), {"text", "video", "meta"})  # 미지정 버킷은 키 그대로(전과 같다)


class TestDisplayNameAndSimilarity(unittest.TestCase):
    """종전 사본(정규식·정화 로직)과 표본 입력에서 같은 결과."""

    _LEGACY_PREFIX = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}__"
    )

    def _legacy_display(self, uri: str) -> str:
        from src.config.filename_util import basename_of
        return self._LEGACY_PREFIX.sub("", basename_of(uri))

    def _legacy_finite(self, value) -> float:
        try:
            x = float(value)
        except (TypeError, ValueError):
            return 0.0
        return x if math.isfinite(x) else 0.0

    def test_표시_파일명이_전과_같다(self) -> None:
        cases = [
            "/data/archive/2026/09/018f0000-0000-7000-8000-000000000001__김치.txt",
            "/data/inbox/김치.txt",
            "https://example.com/a/b/018f0000-0000-7000-8000-000000000002__c.mp4?x=1#frag",
            "C:\\data\\018F0000-0000-7000-8000-000000000003__D.JPG",
            "abc__notuuid.txt",
            "",
        ]
        for uri in cases:
            with self.subTest(uri=uri):
                self.assertEqual(search_group.display_name(uri), self._legacy_display(uri))

    def test_점수_정화가_전과_같다(self) -> None:
        for v in (0.5, "0.25", None, "x", float("nan"), float("inf"), -1, 1e308, [], True):
            with self.subTest(v=v):
                self.assertEqual(search_group._row_similarity({"similarity": v}), self._legacy_finite(v))


if __name__ == "__main__":
    unittest.main()
