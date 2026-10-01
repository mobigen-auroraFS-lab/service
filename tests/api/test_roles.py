"""프로세스 역할 — 같은 코드를 api / files 로 나눠 띄웠을 때 열리는 창구가 서로 겹치지 않고 합치면 전부인가."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from service.api.roles import is_file_path

# 포함된 라우터는 지연 래퍼로 감싸여 ``app.routes`` 로는 경로가 안 보인다 — 문서(openapi)가 보여 주는 창구를 본다(HEAD /health 같은 문서 밖 경로는 제외).
_CODE = "import json;from service.api import app;print(json.dumps(sorted({(m.upper(),p) for p,v in app.openapi()['paths'].items() for m in v})))"


def _routes(role: str) -> set[tuple[str, str]]:
    env = {**os.environ, "PORTAL_ROLE": role, "PORTAL_LOG_CONFIGURE": "0", "PORTAL_WARMUP": "0"}
    out = subprocess.run([sys.executable, "-c", _CODE], env=env, capture_output=True, text=True, check=True).stdout
    return {tuple(x) for x in json.loads(out.strip().splitlines()[-1])}


class IsFilePathTest(unittest.TestCase):
    def test_파일_창구_경로만_참(self) -> None:
        for p in ("/assets/{asset_id}/download", "/assets/{asset_id}/content", "/assets/{asset_id}/bundle", "/assets/{asset_id}/thumbnail",
                  "/assets/bundle", "/mm-meta/bundle", "/mm-meta/{entity_type}/{entity_uid}/bundle"):
            self.assertTrue(is_file_path(p), p)
        for p in ("/assets/{asset_id}", "/assets/unclassified", "/assets/{asset_id}/mm-meta", "/mm-meta", "/mm-meta/facets",
                  "/mm-meta/{entity_type}/{entity_uid}", "/search", "/file-search", "/health", "/me"):
            self.assertFalse(is_file_path(p), p)


class RolesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.all, cls.api, cls.files = _routes("all"), _routes("api"), _routes("files")

    def test_api_와_files_는_겹치지_않는다(self) -> None:
        shared = {("GET", "/health"), ("GET", "/me"), ("POST", "/auth/token")}
        self.assertEqual(shared, self.api & self.files)

    def test_둘을_합치면_종전_전부다(self) -> None:
        self.assertEqual(self.all, self.api | self.files)

    def test_파일_프로세스는_검색을_열지_않는다(self) -> None:
        self.assertNotIn(("GET", "/file-search"), self.files)
        self.assertNotIn(("GET", "/search"), self.files)
        self.assertNotIn(("GET", "/assets/{asset_id}"), self.files)
        self.assertIn(("GET", "/assets/{asset_id}/download"), self.files)
        self.assertIn(("POST", "/assets/bundle"), self.files)

    def test_api_프로세스는_파일을_내주지_않는다(self) -> None:
        for _m, p in self.api:
            self.assertFalse(is_file_path(p), p)
        self.assertIn(("GET", "/file-search"), self.api)
        self.assertIn(("GET", "/assets/{asset_id}"), self.api)

    def test_모르는_역할은_기동을_막는다(self) -> None:
        env = {**os.environ, "PORTAL_ROLE": "x", "PORTAL_LOG_CONFIGURE": "0"}
        r = subprocess.run([sys.executable, "-c", "import service.api"], env=env, capture_output=True, text=True)
        self.assertNotEqual(0, r.returncode)


if __name__ == "__main__":
    unittest.main()
