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
from urllib.parse import quote

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
        self.assertEqual(r.headers["x-bundle-count"], r.headers["x-bundle-files"])       # 창구마다 달랐던 두 이름이 같은 값
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
        self.assertEqual((404, "찾는 파일이 없습니다."), (r.status_code, r.json()["detail"]))

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

    def _picked(self, *names, missing=()):
        targets = [{"asset_id": f"018f0000-0000-7000-8000-0000000000b{i}", "fs_path": self.make(n, n.encode() * 10), "file_name": n}
                   for i, n in enumerate(names)]
        return {"targets": targets, "missing": list(missing)}

    def test_고른_자산을_zip으로(self) -> None:
        r, wr, _ = self._post({"asset_ids": [A1, A2]}, self._picked("a.txt", "b.png"))
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt", "b.png"], self.zip_of(r).namelist())
        self.assertEqual(("2", "0"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        self.assertEqual(r.headers["x-bundle-files"], r.headers["x-bundle-count"])
        wr.assert_called_once()                                    # 담긴 자산의 감사 기록(한 번의 쓰기 트랜잭션)

    def test_노출_대상이_아니라_뺀_것과_원본이_없어_빠진_것을_합쳐_센다(self) -> None:
        picked = self._picked("a.txt", missing=[A3])
        picked["targets"].append({"asset_id": A2, "fs_path": str(self.dir / "없음"), "file_name": "없음.bin"})
        r, _, _ = self._post({"asset_ids": [A1, A2, A3]}, picked)
        self.assertEqual(("1", "2"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))

    def test_빈_목록_형식_오류는_400(self) -> None:
        for body in ({"asset_ids": []}, {"asset_ids": ["nope"]}):
            with self.subTest(n=len(body["asset_ids"])):
                r, _, _ = self._post(body, self._picked("a.txt"))
                self.assertEqual(400, r.status_code)

    def test_모두_노출_대상이_아니면_409(self) -> None:
        r, _, _ = self._post({"asset_ids": [A1]}, {"targets": [], "missing": [A1]})
        self.assertEqual(409, r.status_code)

    def test_건수와_용량_상한은_없다(self) -> None:
        """200건을 넘는 요청도 받는다(2026-10-13 상한을 없앴다). 걸리는 것은 요청 본문 상한과 동시 묶음 수 제한뿐이다."""
        ids = [f"018f0000-0000-7000-8000-{i:012d}" for i in range(500)]
        r, _, _ = self._post({"asset_ids": ids}, self._picked("a.txt"))
        self.assertEqual(200, r.status_code)

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
        self.assertEqual(r.headers["x-bundle-count"], r.headers["x-bundle-files"])       # 창구마다 달랐던 두 이름이 같은 값
        self.assertIn("person", r.headers["content-disposition"])
        self.assertTrue(r.headers["content-disposition"].isascii())           # 헤더 자체는 ASCII(한글은 filename* 에 퍼센트 인코딩)
        # 웹 점검 #41 — 한글 이름을 filename* 로 함께 싣는다(받은 파일끼리 구별된다).
        self.assertIn("filename*=UTF-8''" + quote("개체_person_배우.zip"), r.headers["content-disposition"])

    def test_좁힌_결과가_비면_404_경로가_없으면_409_용량_상한은_없다(self) -> None:
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
        self.assertNotEqual(413, run(big).status_code, "용량 상한을 없앴다(2026-10-13) — DB 에 적힌 크기가 커도 거절하지 않는다")
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

    def test_카드_묶음_헤더(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A"), "file_name": "a.txt"}]

        class _Entity:
            def card_zip_targets(self, **kw):
                return (t, "이순신")

        class _Repo:
            entity = _Entity()
        with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            r = self.client.get("/mm-meta/person/u1/bundle")
        self.assertEqual(200, r.status_code)
        self.assertEqual("1", r.headers["x-bundle-count"])
        self.assertEqual("1", r.headers["x-bundle-files"])
        self.assertNotIn("x-bundle-truncated", r.headers, "건수 상한을 없앴다(2026-10-13) — 잘림 헤더도 없다")
        self.assertTrue(r.headers["content-disposition"].isascii())
        self.assertIn('filename="mmmetaperson_1files.zip"', r.headers["content-disposition"])     # 옛 브라우저용 ASCII 이름은 그대로
        self.assertIn("filename*=UTF-8''" + quote("person_이순신.zip"), r.headers["content-disposition"])

    def test_보일_이름은_파일_이름에_못_쓰는_글자를_바꾼다(self) -> None:
        from service.portal.mm_meta import zip_display_name
        self.assertEqual("작품_기생충.zip", zip_display_name(["작품", "기생충"]))
        self.assertEqual("작품_AC_DC.zip", zip_display_name(["작품", "AC/DC"]))
        self.assertEqual("개체_전체.zip", zip_display_name(["개체", "전체", ""]))
        self.assertEqual("mm_meta.zip", zip_display_name(["", "..", "  "]))

    def test_카드_묶음은_200건을_넘어도_자르지_않는다(self) -> None:
        many = [{"asset_id": f"018f0000-0000-7000-8000-{i:012d}", "fs_path": self.make(f"f{i}.txt", b"x"), "file_name": f"f{i}.txt"}
                for i in range(250)]

        class _Entity:
            def card_zip_targets(self, **kw):
                return (many, "큰 개체")

        class _Repo:
            entity = _Entity()
        with mock.patch.object(routes_mm_meta.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            r = self.client.get("/mm-meta/person/u1/bundle")
        self.assertEqual("250", r.headers["x-bundle-count"])
        self.assertEqual(250, len([n for n in self.zip_of(r).namelist() if n != "_manifest.json"]))

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
        self.assertEqual(409, run(([], "이름")).status_code)


class TestCorsExposeBundle(unittest.TestCase):
    def test_묶음_헤더를_화면이_읽을_수_있다(self) -> None:
        from service.api import CORS_EXPOSE_HEADERS
        for h in ("X-Bundle-Count", "X-Bundle-Files", "X-Bundle-Missing", "X-Bundle-Bytes"):
            self.assertIn(h, CORS_EXPOSE_HEADERS)


if __name__ == "__main__":
    unittest.main()


class TestBundleGate(unittest.TestCase):
    """동시 묶음 수 제한 — 넘으면 기다리게 하지 않고 곧바로 503 + Retry-After, 끝나면(끊겨도) 자리가 돌아온다."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = os.path.join(self._tmp.name, "a.txt")
        with open(p, "wb") as f:
            f.write(b"hello " * 1000)
        from service.portal.asset.bundle_stream import plan_bundle
        self.plan = plan_bundle([{"asset_id": "a", "fs_path": p, "file_name": "a.txt"}])

    def _make(self):
        from service.api.bundle_response import zip_response
        return zip_response(self.plan, content_disposition='attachment; filename="x.zip"', headers={})

    def test_제한을_넘으면_503(self) -> None:
        from fastapi import HTTPException
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "2"}):
            r1, r2 = self._make(), self._make()
            with self.assertRaises(HTTPException) as cm:
                self._make()
            self.assertEqual(503, cm.exception.status_code)
            self.assertEqual("5", cm.exception.headers["Retry-After"])
            del r1, r2

    def test_스트림이_끝나면_자리가_돌아온다(self) -> None:
        import asyncio
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "1"}):
            async def drain(resp):
                async for _ in resp.body_iterator:
                    pass
            resp = self._make()
            asyncio.run(drain(resp))
            self._make()                                    # 자리가 돌아왔으니 다시 만들어진다

    def test_끊기면_백그라운드가_자리를_돌려준다_두_번_불러도_한_번만(self) -> None:
        import asyncio

        from service.api.bundle_response import GATE
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "1"}):
            resp = self._make()
            self.assertEqual(1, GATE.active)

            async def cut():
                it = resp.body_iterator.__aiter__()
                await it.__anext__()
                await resp.background()
                await resp.background()
            asyncio.run(cut())
            self.assertEqual(0, GATE.active)                # 두 번 반납해도 음수로 가지 않는다

    def test_시작도_못_하고_버려져도_자리가_돌아온다(self) -> None:
        import gc

        from service.api.bundle_response import GATE
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "1"}):
            resp = self._make()
            self.assertEqual(1, GATE.active)
            del resp
            gc.collect()
            self.assertEqual(0, GATE.active)

    def test_0_이면_제한이_없다_환경변수_해석(self) -> None:
        from service.api.bundle_response import bundle_max_concurrent
        for raw, want in (("0", 0), ("3", 3)):
            with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": raw}):
                self.assertEqual(want, bundle_max_concurrent(), raw)
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "x"}):
            self.assertTrue(2 <= bundle_max_concurrent() <= 8)


class TestSlotCoversPlanning(_Base):
    """웹 점검 #4 · #48 — 전체 묶음은 확인 단계(대상 조회 · 파일 확인)만 수 분이 걸린다. 자리를 그 단계부터 잡아야 동시 제한이 듣는다."""

    def setUp(self) -> None:
        super().setUp()
        from service.api.bundle_response import GATE
        self.gate = GATE
        self.reads = 0

    def _read(self, targets):
        test = self

        class _Asset:
            def bundle_targets(self, *, seed_asset_id):
                test.reads += 1
                test.active_during_read = test.gate.active
                return targets

        class _Repo:
            asset = _Asset()
        return mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo()))

    def test_대상_조회와_확인은_자리를_잡은_채_한다(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A"), "file_name": "a.txt"}]
        with self._read(t):
            r = self.client.head(f"/assets/{A1}/bundle")
        self.assertEqual(200, r.status_code)
        self.assertEqual(1, self.active_during_read)
        self.assertEqual(0, self.gate.active, "HEAD 는 확인이 끝나면 자리를 놓는다")

    def test_자리가_없으면_HEAD_도_대상을_읽기_전에_503(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_BUNDLE_MAX_CONCURRENT": "1"}), self._read([]):
            self.gate.active = 1                       # 다른 묶음이 자리를 쥐고 있다
            for call in (self.client.head, self.client.get):
                with self.subTest(method=call.__name__):
                    r = call(f"/assets/{A1}/bundle")
                    self.assertEqual(503, r.status_code)
                    self.assertEqual("5", r.headers["retry-after"])
            self.assertEqual(0, self.reads, "자리가 없으면 DB 도 파일도 건드리지 않는다")

    def test_오류로_끝나도_자리를_놓는다(self) -> None:
        with self._read(None):
            r = self.client.get(f"/assets/{A1}/bundle")
        self.assertEqual(404, r.status_code)
        self.assertEqual(0, self.gate.active)

    def test_GET_은_받은_자리를_스트림이_끝날_때_놓는다(self) -> None:
        t = [{"asset_id": A1, "fs_path": self.make("a.txt", b"A" * 10), "file_name": "a.txt"}]
        with self._read(t):
            r = self.client.get(f"/assets/{A1}/bundle")
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt"], self.zip_of(r).namelist())
        self.assertEqual(0, self.gate.active)


class TestPlanInParallel(_Base):
    """파일 확인을 동시에 해도 계획(담을 것 · 빠질 것 · 순서 · 합계)은 차례로 할 때와 같다."""

    def test_동시_확인과_차례_확인의_계획이_같다(self) -> None:
        from service.portal.asset.bundle_stream import PLAN_WORKERS_ENV, plan_bundle
        targets = []
        for i in range(40):
            path = self.make(f"f{i}.txt", b"x" * i) if i % 7 else str(self.dir / f"없음{i}")
            targets.append({"asset_id": f"a{i}", "fs_path": path, "file_name": f"f{i}.txt"})
        plans = {}
        for workers in ("1", "16"):
            with mock.patch.dict(os.environ, {PLAN_WORKERS_ENV: workers}):
                plans[workers] = plan_bundle(targets)
        self.assertEqual(plans["1"], plans["16"])
        self.assertEqual([f"a{i}" for i in range(40) if i % 7], [t["asset_id"] for t in plans["16"].packed])
        self.assertEqual(6, len(plans["16"].unreadable))
