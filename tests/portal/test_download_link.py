"""다운로드 링크 — 헤더 없이 받을 수 있는 짧은 수명의 주소. 발급 · 검증 · 범위 · 접속 토큰과의 분리 · 경로 시험."""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets
from service.portal.auth import authenticate_token
from service.portal.auth.dev_issuer import issue_access_token
from service.portal.auth.download_link import (
    issue_link_token,
    peek_user,
    ttl_seconds,
    verify_link_token,
)

A1 = "018f0000-0000-7000-8000-0000000000a1"
A2 = "018f0000-0000-7000-8000-0000000000a2"
USER = "018f0000-0000-7000-8000-00000000000f"


class TestToken(unittest.TestCase):
    def test_발급한_링크는_그_자산에서만_통한다(self) -> None:
        token, ttl = issue_link_token(user_id=USER, asset_id=A1)
        self.assertEqual(USER, verify_link_token(token, A1))
        self.assertEqual(300, ttl)
        with self.assertRaises(HTTPException) as cm:
            verify_link_token(token, A2)                                   # 다른 자산
        self.assertEqual(401, cm.exception.status_code)

    def test_링크는_접속_토큰으로_통하지_않는다_반대도_마찬가지(self) -> None:
        link, _ = issue_link_token(user_id=USER, asset_id=A1)
        with self.assertRaises(HTTPException):
            authenticate_token(link)                                      # 링크로 API 전체를 열 수 없다
        with self.assertRaises(HTTPException):
            verify_link_token(issue_access_token(user_id=USER), A1)       # 접속 토큰은 링크가 아니다

    def test_만료_변조_엉뚱한_값은_모두_같은_401(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_DOWNLOAD_LINK_TTL_SECONDS": "30"}):
            token, _ = issue_link_token(user_id=USER, asset_id=A1)
        with mock.patch("service.portal.auth.download_link.datetime") as dt:
            from datetime import UTC, datetime, timedelta
            dt.now.return_value = datetime.now(UTC) - timedelta(hours=1)
            old, _ = issue_link_token(user_id=USER, asset_id=A1)         # 한 시간 전에 만든(이미 만료된) 링크
        details = set()
        for bad in (old, token[:-3] + "xyz", "", "abc.def.ghi", jwt.encode({"sub": USER, "aid": A1, "scope": "dl", "exp": int(time.time()) + 60}, "다른 키", algorithm="HS256")):
            with self.assertRaises(HTTPException) as cm:
                verify_link_token(bad, A1)
            details.add(cm.exception.detail)
        self.assertEqual(1, len(details))                                 # 무엇이 틀렸는지 알려 주지 않는다

    def test_범위가_다르면_통하지_않는다(self) -> None:
        # 같은 키로 서명했어도 범위(scope)가 dl 이 아니면 통하지 않는다.
        from service.portal.auth.download_link import _key
        other = jwt.encode({"sub": USER, "aid": A1, "scope": "x", "exp": int(time.time()) + 60}, _key(), algorithm="HS256")
        with self.assertRaises(HTTPException):
            verify_link_token(other, A1)

    def test_수명_설정은_30초에서_1시간으로_고정된다(self) -> None:
        for raw, want in (("", 300), ("x", 300), ("5", 30), ("120", 120), ("999999", 3600)):
            with mock.patch.dict(os.environ, {"PORTAL_DOWNLOAD_LINK_TTL_SECONDS": raw}):
                self.assertEqual(want, ttl_seconds(), raw)

    def test_기록용_주체_읽기(self) -> None:
        token, _ = issue_link_token(user_id=USER, asset_id=A1)
        self.assertEqual(USER, peek_user(token))
        self.assertIsNone(peek_user("쓰레기"))


class _Asset:
    def __init__(self, target): self._t = target
    def download_target(self, *, asset_id): return self._t


class _Repo:
    def __init__(self, target): self.asset = _Asset(target)


class TestRoutes(unittest.TestCase):
    """인증이 **켜진** 상태로 — 헤더 없이 링크만으로 받아지는가."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "a.bin")
        Path(self.path).write_bytes(bytes(range(256)) * 20)
        self.target = {"asset_id": A1, "fs_path": self.path, "fs_uri": None, "file_size": 0, "modality": "text", "file_name": "a.bin"}
        env = mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret-for-link-routes-0123456789"})
        env.start()
        self.addCleanup(env.stop)
        # 계정 표 조회는 대역으로 — 활성 계정
        acc = mock.patch("service.portal.auth.deps.with_account", side_effect=lambda p, **_: p)
        acc.start()
        self.addCleanup(acc.stop)
        db = mock.patch.object(routes_assets.DbManager, "read", side_effect=lambda fn: fn(_Repo(self.target)))
        db.start()
        self.addCleanup(db.stop)
        from service.portal.auth import verifier
        verifier._verifier = None
        self.addCleanup(lambda: setattr(verifier, "_verifier", None))
        self.client = TestClient(app)
        self.bearer = {"Authorization": f"Bearer {issue_access_token(user_id=USER)}"}

    def _mint(self, headers=None, aid=A1):
        return self.client.post(f"/assets/{aid}/download-link", headers=self.bearer if headers is None else headers)

    def test_링크를_받아_헤더_없이_받는다(self) -> None:
        r = self._mint()
        self.assertEqual(200, r.status_code)
        self.assertEqual("no-store", r.headers["cache-control"])
        body = r.json()
        self.assertEqual({"url", "expires_in"}, set(body))
        self.assertTrue(body["url"].startswith(f"/assets/{A1}/download?link="))
        got = self.client.get(body["url"])                                    # Authorization 헤더 없음
        self.assertEqual(200, got.status_code)
        self.assertEqual(5120, len(got.content))
        self.assertEqual("no-referrer", got.headers["referrer-policy"])
        head = self.client.head(body["url"])
        self.assertEqual((200, "5120"), (head.status_code, head.headers["content-length"]))
        part = self.client.get(body["url"], headers={"Range": "bytes=10-19"})   # 이어받기도 같은 링크로
        self.assertEqual((206, 10), (part.status_code, len(part.content)))

    def test_링크를_받는_창구는_헤더가_필요하다(self) -> None:
        self.assertEqual(401, self._mint(headers={}).status_code)

    def test_노출_대상이_아니면_링크를_주지_않는다(self) -> None:
        self.target = None
        self.assertEqual(404, self._mint().status_code)
        self.assertEqual(404, self._mint(aid="not-a-uuid").status_code)

    def test_다른_자산에_링크를_쓰면_401(self) -> None:
        url = self._mint().json()["url"]
        r = self.client.get(url.replace(A1, A2))
        self.assertEqual(401, r.status_code)

    def test_링크도_헤더도_없으면_401_틀린_링크도_401(self) -> None:
        self.assertEqual(401, self.client.get(f"/assets/{A1}/download").status_code)
        self.assertEqual(401, self.client.get(f"/assets/{A1}/download?link=아무거나").status_code)

    def test_링크로는_다른_창구를_못_연다(self) -> None:
        url = self._mint().json()["url"]
        link = url.split("link=")[1]
        for path in (f"/assets/{A1}", f"/assets/{A1}/content", "/file-search", "/me"):
            self.assertEqual(401, self.client.get(f"{path}?link={link}").status_code, path)

    def test_접속_토큰은_링크로_통하지_않는다(self) -> None:
        token = self.bearer["Authorization"].split()[1]
        self.assertEqual(401, self.client.get(f"/assets/{A1}/download?link={token}").status_code)

    def test_링크가_있어도_헤더가_있으면_헤더를_따른다(self) -> None:
        url = self._mint().json()["url"]
        r = self.client.get(url, headers={"Authorization": "Bearer wrong.token.value"})
        self.assertEqual(401, r.status_code)                                  # 헤더를 주면 그것으로 판정한다(링크로 되살리지 않는다)

    def test_정지된_계정은_링크로도_막힌다(self) -> None:
        url = self._mint().json()["url"]
        with mock.patch("service.portal.auth.deps.with_account", side_effect=HTTPException(status_code=401, detail="정지된 계정입니다")):
            self.assertEqual(401, self.client.get(url).status_code)

    def test_문서에_창구가_올라간다(self) -> None:
        ops = app.openapi()["paths"]["/assets/{asset_id}/download-link"]
        self.assertEqual({"post"}, set(ops))


if __name__ == "__main__":
    unittest.main()
