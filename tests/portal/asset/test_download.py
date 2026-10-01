"""원본 내려받기 지원(``service.portal.asset.download``) — 대상 확정 · 원본 열기 · 이어받기 기준값(ETag · If-Range).

노출 게이트(registered 만 · 그 밖은 404 와 같게 ``None``), 원본은 **한 번 열어** 크기를 확정하는지(없음 · 디렉터리 · 빈 경로 → ``OSError``),
ETag 가 크기 · 수정 시각에 따라 바뀌는지, ``If-Range`` 가 맞을 때만 구간을 허용하는지. DB 없이 임시 파일로 돈다.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from service.portal.asset.download import (
    if_range_matches,
    make_etag,
    make_last_modified,
    open_original,
)


def _conn_one(row):
    """``cursor(row_factory=dict_row).fetchone`` 이 ``row`` 를 돌려주는 mock conn."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn, cur


class TestResolveDownloadTarget(unittest.TestCase):
    _ROW = {
        "asset_id": "A1", "fs_path": "/data/in/보고서.pdf", "fs_uri": "file:///data/in/보고서.pdf",
        "file_size": 2048, "modality": "text", "domain_label": "general", "status": "registered",
    }

    def test_registered_general_returns_target(self) -> None:
        # registered·비의료 → 다운로드 타깃 dict(file_name = fs_path basename).
        conn, _ = _conn_one(dict(self._ROW))
        from service.portal.asset.download import resolve_download_target

        out = resolve_download_target(conn, asset_id="A1")
        self.assertEqual(out, {
            "asset_id": "A1", "fs_path": "/data/in/보고서.pdf",
            "fs_uri": "file:///data/in/보고서.pdf", "file_size": 2048,
            "modality": "text", "file_name": "보고서.pdf",
        })

    def test_missing_returns_none(self) -> None:
        # 행 없음 → None(404).
        conn, _ = _conn_one(None)
        from service.portal.asset.download import resolve_download_target

        self.assertIsNone(resolve_download_target(conn, asset_id="ZZ"))

    def test_non_registered_returns_none(self) -> None:
        # status != 'registered' → None(404 게이트).
        row = dict(self._ROW)
        row["status"] = "failed"
        conn, _ = _conn_one(row)
        from service.portal.asset.download import resolve_download_target

        self.assertIsNone(resolve_download_target(conn, asset_id="A1"))

    def test_medical_returns_target(self) -> None:
        # 2026-07-23: 도메인 제외 전면 제거 — 의료 자산도 다운로드 타깃이 해소된다(None 아님).
        row = dict(self._ROW)
        row["domain_label"] = "medical"
        conn, _ = _conn_one(row)
        from service.portal.asset.download import resolve_download_target

        self.assertIsNotNone(resolve_download_target(conn, asset_id="A1"))



class TestOpenOriginal(unittest.TestCase):
    def test_열린_핸들과_크기_수정시각을_함께_돌려준다(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"0123456789")
        try:
            fh, size, mtime_ns = open_original(tmp.name)
            with fh:
                self.assertEqual(10, size)
                self.assertEqual(os.stat(tmp.name).st_mtime_ns, mtime_ns)
                self.assertEqual(b"0123", fh.read(4))               # 바로 읽을 수 있게 열려 있다
        finally:
            os.unlink(tmp.name)

    def test_없는_파일은_OSError(self) -> None:
        with self.assertRaises(OSError):
            open_original("/no/such/dir/file.bin")

    def test_경로가_비었으면_OSError(self) -> None:
        for empty in (None, ""):
            with self.subTest(empty=empty), self.assertRaises(OSError):
                open_original(empty)

    def test_디렉터리는_일반_파일이_아니라_OSError(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(OSError):
            open_original(d)

    def test_빈_파일도_열린다(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            pass
        try:
            fh, size, _ = open_original(tmp.name)
            fh.close()
            self.assertEqual(0, size)
        finally:
            os.unlink(tmp.name)


class TestEtagAndIfRange(unittest.TestCase):
    def test_ETag_는_크기와_수정시각이_바뀌면_바뀐다(self) -> None:
        base = make_etag(100, 1_700_000_000_000_000_000)
        self.assertTrue(base.startswith('"') and base.endswith('"'))     # 강한 ETag(따옴표 포함)
        self.assertEqual(base, make_etag(100, 1_700_000_000_000_000_000))
        self.assertNotEqual(base, make_etag(101, 1_700_000_000_000_000_000))
        self.assertNotEqual(base, make_etag(100, 1_700_000_000_000_000_001))

    def test_Last_Modified_는_HTTP_날짜(self) -> None:
        self.assertEqual("Thu, 01 Jan 1970 00:00:00 GMT", make_last_modified(0))
        self.assertTrue(make_last_modified(1_700_000_000_000_000_000).endswith(" GMT"))

    def test_If_Range_가_없으면_구간을_받아도_된다(self) -> None:
        self.assertTrue(if_range_matches(None, '"a"', "x"))

    def test_If_Range_ETag_가_같을_때만_허용한다(self) -> None:
        etag, lm = make_etag(10, 5), make_last_modified(5)
        self.assertTrue(if_range_matches(etag, etag, lm))
        self.assertFalse(if_range_matches(make_etag(11, 5), etag, lm))      # 받는 도중 바뀐 파일
        self.assertFalse(if_range_matches('W/"abc"', etag, lm))             # 약한 ETag 는 If-Range 에서 맞지 않는다

    def test_If_Range_날짜는_Last_Modified_와_같을_때만(self) -> None:
        etag, lm = make_etag(10, 5_000_000_000), make_last_modified(5_000_000_000)
        self.assertTrue(if_range_matches(lm, etag, lm))
        self.assertFalse(if_range_matches(make_last_modified(6_000_000_000), etag, lm))
        self.assertFalse(if_range_matches("아무 말", etag, lm))


if __name__ == "__main__":
    unittest.main()
