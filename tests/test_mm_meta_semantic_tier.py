"""뜻으로 상위인 개체를 **앞자리로 승급**시킨다(2026-09-21).

무엇이 문제였나: 화면은 ``prio_tier DESC, confirmed_count DESC`` 로 정렬한다(099 — 커서가
성립하려면 DB 정렬이어야 한다). 그런데 이름이 하나도 안 맞는 개념 질의에서는 전원이 티어 0 이
되어 **구성 자산 수가 순서를 지배**했다. 실측(「남자 배우」): kNN 1등은 하정우인데 화면 1위는
채원빈이었다(하정우는 자산이 적어 뒤로 밀림). 사용자가 보는 것은 앞 5~7개라 "잘못 나온다"가 된다.

어떻게: 티어를 3단으로 — 이름 일치(2) · 뜻 상위 N(1) · 나머지(0). 관련도로 **정렬**하는 것이
아니라 관련도 상위를 **승급**시키는 것이라, 099 가 막은 "관련도순은 커서를 못 만든다" 제약에
걸리지 않는다.

🔴 **커서의 티어 계산이 SQL 과 같아야 한다.** 갈라지면 다음 쪽이 엉뚱한 자리에서 이어져 개체가
빠진다 — 오류 없이 조용히. 이 파일이 그 일치를 봉인한다.
"""
from __future__ import annotations

import unittest

from service.portal import mm_meta


class TestSemanticFirstKeys(unittest.TestCase):
    """몇 개를 앞세울지는 **화면 정책**이라 백엔드가 정한다(093 경계)."""

    def test_뜻_상위_N개만_고른다(self) -> None:
        ranked = tuple(("인물", f"배우{i}") for i in range(10))
        got = mm_meta.semantic_first_keys(ranked)
        self.assertEqual(len(got), mm_meta.SEMANTIC_FIRST_N)
        self.assertEqual(got, {("인물", f"배우{i}") for i in range(mm_meta.SEMANTIC_FIRST_N)})

    def test_후보가_N보다_적으면_있는_만큼(self) -> None:
        ranked = (("인물", "하정우"), ("인물", "정해인"))
        self.assertEqual(mm_meta.semantic_first_keys(ranked), {("인물", "하정우"), ("인물", "정해인")})

    def test_비었으면_빈_집합(self) -> None:
        self.assertEqual(mm_meta.semantic_first_keys(()), set())

    def test_N은_첫_화면에_담기는_수다(self) -> None:
        """너무 크면 뜻 10등처럼 이미 먼 것까지 앞으로 오고, 너무 작으면 앞자리가 안 바뀐다."""
        self.assertEqual(mm_meta.SEMANTIC_FIRST_N, 5)


class TestCursorTierMatchesSql(unittest.TestCase):
    """커서가 적는 티어가 SQL 의 ``CASE`` 와 **같은 값**이어야 한다."""

    @staticmethod
    def _rows(n: int, uid: str) -> list[dict]:
        rows = [{"entity_type": "인물", "entity_uid": f"x{i}", "confirmed_count": 10 - i}
                for i in range(n - 1)]
        rows.append({"entity_type": "인물", "entity_uid": uid, "confirmed_count": 1})
        return rows

    def _tier_of(self, uid: str, *, first: set | None, semantic: set | None) -> int:
        """마지막 행으로 커서를 만들고 거기 적힌 티어를 꺼낸다."""
        import base64
        import json

        rows = self._rows(3, uid)
        token = mm_meta.next_entity_cursor(
            rows, page_size=3, scope="s", uid_first=first, uid_semantic=semantic)
        assert token is not None
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return payload["s"][0]

    def test_이름_일치는_2(self) -> None:
        self.assertEqual(self._tier_of("숭례문", first={("인물", "숭례문")}, semantic=None), 2)

    def test_뜻_상위는_1(self) -> None:
        self.assertEqual(self._tier_of("하정우", first=None, semantic={("인물", "하정우")}), 1)

    def test_둘_다면_이름이_이긴다(self) -> None:
        """SQL 의 ``CASE`` 가 이름을 먼저 보므로 커서도 같아야 한다 — 갈라지면 경계에서 샌다."""
        self.assertEqual(
            self._tier_of("숭례문", first={("인물", "숭례문")}, semantic={("인물", "숭례문")}), 2)

    def test_아무_데도_없으면_0(self) -> None:
        self.assertEqual(self._tier_of("무명", first={("인물", "숭례문")},
                                       semantic={("인물", "하정우")}), 0)

    def test_집합을_안_주면_0(self) -> None:
        self.assertEqual(self._tier_of("무명", first=None, semantic=None), 0)


if __name__ == "__main__":
    unittest.main()
