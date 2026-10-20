"""파일 메타 질의 — 기본키 인덱스를 타게 열은 UUID 그대로 비교하고, 맞는 id 는 종전(텍스트 비교)과 같다."""

from __future__ import annotations

import unittest

from service.portal.asset import file_meta

_ID = "01a08fa7-65fc-73c8-a738-ce1adc6da1ed"


class _Cur:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._calls.append((sql, params))

    def fetchall(self):
        return [{"asset_id": _ID, "file_size": None, "updated_at": None, "created_at": None}]


class _Conn:
    def __init__(self) -> None:
        self.calls: list = []

    def cursor(self, **_k):
        return _Cur(self.calls)


class TestFileMetaSql(unittest.TestCase):
    def test_열을_텍스트로_바꾸지_않는다(self) -> None:
        # 열을 ::text 로 바꾸면 기본키 인덱스를 못 타 표 전체를 훑는다(2026-10-20 실측).
        self.assertIn("asset_id = ANY(%(ids)s::uuid[])", file_meta._FILE_META_SQL)
        self.assertNotIn("asset_id::text = ANY", file_meta._FILE_META_SQL)

    def test_표준_표기만_남긴다(self) -> None:
        raw = [_ID, _ID.upper(), _ID.replace("-", ""), "{" + _ID + "}", "urn:uuid:" + _ID, "not-a-uuid", "", None, _ID]
        self.assertEqual([_ID, _ID], file_meta._canonical_ids(raw), "종전 텍스트 비교에 맞던 값만 · 순서 유지")

    def test_표준_표기_id_만_DB_로_보낸다(self) -> None:
        conn = _Conn()
        out = file_meta.fetch_file_meta(conn, [_ID, _ID.upper(), "x"])
        self.assertEqual([_ID], conn.calls[0][1]["ids"])
        self.assertEqual({_ID: {"file_size": 0, "updated_at": None, "created_at": None}}, out)

    def test_맞을_id_가_없으면_DB_를_건드리지_않는다(self) -> None:
        conn = _Conn()
        self.assertEqual({}, file_meta.fetch_file_meta(conn, ["x", "", _ID.upper()]))
        self.assertEqual([], conn.calls)


if __name__ == "__main__":
    unittest.main()
