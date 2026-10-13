"""분류 묶음 질의 — 하위주제를 안 주면 주제 전체, 빈 값이면 하위주제 없는 자산만, 값이 있으면 그 하위주제."""

from __future__ import annotations

import unittest
from unittest import mock

from service.portal.repositories.asset_repo import AssetRepository


class TestTopicBundleSql(unittest.TestCase):
    def _run(self, **kw):
        repo = AssetRepository(object())
        with mock.patch.object(AssetRepository, "rows", return_value=[{"asset_id": "a", "fs_path": "/x/a__한글.txt"}]) as rows:
            out = repo.topic_bundle_targets(topic="주제", exclude_video=kw.pop("exclude_video", False), **kw)
        return out, rows.call_args.args[0], rows.call_args.args[1]

    def test_하위주제를_안_주면_주제_전체(self) -> None:
        out, sql, args = self._run(subtopic=None)
        self.assertNotIn("subtopic_ko", sql)
        self.assertEqual(("주제",), args)
        self.assertEqual("a", out[0]["asset_id"])
        self.assertIn("registered", sql)

    def test_빈_값은_하위주제가_없는_자산만(self) -> None:
        _out, sql, args = self._run(subtopic="")
        self.assertIn("COALESCE(at.subtopic_ko, '') = ''", sql)
        self.assertEqual(("주제",), args)

    def test_값이_있으면_그_하위주제(self) -> None:
        _out, sql, args = self._run(subtopic="하위")
        self.assertIn("at.subtopic_ko = %s", sql)
        self.assertEqual(("주제", "하위"), args)

    def test_영상_제외(self) -> None:
        _out, sql, _args = self._run(subtopic=None, exclude_video=True)
        self.assertIn("a.modality <> 'video'", sql)

    def test_경로가_없는_행은_이름을_만들지_않는다(self) -> None:
        repo = AssetRepository(object())
        with mock.patch.object(AssetRepository, "rows", return_value=[{"asset_id": "a", "fs_path": None}]):
            out = repo.topic_bundle_targets(topic="주제", subtopic=None, exclude_video=False)
        self.assertEqual([{"asset_id": "a", "fs_path": None, "file_name": None}], out)


if __name__ == "__main__":
    unittest.main()
