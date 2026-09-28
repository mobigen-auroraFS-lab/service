"""태그 목록 캐시 — 같은 조건은 수명 동안 DB 를 다시 부르지 않는다(2026-09-28 · 조건 없는 목록 2~3초)."""

from __future__ import annotations

import unittest
from unittest import mock

from service.portal.repositories import catalog_repo
from service.portal.repositories.catalog_repo import CatalogRepository


class TestTagsCache(unittest.TestCase):
    def setUp(self) -> None:
        catalog_repo.clear_tags_cache()
        self.addCleanup(catalog_repo.clear_tags_cache)
        self.repo = CatalogRepository(None)
        self.calls = 0

        def fake_rows(_sql, _params):
            self.calls += 1
            return [{"tag": f"태그{self.calls}", "count": 3}]

        patcher = mock.patch.object(CatalogRepository, "rows", side_effect=fake_rows)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tags(self, **kw):
        base = {"topics": [], "subtopics": [], "q": None, "limit": 10}
        return self.repo.tags(**{**base, **kw})

    def test_같은_조건은_한_번만_센다(self) -> None:
        with mock.patch.dict("os.environ", {catalog_repo.TAGS_CACHE_ENV: "300"}):
            first = self._tags()
            first[0]["count"] = 999          # 받은 쪽이 고쳐도 캐시는 그대로다
            again = self._tags()
        self.assertEqual(1, self.calls)
        self.assertEqual([{"tag": "태그1", "count": 3}], again)

    def test_조건이_다르면_따로_센다(self) -> None:
        with mock.patch.dict("os.environ", {catalog_repo.TAGS_CACHE_ENV: "300"}):
            self._tags()
            self._tags(topics=["역사·문화유산"])
            self._tags(q="전통")
            self._tags(topics=["역사·문화유산"])
        self.assertEqual(3, self.calls)

    def test_수명이_지나면_다시_센다(self) -> None:
        with mock.patch.dict("os.environ", {catalog_repo.TAGS_CACHE_ENV: "300"}), \
             mock.patch.object(catalog_repo.time, "monotonic", side_effect=[0.0, 0.0, 301.0, 301.0]):
            self._tags()
            self._tags()
        self.assertEqual(2, self.calls)

    def test_0_이면_끈다(self) -> None:
        with mock.patch.dict("os.environ", {catalog_repo.TAGS_CACHE_ENV: "0"}):
            self._tags()
            self._tags()
        self.assertEqual(2, self.calls)


if __name__ == "__main__":
    unittest.main()
