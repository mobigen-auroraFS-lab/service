"""임시 파일 없이 만드는 스트리밍 ZIP — 열리는 zip 인가 · 조각으로 나오나 · 빠진 것은 목록에 남나 · 같은 입력이면 같은 바이트인가."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from service.portal.asset import bundle_stream
from service.portal.asset.bundle_stream import MANIFEST_NAME, plan_bundle, stream_zip


class _Files(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, name: str, data: bytes, asset_id: str | None = None, shown: str | None = None) -> dict:
        p = self.dir / name
        p.write_bytes(data)
        return {"asset_id": asset_id or name, "fs_path": str(p), "file_name": shown or name}

    @staticmethod
    def unzip(chunks) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


class TestPlan(_Files):
    def test_존재하는_것만_담고_합계는_실제_크기다(self) -> None:
        a, b = self.make("a.txt", b"x" * 10), self.make("b.bin", b"y" * 32)
        gone = {"asset_id": "gone", "fs_path": str(self.dir / "없음"), "file_name": "gone.bin"}
        plan = plan_bundle([a, gone, b])
        self.assertEqual(["a.txt", "b.bin"], [t["file_name"] for t in plan.packed])
        self.assertEqual([{"asset_id": "gone", "file_name": "gone.bin"}], plan.unreadable)
        self.assertEqual(42, plan.total_bytes)

    def test_경로가_비었거나_디렉터리면_빠진다(self) -> None:
        plan = plan_bundle([{"asset_id": "n", "fs_path": None, "file_name": "n"},
                            {"asset_id": "d", "fs_path": str(self.dir), "file_name": "d"}])
        self.assertEqual([], plan.packed)
        self.assertEqual({"n", "d"}, {u["asset_id"] for u in plan.unreadable})

    def test_빠진_항목에_서버_경로를_싣지_않는다(self) -> None:
        plan = plan_bundle([{"asset_id": "g", "fs_path": "/no/such/secret/path.bin", "file_name": "g.bin"}])
        self.assertNotIn("/no/such", json.dumps(plan.unreadable))


class TestStream(_Files):
    def test_열리는_zip이고_내용이_같다(self) -> None:
        a, b = self.make("a.txt", b"hello" * 100), self.make("b.jpg", os.urandom(300_000))
        z = self.unzip(stream_zip(plan_bundle([a, b])))
        self.assertIsNone(z.testzip())
        self.assertEqual(b"hello" * 100, z.read("a.txt"))
        self.assertEqual(Path(b["fs_path"]).read_bytes(), z.read("b.jpg"))
        self.assertNotIn(MANIFEST_NAME, z.namelist())            # 빠진 게 없으면 목록 파일도 없다

    def test_이미지_영상은_압축하지_않고_문서는_압축한다(self) -> None:
        files = [self.make("p.jpg", os.urandom(1000)), self.make("v.MP4", os.urandom(1000)), self.make("t.txt", b"a" * 5000)]
        z = self.unzip(stream_zip(plan_bundle(files)))
        kinds = {i.filename: i.compress_type for i in z.infolist()}
        self.assertEqual(zipfile.ZIP_STORED, kinds["p.jpg"])
        self.assertEqual(zipfile.ZIP_STORED, kinds["v.MP4"])      # 확장자 대소문자 무시
        self.assertEqual(zipfile.ZIP_DEFLATED, kinds["t.txt"])

    def test_조각으로_나온다_한꺼번에_쌓지_않는다(self) -> None:
        big = self.make("big.bin", os.urandom(3 * 1024 * 1024 + 7))
        chunks = list(stream_zip(plan_bundle([big])))
        self.assertGreater(len(chunks), 3)                        # 3MiB 남짓이 한 덩어리로 나오지 않는다
        self.assertLessEqual(max(len(c) for c in chunks), 2 * bundle_stream.CHUNK)    # 조각 하나는 읽기 단위 안팎

    def test_빠진_것은_끝의_목록_파일에_이름만_남는다(self) -> None:
        a = self.make("a.txt", b"x")
        gone = {"asset_id": "gone", "fs_path": str(self.dir / "없음.bin"), "file_name": "없음.bin"}
        z = self.unzip(stream_zip(plan_bundle([a, gone])))
        self.assertEqual("a.txt", z.namelist()[0])
        self.assertEqual(MANIFEST_NAME, z.namelist()[-1])
        manifest = json.loads(z.read(MANIFEST_NAME))
        self.assertEqual({"missing": [{"asset_id": "gone", "file_name": "없음.bin"}]}, manifest)
        self.assertNotIn(str(self.dir), z.read(MANIFEST_NAME).decode())

    def test_전부_빠지면_목록_파일만_든_zip(self) -> None:
        z = self.unzip(stream_zip(plan_bundle([{"asset_id": "g", "fs_path": "/no/file", "file_name": "g"}])))
        self.assertEqual([MANIFEST_NAME], z.namelist())

    def test_확인한_뒤_읽기_직전에_사라지면_그_항목만_건너뛴다(self) -> None:
        a, b = self.make("a.txt", b"a"), self.make("b.txt", b"b")
        plan = plan_bundle([a, b])
        os.unlink(a["fs_path"])                                    # 계획을 세운 뒤 사라졌다
        z = self.unzip(stream_zip(plan))
        self.assertEqual(["b.txt", MANIFEST_NAME], z.namelist())
        self.assertEqual([{"asset_id": "a.txt", "file_name": "a.txt"}], json.loads(z.read(MANIFEST_NAME))["missing"])

    def test_같은_이름은_번호를_붙이고_위험한_이름은_걸러낸다(self) -> None:
        t = [self.make("1", b"1", shown="a.txt"), self.make("2", b"2", shown="a.txt"), self.make("3", b"3", shown="../../evil.txt"),
             self.make("4", b"4", shown='x"y\r\n.txt')]
        names = self.unzip(stream_zip(plan_bundle(t))).namelist()
        self.assertEqual(4, len(set(names)))
        self.assertIn("a.txt", names)
        self.assertIn("a_1.txt", names)
        for n in names:
            self.assertNotIn("/", n)                                # 풀 때 폴더 밖으로 나가는 이름이 아니다
            self.assertNotIn("\\", n)
            self.assertNotIn(n, (".", ".."))
            self.assertTrue(n.isprintable())

    def test_한글_파일명(self) -> None:
        z = self.unzip(stream_zip(plan_bundle([self.make("k", b"1", shown="한글 보고서.pdf")])))
        self.assertEqual(["한글 보고서.pdf"], z.namelist())

    def test_같은_입력이면_같은_바이트다(self) -> None:
        files = [self.make("a.txt", b"a" * 3000), self.make("b.jpg", os.urandom(5000))]
        first = b"".join(stream_zip(plan_bundle(files)))
        self.assertEqual(first, b"".join(stream_zip(plan_bundle(files))))
        z = zipfile.ZipFile(io.BytesIO(first))
        self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist()))

    def test_연결이_끊겨_생성기를_닫으면_열린_파일도_닫힌다(self) -> None:
        big = self.make("big.bin", os.urandom(5 * 1024 * 1024))
        opened = []
        real_open = open

        def spy(path, *a, **k):
            fh = real_open(path, *a, **k)
            opened.append(fh)
            return fh

        with mock.patch("builtins.open", side_effect=spy):
            gen = stream_zip(plan_bundle([big]))
            next(gen)                                              # 한 조각만 받고
            gen.close()                                            # 클라이언트가 끊었다
        target = [fh for fh in opened if getattr(fh, "name", "") == big["fs_path"]]
        self.assertTrue(target and all(fh.closed for fh in target))

    def test_빈_파일도_담긴다(self) -> None:
        z = self.unzip(stream_zip(plan_bundle([self.make("e.txt", b"")])))
        self.assertEqual(b"", z.read("e.txt"))

    def test_2GiB_이상_항목은_ZIP64로_쓴다(self) -> None:
        # 실제로 2GiB 를 만들지 않고 계획의 크기만 크게 속여 force_zip64 경로가 켜지는지 본다
        a = self.make("a.bin", b"x" * 100)
        plan = plan_bundle([a])
        plan.packed[0]["size"] = 2**31
        z = self.unzip(stream_zip(plan))
        self.assertEqual(b"x" * 100, z.read("a.bin"))


if __name__ == "__main__":
    unittest.main()


class TestResponseClosesStream(_Files):
    def test_응답이_취소돼도_백그라운드가_제너레이터를_닫아_핸들이_풀린다(self) -> None:
        """스트리밍이 취소되면 제너레이터는 멈춘 채 남는다 — 응답의 background 가 닫아야 핸들이 가비지 수집 전에 풀린다."""
        import asyncio

        from service.api.bundle_response import zip_response

        plan = plan_bundle([self.make("big.bin", os.urandom(3 * 1024 * 1024))])
        opened: list = []
        real_open = open

        def spy(path, *a, **k):
            fh = real_open(path, *a, **k)
            if str(path).endswith("big.bin"):
                opened.append(fh)
            return fh

        with mock.patch("builtins.open", spy):
            resp = zip_response(plan, content_disposition='attachment; filename="x.zip"', headers={})

            async def run() -> None:
                it = resp.body_iterator.__aiter__()
                await it.__anext__()                       # 첫 조각만 받고
                await resp.background()                    # 끊긴 뒤 Starlette 가 부르는 백그라운드

            asyncio.run(run())
        self.assertTrue(opened and all(fh.closed for fh in opened))


class TestSmartStore(_Files):
    """2026-10-01 실험 반영 — 압축 이득이 없는 파일은 무압축으로 담는다(서버 CPU 절약)."""

    @staticmethod
    def kinds(z: zipfile.ZipFile) -> dict[str, int]:
        return {i.filename: i.compress_type for i in z.infolist()}

    def test_압축_컨테이너_확장자는_무압축이다(self) -> None:
        targets = [self.make(f"a.{e}", b"x" * 5000) for e in ("pdf", "docx", "xlsx", "pptx", "hwpx", "epub")]
        z = self.unzip(stream_zip(plan_bundle(targets)))
        self.assertEqual({zipfile.ZIP_STORED}, set(self.kinds(z).values()))

    def test_목록에_없는_형식도_안_줄면_무압축이다(self) -> None:
        # hwp 는 목록에 없다 — 앞부분이 무작위(안 줄어듦)이면 시험 압축으로 가려 무압축, 글이면 압축한다.
        z = self.unzip(stream_zip(plan_bundle([self.make("rand.hwp", os.urandom(200_000)), self.make("text.hwp", b"hello world " * 20_000)])))
        k = self.kinds(z)
        self.assertEqual(zipfile.ZIP_STORED, k["rand.hwp"])
        self.assertEqual(zipfile.ZIP_DEFLATED, k["text.hwp"])

    def test_시험_압축_판정(self) -> None:
        self.assertFalse(bundle_stream.looks_incompressible(b""))
        self.assertFalse(bundle_stream.looks_incompressible(b"a" * 10_000))
        self.assertTrue(bundle_stream.looks_incompressible(os.urandom(10_000)))

    def test_압축하든_안_하든_내용은_그대로다(self) -> None:
        data = {"r.hwp": os.urandom(120_000), "t.hwp": b"abc " * 50_000, "x.pdf": os.urandom(30_000)}
        z = self.unzip(stream_zip(plan_bundle([self.make(n, b) for n, b in data.items()])))
        self.assertIsNone(z.testzip())
        for n, b in data.items():
            self.assertEqual(b, z.read(n), n)


class TestLevelAndReadOptions(_Files):
    def test_압축_수준_환경변수(self) -> None:
        with mock.patch.dict(os.environ, {bundle_stream.ZIP_LEVEL_ENV: "1"}):
            self.assertEqual(1, bundle_stream.zip_level())
        for raw, want in (("", 6), ("x", 6), ("0", 1), ("99", 9), ("9", 9)):
            with mock.patch.dict(os.environ, {bundle_stream.ZIP_LEVEL_ENV: raw}):
                self.assertEqual(want, bundle_stream.zip_level(), raw)

    def test_수준이_실제로_크기에_반영된다(self) -> None:
        body = (b'{"id": 12345, "name": "kimchi", "score": 0.123456}\n' * 30_000)
        sizes = {}
        for lvl in ("1", "9"):
            with mock.patch.dict(os.environ, {bundle_stream.ZIP_LEVEL_ENV: lvl}):
                sizes[lvl] = len(b"".join(stream_zip(plan_bundle([self.make("t.txt", body)]))))
        self.assertGreater(sizes["1"], sizes["9"])

    def test_동시_읽기와_앞서_읽기는_결과_바이트가_같다(self) -> None:
        big = os.urandom(5 * 1024 * 1024 + 17)          # 조각(1MiB) 5개 넘게
        targets = [self.make("a.mp4", big), self.make("b.txt", b"hello " * 100_000), self.make("c.mp4", os.urandom(3_000_000))]
        base = b"".join(stream_zip(plan_bundle(targets)))
        for env in ({bundle_stream.READ_WINDOW_ENV: "4"}, {bundle_stream.FILES_AHEAD_ENV: "2"},
                    {bundle_stream.READ_WINDOW_ENV: "4", bundle_stream.FILES_AHEAD_ENV: "2"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(base, b"".join(stream_zip(plan_bundle(targets))), env)       # 같은 입력이면 같은 바이트

    def test_앞서_읽던_중_끊기면_모든_핸들이_닫힌다(self) -> None:
        targets = [self.make(f"f{i}.mp4", os.urandom(3 * 1024 * 1024)) for i in range(4)]
        opened: list = []
        real_open = open

        def spy(path, *a, **k):
            fh = real_open(path, *a, **k)
            if str(path).endswith(".mp4"):
                opened.append(fh)
            return fh

        with mock.patch.dict(os.environ, {bundle_stream.FILES_AHEAD_ENV: "3", bundle_stream.READ_WINDOW_ENV: "3"}), mock.patch("builtins.open", spy):
            gen = stream_zip(plan_bundle(targets))
            next(gen)
            gen.close()                                   # 클라이언트가 끊었다
        self.assertGreaterEqual(len(opened), 2)
        self.assertTrue(all(fh.closed for fh in opened))

    def test_앞서_연_파일이_사라져_있으면_그_항목만_빠진다(self) -> None:
        a, b = self.make("a.txt", b"aaa" * 100), self.make("b.txt", b"bbb" * 100)
        plan = plan_bundle([a, b])
        os.remove(b["fs_path"])                          # 확인 뒤 사라짐
        with mock.patch.dict(os.environ, {bundle_stream.FILES_AHEAD_ENV: "2"}):
            z = self.unzip(stream_zip(plan))
        self.assertIn("a.txt", z.namelist())
        self.assertNotIn("b.txt", z.namelist())
        self.assertIn("b.txt", json.loads(z.read(MANIFEST_NAME))["missing"][0]["file_name"])
