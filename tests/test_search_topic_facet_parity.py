"""093 2단계 — 주제 패싯이 **코어 집계 정본**을 부르되, 응답은 종전과 글자까지 같다.

무엇을 봉인하나: 두 가지다.
① 세는 규칙(자산당 1회 계수·건수 내림차순·동수 이름순)은 서비스가 자체 구현하지 않고 코어
   ``aggregate_facets`` 를 그대로 참조한다(093 규칙 ① — 어디서 세어도 같은 건수).
② 바꾸기 전 서비스 자체 구현(아래 ``_legacy_topic_facet`` 에 그대로 옮겨 둠)과 새 구현이
   **무작위 입력 전수에서 정확히 같은 응답**을 낸다(사용자 요구 — "책임 분리 후 모든 기능은 이전과
   같아야 한다"). 실서버 골든 대조(1,542요청)는 PR 에 별도 기록한다.

DB·OpenSearch 없음(순수).
"""

from __future__ import annotations

import random
import unittest
from typing import Any

from service.api import routes_search
from src.search.facets import aggregate_facets as core_aggregate_facets


def _legacy_topic_facet(grouped: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """바꾸기 전 서비스 자체 구현(2026-09-07 `main` 의 ``_search_topic_facet`` 본문 그대로)."""
    topic_assets: dict[str, set[str]] = {}
    topic_subs: dict[str, dict[str, set[str]]] = {}
    for rows in grouped.values():
        for r in rows:
            aid = str(r.get("asset_id") or "")
            if not aid:
                continue
            pairs = [str(p) for p in (r.get("topic_pairs") or []) if p]
            if not pairs:
                pairs = [str(t) for t in (r.get("topics") or []) if t]
            for pair in pairs:
                idx = pair.find(">")
                tk = pair if idx < 0 else pair[:idx]
                sk = "" if idx < 0 else pair[idx + 1 :]
                if not tk:
                    continue
                topic_assets.setdefault(tk, set()).add(aid)
                sub_map = topic_subs.setdefault(tk, {})
                if sk:
                    sub_map.setdefault(sk, set()).add(aid)
    facet = []
    for tk, assets in topic_assets.items():
        subs = [
            {"subtopic_ko": sk, "asset_count": len(a)}
            for sk, a in topic_subs.get(tk, {}).items()
        ]
        subs.sort(key=lambda s: (-s["asset_count"], s["subtopic_ko"]))
        facet.append({"topic_ko": tk, "asset_count": len(assets), "subtopics": subs})
    facet.sort(key=lambda f: (-f["asset_count"], f["topic_ko"]))
    return facet


_PAIRS = [
    "한식>김치", "한식>떡", "한식", "양식>파스타", "양식>피자>나폴리", "여행>파리", "여행>로마",
    ">고아", "부모만>", "", " 공백 >앞뒤 ", "한식>김치", "a>b", "a>c", "b>a",
]
_TOPICS = ["한식", "양식", "여행", "", "스포츠"]


def _random_grouped(rng: random.Random) -> dict[str, list[dict[str, Any]]]:
    """모달리티 1~3개 · 행 0~6개 · 자산 id 는 작은 풀에서 뽑아 버킷 간 중복이 생기게 한다."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for modality in rng.sample(["text", "image", "video", "audio"], rng.randint(1, 3)):
        rows: list[dict[str, Any]] = []
        for _ in range(rng.randint(0, 6)):
            row: dict[str, Any] = {"asset_id": rng.choice(["a1", "a2", "a3", "a4", "", None])}
            kind = rng.random()
            if kind < 0.6:
                row["topic_pairs"] = [rng.choice(_PAIRS) for _ in range(rng.randint(0, 4))]
                if rng.random() < 0.5:
                    row["topics"] = [rng.choice(_TOPICS) for _ in range(rng.randint(0, 2))]
            elif kind < 0.85:
                row["topics"] = [rng.choice(_TOPICS) for _ in range(rng.randint(0, 3))]
            elif kind < 0.9:
                row["topic_pairs"] = None
            rows.append(row)
        grouped[modality] = rows
    return grouped


class TestWiring(unittest.TestCase):
    """세는 규칙은 코어 정본을 **그대로** 참조한다."""

    def test_집계는_코어_정본을_쓴다(self) -> None:
        self.assertIs(routes_search.aggregate_facets, core_aggregate_facets)


class TestParityWithLegacy(unittest.TestCase):
    """새 구현 == 종전 자체 구현. 손으로 짠 경계 사례 + 무작위 1,000조합."""

    def test_경계_사례(self) -> None:
        cases = [
            {},
            {"text": []},
            {"text": [{"asset_id": "a1", "topic_pairs": ["한식>김치", "한식>떡"]}]},
            # 같은 자산이 두 버킷 → 1건
            {"text": [{"asset_id": "a1", "topic_pairs": ["한식>김치"]}],
             "video": [{"asset_id": "a1", "topic_pairs": ["한식>김치"]}]},
            # 자식에 '>' 포함 — 첫 '>' 로만 자른다
            {"text": [{"asset_id": "a1", "topic_pairs": ["양식>피자>나폴리"]}]},
            # 부모 빈 짝·자식 빈 짝·빈 문자열
            {"text": [{"asset_id": "a1", "topic_pairs": [">고아", "부모만>", ""]}]},
            # 짝 없음 → topics 폴백
            {"text": [{"asset_id": "a1", "topics": ["한식", "양식"]}]},
            # 자산 id 없음 → 세지 않음
            {"text": [{"asset_id": "", "topic_pairs": ["한식>김치"]}, {"topic_pairs": ["한식>김치"]}]},
            # 동수 정렬 — 이름순
            {"text": [{"asset_id": "a1", "topic_pairs": ["b>a", "a>c", "a>b"]}]},
            # 앞뒤 공백은 그대로(다듬지 않는다)
            {"text": [{"asset_id": "a1", "topic_pairs": [" 공백 >앞뒤 "]}]},
        ]
        for grouped in cases:
            with self.subTest(grouped=grouped):
                self.assertEqual(routes_search._search_topic_facet(grouped), _legacy_topic_facet(grouped))

    def test_무작위_1000조합(self) -> None:
        rng = random.Random(2026_09_07)
        for _ in range(1000):
            grouped = _random_grouped(rng)
            self.assertEqual(
                routes_search._search_topic_facet(grouped), _legacy_topic_facet(grouped), grouped
            )


if __name__ == "__main__":
    unittest.main()
