"""화면이 요구하던 창구들 — 목록(관계 종류·태그) · 추천 · 추가 칩 · 계정.

여기서 지키는 것
  · 오류 코드가 기존 창구와 **같은 말을 쓴다**.
  · 미구현 창구는 **조용한 성공을 주지 않는다**(501) — 화면이 "저장됐다"고 믿으면 안 된다.
  · 원문 · 고른 자산 묶음 창구는 2026-09-28 에 지웠다(파일 제공은 협의 후 재설계 · `TODO.md`).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db  # noqa: E402
from service.portal.auth import authenticate_token  # noqa: E402
from service.portal.auth.passwords import hash_password  # noqa: E402
from service.portal.repositories import admin_repo  # noqa: E402
from service.portal.repositories.account_repo import AccountRepository  # noqa: E402

A1 = "01a08fa8-0000-7000-8000-00000000a001"
A2 = "01a08fa8-0000-7000-8000-00000000a002"


def _read_only(cb):
    """DB seam 대역 — 커넥션 대신 ``None`` 을 넘긴다(저장소 메서드는 대역으로 갈아끼운다)."""
    return cb(None)


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

    def test_태그_목록은_검색_필터와_같은_열쇠로_묶는다(self) -> None:
        """`역사기록`·`역사 기록` 은 검색에서 한 태그다 — 목록도 한 줄이어야 적힌 숫자 = 누르면 나오는 수(2026-09-28)."""
        from service.portal.repositories.catalog_repo import CatalogRepository

        seen: list[str] = []
        repo = CatalogRepository.__new__(CatalogRepository)
        with mock.patch.object(CatalogRepository, "rows",
                               side_effect=lambda sql, params: seen.append(sql) or []):
            repo.tags(topics=["역사·문화유산"], subtopics=[], q="역사", limit=5)
        sql = " ".join(seen[0].split())
        self.assertIn("normalize(k.kw, NFKC)", sql)          # 코어 normalize_text_key 와 같은 순서: NFKC →
        self.assertIn("regexp_replace(", sql)                  #   공백 제거 →
        self.assertIn("lower(", sql)                           #   소문자
        self.assertIn("GROUP BY key", sql)                     # 원문(kw)이 아니라 열쇠로 묶는다
        self.assertIn("COUNT(DISTINCT asset_id)", sql)         # 두 표기를 다 단 자산도 한 번
        self.assertIn('GROUP BY key COLLATE "C"', sql)          # 한글 정렬 비용(2026-10-01) — 열쇠에만
        self.assertIn("mode() WITHIN GROUP (ORDER BY kw)", sql)  # 대표 표기 동점 규칙은 기본 콜레이션 그대로
        self.assertIn("JOIN asset_topic t", sql)               # 주제로 좁히는 길은 그대로
        self.assertIn("k.kw ILIKE %s", sql)

    def test_추천은_출처를_합쳐_건수순으로_자른다(self) -> None:
        """주제가 상한만큼 걸려도 건수가 더 많은 태그가 빠지면 안 된다(2026-09-23 결함 수정)."""
        from service.portal.repositories.catalog_repo import CatalogRepository

        repo = CatalogRepository.__new__(CatalogRepository)
        topics = [{"value": "한국사", "kind": "topic", "count": 5},
                  {"value": "한식", "kind": "subtopic", "count": 2}]
        with mock.patch.object(CatalogRepository, "rows", return_value=topics), \
             mock.patch.object(CatalogRepository, "tags",
                               return_value=[{"tag": "한옥", "count": 9}, {"tag": "한복", "count": 2}]):
            out = repo.suggest(q="한", limit=2)
        self.assertEqual([("한옥", "tag", 9), ("한국사", "topic", 5)],
                         [(r["value"], r["kind"], r["count"]) for r in out])

    def test_추천은_같은_건수면_값_순이다(self) -> None:
        from service.portal.repositories.catalog_repo import CatalogRepository

        repo = CatalogRepository.__new__(CatalogRepository)
        with mock.patch.object(CatalogRepository, "rows",
                               return_value=[{"value": "한식", "kind": "subtopic", "count": 2}]), \
             mock.patch.object(CatalogRepository, "tags", return_value=[{"tag": "한복", "count": 2}]):
            out = repo.suggest(q="한", limit=10)
        self.assertEqual(["한복", "한식"], [r["value"] for r in out])

    def test_칩의_모르는_종류도_422(self) -> None:
        """목록(/file-search)과 같은 닫힌 어휘 — 칩만 조용히 0건이면 두 숫자가 갈린다(2026-09-23)."""
        r = self.client.get("/file-search/facet-extra?modality=문서")
        self.assertEqual(422, r.status_code)
        self.assertIn("알 수 없는 종류", r.json()["detail"])

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
