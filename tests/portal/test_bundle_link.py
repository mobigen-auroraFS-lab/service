"""묶음 링크 — 요청 대상(메서드 + 경로 + 조건)에 묶인 짧은 수명의 주소로 헤더 없이 묶음(zip)을 받는다."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from service.api import app
from service.api.routes import assets as routes_assets
from service.api.routes import mm_meta as routes_mm_meta
from service.portal.auth.dev_issuer import issue_access_token
from service.portal.auth.download_link import (
    canonical_target,
    issue_bundle_link_token,
    issue_link_token,
    verify_bundle_link_token,
    verify_link_token,
)

A1 = "018f0000-0000-7000-8000-0000000000a1"
A2 = "018f0000-0000-7000-8000-0000000000a2"
USER = "018f0000-0000-7000-8000-00000000000f"


class TestToken(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret-for-bundle-links-0123456789"})
        env.start()
        self.addCleanup(env.stop)

    def test_대상을_정규화한다(self) -> None:
        self.assertEqual("GET /x", canonical_target("HEAD", "/x", []))
        self.assertEqual("GET /x?a=1&b=2", canonical_target("get", "/x", [("b", "2"), ("a", "1")]), "조건 순서는 상관없다")
        self.assertEqual("GET /x?a=1", canonical_target("GET", "/x", [("a", "1"), ("link", "토큰")]), "link 는 대상이 아니다")
        self.assertEqual("POST /y", canonical_target("POST", "/y", []))

    def test_발급한_링크는_그_대상에서만_통한다(self) -> None:
        token, ttl = issue_bundle_link_token(user_id=USER, target="GET /assets/a/bundle")
        self.assertEqual(USER, verify_bundle_link_token(token, "GET /assets/a/bundle"))
        self.assertEqual(300, ttl)
        for other in ("GET /assets/b/bundle", "GET /assets/a/bundle?x=1", "POST /assets/a/bundle"):
            with self.subTest(other=other), self.assertRaises(HTTPException) as cm:
                verify_bundle_link_token(token, other)
            self.assertEqual(401, cm.exception.status_code)

    def test_단건_링크와_묶음_링크는_서로_통하지_않는다(self) -> None:
        bundle, _ = issue_bundle_link_token(user_id=USER, target=f"GET /assets/{A1}/download")
        single, _ = issue_link_token(user_id=USER, asset_id=A1)
        with self.assertRaises(HTTPException):
            verify_link_token(bundle, A1)                                  # 묶음 링크로 단건을 못 받는다
        with self.assertRaises(HTTPException):
            verify_bundle_link_token(single, f"GET /assets/{A1}/download")  # 단건 링크로 묶음을 못 받는다

    def test_접속_토큰은_링크가_아니다(self) -> None:
        with self.assertRaises(HTTPException):
            verify_bundle_link_token(issue_access_token(user_id=USER), "GET /x")

    def test_변조는_같은_401(self) -> None:
        token, _ = issue_bundle_link_token(user_id=USER, target="GET /x")
        details = set()
        for bad in (token[:-3] + "xyz", "", "abc.def.ghi"):
            with self.assertRaises(HTTPException) as cm:
                verify_bundle_link_token(bad, "GET /x")
            details.add(cm.exception.detail)
        self.assertEqual(1, len(details))


class _Routes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "a.txt").write_bytes(b"A" * 50)
        (d / "b.txt").write_bytes(b"B" * 80)
        self.targets = [
            {"asset_id": A1, "fs_path": str(d / "a.txt"), "file_name": "a.txt"},
            {"asset_id": A2, "fs_path": str(d / "b.txt"), "file_name": "b.txt"},
        ]
        env = mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret-for-bundle-links-0123456789"})
        env.start()
        self.addCleanup(env.stop)
        acc = mock.patch("service.portal.auth.deps.with_account", side_effect=lambda p, **_: p)
        acc.start()
        self.addCleanup(acc.stop)
        from service.portal.auth import verifier
        verifier._verifier = None
        self.addCleanup(lambda: setattr(verifier, "_verifier", None))

        outer = self

        class _Asset:
            def bundle_targets(self, *, seed_asset_id):
                return outer.targets

        class _Selection:
            def targets(self, ids):
                return {"targets": [t for t in outer.targets if t["asset_id"] in ids], "missing": []}

        class _Entity:
            def zip_rows(self, **kw):
                outer.zip_rows_kw = kw
                return [{"asset_id": t["asset_id"], "modality": "text", "file_name": t["file_name"], "file_size": 1, "fs_path": t["fs_path"]} for t in outer.targets]

            def card_zip_targets(self, **kw):
                outer.card_kw = kw
                return (outer.targets, "카드")

        class _AssetWithTopic(_Asset):
            def topic_bundle_targets(self, *, topic, subtopic, exclude_video):
                outer.topic_kw = {"topic": topic, "subtopic": subtopic, "exclude_video": exclude_video}
                return [] if topic == "없는주제" else [{**t, "file_name": t["file_name"]} for t in outer.targets]

        class _Repo:
            asset = _AssetWithTopic()
            selection = _Selection()
            entity = _Entity()
            conn = object()

        for mod in (routes_assets, routes_mm_meta):
            p = mock.patch.object(mod.DbManager, "read", side_effect=lambda fn: fn(_Repo()))
            p.start()
            self.addCleanup(p.stop)
        w = mock.patch.object(routes_assets.DbManager, "write", side_effect=lambda fn: fn(_Repo()))
        w.start()
        self.addCleanup(w.stop)
        self.audited = mock.patch.object(routes_assets, "record_access_many")
        self.audit = self.audited.start()
        self.addCleanup(self.audited.stop)
        self.client = TestClient(app)
        self.bearer = {"Authorization": f"Bearer {issue_access_token(user_id=USER)}"}

    def mint(self, url: str, method: str = "GET", headers=None):
        return self.client.post("/bundle-links", json={"method": method, "url": url}, headers=self.bearer if headers is None else headers)

    @staticmethod
    def names(r) -> list[str]:
        return zipfile.ZipFile(io.BytesIO(r.content)).namelist()


class TestMint(_Routes):
    def test_헤더가_있어야_받는다(self) -> None:
        self.assertEqual(401, self.mint(f"/assets/{A1}/bundle", headers={}).status_code)

    def test_받으면_url_과_수명이_온다(self) -> None:
        r = self.mint(f"/assets/{A1}/bundle")
        self.assertEqual(200, r.status_code)
        self.assertEqual("no-store", r.headers["cache-control"])
        body = r.json()
        self.assertEqual({"url", "method", "expires_in"}, set(body))
        self.assertEqual(("GET", 300), (body["method"], body["expires_in"]))
        self.assertTrue(body["url"].startswith(f"/assets/{A1}/bundle?link="))

    def test_묶음이_아닌_주소는_거절한다(self) -> None:
        for method, url in (("GET", f"/assets/{A1}/download"), ("GET", "/me"), ("GET", f"/assets/{A1}"), ("POST", f"/assets/{A1}/bundle"),
                            ("GET", "/assets/bundle/download"), ("GET", "http://evil.example/assets/x/bundle"), ("GET", "//evil.example/mm-meta/bundle"),
                            ("GET", "assets/bundle"), ("POST", "/assets/bundle")):
            with self.subTest(method=method, url=url):
                r = self.mint(url, method)
                self.assertEqual(400, r.status_code, r.text)
                self.assertIn("묶음 링크를 받을 수 없는 주소", r.json()["detail"])

    def test_받는_주소에_이미_link_가_있으면_걷어낸다(self) -> None:
        url = self.mint(f"/assets/{A1}/bundle?link=옛토큰").json()["url"]
        self.assertEqual(1, url.count("link="))
        self.assertEqual(200, self.client.get(url).status_code)


class TestRelationBundleLink(_Routes):
    def test_헤더_없이_링크로_받는다(self) -> None:
        url = self.mint(f"/assets/{A1}/bundle").json()["url"]
        r = self.client.get(url)
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt", "b.txt"], self.names(r))
        self.assertEqual("no-referrer", r.headers["referrer-policy"])
        self.assertEqual(("2", "2"), (r.headers["x-bundle-count"], r.headers["x-bundle-files"]))

    def test_링크도_헤더도_없으면_401(self) -> None:
        self.assertEqual(401, self.client.get(f"/assets/{A1}/bundle").status_code)

    def test_헤더로도_받는다(self) -> None:
        self.assertEqual(200, self.client.get(f"/assets/{A1}/bundle", headers=self.bearer).status_code)

    def test_다른_자산이나_조건을_바꾸면_401(self) -> None:
        link = self.mint(f"/assets/{A1}/bundle").json()["url"].split("link=")[1]
        self.assertEqual(401, self.client.get(f"/assets/{A2}/bundle?link={link}").status_code)
        self.assertEqual(401, self.client.get(f"/assets/{A1}/bundle?x=1&link={link}").status_code)

    def test_HEAD_는_본문_없이_같은_머리를_주고_기록을_남기지_않는다(self) -> None:
        url = self.mint(f"/assets/{A1}/bundle").json()["url"]
        g, h = self.client.get(url), self.client.head(url)
        self.assertEqual((200, b""), (h.status_code, h.content))
        for k in ("x-bundle-count", "x-bundle-missing", "x-bundle-bytes", "content-disposition", "content-type"):
            self.assertEqual(g.headers[k], h.headers[k], k)

    def test_단건_링크로는_묶음을_못_연다(self) -> None:
        token, _ = issue_link_token(user_id=USER, asset_id=A1)
        self.assertEqual(401, self.client.get(f"/assets/{A1}/bundle?link={token}").status_code)


class TestEntityBundleLink(_Routes):
    def test_조건이_묶여_있고_순서는_상관없다(self) -> None:
        url = self.mint("/mm-meta/bundle?entity_type=작품&areas=가,나").json()["url"]
        r = self.client.get(url)
        self.assertEqual(200, r.status_code)
        self.assertEqual(["작품", ["가", "나"]], [self.zip_rows_kw["entity_type"], self.zip_rows_kw["areas"]])
        link = url.split("link=")[1]
        self.assertEqual(200, self.client.get(f"/mm-meta/bundle?areas=가,나&entity_type=작품&link={link}").status_code, "조건 순서가 달라도 같은 요청 대상")
        self.assertEqual(401, self.client.get(f"/mm-meta/bundle?entity_type=작품&areas=가&link={link}").status_code, "조건이 바뀌면 안 된다")
        self.assertEqual(401, self.client.get(f"/mm-meta/bundle?link={link}").status_code)

    def test_카드_묶음은_한글_경로도_받는다(self) -> None:
        url = self.mint("/mm-meta/작품/묘법연화경/bundle").json()["url"]
        self.assertIn("%EC%9E%91%ED%92%88", url)                           # 경로는 인코딩해서 돌려준다
        r = self.client.get(url)
        self.assertEqual(200, r.status_code)
        self.assertEqual(("작품", "묘법연화경"), (self.card_kw["entity_type"], self.card_kw["entity_uid"]))
        self.assertEqual(200, self.client.head(url).status_code)
        link = url.split("link=")[1]
        self.assertEqual(401, self.client.get(f"/mm-meta/작품/다른개체/bundle?link={link}").status_code)


class TestTopicBundleLink(_Routes):
    def test_분류_묶음을_링크로_받는다(self) -> None:
        url = self.mint("/topics/역사·문화유산/bundle?subtopic=유적·유물&exclude_video=true").json()["url"]
        r = self.client.get(url)                                           # 헤더 없음
        self.assertEqual(200, r.status_code)
        self.assertEqual(["a.txt", "b.txt"], self.names(r))
        self.assertEqual({"topic": "역사·문화유산", "subtopic": "유적·유물", "exclude_video": True}, self.topic_kw)
        self.assertEqual(("2", "2"), (r.headers["x-bundle-count"], r.headers["x-bundle-files"]))
        self.assertIn("filename*=UTF-8", r.headers["content-disposition"])
        self.assertEqual(200, self.client.head(url).status_code)

    def test_하위주제_빈_값은_하위주제가_없는_자산만(self) -> None:
        url = self.mint("/topics/가족/bundle?subtopic=").json()["url"]
        self.assertEqual(200, self.client.get(url).status_code)
        self.assertEqual({"topic": "가족", "subtopic": "", "exclude_video": False}, self.topic_kw)
        self.assertEqual(401, self.client.get("/topics/가족/bundle?link=" + url.split("link=")[1]).status_code, "빈 값과 안 줌은 다른 요청 대상이다")

    def test_주제_전체는_하위주제_없이(self) -> None:
        self.assertEqual(200, self.client.get("/topics/역사/bundle", headers=self.bearer).status_code)
        self.assertEqual({"topic": "역사", "subtopic": None, "exclude_video": False}, self.topic_kw)

    def test_조건을_바꾸면_링크가_통하지_않는다(self) -> None:
        link = self.mint("/topics/역사/bundle?subtopic=가").json()["url"].split("link=")[1]
        self.assertEqual(401, self.client.get(f"/topics/역사/bundle?subtopic=나&link={link}").status_code)
        self.assertEqual(401, self.client.get(f"/topics/다른/bundle?subtopic=가&link={link}").status_code)
        self.assertEqual(401, self.client.get("/topics/역사/bundle?subtopic=가").status_code)

    def test_분류가_없으면_404(self) -> None:
        self.assertEqual(404, self.client.get("/topics/없는주제/bundle", headers=self.bearer).status_code)

    def test_경로가_없는_자료만이면_409(self) -> None:
        for t in self.targets:
            t["fs_path"] = None
        self.assertEqual(409, self.client.get("/topics/역사/bundle", headers=self.bearer).status_code)


class TestSelectionFormLink(_Routes):
    FORM = {"Content-Type": "application/x-www-form-urlencoded"}

    def _post(self, body: str, *, link: str | None = None, headers=None, url: str = "/assets/bundle/download"):
        if link is None:
            link = self.mint("/assets/bundle/download", "POST").json()["url"].split("link=")[1]
        return self.client.post(f"{url}?link={link}", content=body, headers=self.FORM if headers is None else headers)

    def test_폼으로_제출해_받는다(self) -> None:
        r = self._post(f"asset_ids={A1}&asset_ids={A2}")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(["a.txt", "b.txt"], self.names(r))
        self.assertEqual(("2", "0"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        self.assertEqual(USER, self.audit.call_args.kwargs["user_id"], "접근 기록은 링크를 받은 사용자 이름으로 남는다")

    def test_쉼표로_이어도_받는다(self) -> None:
        self.assertEqual(["a.txt", "b.txt"], self.names(self._post(f"asset_ids={A1},{A2}")))

    def test_형식이_틀리면_400_이고_폼이_아니면_400(self) -> None:
        self.assertEqual(400, self._post("asset_ids=nope").status_code)
        self.assertEqual(400, self._post("").status_code)
        r = self._post('{"asset_ids": []}', headers={"Content-Type": "application/json"})
        self.assertEqual(400, r.status_code)
        self.assertIn("urlencoded", r.json()["detail"])

    def test_링크가_없으면_401_이고_다른_창구에는_안_통한다(self) -> None:
        self.assertEqual(401, self.client.post("/assets/bundle/download", content=f"asset_ids={A1}", headers=self.FORM).status_code)
        link = self.mint("/assets/bundle/download", "POST").json()["url"].split("link=")[1]
        # JSON 창구(헤더 전용)와 GET 묶음에는 이 링크가 통하지 않는다.
        self.assertEqual(401, self.client.post(f"/assets/bundle?link={link}", json={"asset_ids": [A1]}).status_code)
        self.assertEqual(401, self.client.get(f"/assets/{A1}/bundle?link={link}").status_code)

    def test_헤더로도_제출한다(self) -> None:
        r = self.client.post("/assets/bundle/download", content=f"asset_ids={A1}", headers={**self.FORM, **self.bearer})
        self.assertEqual(200, r.status_code)

    def test_JSON_창구는_그대로다(self) -> None:
        r = self.client.post("/assets/bundle", json={"asset_ids": [A1, A2]}, headers=self.bearer)
        self.assertEqual(["a.txt", "b.txt"], self.names(r))

    def test_건수_상한은_없다(self) -> None:
        ids = "&".join(f"asset_ids=018f0000-0000-7000-8000-{i:012d}" for i in range(400)) + f"&asset_ids={A1}"
        self.assertEqual(200, self._post(ids).status_code)


if __name__ == "__main__":
    unittest.main()
