"""화면이 요구하던 창구들 — 원문 · 고른 자산 묶음 · 목록(관계 종류·태그) · 추천 · 추가 칩 · 계정.

여기서 지키는 것
  · 노출 게이트와 오류 코드가 기존 창구와 **같은 말을 쓴다**(404·409·413·410).
  · 미구현 창구는 **조용한 성공을 주지 않는다**(501) — 화면이 "저장됐다"고 믿으면 안 된다.
  · 묶음은 담긴 자산마다 감사 기록을 남긴다(개별 다운로드와 같은 낱개).
"""

from __future__ import annotations

import os
import unittest
import zipfile
from io import BytesIO
from unittest import mock

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db  # noqa: E402
from service.portal.asset import selection  # noqa: E402
from service.portal.auth import authenticate_token  # noqa: E402
from service.portal.auth.passwords import hash_password  # noqa: E402
from service.portal.repositories import admin_repo  # noqa: E402
from service.portal.repositories.account_repo import AccountRepository  # noqa: E402
from service.portal.repositories.content_repo import AssetContentRepository  # noqa: E402
from service.portal.repositories.selection_repo import SelectionRepository  # noqa: E402

A1 = "01a08fa8-0000-7000-8000-00000000a001"
A2 = "01a08fa8-0000-7000-8000-00000000a002"


def _read_only(cb):
    """DB seam 대역 — 커넥션 대신 ``None`` 을 넘긴다(저장소 메서드는 대역으로 갈아끼운다)."""
    return cb(None)


class TestAssetContent(unittest.TestCase):
    """``GET /assets/{id}/content`` — 상세 화면의 원문 영역."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _get(self, source, *, asset_id=A1):
        with mock.patch.object(AssetContentRepository, "source_of", return_value=source), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            return self.client.get(f"/assets/{asset_id}/content")

    def test_형식이_아닌_id_는_404(self) -> None:
        self.assertEqual(404, self.client.get("/assets/not-a-uuid/content").status_code)

    def test_노출_대상이_아니면_404(self) -> None:
        self.assertEqual(404, self._get(None).status_code)

    def test_글자가_없으면_404(self) -> None:
        r = self._get({"asset_id": A1, "modality": "image", "fs_path": "/x.jpg", "stt": None})
        self.assertEqual(404, r.status_code)
        self.assertIn("원문", r.json()["detail"])

    def test_받아쓰기를_돌려준다(self) -> None:
        r = self._get({"asset_id": A1, "modality": "audio", "fs_path": "/x.mp3", "stt": "안녕"})
        self.assertEqual(200, r.status_code)
        self.assertEqual({"stt", "안녕", False}, {r.json()["source"], r.json()["text"],
                                                 r.json()["truncated"]})

    def test_원본이_사라졌으면_410(self) -> None:
        """404(없는 자산)와 가른다 — 있는데 파일만 없어진 것은 복구 대상이다."""
        r = self._get({"asset_id": A1, "modality": "text", "fs_path": "/없는/파일.txt", "stt": None})
        self.assertEqual(410, r.status_code)


class TestSelectionBundle(unittest.TestCase):
    """``POST /assets/bundle`` — 목록에서 고른 자산들을 한 zip 으로."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _post(self, ids, picked=None):
        picked = picked if picked is not None else {"targets": [], "missing": ids, "total_bytes": 0}
        with mock.patch.object(SelectionRepository, "targets", return_value=picked), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only), \
             mock.patch.object(db, "run_in_db_write", side_effect=_read_only), \
             mock.patch("service.api.routes.assets.record_access", return_value="acc"):
            return self.client.post("/assets/bundle", json={"asset_ids": ids})

    def test_빈_목록은_400(self) -> None:
        self.assertEqual(400, self._post([]).status_code)

    def test_UUID_가_아니면_400(self) -> None:
        r = self._post(["not-a-uuid"])
        self.assertEqual(400, r.status_code)
        self.assertIn("asset_ids", r.json()["detail"])

    def test_건수_상한을_넘으면_400(self) -> None:
        r = self._post([A1] * (selection.MAX_SELECTION + 1))
        self.assertEqual(400, r.status_code)
        self.assertIn(str(selection.MAX_SELECTION), r.json()["detail"])

    def test_전부_노출_대상이_아니면_409(self) -> None:
        """빈 zip 을 주면 사용자는 '받았는데 비었다'를 오류로 오해한다."""
        self.assertEqual(409, self._post([A1, A2]).status_code)

    def test_용량_상한을_넘으면_413(self) -> None:
        picked = {"targets": [{"asset_id": A1, "fs_path": "/a.bin", "file_name": "a.bin"}],
                  "missing": [], "total_bytes": selection.MAX_SELECTION_BYTES + 1}
        r = self._post([A1], picked)
        self.assertEqual(413, r.status_code)
        self.assertIn("MB", r.json()["detail"])

    def _post_targets(self, targets, missing, ids):
        with mock.patch.object(SelectionRepository, "targets", return_value={
                "targets": targets, "missing": missing, "total_bytes": 10}), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only), \
             mock.patch.object(db, "run_in_db_write", side_effect=_read_only), \
             mock.patch("service.api.routes.assets.record_access", return_value="acc") as rec:
            r = self.client.post("/assets/bundle", json={"asset_ids": ids})
        return r, rec

    def test_zip_을_흘려보낸다(self) -> None:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh:
            fh.write(b"hello")
            fh.flush()
            r, rec = self._post_targets(
                [{"asset_id": A1, "fs_path": fh.name, "file_name": "a.txt"}], [A2], [A1, A2])
        self.assertEqual(200, r.status_code)
        self.assertEqual("application/zip", r.headers["content-type"])
        self.assertEqual(("1", "1"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        self.assertEqual(1, rec.call_count)   # 담긴 자산마다 감사 한 행(개별 다운로드와 같은 낱개)
        self.assertEqual(["a.txt"], zipfile.ZipFile(BytesIO(r.content)).namelist())

    def test_원본이_없으면_헤더가_빠진_것으로_센다(self) -> None:
        """🔴 2026-09-22 사용자 흐름 실측 — 원본이 없는데 헤더가 '담김 3 · 빠짐 0' 이라 했다.

        헤더는 화면이 받은 zip 이 온전한지 맞춰 보라고 둔 것이다. 원본이 없을 때 틀리면 쓸모가 없다.
        감사도 **실제로 담긴 것만** 남긴다(받지도 않은 파일을 받았다고 적으면 이력이 거짓이 된다).
        """
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh:
            fh.write(b"hello")
            fh.flush()
            r, rec = self._post_targets(
                [{"asset_id": A1, "fs_path": fh.name, "file_name": "a.txt"},
                 {"asset_id": A2, "fs_path": "/없는/b.txt", "file_name": "b.txt"}],
                [], [A1, A2])
        self.assertEqual(200, r.status_code)
        self.assertEqual(("1", "1"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        self.assertEqual(1, rec.call_count)
        self.assertEqual(A1, rec.call_args.kwargs["asset_id"])
        names = zipfile.ZipFile(BytesIO(r.content)).namelist()
        self.assertEqual(["a.txt", "_manifest.json"], names)

    def test_전부_원본이_없으면_목록_파일만(self) -> None:
        """모두 빠져도 실패로 끊지 않는다 — 무엇이 빠졌는지는 헤더와 목록 파일이 알린다."""
        r, rec = self._post_targets(
            [{"asset_id": A1, "fs_path": "/없는/a.txt", "file_name": "a.txt"}], [A2], [A1, A2])
        self.assertEqual(200, r.status_code)
        self.assertEqual(("0", "2"), (r.headers["x-bundle-files"], r.headers["x-bundle-missing"]))
        self.assertEqual(0, rec.call_count)
        self.assertEqual(["_manifest.json"], zipfile.ZipFile(BytesIO(r.content)).namelist())


class TestCatalogRoutes(unittest.TestCase):
    """``GET /relation-kinds`` · ``GET /tags`` — 고를 값의 목록."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_관계_종류_상태_어휘_밖은_400(self) -> None:
        self.assertEqual(400, self.client.get("/relation-kinds?status=bogus").status_code)

    def test_관계_종류는_한글_이름을_준다(self) -> None:
        rows = {"rows": [{"kind_code": "same_domain", "kind_name_ko": "같은 분야",
                          "description": "…", "status": "active"}], "total": 1}
        with mock.patch.object(admin_repo, "list_relation_kinds", return_value=rows), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.get("/relation-kinds")
        self.assertEqual(200, r.status_code)
        self.assertEqual("같은 분야", r.json()["rows"][0]["kind_name_ko"])

    def test_태그는_주제로_좁혀_받는다(self) -> None:
        from service.portal.repositories.catalog_repo import CatalogRepository
        with mock.patch.object(CatalogRepository, "tags",
                               return_value=[{"tag": "한옥", "count": 3}]) as tags, \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.get("/tags?topic=역사·문화유산&subtopic=유적·유물&limit=5")
        self.assertEqual(200, r.status_code)
        self.assertEqual({"rows": [{"tag": "한옥", "count": 3}], "total": 1}, r.json())
        kw = tags.call_args.kwargs
        self.assertEqual((["역사·문화유산"], ["유적·유물"], 5), (kw["topics"], kw["subtopics"], kw["limit"]))


class TestSuggestAndFacetExtra(unittest.TestCase):
    """``/file-search/suggest`` · ``/file-search/facet-extra``."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_추천은_검색어가_있어야_한다(self) -> None:
        self.assertEqual(422, self.client.get("/file-search/suggest").status_code)

    def test_추천을_돌려준다(self) -> None:
        from service.portal.repositories.catalog_repo import CatalogRepository
        with mock.patch.object(CatalogRepository, "suggest",
                               return_value=[{"value": "한옥", "kind": "tag", "count": 3}]), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.get("/file-search/suggest?q=한")
        self.assertEqual(200, r.status_code)
        self.assertEqual(1, r.json()["total"])

    def test_모르는_축은_422(self) -> None:
        self.assertEqual(422, self.client.get("/file-search/facet-extra?axis=bogus").status_code)

    def test_축_건수를_돌려준다(self) -> None:
        out = {"axes": {"file_ext": [{"key": "txt", "count": 2}]}, "total": 2, "as_of": "x"}
        with mock.patch("service.api.routes.file_search.extra_facets", return_value=out), \
             mock.patch("src.search.opensearch_sync.get_client", lambda *a, **k: object()), \
             mock.patch("service.api.routes.file_search.get_current_settings"):
            r = self.client.get("/file-search/facet-extra?axis=file_ext&topic=역사·문화유산")
        self.assertEqual(200, r.status_code)
        self.assertEqual(out, r.json())


class TestAccount(unittest.TestCase):
    """회원가입 · 아이디 중복 확인 · 로그인 — 운영 모드에서 토큰을 받는 유일한 길이다."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_가입하면_201_과_계정_정보를_준다(self) -> None:
        with mock.patch.object(AccountRepository, "create") as create, \
             mock.patch.object(db, "run_in_db_write", side_effect=_read_only):
            r = self.client.post("/auth/signup", json={"login_id": " bc ", "password": "pw",
                                                       "display_name": " 백채영 "})
        self.assertEqual(201, r.status_code, r.text)
        body = r.json()
        self.assertEqual(("bc", "active"), (body["login_id"], body["status"]))
        kw = create.call_args.kwargs
        # 앞뒤 공백은 떼고 저장한다 — ' bc' 와 'bc' 가 다른 계정이 되면 안 된다.
        self.assertEqual(("bc", "백채영"), (kw["login_id"], kw["display_name"]))
        # 🔴 평문을 저장하지 않는다.
        self.assertNotIn("pw", kw["password_hash"])
        self.assertTrue(kw["password_hash"].startswith("$argon2id$"))

    def test_아이디가_겹치면_409(self) -> None:
        """미리 세어 보지 않고 **DB 의 유일 인덱스**로 판정한다(동시 가입 방지)."""
        from psycopg import errors as pg_errors

        with mock.patch.object(AccountRepository, "create",
                               side_effect=pg_errors.UniqueViolation("dup")), \
             mock.patch.object(db, "run_in_db_write", side_effect=_read_only):
            r = self.client.post("/auth/signup", json={"login_id": "bc", "password": "pw"})
        self.assertEqual(409, r.status_code)
        self.assertIn("bc", r.json()["detail"])

    def test_공백만_보낸_값은_422(self) -> None:
        for body in ({"login_id": "   ", "password": "pw"}, {"login_id": "bc", "password": "   "}):
            with self.subTest(body=body):
                self.assertEqual(422, self.client.post("/auth/signup", json=body).status_code)

    def test_중복_확인은_확정이_아니다(self) -> None:
        with mock.patch.object(AccountRepository, "exists", return_value=True), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.get("/auth/login-id/availability?login_id=bc")
        self.assertEqual({"login_id": "bc", "available": False}, r.json())

    def _row(self, password: str = "pw", status: str = "active") -> dict:
        return {"user_id": "01a08fa8-0000-7000-8000-0000000000aa", "login_id": "bc",
                "display_name": None, "password_hash": hash_password(password), "status": status,
                "role": "user"}

    def test_로그인하면_토큰을_준다(self) -> None:
        with mock.patch.object(AccountRepository, "find_by_login_id", return_value=self._row()), \
             mock.patch.object(AccountRepository, "touch_login") as touch, \
             mock.patch.object(db, "run_in_db", side_effect=_read_only), \
             mock.patch.object(db, "run_in_db_write", side_effect=_read_only):
            r = self.client.post("/auth/login", json={"login_id": "bc", "password": "pw"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("bearer", r.json()["token_type"])
        # 받은 토큰이 실제로 이 서비스의 검증을 통과해야 한다.
        self.assertEqual("01a08fa8-0000-7000-8000-0000000000aa",
                         authenticate_token(r.json()["access_token"]).user_id)
        touch.assert_called_once()

    def test_비밀번호가_틀리면_401(self) -> None:
        with mock.patch.object(AccountRepository, "find_by_login_id", return_value=self._row()), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.post("/auth/login", json={"login_id": "bc", "password": "nope"})
        self.assertEqual(401, r.status_code)
        self.assertEqual("아이디 또는 비밀번호가 올바르지 않습니다", r.json()["detail"])

    def test_없는_아이디도_같은_문구다(self) -> None:
        """🔴 가르면 어떤 아이디가 존재하는지 알려 주는 셈이다."""
        with mock.patch.object(AccountRepository, "find_by_login_id", return_value=None), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.post("/auth/login", json={"login_id": "nobody", "password": "pw"})
        self.assertEqual(401, r.status_code)
        self.assertEqual("아이디 또는 비밀번호가 올바르지 않습니다", r.json()["detail"])

    def test_정지된_계정은_로그인하지_못한다(self) -> None:
        with mock.patch.object(AccountRepository, "find_by_login_id",
                               return_value=self._row(status="suspended")), \
             mock.patch.object(db, "run_in_db", side_effect=_read_only):
            r = self.client.post("/auth/login", json={"login_id": "bc", "password": "pw"})
        self.assertEqual(401, r.status_code)
        self.assertIn("정지", r.json()["detail"])

    def test_즐겨찾기는_열지_않았다(self) -> None:
        """만들지 않기로 한 창구다 — 주소가 있으면 화면이 기대하게 된다(계약은 주석으로만)."""
        self.assertEqual(404, self.client.get("/favorites").status_code)


if __name__ == "__main__":
    unittest.main()
