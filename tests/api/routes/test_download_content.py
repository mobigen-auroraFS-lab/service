"""``GET /assets/{id}/download`` · ``GET /assets/{id}/content`` — 실제 임시 파일로 응답을 확인한다.

원본은 **DB 에 적힌 경로에 있다고 본다**(2026-10-01): 없으면 410 봉투. 구간(Range) · 이어받기 기준값(ETag · If-Range) · 416 ·
파일명 헤더(한글 · 위조 방지) · 노출 게이트(404) · 다 읽은 뒤 핸들이 닫히는지.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets

AID = "018f0000-0000-7000-8000-0000000000a1"
DATA = bytes(range(256)) * 40            # 10,240바이트 — 구간을 눈으로 계산하기 쉬운 크기


def _target(path: str | None, name: str = "보고서.pdf", modality: str = "text") -> dict:
    return {"asset_id": AID, "fs_path": path, "fs_uri": None, "file_size": 0, "modality": modality, "file_name": name}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "원본.bin")
        Path(self.path).write_bytes(DATA)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _get(self, target, headers=None, url=None):
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo(target))):
            return self.client.get(url or f"/assets/{AID}/download", headers=headers or {})


class _Asset:
    def __init__(self, target): self._t = target
    def download_target(self, *, asset_id): return self._t


class _Repo:
    def __init__(self, target): self.asset = _Asset(target)


class TestDownload(_Base):
    def test_전체를_200으로_준다(self) -> None:
        r = self._get(_target(self.path))
        self.assertEqual(200, r.status_code)
        self.assertEqual(DATA, r.content)
        self.assertEqual("10240", r.headers["content-length"])
        self.assertEqual("bytes", r.headers["accept-ranges"])

    def test_헤더_파일명_이어받기_기준값_캐시(self) -> None:
        r = self._get(_target(self.path, name="한글 보고서.pdf"))
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("filename*=UTF-8''%ED%95%9C%EA%B8%80%20%EB%B3%B4%EA%B3%A0%EC%84%9C.pdf", r.headers["content-disposition"])
        self.assertRegex(r.headers["etag"], r'^"[0-9a-f]+-[0-9a-f]+"$')
        self.assertTrue(r.headers["last-modified"].endswith(" GMT"))
        self.assertEqual("private, no-cache", r.headers["cache-control"])      # 인증 응답은 공유 캐시에 남기지 않는다
        self.assertEqual("nosniff", r.headers["x-content-type-options"])
        self.assertEqual("application/pdf", r.headers["content-type"])

    def test_파일명의_따옴표와_개행은_헤더를_쪼개지_못한다(self) -> None:
        r = self._get(_target(self.path, name='a"b\r\nX-Evil: 1.txt'))
        self.assertNotIn("x-evil", r.headers)
        self.assertNotIn('"b', r.headers["content-disposition"].split("filename*")[0].replace('filename="', "", 1))

    def test_구간_요청은_206(self) -> None:
        r = self._get(_target(self.path), headers={"Range": "bytes=10-19"})
        self.assertEqual(206, r.status_code)
        self.assertEqual(DATA[10:20], r.content)
        self.assertEqual("bytes 10-19/10240", r.headers["content-range"])
        self.assertEqual("10", r.headers["content-length"])

    def test_열린_끝_접미_끝_클램프(self) -> None:
        self.assertEqual(DATA[10230:], self._get(_target(self.path), headers={"Range": "bytes=10230-"}).content)
        self.assertEqual(DATA[-5:], self._get(_target(self.path), headers={"Range": "bytes=-5"}).content)
        r = self._get(_target(self.path), headers={"Range": "bytes=10-999999"})        # 끝이 파일을 넘으면 끝까지(거부 아님)
        self.assertEqual((206, DATA[10:]), (r.status_code, r.content))

    def test_범위가_파일_밖이면_416과_크기_안내(self) -> None:
        r = self._get(_target(self.path), headers={"Range": "bytes=20000-"})
        self.assertEqual(416, r.status_code)
        self.assertEqual("bytes */10240", r.headers["content-range"])
        self.assertIn("요청 범위 충족 불가", r.json()["detail"])

    def test_형식이_틀리거나_다중_범위는_416(self) -> None:
        for bad in ("bytes=a-b", "items=0-1", "bytes=0-1,5-6", "bytes=5-2"):
            with self.subTest(bad=bad):
                self.assertEqual(416, self._get(_target(self.path), headers={"Range": bad}).status_code)

    def test_If_Range_가_맞으면_구간_다르면_전체(self) -> None:
        first = self._get(_target(self.path))
        etag = first.headers["etag"]
        ok = self._get(_target(self.path), headers={"Range": "bytes=0-9", "If-Range": etag})
        self.assertEqual((206, DATA[:10]), (ok.status_code, ok.content))
        changed = self._get(_target(self.path), headers={"Range": "bytes=0-9", "If-Range": '"deadbeef-1"'})
        self.assertEqual((200, DATA), (changed.status_code, changed.content))    # 받는 도중 바뀐 파일 — 조각을 섞지 않는다

    def test_받는_도중_파일이_바뀌면_ETag_가_달라진다(self) -> None:
        before = self._get(_target(self.path)).headers["etag"]
        Path(self.path).write_bytes(DATA + b"!")
        self.assertNotEqual(before, self._get(_target(self.path)).headers["etag"])

    def test_빈_파일은_200과_길이_0(self) -> None:
        empty = Path(self._tmp.name) / "빈.txt"
        empty.write_bytes(b"")
        r = self._get(_target(str(empty), name="빈.txt"))
        self.assertEqual((200, b"", "0"), (r.status_code, r.content, r.headers["content-length"]))

    def test_파일이_없으면_410_봉투이고_서버_경로를_싣지_않는다(self) -> None:
        with self.assertLogs("meta_extract.portal_api", "WARNING") as cm:
            r = self._get(_target("/no/such/dir/file.bin"))
        self.assertEqual(410, r.status_code)
        self.assertEqual({"detail": "원본 파일이 존재하지 않거나 접근할 수 없음"}, r.json())
        self.assertNotIn("/no/such", r.text)
        self.assertIn("/no/such/dir/file.bin", cm.records[0].getMessage())       # 운영자는 로그에서 본다

    def test_경로가_비었거나_디렉터리여도_410(self) -> None:
        self.assertEqual(410, self._get(_target(None)).status_code)
        self.assertEqual(410, self._get(_target(self._tmp.name)).status_code)

    def test_노출_대상이_아니면_404(self) -> None:
        r = self._get(None)
        self.assertEqual(404, r.status_code)
        self.assertEqual("다운로드 대상을 찾을 수 없거나 노출 대상이 아님", r.json()["detail"])

    def test_UUID_가_아니면_DB_를_부르지_않고_404(self) -> None:
        with mock.patch.object(routes_assets.DbManager, "read") as read:
            r = self.client.get("/assets/not-a-uuid/download")
        self.assertEqual(404, r.status_code)
        read.assert_not_called()

    def test_다_읽은_뒤_핸들이_닫힌다(self) -> None:
        opened: list = []
        real = routes_assets.open_original

        def spy(p):
            out = real(p)
            opened.append(out[0])
            return out

        with mock.patch.object(routes_assets, "open_original", side_effect=spy):
            self._get(_target(self.path))
            self._get(_target(self.path), headers={"Range": "bytes=0-9"})
            self._get(_target(self.path), headers={"Range": "bytes=99999-"})      # 416 도 닫는다
        self.assertEqual(3, len(opened))
        self.assertTrue(all(fh.closed for fh in opened))

    def test_큰_파일도_조각으로_흘려_받는다(self) -> None:
        big = Path(self._tmp.name) / "big.bin"
        payload = os.urandom(300_000)                    # 64KiB 조각 5개 가까이
        big.write_bytes(payload)
        r = self._get(_target(str(big), name="big.bin"))
        self.assertEqual(payload, r.content)
        mid = self._get(_target(str(big), name="big.bin"), headers={"Range": "bytes=70000-200000"})
        self.assertEqual(payload[70000:200001], mid.content)

    def test_인증_없이는_운영_모드에서_401(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "x" * 40}):
            r = TestClient(app).get(f"/assets/{AID}/download")
        self.assertEqual(401, r.status_code)


class _Content:
    def __init__(self, source): self._s = source
    def source_of(self, asset_id): return self._s


class _ContentRepo:
    def __init__(self, source): self.content = _Content(source)


class TestContent(_Base):
    def _content(self, source):
        with mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_ContentRepo(source))):
            return self.client.get(f"/assets/{AID}/content")

    def test_문서는_원본_파일_글자(self) -> None:
        Path(self.path).write_text("안녕하세요\n두 번째 줄", encoding="utf-8")
        r = self._content({"asset_id": AID, "modality": "text", "fs_path": self.path, "stt": None})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual(("file", "안녕하세요\n두 번째 줄", False), (body["source"], body["text"], body["truncated"]))

    def test_소리는_받아쓰기(self) -> None:
        r = self._content({"asset_id": AID, "modality": "audio", "fs_path": "/없어도/상관없다", "stt": "받아쓴 글"})
        self.assertEqual(("stt", "받아쓴 글"), (r.json()["source"], r.json()["text"]))

    def test_글자가_없는_종류는_404_문구가_다르다(self) -> None:
        r = self._content({"asset_id": AID, "modality": "image", "fs_path": self.path, "stt": None})
        self.assertEqual((404, "이 자산에는 읽을 수 있는 원문이 없습니다"), (r.status_code, r.json()["detail"]))

    def test_노출_대상이_아니면_404(self) -> None:
        r = self._content(None)
        self.assertEqual((404, "자산을 찾을 수 없거나 노출 대상이 아님"), (r.status_code, r.json()["detail"]))

    def test_문서인데_원본이_없으면_410_봉투(self) -> None:
        r = self._content({"asset_id": AID, "modality": "text", "fs_path": "/no/such.txt", "stt": None})
        self.assertEqual((410, {"detail": "원본 파일이 존재하지 않거나 접근할 수 없음"}), (r.status_code, r.json()))

    def test_상한을_넘으면_잘라서_알린다(self) -> None:
        Path(self.path).write_text("가" * 600_000, encoding="utf-8")         # 한글 3바이트 × 60만 = 180만 바이트
        body = self._content({"asset_id": AID, "modality": "text", "fs_path": self.path, "stt": None}).json()
        self.assertTrue(body["truncated"])
        self.assertLessEqual(len(body["text"].encode("utf-8")), body["byte_cap"] + 3)


if __name__ == "__main__":
    unittest.main()
