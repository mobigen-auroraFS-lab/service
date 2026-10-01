"""묶음 네 창구 — 실제 임시 파일로 zip 을 받아 열어 본다. 임시 파일 없이 흘려 보내는지, 헤더가 시작 전에 정해지는지, 오류가 봉투로 나오는지."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets
from service.api.routes import mm_meta as routes_mm_meta

A1 = "018f0000-0000-7000-8000-0000000000a1"
A2 = "018f0000-0000-7000-8000-0000000000a2"
A3 = "018f0000-0000-7000-8000-0000000000a3"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, name: str, data: bytes) -> str:
        p = self.dir / name
        p.write_bytes(data)
        return str(p)

    @staticmethod
    def zip_of(r) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(r.content))


class TestRelationBundle(_Base):
    def _get(self, targets):
        class _Asset:
            def bundle_targets(self, *, seed_asset_id):
                return targets
        class _Repo:
            asset = _Asset()
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            return self.client.get(f"/assets/{A1}/bundle")

    def test_zip과_헤더(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A" * 50), "file_name": "a.txt"},
             {"asset_id": A2, "fs_path": self.make("b.jpg", b"B" * 80), "file_name": "b.jpg"}]
        r = self._get(t)
        self.assertEqual(200, r.status_code)
        self.assertEqual("application/zip", r.headers["content-type"])
        self.assertEqual(["a.txt", "b.jpg"], self.zip_of(r).namelist())
        self.assertEqual(("2", "0", "130"), (r.headers["x-bundle-count"], r.headers["x-bundle-missing"], r.headers["x-bundle-bytes"]))
        self.assertIn(f"bundle_{A1}.zip", r.headers["content-disposition"])
        self.assertNotIn("content-length", r.headers)              # 만들면서 보내므로 크기를 미리 모른다(청크 전송)
        self.assertEqual("private, no-store", r.headers["cache-control"])

    def test_원본이_없으면_부분_zip과_빠진_수(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A"), "file_name": "a.txt"},
             {"asset_id": A2, "fs_path": str(self.dir / "없음"), "file_name": "없음.bin"}]
        r = self._get(t)
        self.assertEqual(("1", "1"), (r.headers["x-bundle-count"], r.headers["x-bundle-missing"]))   # 시작 전에 확인한 실제 값
        z = self.zip_of(r)
        self.assertEqual(["a.txt", "_manifest.json"], z.namelist())
        self.assertEqual({"missing": [{"asset_id": A2, "file_name": "없음.bin"}]}, json.loads(z.read("_manifest.json")))
        self.assertNotIn(str(self.dir), r.text if False else z.read("_manifest.json").decode())

    def test_전부_누락이면_목록_파일만(self) -> None:
        r = self._get([{"asset_id": A1, "fs_path": "/no/file", "file_name": "x"}])
        self.assertEqual(200, r.status_code)
        self.assertEqual(["_manifest.json"], self.zip_of(r).namelist())
        self.assertEqual(("0", "1"), (r.headers["x-bundle-count"], r.headers["x-bundle-missing"]))

    def test_기준_자산이_노출_대상이_아니면_404(self) -> None:
        r = self._get(None)
        self.assertEqual((404, "묶음 seed 를 찾을 수 없거나 노출 대상이 아님"), (r.status_code, r.json()["detail"]))

    def test_UUID가_아니면_404(self) -> None:
        self.assertEqual(404, self.client.get("/assets/not-a-uuid/bundle").status_code)


class TestSelectionBundle(_Base):
    def _post(self, body, picked, audit_sink=None):
        class _Sel:
            def targets(self, ids):
                return picked
        class _Repo:
            selection = _Sel()
        audited: list = []
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo())), \
                mock.patch.object(routes_assets.DbManager, "write", side_effect=lambda fn: audited.append(fn)) as wr:
            r = self.client.post("/assets/bundle", json=body)
        return r, wr, audited

    def _picked(self, *names, missing=(), total=None):
        targets = [{"asset_id": f"018f0000-0000-7000-8000-0000000000b{i}", "fs_path": self.make(n, n.encode() * 10), "file_name": n}
                   for i, n in enumerate(names)]
        return {"targets": targets, "missing": list(missing), "total_bytes": total if total is not None else sum(10 * len(n) for n in names)}

    def test_고른_자산을_zip으로(self) -> None:
        r, wr, _ = self._post({"asset_ids": [A1, A2]}, self._picked("a.txt", "b.png"))
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt", "b.png"], self.zip_of(r).namelist())
        self.assertEqual(("2", "0"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        wr.assert_called_once()                                    # 담긴 자산의 감사 기록(한 번의 쓰기 트랜잭션)

    def test_노출_대상이_아니라_뺀_것과_원본이_없어_빠진_것을_합쳐_센다(self) -> None:
        picked = self._picked("a.txt", missing=[A3])
        picked["targets"].append({"asset_id": A2, "fs_path": str(self.dir / "없음"), "file_name": "없음.bin"})
        r, _, _ = self._post({"asset_ids": [A1, A2, A3]}, picked)
        self.assertEqual(("1", "2"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))

    def test_빈_목록_형식_오류_건수_초과는_400(self) -> None:
        for body in ({"asset_ids": []}, {"asset_ids": ["nope"]}, {"asset_ids": [f"018f0000-0000-7000-8000-{i:012d}" for i in range(201)]}):
            with self.subTest(n=len(body["asset_ids"])):
                r, _, _ = self._post(body, self._picked("a.txt"))
                self.assertEqual(400, r.status_code)

    def test_모두_노출_대상이_아니면_409(self) -> None:
        r, _, _ = self._post({"asset_ids": [A1]}, {"targets": [], "missing": [A1], "total_bytes": 0})
        self.assertEqual(409, r.status_code)

    def test_DB_크기_합이_상한을_넘으면_413_파일을_건드리기_전에(self) -> None:
        with mock.patch.object(routes_assets, "make_plan") as plan:
            r, _, _ = self._post({"asset_ids": [A1]}, self._picked("a.txt", total=600 * 1024 * 1024))
        self.assertEqual(413, r.status_code)
        self.assertIn("MB > 500MB", r.json()["detail"])
        plan.assert_not_called()

    def test_디스크의_실제_크기가_상한을_넘으면_413(self) -> None:
        picked = self._picked("a.txt")                              # DB 에는 작게 적혀 있다
        with mock.patch.object(routes_assets, "MAX_SELECTION_BYTES", 5):
            r, _, _ = self._post({"asset_ids": [A1]}, picked)
        self.assertEqual(413, r.status_code)

    def test_감사_기록이_실패해도_내려받기는_된다(self) -> None:
        class _Sel:
            def targets(self, ids):
                return self_picked
        self_picked = self._picked("a.txt")
        class _Repo:
            selection = _Sel()
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo())), \
                mock.patch.object(routes_assets.DbManager, "write", side_effect=RuntimeError("DB 끊김")), \
                self.assertLogs("meta_extract.portal_api", "WARNING"):
            r = self.client.post("/assets/bundle", json={"asset_ids": [A1]})
        self.assertEqual(200, r.status_code)


class TestEntityBundles(_Base):
    def test_개체_목록_묶음(self) -> None:
        rows = [{"asset_id": A1, "modality": "text", "file_name": "a.txt", "file_size": 10, "fs_path": self.make("a.txt", b"A" * 10)},
                {"asset_id": A2, "modality": "image", "file_name": "b.jpg", "file_size": 20, "fs_path": self.make("b.jpg", b"B" * 20)}]

        class _Entity:
            def zip_rows(self, **kw):
                return rows
        class _Repo:
            entity = _Entity()
        with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            r = self.client.get("/mm-meta/bundle", params={"entity_type": "person", "areas": "배우"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt", "b.jpg"], self.zip_of(r).namelist())
        self.assertEqual(("2", "0", "30"), (r.headers["x-bundle-count"], r.headers["x-bundle-missing"], r.headers["x-bundle-bytes"]))
        self.assertIn("person", r.headers["content-disposition"])
        self.assertTrue(r.headers["content-disposition"].isascii())           # 한글 이름은 헤더에서 깨지므로 ASCII 만

    def test_좁힌_결과가_비면_404_용량_초과면_413_경로가_없으면_409(self) -> None:
        def run(rows):
            class _Entity:
                def zip_rows(self, **kw):
                    return rows
            class _Repo:
                entity = _Entity()
            with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
                return self.client.get("/mm-meta/bundle")
        self.assertEqual(404, run([]).status_code)
        big = [{"asset_id": A1, "modality": "video", "file_name": "v.mp4", "file_size": 600 * 1024 * 1024, "fs_path": "/x"}]
        r = run(big)
        self.assertEqual(413, r.status_code)
        self.assertIn("영상을 제외", r.json()["detail"])
        self.assertEqual(409, run([{"asset_id": A1, "modality": "text", "file_name": "a", "file_size": 1, "fs_path": None}]).status_code)

    def test_옛_이름_labels도_받는다(self) -> None:
        seen = {}

        class _Entity:
            def zip_rows(self, **kw):
                seen.update(kw)
                return []
        class _Repo:
            entity = _Entity()
        with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            self.client.get("/mm-meta/bundle", params={"labels": "배우,감독"})
        self.assertEqual(["배우", "감독"], seen["areas"])

    def test_카드_묶음과_잘림_헤더(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A"), "file_name": "a.txt"}]

        class _Entity:
            def __init__(self, truncated): self._tr = truncated
            def card_zip_targets(self, **kw):
                return (t, "이순신", self._tr)
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                class _Repo:
                    entity = _Entity(truncated)
                with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
                    r = self.client.get("/mm-meta/person/u1/bundle")
                self.assertEqual(200, r.status_code)
                self.assertEqual("1", r.headers["x-bundle-count"])
                self.assertEqual("200" if truncated else None, r.headers.get("x-bundle-truncated"))
                self.assertTrue(r.headers["content-disposition"].isascii())

    def test_카드_묶음_오류(self) -> None:
        def run(result):
            class _Entity:
                def card_zip_targets(self, **kw):
                    return result
            class _Repo:
                entity = _Entity()
            with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
                return self.client.get("/mm-meta/person/u1/bundle")
        self.assertEqual(404, run(None).status_code)
        self.assertEqual(409, run(([], "이름", False)).status_code)


class TestCorsExposeBundle(unittest.TestCase):
    def test_묶음_헤더를_화면이_읽을_수_있다(self) -> None:
        from service.api import CORS_EXPOSE_HEADERS
        for h in ("X-Bundle-Count", "X-Bundle-Files", "X-Bundle-Missing", "X-Bundle-Bytes", "X-Bundle-Truncated"):
            self.assertIn(h, CORS_EXPOSE_HEADERS)


if __name__ == "__main__":
    unittest.main()
