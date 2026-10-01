"""원문(글자) 읽기 — 어디서 가져오는지와 상한 자르기.

모달리티마다 출처가 다르고(문서=파일 · 소리·영상=받아쓰기 · 그림=없음), 상한을 넘으면
**잘렸다는 사실을 알려야** 한다. 조용히 자르면 화면은 원문이 거기서 끝난 줄 안다.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from service.portal.asset.content import TEXT_BYTE_CAP, build_content, read_text_file


class TestReadTextFile(unittest.TestCase):
    def test_상한_안이면_그대로_읽는다(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("가나다라", encoding="utf-8")
            text, truncated = read_text_file(str(p))
        self.assertEqual("가나다라", text)
        self.assertFalse(truncated)

    def test_상한을_넘으면_자르고_알린다(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.txt"
            p.write_text("A" * 100, encoding="utf-8")
            text, truncated = read_text_file(str(p), cap=10)
        self.assertEqual(10, len(text))
        self.assertTrue(truncated)

    def test_해독할_수_없는_바이트도_읽는다(self) -> None:
        """인코딩 하나 때문에 원문 전체를 못 보여 주는 것보다 대체 문자가 낫다."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bin.txt"
            p.write_bytes(b"\xff\xfe abc")
            text, _ = read_text_file(str(p))
        self.assertIn("abc", text)


class TestBuildContent(unittest.TestCase):
    def test_문서는_파일에서_읽는다(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "doc.txt"
            p.write_text("본문입니다", encoding="utf-8")
            out = build_content({"asset_id": "a1", "modality": "text",
                                 "fs_path": str(p), "stt": None})
        self.assertEqual(("file", "본문입니다", False), (out["source"], out["text"], out["truncated"]))
        self.assertEqual(TEXT_BYTE_CAP, out["byte_cap"])

    def test_소리는_받아쓰기를_준다(self) -> None:
        out = build_content({"asset_id": "a2", "modality": "audio",
                             "fs_path": "/nowhere.mp3", "stt": "안녕하세요"})
        self.assertEqual(("stt", "안녕하세요"), (out["source"], out["text"]))

    def test_받아쓰기가_없으면_없는_것이다(self) -> None:
        """🔴 빈 글자를 주면 화면은 '원문이 비었다'로 그린다 — 없는 것과 구분되게 None 이다."""
        self.assertIsNone(build_content({"asset_id": "a3", "modality": "video",
                                         "fs_path": "/x.mp4", "stt": None}))

    def test_그림은_글자가_없다(self) -> None:
        self.assertIsNone(build_content({"asset_id": "a4", "modality": "image",
                                         "fs_path": "/x.jpg", "stt": "무시된다"}))

    def test_파일이_사라졌으면_오류다(self) -> None:
        """404(없는 자산)와 410(원본 유실)을 가르려면 여기서 예외를 내야 한다."""
        with self.assertRaises(OSError):
            build_content({"asset_id": "a5", "modality": "text",
                           "fs_path": "/없는/경로.txt", "stt": None})

    def test_받아쓰기도_상한에서_자른다(self) -> None:
        out = build_content({"asset_id": "a6", "modality": "audio",
                             "fs_path": "", "stt": "가" * 100}, cap=30)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["text"].encode("utf-8")), 30)


if __name__ == "__main__":
    unittest.main()
