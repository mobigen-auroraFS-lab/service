"""``REVIEW_STATUSES`` — 코어 어휘에서 파생한 값이 종전 내부 상수와 **값·순서까지 같다**. DB 없음.

책무 분리 후 동작은 전과 같아야 한다. 검토 라우트의 400 응답 문구는 ``list(REVIEW_STATUSES)`` 를
그대로 찍으므로, 값뿐 아니라 **순서**가 같아야 문구가 같다. 코어의 ``_REVIEW_STATUSES`` 가 남아 있는
동안은 직접 대조하고, 코어가 그 이름을 지우면 첫 테스트는 skip 되고 둘째가 계약을 지킨다.
"""
from __future__ import annotations

import unittest

from service.api import routes_admin, routes_review
from service.portal.review_vocab import REVIEW_STATUSES
from src.domain.status_vocab import GraphEdgeStatus


class TestReviewStatuses(unittest.TestCase):
    """값·순서·출처."""

    def test_종전_코어_내부_상수와_값_순서가_같다(self) -> None:
        try:
            from src.relations.review import (
                _REVIEW_STATUSES as legacy,  # 코어 내부 이름 — 대조 전용
            )
        except ImportError:
            self.skipTest("코어가 _REVIEW_STATUSES 를 제거함 — 파생 계약(아래 테스트)이 대신 지킨다")
        self.assertEqual(tuple(REVIEW_STATUSES), tuple(legacy))

    def test_값과_순서는_proposed_active_rejected(self) -> None:
        self.assertEqual(REVIEW_STATUSES, ("proposed", "active", "rejected"))
        self.assertEqual(REVIEW_STATUSES, tuple(s.value for s in GraphEdgeStatus))

    def test_두_라우트가_같은_객체를_쓴다(self) -> None:
        self.assertIs(routes_admin.REVIEW_STATUSES, REVIEW_STATUSES)
        self.assertIs(routes_review.REVIEW_STATUSES, REVIEW_STATUSES)


if __name__ == "__main__":
    unittest.main()
