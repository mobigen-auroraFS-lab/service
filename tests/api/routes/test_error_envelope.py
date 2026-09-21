"""실패 응답은 **언제나 같은 봉투**로 나간다(실측 2026-09-21 결함 봉인).

고치기 전에는 실패 응답이 네 가지 모양이었다 — 우리 4xx 는 ``{"detail": "<문장>"}``, FastAPI 자동
검증 422 는 ``detail`` 이 **배열**, 미처리 예외는 ``text/plain`` 으로 ``Internal Server Error``.
JSON 을 기대하고 ``detail`` 을 문자열로 읽던 화면은 뒤 둘에서 그대로 깨졌다.

계약(이 테스트가 지키는 것):
  · 실패 응답은 전부 ``application/json``
  · ``detail`` 은 **항상 문자열 하나** — 화면이 그대로 띄울 수 있다
  · 칸별 검증 실패에만 ``errors: [{loc, msg, type}]`` 가 붙는다(받은 값 ``input`` 은 싣지 않는다)
  · 프레임워크가 영어로 채우던 문구도 한국어다
  · 경로·쿼리의 NUL(0x00)은 **DB 에 닿기 전에** 400 으로 끊긴다
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db  # noqa: E402

AID = "01a08fa8-177e-7efa-85cf-45c52619f9bf"


def _no_db():
    """DB seam 두 개를 '부르면 실패'로 막는다 — 닿으면 테스트가 깨진다."""
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("검사 전에 DB 를 호출했다")
    return patch.object(db, "run_in_db", boom), patch.object(db, "run_in_db_write", boom)


class TestEnvelopeShape(unittest.TestCase):
    """모든 실패 응답이 같은 모양인지 본다."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _assert_envelope(self, r, *, want_status: int, with_errors: bool) -> None:
        """봉투 규약을 한 자리에서 검사한다."""
        self.assertEqual(want_status, r.status_code, r.text)
        self.assertIn("application/json", r.headers.get("content-type", ""), r.text)
        body = r.json()
        self.assertIsInstance(body, dict, r.text)
        self.assertIsInstance(body.get("detail"), str, f"detail 이 문자열이 아니다: {r.text}")
        self.assertTrue(body["detail"].strip(), "detail 이 비었다")
        if with_errors:
            self.assertIn("errors", body, r.text)
            self.assertIsInstance(body["errors"], list)
            for item in body["errors"]:
                self.assertEqual({"loc", "msg", "type"}, set(item), "errors 항목 키가 다르다")
                self.assertIsInstance(item["loc"], list)
            self.assertEqual(set(body) - {"detail", "errors"}, set(), "봉투에 모르는 키가 있다")
        else:
            self.assertEqual({"detail"}, set(body), f"봉투에 모르는 키가 있다: {r.text}")

    def test_our_4xx_is_detail_string(self) -> None:
        """라우트가 던진 4xx — detail 문자열 하나뿐이다."""
        cases = [
            (404, self.client.get("/assets/not-a-uuid")),
            (400, self.client.post("/admin/relations/approve", json={"edge_ids": []})),
            (400, self.client.get("/search?q=x&mode=nope")),
            (422, self.client.get("/admin/access-logs?from=notadate")),
        ]
        for want, r in cases:
            with self.subTest(want):
                self._assert_envelope(r, want_status=want, with_errors=False)

    def test_framework_validation_carries_errors(self) -> None:
        """자동 검증 422 — detail 은 한 문장, 칸 정보는 errors 로 내려온다."""
        r = self.client.get("/search?q=x&size=0")
        self._assert_envelope(r, want_status=422, with_errors=True)
        self.assertEqual(["query", "size"], r.json()["errors"][0]["loc"])
        self.assertIn("1 이상", r.json()["errors"][0]["msg"])
        self.assertIn("query.size", r.json()["detail"])

    def test_validation_does_not_echo_input(self) -> None:
        """🔴 받은 값을 되돌려 보내지 않는다 — 큰 본문·남의 값이 반사되면 안 된다."""
        needle = "x" * 400
        r = self.client.get(f"/search?q=y&size={needle}")
        self.assertEqual(422, r.status_code)
        self.assertNotIn(needle, r.text)

    def test_multiple_field_errors_are_counted(self) -> None:
        """여러 칸이 틀리면 첫 건을 문장으로, 나머지는 건수로 알린다."""
        r = self.client.get("/search?q=x&size=0&limit_per_bucket=0")
        self._assert_envelope(r, want_status=422, with_errors=True)
        self.assertEqual(2, len(r.json()["errors"]))
        self.assertIn("그 밖 1건", r.json()["detail"])

    def test_broken_json_body(self) -> None:
        """깨진 본문도 같은 봉투로, 한국어로 답한다."""
        r = self.client.post("/admin/relations/approve", content=b"{",
                             headers={"content-type": "application/json"})
        self._assert_envelope(r, want_status=422, with_errors=True)
        self.assertIn("JSON", r.json()["detail"])

    def test_framework_phrases_are_korean(self) -> None:
        """없는 경로·안 받는 메서드 — 영어 기본 문구가 새지 않는다."""
        miss = self.client.get("/no-such-route")
        self._assert_envelope(miss, want_status=404, with_errors=False)
        self.assertNotIn("Not Found", miss.json()["detail"])

        wrong = self.client.post("/health")
        self._assert_envelope(wrong, want_status=405, with_errors=False)
        self.assertNotIn("Method Not Allowed", wrong.json()["detail"])
        self.assertIn("allow", {k.lower() for k in wrong.headers}, "Allow 헤더를 잃었다")

    def test_unhandled_exception_is_json(self) -> None:
        """미처리 예외 — text/plain 이 아니라 JSON 봉투이고, 내부 정보를 싣지 않는다."""
        secret = "비밀-내부-사정-1234"

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError(secret)

        client = TestClient(app, raise_server_exceptions=False)
        with patch.object(db, "run_in_db", boom):
            r = client.get(f"/assets/{AID}")
        self._assert_envelope(r, want_status=500, with_errors=False)
        self.assertNotIn(secret, r.text, "예외 내용이 응답으로 샜다")
        self.assertNotIn("Traceback", r.text)


class TestNulBytesRejected(unittest.TestCase):
    """NUL(0x00)은 DB 에 닿기 전에 끊는다 — PostgreSQL text 가 받지 않는 문자다."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    # (설명, 경로) — 고치기 전 500 이 나던 곳들에서 하나씩 골랐다.
    CASES = [
        ("경로(개체 상세)", "/mm-meta/person/a%00b"),
        ("경로(주제)", "/topics/a%00b"),
        ("경로(모달리티 통계)", "/admin/assets/modality/a%00b"),
        ("쿼리(접근 이력 user)", "/admin/access-logs?user=a%00b"),
        ("쿼리(관계 검색 q)", "/admin/relations?q=a%00b"),
        ("쿼리(자산 목록 domain)", "/admin/assets?domain=a%00b"),
        ("쿼리(개체 갈래)", "/mm-meta?areas=a%00b"),
    ]

    def test_rejected_before_db(self) -> None:
        for desc, path in self.CASES:
            with self.subTest(desc), _no_db()[0], _no_db()[1]:
                r = self.client.get(path)
                self.assertEqual(400, r.status_code, f"{desc}: {r.text}")
                self.assertIn("NUL", r.json()["detail"], desc)
                self.assertEqual({"detail"}, set(r.json()), desc)

    def test_literal_percent_zero_zero_is_not_blocked(self) -> None:
        """글자 그대로의 ``%00``(전송 시 ``%2500``)은 NUL 이 아니다 — 막으면 과잉 차단이다."""
        self.assertTrue(self._reached_route("/topics/abc", params={"subtopic": "a%00b"}))

    def test_normal_request_passes(self) -> None:
        """검사가 정상 요청을 막지 않는다(한글·퍼센트 인코딩 포함)."""
        self.assertTrue(self._reached_route("/topics/%EA%B5%90%ED%86%B5"))

    def _reached_route(self, path: str, **kw: object) -> bool:
        """차단되지 않고 라우트까지 갔는지 본다 — DB seam 이 불렸으면 통과한 것이다."""
        called: list[bool] = []

        def mark(*_a: object, **_k: object) -> None:
            called.append(True)
            raise RuntimeError("여기까지 왔으면 통과한 것이다")

        client = TestClient(app, raise_server_exceptions=False)
        with patch.object(db, "run_in_db", mark):
            client.get(path, **kw)
        return bool(called)


if __name__ == "__main__":
    unittest.main()
