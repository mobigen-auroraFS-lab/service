"""개체 묶음 헤더 — ``X-Bundle-Count`` 는 **실제로 담긴 수**여야 한다(2026-09-28).

종전에는 DB 기준으로 세어, 원본이 없는 장비에서 "2건"이라 하고 목록 파일만 든 zip 을 보냈다.
고른 자산 묶음(``POST /assets/bundle``)은 2026-09-22 에 같은 결함을 고쳤다.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest import mock

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app  # noqa: E402
from service.api.routes import mm_meta as route  # noqa: E402


class TestEntityBundleHeaders(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)  # noqa: SIM115
        self.tmp.write(b"hello")
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)
        self.targets = [
            {"asset_id": "a1", "fs_path": self.tmp.name, "file_name": "a.txt"},
            {"asset_id": "a2", "fs_path": "/없는/파일.txt", "file_name": "b.txt"},
        ]

    def _card(self, truncated: bool):
        with mock.patch.object(route.DbManager, "read",
                               return_value=(self.targets, "경복궁", truncated)):
            return self.client.get("/mm-meta/place/x/bundle")

    def test_카드_묶음은_담긴_수와_빠진_수를_따로_센다(self) -> None:
        r = self._card(False)
        self.assertEqual(200, r.status_code)
        self.assertEqual(("1", "1"), (r.headers["X-Bundle-Count"], r.headers["X-Bundle-Missing"]))
        self.assertNotIn("X-Bundle-Truncated", r.headers)
        names = zipfile.ZipFile(BytesIO(r.content)).namelist()
        self.assertEqual(["a.txt", "_manifest.json"], names)
        self.assertIn("_1files.zip", r.headers["Content-Disposition"])

    def test_잘렸으면_상한을_알린다(self) -> None:
        r = self._card(True)
        self.assertEqual(str(route.mm_meta.CARD_BUNDLE_MAX_ASSETS), r.headers["X-Bundle-Truncated"])

    def test_좁힌_개체_묶음도_담긴_수로_센다(self) -> None:
        rows = [{**t, "file_size": 5} for t in self.targets]
        with mock.patch.object(route.DbManager, "read", return_value=rows):
            r = self.client.get("/mm-meta/bundle")
        self.assertEqual(200, r.status_code)
        self.assertEqual(("1", "1", "10"), (r.headers["X-Bundle-Count"], r.headers["X-Bundle-Missing"],
                                            r.headers["X-Bundle-Bytes"]))


if __name__ == "__main__":
    unittest.main()
