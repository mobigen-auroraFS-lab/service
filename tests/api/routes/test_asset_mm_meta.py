"""``GET /assets/{id}/mm-meta`` — 파일이 속한 개체마다 멀티모달 목록에 보이는지(``listed``)를 함께 싣는다(웹 점검 #32)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets
from src.mm_meta.rules import MIN_BUNDLE_SIZE

A1 = "018f0000-0000-7000-8000-0000000000a1"


class TestAssetEntities(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)

    def _get(self, items):
        class _Asset:
            def entities_of(self, *, asset_id):
                return items

        class _Repo:
            asset = _Asset()
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            return self.client.get(f"/assets/{A1}/mm-meta")

    def test_목록_노출_임계로_listed_를_단다(self) -> None:
        """임계값은 코어 값 하나다 — 화면이 상수로 따로 들면 바꿀 때 두 곳을 고쳐야 한다."""
        items = [{"entity_type": "작품", "entity_uid": "기생충", "name": "기생충", "bundle_size": 77,
                  "edge_id": "e1", "status": "proposed", "reason": ""},
                 {"entity_type": "인물", "entity_uid": "박소담", "name": "박소담", "bundle_size": 1,
                  "edge_id": "e2", "status": "proposed", "reason": ""},
                 {"entity_type": "인물", "entity_uid": "경계", "name": "경계", "bundle_size": MIN_BUNDLE_SIZE,
                  "edge_id": "e3", "status": "proposed", "reason": ""}]
        r = self._get(items)
        self.assertEqual(200, r.status_code)
        self.assertEqual([True, False, True], [i["listed"] for i in r.json()["items"]])
        self.assertEqual(77, r.json()["items"][0]["bundle_size"], "다른 칸은 그대로다")


if __name__ == "__main__":
    unittest.main()
