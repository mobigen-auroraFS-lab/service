"""기동 시 자기 점검 · 접속 주소 제한 — 위험한 설정은 로그로 알리고, 허용 목록 밖의 주소는 403 봉투로 거절한다."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from service.api import posture
from service.api.client_allowlist import ClientAllowlistMiddleware, parse_networks


class TestAllowlist(unittest.TestCase):
    def test_형식_틀린_항목은_무시한다(self) -> None:
        self.assertEqual(2, len(parse_networks("172.16.0.0/24, 10.0.0.5 , 쓰레기, ")))

    def test_허용_판정(self) -> None:
        mw = ClientAllowlistMiddleware(app=None, networks=parse_networks("172.16.0.0/24,10.0.0.5"))
        for ok in ("172.16.0.63", "10.0.0.5", "127.0.0.1", "::1"):       # 루프백은 늘 허용(자기 자신이 잠기지 않게)
            self.assertTrue(mw.allowed(ok), ok)
        for no in ("172.16.1.1", "10.0.0.6", "8.8.8.8", "testclient", None, ""):
            self.assertFalse(mw.allowed(no), str(no))

    def test_목록이_비면_아무것도_거르지_않는다(self) -> None:
        self.assertTrue(ClientAllowlistMiddleware(app=None, networks=[]).allowed("8.8.8.8"))

    def _call(self, mw, host):
        sent: list[dict] = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        asyncio.run(mw({"type": "http", "client": (host, 1234), "method": "GET", "path": "/x", "headers": [], "query_string": b""}, receive, send))
        return sent

    def test_밖의_주소는_403_봉투로_거절하고_앱을_부르지_않는다(self) -> None:
        called = []

        async def app(scope, receive, send):
            called.append(1)

        mw = ClientAllowlistMiddleware(app, networks=parse_networks("172.16.0.0/24"))
        sent = self._call(mw, "8.8.8.8")
        self.assertEqual([], called)
        self.assertEqual(403, sent[0]["status"])
        self.assertIsInstance(json.loads(sent[1]["body"])["detail"], str)
        self._call(mw, "172.16.0.9")
        self.assertEqual([1], called)


class TestPosture(unittest.TestCase):
    def test_인증이_꺼져_있으면_경고한다_목록이_있으면_다른_문장(self) -> None:
        cfg = mock.Mock(auth_disabled=True)
        with mock.patch("service.portal.auth.config.load_portal_auth_config", return_value=cfg):
            with mock.patch.dict(os.environ, {"PORTAL_ALLOWED_CLIENT_CIDRS": ""}), self.assertLogs(posture._LOG, "WARNING") as a:
                posture.warn_auth_disabled()
            self.assertIn("누구나", a.output[0])
            with mock.patch.dict(os.environ, {"PORTAL_ALLOWED_CLIENT_CIDRS": "172.16.0.0/24"}), self.assertLogs(posture._LOG, "WARNING") as b:
                posture.warn_auth_disabled()
            self.assertIn("제한돼 있다", b.output[0])
        with mock.patch("service.portal.auth.config.load_portal_auth_config", return_value=mock.Mock(auth_disabled=False)):
            with self.assertNoLogs(posture._LOG, "WARNING"):
                posture.warn_auth_disabled()

    def test_httptools_가_있으면_경고한다(self) -> None:
        with mock.patch("importlib.util.find_spec", return_value=object()), self.assertLogs(posture._LOG, "WARNING") as cm:
            posture.warn_http_parser()
        self.assertIn("h11", cm.output[0])
        with mock.patch("importlib.util.find_spec", return_value=None), self.assertNoLogs(posture._LOG, "WARNING"):
            posture.warn_http_parser()

    def _check(self, paths):
        with mock.patch("service.portal.common.db_manager.DbManager.read", side_effect=lambda fn: paths):
            return posture.check_origin_paths()

    def test_원본_경로가_하나도_안_보이면_경고한다(self) -> None:
        with self.assertLogs(posture._LOG, "WARNING") as cm:
            self._check(["/없는/마운트/data/a.jpg", "/없는/마운트/data/b.jpg"])
        self.assertIn("마운트", cm.output[0])
        self.assertIn("/없는/마운트/data", cm.output[0])
        self.assertNotIn("a.jpg", cm.output[0])                      # 파일 이름까지 남기지 않는다(앞 4마디만)

    def test_일부만_보이면_개수를_알린다_모두_보이면_정보(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ok = os.path.join(d, "ok.txt")
            open(ok, "w").close()
            with self.assertLogs(posture._LOG, "WARNING") as cm:
                self._check([ok, "/없음/x.txt"])
            self.assertIn("2개 중 1개만", cm.output[0])
            with self.assertLogs(posture._LOG, "INFO") as cm2:
                self._check([ok])
            self.assertIn("모두 보인다", cm2.output[0])

    def test_DB_가_안_되면_조용히_건너뛴다(self) -> None:
        with mock.patch("service.portal.common.db_manager.DbManager.read", side_effect=RuntimeError("db")), self.assertNoLogs(posture._LOG, "INFO"):
            posture.check_origin_paths()


if __name__ == "__main__":
    unittest.main()
