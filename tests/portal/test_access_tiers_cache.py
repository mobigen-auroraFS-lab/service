"""등급표 캐시 — 같은 도메인은 수명 동안 DB 를 다시 읽지 않고, 복사본을 주며, 실패는 캐시하지 않는다."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from service.portal.common import access_tiers
from service.portal.common.access_tiers import (
    cache_seconds,
    clear_access_tier_cache,
    fetch_access_tiers,
)

TIERS = {"summary": "authorized", "stt": "authorized"}


class TestAccessTiersCache(unittest.TestCase):
    def setUp(self) -> None:
        clear_access_tier_cache()
        self._env = mock.patch.dict(os.environ, {"PORTAL_ACCESS_TIER_CACHE_SECONDS": "60"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        clear_access_tier_cache()

    def test_같은_도메인은_한_번만_읽는다(self) -> None:
        with mock.patch.object(access_tiers, "_fetch_from_db", return_value=dict(TIERS)) as db:
            self.assertEqual(TIERS, fetch_access_tiers(object(), "general"))
            self.assertEqual(TIERS, fetch_access_tiers(object(), "general"))
        db.assert_called_once()

    def test_도메인마다_따로_든다(self) -> None:
        with mock.patch.object(access_tiers, "_fetch_from_db", side_effect=lambda _c, d: {"k": d}) as db:
            self.assertEqual({"k": "general"}, fetch_access_tiers(object(), "general"))
            self.assertEqual({"k": "medical"}, fetch_access_tiers(object(), "medical"))
        self.assertEqual(2, db.call_count)

    def test_복사본을_준다(self) -> None:
        with mock.patch.object(access_tiers, "_fetch_from_db", return_value=dict(TIERS)):
            first = fetch_access_tiers(object(), "general")
            first["summary"] = "public"             # 호출부가 고쳐도
            first["extra"] = "x"
            self.assertEqual(TIERS, fetch_access_tiers(object(), "general"))   # 캐시는 그대로

    def test_수명이_지나면_다시_읽는다(self) -> None:
        with mock.patch.object(access_tiers, "_fetch_from_db", return_value=dict(TIERS)) as db, \
                mock.patch.object(access_tiers.time, "monotonic", side_effect=[0.0, 0.0, 61.0, 61.0]):
            fetch_access_tiers(object(), "general")         # 읽기(now=0 · 저장 0)
            fetch_access_tiers(object(), "general")         # 61초 뒤 — 수명 60 을 넘겼다
        self.assertEqual(2, db.call_count)

    def test_수명_안이면_다시_읽지_않는다(self) -> None:
        with mock.patch.object(access_tiers, "_fetch_from_db", return_value=dict(TIERS)) as db, \
                mock.patch.object(access_tiers.time, "monotonic", side_effect=[0.0, 0.0, 59.0]):
            fetch_access_tiers(object(), "general")
            fetch_access_tiers(object(), "general")
        db.assert_called_once()

    def test_0_이면_끈다(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_ACCESS_TIER_CACHE_SECONDS": "0"}), \
                mock.patch.object(access_tiers, "_fetch_from_db", return_value=dict(TIERS)) as db:
            fetch_access_tiers(object(), "general")
            fetch_access_tiers(object(), "general")
        self.assertEqual(2, db.call_count)

    def test_읽기_실패는_캐시하지_않는다(self) -> None:
        boom = RuntimeError("DB 끊김")
        with mock.patch.object(access_tiers, "_fetch_from_db", side_effect=[boom, dict(TIERS)]) as db:
            with self.assertRaises(RuntimeError):
                fetch_access_tiers(object(), "general")
            self.assertEqual(TIERS, fetch_access_tiers(object(), "general"))   # 다음 요청은 다시 읽는다
        self.assertEqual(2, db.call_count)

    def test_수명_환경변수_해석(self) -> None:
        for raw, want in (("", 60), ("30", 30), ("0", 0), ("-3", 0), ("abc", 60)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"PORTAL_ACCESS_TIER_CACHE_SECONDS": raw}):
                self.assertEqual(want, cache_seconds())


if __name__ == "__main__":
    unittest.main()
