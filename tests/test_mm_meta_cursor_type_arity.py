"""개체 커서가 **자연키 전체**를 싣는가 — 표기만으로는 자리가 정해지지 않는다(2026-09-18).

무엇이 문제였나: 개체를 구별하는 자연키는 **(종류, 표기) 둘**인데 커서에는 표기만 실렸다.
같은 표기가 종류로 갈린 개체가 실제로 있고(``백두산`` 장소/작품 등 4쌍), 그 둘이 같은 우선
티어·같은 구성 자산 수가 되면 책갈피가 가리키는 자리가 **하나로 정해지지 않는다.** 그때
이어읽기는 한 쪽을 건너뛰거나 두 번 낸다 — 오류 없이 조용히.

여기서 봉인하는 것 둘:
① 새 커서에는 정렬값이 **넷**(티어·구성 자산 수·표기·종류) 실린다.
② 정렬값이 **셋**이던 옛 커서는 **400** 으로 끊긴다 — 조용히 통과하면 네 번째 자리가 비어
   엉뚱한 곳에서 이어진다. 끊는 쪽이 낫다(자료가 빠지는 것보다 다시 받는 편이 낫다).
"""
from __future__ import annotations

import unittest

# 같은 폴더의 라우트 테스트가 세워 둔 대역을 그대로 쓴다(중복 배선 방지).
from test_mm_meta_cursor_route import _RouteCase, routes_mm_meta

from src.search.cursor import encode_cursor


class TestEntityCursorCarriesType(_RouteCase):
    def _scope(self) -> str:
        """조건 없는 목록 요청의 지문 재료 — 위조 토큰도 여기까지는 맞춰야 한다.

        Returns:
            라우트가 만드는 것과 같은 지문 재료 문자열.
        """
        return routes_mm_meta.mm_meta.entity_cursor_scope(
            q=None, refine=None, entity_type=None, areas=[],
            min_bundle_size=routes_mm_meta._MIN_BUNDLE_SIZE)

    def test_커서에_정렬값이_넷_실린다(self) -> None:
        """자연키 전체가 들어가야 같은 자리를 가리키는 책갈피가 둘일 수 없다."""
        self.assertEqual(routes_mm_meta.mm_meta.ENTITY_CURSOR_ARITY, 4)

    def test_해독하면_종류까지_돌려준다(self) -> None:
        scope = self._scope()
        token = encode_cursor(routes_mm_meta.mm_meta.ENTITY_CURSOR_SORT,
                              [0, 5, "백두산", "장소"], scope=scope)
        got = routes_mm_meta.mm_meta.decode_entity_cursor(token, scope=scope)
        self.assertEqual(got, (0, 5, "백두산", "장소"))

    def test_종류가_빠진_옛_커서는_400_이다(self) -> None:
        """🔴 조용히 통과시키면 같은 표기의 두 개체 중 하나가 목록에서 사라진다."""
        token = encode_cursor(routes_mm_meta.mm_meta.ENTITY_CURSOR_SORT,
                              [0, 5, "백두산"], scope=self._scope())
        resp = self.client.get("/mm-meta", params={"limit": 3, "cursor": token})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_종류_자리가_비면_거부한다(self) -> None:
        """빈 문자열이 SQL 로 흘러가면 비교가 늘 참이 되어 같은 쪽을 다시 낸다."""
        scope = self._scope()
        token = encode_cursor(routes_mm_meta.mm_meta.ENTITY_CURSOR_SORT,
                              [0, 5, "백두산", ""], scope=scope)
        with self.assertRaises(routes_mm_meta.mm_meta.CursorError):
            routes_mm_meta.mm_meta.decode_entity_cursor(token, scope=scope)


if __name__ == "__main__":
    unittest.main()
