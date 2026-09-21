"""경로·본문의 **id 형식 오류**를 DB 에 묻기 전에 끊는다(실측 2026-09-21 결함 봉인).

무엇을 막나: ``asset_id``·``edge_id`` 컬럼은 uuid 라, 형식이 아닌 값을 그대로 넘기면 PostgreSQL 이
``invalid input syntax for type uuid`` 로 거절하고 그것이 **500** 으로 새어 나갔다. 500 은 "서버가
고장났다"는 뜻이라 클라이언트가 잘못 보낸 것과 구분되지 않고, IDD 가 그 창구들에 적어 둔 404 와도
어긋났다(실 서버 실측에서 10개 창구가 그랬다).

계약:
  · 조회 창구(IDD 가 404 를 선언한 곳)  → **404**, 없는 자산과 같은 문구(존재 여부를 흘리지 않는다)
  · 미존재를 200·빈 목록으로 답하는 창구 → **200**, 빈 목록(없는 자산과 같게)
  · 쓰기 창구(관계 검토 · 본문 id)      → **400**, 어느 값이 문제인지 함께 알린다

🔴 **DB 에 닿지 않아야 한다** — 이 테스트는 DB seam 이 호출되지 않았음까지 본다. 닿는다면 형식
검사가 조회 뒤로 밀린 것이고, 그때는 다시 500 이 난다.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db  # noqa: E402

BAD = "not-a-uuid"

# (설명, method, path, body, 기대 상태, 기대 응답 본문 일부)
CASES = [
    ("자산 상세", "GET", f"/assets/{BAD}", None, 404, "자산을 찾을 수 없거나"),
    ("관리자 자산 상세", "GET", f"/admin/assets/{BAD}", None, 404, "자산을 찾을 수 없거나"),
    ("다운로드", "GET", f"/assets/{BAD}/download", None, 404, "다운로드 대상을 찾을 수 없거나"),
    ("썸네일", "GET", f"/assets/{BAD}/thumbnail", None, 404, "썸네일 대상을 찾을 수 없거나"),
    ("묶음", "GET", f"/assets/{BAD}/bundle", None, 404, "묶음 seed 를 찾을 수 없거나"),
    ("승인", "POST", "/admin/relations/approve", {"edge_ids": [BAD]}, 400, "UUID 형식"),
    ("반려", "POST", "/admin/relations/reject", {"edge_ids": [BAD]}, 400, "UUID 형식"),
    ("정정", "POST", "/admin/relations/revise", {"edge_id": BAD, "to_status": "active"}, 400, "UUID 형식"),
]

# 미존재를 200·빈 목록으로 답하는 창구 — 형식 오류도 같게 본다.
EMPTY_CASES = [
    ("자산의 개체 소속", f"/assets/{BAD}/mm-meta", {"items": []}),
    ("자산 계보", f"/admin/assets/{BAD}/lineage", {"asset_id": BAD, "activities": []}),
]


class TestBadIdDoesNotReachDb(unittest.TestCase):
    """형식이 아닌 id 는 DB 에 닿기 전에 끊긴다."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _no_db(self):
        """DB seam 두 개를 '부르면 실패'로 막는다 — 닿으면 테스트가 깨진다."""
        def boom(*_a: object, **_k: object) -> None:
            raise AssertionError("형식 검사 전에 DB 를 호출했다")
        return (patch.object(db, "run_in_db", boom), patch.object(db, "run_in_db_write", boom))

    def test_rejected_before_db(self) -> None:
        for desc, method, path, body, want, phrase in CASES:
            with self.subTest(desc), self._no_db()[0], self._no_db()[1]:
                r = self.client.request(method, path, json=body)
                self.assertEqual(want, r.status_code, f"{desc}: {r.text}")
                self.assertIn(phrase, r.json()["detail"], desc)

    def test_empty_shape_endpoints(self) -> None:
        for desc, path, want in EMPTY_CASES:
            with self.subTest(desc), self._no_db()[0], self._no_db()[1]:
                r = self.client.get(path)
                self.assertEqual(200, r.status_code, f"{desc}: {r.text}")
                self.assertEqual(want, r.json(), desc)

    def test_valid_uuid_still_reaches_db(self) -> None:
        """형식이 맞으면 통과해 조회로 간다 — 검사가 정상 경로를 막지 않는다."""
        aid = "01a08fa8-177e-7efa-85cf-45c52619f9bf"
        with patch.object(db, "run_in_db", lambda cb: None) as _:
            self.assertEqual(404, self.client.get(f"/assets/{aid}").status_code)


if __name__ == "__main__":
    unittest.main()
