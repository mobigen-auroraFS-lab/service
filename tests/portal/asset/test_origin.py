"""원본 읽기 계층 — 로컬 구현의 동작, 구현 선택(설정), 다른 구현으로 바꿔 끼워도 다운로드가 그대로 되는지."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets
from service.portal.asset import origin
from service.portal.asset.origin import CHUNK, LocalFileReader


class TestLocalFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.payload = os.urandom(5 * CHUNK + 123)
        self.path = str(Path(self._tmp.name) / "a.bin")
        Path(self.path).write_bytes(self.payload)
        self.reader = LocalFileReader()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read(self, start: int, end: int, window: int = 1) -> bytes:
        src = self.reader.open(self.path)
        try:
            return b"".join(src.read_range(start, end, window=window))
        finally:
            src.close()

    def test_구간은_양_끝을_포함해_읽는다(self) -> None:
        self.assertEqual(self.payload[10:20], self._read(10, 19))
        self.assertEqual(self.payload, self._read(0, len(self.payload) - 1))

    def test_동시_읽기도_순서와_내용이_같다(self) -> None:
        for start, end in ((0, len(self.payload) - 1), (7, len(self.payload) - 9)):
            for window in (2, 4, 8):
                with self.subTest(start=start, window=window):
                    self.assertEqual(self.payload[start:end + 1], self._read(start, end, window))

    def test_동시_읽기는_조각이_하나로_충분한_작은_구간이면_차례로_읽는다(self) -> None:
        self.assertEqual(self.payload[:100], self._read(0, 99, window=8))

    def test_파일이_도중에_짧아지면_거기서_멈춘다(self) -> None:
        src = self.reader.open(self.path)
        try:
            Path(self.path).write_bytes(self.payload[:CHUNK + 5])
            got = b"".join(src.read_range(0, len(self.payload) - 1))
        finally:
            src.close()
        self.assertEqual(self.payload[:CHUNK + 5], got)

    def test_머리만_읽는다(self) -> None:
        src = self.reader.open(self.path)
        try:
            self.assertEqual(self.payload[:16], src.read_head(16))
        finally:
            src.close()

    def test_stat_은_크기와_수정시각(self) -> None:
        size, mtime_ns = self.reader.stat(self.path)
        self.assertEqual(len(self.payload), size)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime_ns)

    def test_열_수_없으면_OSError(self) -> None:
        for bad in (None, "", "/no/such/file", self._tmp.name):
            with self.subTest(bad=bad):
                with self.assertRaises(OSError):
                    self.reader.open(bad)
                with self.assertRaises(OSError):
                    self.reader.stat(bad)


class TestBackendChoice(unittest.TestCase):
    def test_기본은_local(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(origin.BACKEND_ENV, None)
            self.assertIsInstance(origin.get_reader(), LocalFileReader)
        with mock.patch.dict(os.environ, {origin.BACKEND_ENV: " LOCAL "}):
            self.assertIsInstance(origin.get_reader(), LocalFileReader)

    def test_알_수_없는_값은_기동_때_알린다(self) -> None:
        with mock.patch.dict(os.environ, {origin.BACKEND_ENV: "s3"}), self.assertRaises(ValueError):
            origin.get_reader()

    def test_storage_api_는_아직_없다(self) -> None:
        with mock.patch.dict(os.environ, {origin.BACKEND_ENV: "storage_api"}), self.assertRaises(NotImplementedError):
            origin.get_reader()


AID = "018f0000-0000-7000-8000-0000000000b1"
DATA = bytes(range(256)) * 40


class _MemFile:
    def __init__(self, data: bytes) -> None:
        self._d, self.size, self.mtime_ns, self.closed = data, len(data), 1_700_000_000_000_000_000, False

    def read_range(self, start: int, end: int, *, window: int = 1) -> Iterator[bytes]:
        for i in range(start, end + 1, 1000):
            yield self._d[i:min(i + 1000, end + 1)]

    def read_head(self, n: int) -> bytes:
        return self._d[:n]

    def close(self) -> None:
        self.closed = True


class _MemReader:
    def __init__(self) -> None:
        self.opened: list[_MemFile] = []

    def open(self, path):
        if path != "mem://a":
            raise OSError("없음")
        f = _MemFile(DATA)
        self.opened.append(f)
        return f

    def stat(self, path):
        if path != "mem://a":
            raise OSError("없음")
        return len(DATA), 1_700_000_000_000_000_000


class _Repo:
    class asset:                                                    # noqa: N801
        @staticmethod
        def download_target(*, asset_id):
            return {"asset_id": AID, "fs_path": "mem://a", "fs_uri": None, "file_size": 0, "modality": "text", "file_name": "a.bin"}


class TestSwapReader(unittest.TestCase):
    """디스크 없는 다른 구현으로 바꿔 끼워도 다운로드 · 구간 · 닫기가 그대로다 — 호출부가 구현을 모른다는 증거."""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.reader = _MemReader()
        self.client = TestClient(app)

    def _get(self, headers=None):
        with mock.patch.object(origin, "get_reader", return_value=self.reader), \
                mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo())):
            return self.client.get(f"/assets/{AID}/download", headers=headers or {})

    def test_전체와_구간과_닫기(self) -> None:
        r = self._get()
        self.assertEqual((200, DATA), (r.status_code, r.content))
        p = self._get({"Range": "bytes=100-2999"})
        self.assertEqual((206, DATA[100:3000]), (p.status_code, p.content))
        self.assertEqual("bytes 100-2999/10240", p.headers["content-range"])
        self.assertTrue(all(f.closed for f in self.reader.opened))

    def test_416_도_닫는다(self) -> None:
        self.assertEqual(416, self._get({"Range": "bytes=99999-"}).status_code)
        self.assertTrue(all(f.closed for f in self.reader.opened))

    def test_구현이_OSError_를_던지면_410(self) -> None:
        class _Gone(_MemReader):
            def open(self, path):
                raise OSError("스토리지에 없음")
        self.reader = _Gone()
        self.assertEqual(410, self._get().status_code)
