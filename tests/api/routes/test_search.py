"""포탈 FastAPI 진입점(`src/app/portal_api.py`) 단위 테스트 — DB·LLM 불필요.

전략(plan 010 D-7, G4)
    FastAPI ``TestClient`` 로 라우팅·상태코드·계약·의료배제만 검증한다. 소비 서비스 함수
    (``search_hybrid``/``fetch_asset_detail``)와 DB 실행 seam(``db.run_in_db``)을
    ``unittest.mock.patch`` 로 대체해 **DB·LLM·네트워크 없이** 순수 단위로 돈다.

검증 대상
    - T022: ``/health`` 200 · ``/search`` 정상(query+results(모달리티별)+meta) · 버킷별 의료
      배제(FR-014) · size top-N.
    - T023: ``/assets/{id}`` 200/404. (다운로드 · 썸네일 · 묶음 창구는 2026-09-28 에 지웠다.)

주의: ``TestClient(app)`` 를 ``with`` 없이 쓰면 lifespan(init_settings)이 돌지 않으므로
``.env``·DB 없이 라우팅만 검증된다(부트스트랩은 G5 실DB e2e 책임).
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app

# 경로에 쓰는 id 는 **실제와 같은 UUID** 여야 한다 — 라우트가 DB 에 묻기 전에 형식을 보고
# 아니면 404/400 으로 끊는다(2026-09-21 · `tests/api/routes/test_bad_id.py`).
A1 = "01a08fa8-0000-7000-8000-00000000a001"
NOPE = "01a08fa8-0000-7000-8000-00000000f404"
SEED = "01a08fa8-0000-7000-8000-000000005eed"
MEDSEED = "01a08fa8-0000-7000-8000-0000000d5eed"


def _passthrough_db(callback):
    """``db.run_in_db`` 대역: 가짜 conn 으로 callback 을 즉시 실행한다(DB 불필요).

    실제 DB 조회 함수들은 각 테스트에서 patch 로 대체되므로, 넘기는 conn 값은 무의미하다.
    """
    return callback(object())


def _empty_tiers(*_args, **_kwargs):
    """registry 미조회 단위 테스트 — tier 미등록 키는 projection 통과."""
    return {}


_AUTH_DISABLED_ENV = {
    "PORTAL_AUTH_DISABLED": "1",
    "PORTAL_JWT_SECRET": "test-secret",
}


def _enable_portal_test_auth_bypass(test_case: unittest.TestCase) -> None:
    """보호 라우트 단위 테스트용 dev bypass + DB/tier mock."""
    env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
    env.start()
    test_case.addCleanup(env.stop)
    db = patch("service.api.db.run_in_db", _passthrough_db)
    db.start()
    test_case.addCleanup(db.stop)


def _fake_search_result() -> dict:
    """``search_hybrid`` 가 돌려주는 모달리티 버킷 결과 대역.

    의료 행(domain_label='medical')을 image 버킷에 섞어 FR-014 버킷별 배제를 검증할 수 있게
    한다. text 는 a1>a2>a3, image 는 의료(0.95) 1건뿐이라 배제 후 빈 섹션이 된다.
    """
    return {
        "query": "회식",
        "results": {
            "text_documents": [
                {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1"},
                {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2"},
                {"id": "a3", "similarity": 0.7, "file_uri": "/x/a3.txt", "summary": "s3"},
            ],
            "image": [
                {
                    "id": "med1",
                    "similarity": 0.95,
                    "file_uri": "/x/m.png",
                    "summary": "medical",
                    "domain_label": "medical",
                },
            ],
        },
        "meta": {"fusion": "alpha"},
    }


class TestHealth(unittest.TestCase):
    """``/health`` 헬스 체크."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_returns_ok(self) -> None:
        # 설정 초기화 없이도 헬스는 200 + 환경 라벨을 돌려준다.
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["env"], os.getenv("PORTAL_API_ENV", "dev"))

    def test_health_accepts_head_without_body(self) -> None:
        """HEAD 로 묻는 헬스체커가 있다 — 405 면 멀쩡한 서버를 "죽었다"로 읽는다."""
        resp = self.client.head("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"", resp.content)

    def test_head_stays_405_elsewhere(self) -> None:
        """예외는 헬스 체크 하나뿐 — 다른 GET 창구는 선언한 메서드만 받는다(공통규약)."""
        self.assertEqual(405, self.client.head("/topics").status_code)


class TestSearch(unittest.TestCase):
    """``/search`` — 모달리티별 그룹 응답·버킷별 의료배제·size top-N."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        tiers = patch("service.portal.search.projection.fetch_access_tiers", side_effect=_empty_tiers)
        tiers.start()
        self.addCleanup(tiers.stop)
        # 057-후속/065: /search 주제 패싯(FR-503)은 결과 행의 **색인 topics**(=필터 소스)로 계산하며
        # 별도 DB 주제 seam 을 호출하지 않는다(라이브 투영 미사용). 패싯 자체 검증은 test_portal_topics.
        self.client = TestClient(app)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_returns_grouped_contract(self, mock_search) -> None:
        # 정상 검색: query + results(모달리티별 dict) + meta(counts). cursor/평탄 items 없음.
        mock_search.return_value = _fake_search_result()
        resp = self.client.get("/search", params={"q": "회식", "size": 10})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("next_cursor", body)  # cursor 제거됨
        self.assertEqual(body["query"], "회식")
        # results 는 모달리티별 dict — text 섹션 안에서만 랭킹(a1>a2>a3).
        self.assertEqual([r["asset_id"] for r in body["results"]["text"]], ["a1", "a2", "a3"])
        # 2026-07-23: 도메인 제외 전면 제거 — image 섹션의 의료 자산(med1)도 노출된다.
        self.assertEqual([r["asset_id"] for r in body["results"]["image"]], ["med1"])
        self.assertEqual(body["meta"]["counts"], {"text": 3, "image": 1})
        self.assertEqual(body["meta"]["size"], 10)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_meta_propagates_observability_when_present(self, mock_search) -> None:
        # 069 P1-4: search_hybrid meta 의 관측성 3종(os_gate·llm_verify·query_norm)을 포탈이 전파.
        r = _fake_search_result()
        r["meta"].update({
            "os_gate": {"text": {"gate_passed": True}},
            "llm_verify": {"verified": 3, "dropped": 1, "fallback": False},
            "query_norm": {"enabled": True, "method": "morph", "original": "회식 영상", "normalized": "회식"},
        })
        mock_search.return_value = r
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        self.assertEqual(body["meta"]["os_gate"], {"text": {"gate_passed": True}})
        self.assertEqual(body["meta"]["llm_verify"]["dropped"], 1)
        self.assertEqual(body["meta"]["query_norm"]["method"], "morph")

    @patch("service.api.routes.search.search_hybrid")
    def test_search_meta_observability_keys_absent_when_off(self, mock_search) -> None:
        # off 관례: search_hybrid meta 에 없으면 포탈 meta 에도 키 부재(빈 값 주입 금지).
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        for k in ("os_gate", "llm_verify", "query_norm"):
            self.assertNotIn(k, body["meta"])

    @patch("service.api.routes.search.search_hybrid")
    def test_os_connection_error_returns_503(self, mock_search) -> None:
        # 069 P1-4 권고: OS 연결 실패(인프라)는 503 — 코드버그 500 과 구분(운영 알람 분리).
        from opensearchpy.exceptions import ConnectionError as OSConnectionError

        mock_search.side_effect = OSConnectionError("N/A", "conn refused", None)
        resp = self.client.get("/search", params={"q": "회식"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("OpenSearch", resp.json()["detail"])

    @patch("service.api.routes.search.search_hybrid")
    def test_search_limit_per_bucket_param_and_default(self, mock_search) -> None:
        # 후보 풀(limit_per_bucket) 요청 파라미터화: 미지정=기본 50, 지정 시 그 값이 search_hybrid 에 전달.
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "size": 10})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 50)  # 기본값
        self.client.get("/search", params={"q": "회식", "size": 10, "limit_per_bucket": 200})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 200)  # 요청 지정

    @patch("service.api.routes.search.search_hybrid")
    def test_search_pool_floored_to_size(self, mock_search) -> None:
        # size 계약 보장: 요청 풀이 size 보다 얕으면 max(풀, size) 로 끌어올린다(풀<size 회귀 방지).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "size": 80, "limit_per_bucket": 20})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 80)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_limit_per_bucket_bounds(self, mock_search) -> None:
        # 상한(500) 초과·하한(1) 미만은 422(Query ge/le 계약).
        mock_search.return_value = _fake_search_result()
        self.assertEqual(self.client.get("/search", params={"q": "x", "limit_per_bucket": 501}).status_code, 422)
        self.assertEqual(self.client.get("/search", params={"q": "x", "limit_per_bucket": 0}).status_code, 422)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_response_rows_include_topic_pairs(self, mock_search) -> None:
        # 059 FR-104: /search 응답 행에 topic_pairs(부모>자식 짝) 포함(하위호환 필드·프론트 트리용).
        # 짝 없는 행은 [] 폴백. os_hit_to_row→_shape→_project_grouped_search 경유로 전달된다.
        mock_search.return_value = {
            "query": "먹방",
            "results": {
                "text_documents": [
                    {
                        "id": "a1",
                        "similarity": 0.9,
                        "file_uri": "/x/a1.mp4",
                        "summary": "s1",
                        "topics": ["음식·요리", "IT·기술"],
                        "subtopics": ["먹방", "데이터"],
                        "topic_pairs": ["음식·요리>먹방", "IT·기술>데이터"],
                    },
                    {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2"},
                ],
            },
            "meta": {},
        }
        body = self.client.get("/search", params={"q": "먹방", "size": 10}).json()
        rows = body["results"]["text"]
        self.assertEqual(rows[0]["topic_pairs"], ["음식·요리>먹방", "IT·기술>데이터"])
        self.assertEqual(rows[1]["topic_pairs"], [])  # 짝 없는 행 → [] 폴백(하위호환)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_includes_medical_per_bucket(self, mock_search) -> None:
        # 2026-07-23: 도메인 제외 전면 제거 — 의료 자산(med1)도 해당 버킷에 노출된다.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        all_ids = [r["asset_id"] for rows in body["results"].values() for r in rows]
        self.assertIn("med1", all_ids)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_passes_exclude_and_size_to_group(self, mock_search) -> None:
        # 배선: group_ranked 가 exclude_domains(2026-07-23 빈집합) · limit_per_modality=size 로 호출되는지.
        mock_search.return_value = _fake_search_result()
        with patch("service.api.routes.search.group_ranked", return_value={}) as mock_group:
            self.client.get("/search", params={"q": "x", "size": 7})
        self.assertEqual(
            mock_group.call_args.kwargs["exclude_domains"], frozenset()
        )
        self.assertEqual(mock_group.call_args.kwargs["limit_per_modality"], 7)

    @patch("service.api.routes.search.search_hybrid")
    def test_search_size_caps_per_modality(self, mock_search) -> None:
        # size=2 → 각 모달리티 섹션이 상위 2건으로 제한된다(섹션별 독립 top-N).
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 2}).json()
        self.assertEqual([r["asset_id"] for r in body["results"]["text"]], ["a1", "a2"])

    @patch("service.api.routes.search.search_hybrid")
    def test_search_passes_mode_and_exposes_search_plan(self, mock_search) -> None:
        mock_search.return_value = {
            **_fake_search_result(),
            "meta": {
                "search_plan": {
                    "content_query": "테스트",
                    "lexical_rescue": "restricted",
                    "generic_single_term": True,
                    "mode": "auto",
                    "suggestions": ["hint"],
                },
            },
        }
        resp = self.client.get("/search", params={"q": "테스트", "mode": "auto"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_search.call_args.kwargs["search_mode"], "auto")
        plan = resp.json()["meta"]["search_plan"]
        self.assertEqual(plan["lexical_rescue"], "restricted")
        self.assertTrue(plan["generic_single_term"])

    @patch("service.api.routes.search.search_hybrid")
    def test_search_invalid_mode_400(self, mock_search) -> None:
        resp = self.client.get("/search", params={"q": "x", "mode": "invalid"})
        self.assertEqual(resp.status_code, 400)
        mock_search.assert_not_called()

    @patch("service.api.routes.search.search_hybrid")
    def test_search_unknown_modality_400(self, mock_search) -> None:
        # 069 T301: 포탈은 미지 모달리티를 현행대로 HTTPException 400 으로 거부(공유 상수 검증·계약 보존).
        resp = self.client.get("/search", params={"q": "x", "modalities": "bogus"})
        self.assertEqual(resp.status_code, 400)
        mock_search.assert_not_called()

    @patch("service.api.routes.search.search_hybrid")
    def test_search_valid_modalities_passthrough(self, mock_search) -> None:
        # 069 T301: 유효 모달리티(공유 파서)는 그대로 search_hybrid 로 전달(valid 입력 결과 불변).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "modalities": "text,image"})
        self.assertEqual(mock_search.call_args.kwargs["modalities"], ["text", "image"])

    @patch("service.api.routes.search.search_hybrid")
    def test_search_passes_v1_filters(self, mock_search) -> None:
        mock_search.return_value = _fake_search_result()
        resp = self.client.get(
            "/search",
            params=[
                ("q", "회식"),
                ("file_ext", "txt"),
                ("file_ext", "pdf"),
                ("created_from", "2026-01-01"),
                ("created_to", "2026-06-30"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        sf = mock_search.call_args.kwargs["search_filters"]
        self.assertEqual(sf.file_exts, ("pdf", "txt"))
        meta_filters = resp.json()["meta"]["filters"]
        self.assertEqual(meta_filters["file_ext"], ["pdf", "txt"])

    @patch("service.api.routes.search.search_hybrid")
    def test_search_invalid_date_returns_422(self, mock_search) -> None:
        resp = self.client.get(
            "/search",
            params=[("q", "테스트"), ("created_from", "not-a-date")],
        )
        self.assertEqual(resp.status_code, 422)
        mock_search.assert_not_called()

    # ── 디버그 뷰(no_cutoff·compact) opt-in — 기본 off = 기존 응답 불변 ──────────────
    # no_cutoff 는 search_hybrid 배선, compact 는 group_ranked 로 의료 배제·projection 된 grouped 위 축약.
    # (2026-07-24: group 뷰·summary_chars 옵션 제거.)

    @patch("service.api.routes.search.search_hybrid")
    def test_no_cutoff_default_off_not_disabled(self, mock_search) -> None:
        # 기본(off): disable_os_cutoff=True 를 전달하지 않는다(동작 불변).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식"})
        self.assertNotEqual(mock_search.call_args.kwargs.get("disable_os_cutoff"), True)

    @patch("service.api.routes.search.search_hybrid")
    def test_no_cutoff_true_wires_disable_os_cutoff(self, mock_search) -> None:
        # no_cutoff=true → search_hybrid(disable_os_cutoff=True) 배선(027 디버그 우회).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "no_cutoff": "true"})
        self.assertIs(mock_search.call_args.kwargs["disable_os_cutoff"], True)

    @patch("service.api.routes.search.search_hybrid")
    def test_compact_default_off_returns_grouped(self, mock_search) -> None:
        # 기본(off): 기존 grouped 응답 계약 불변.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        self.assertIn("results", body)
        self.assertNotIn("결과", body)

    @patch("service.api.routes.search.search_hybrid")
    def test_compact_true_returns_flat_ranking(self, mock_search) -> None:
        # compact=true → {query, 건수, 결과} 축약 뷰(전 모달리티 합쳐 점수순). 2026-07-23: 도메인 제외 전면 제거.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "compact": "true"}).json()
        self.assertEqual(body["query"], "회식")
        self.assertIn("건수", body)
        self.assertIn("결과", body)
        # 의료(image med1·0.95)도 이제 포함 → 4건, 점수순 med1>a1>a2>a3.
        self.assertEqual(body["건수"], 4)
        rows = body["결과"]
        self.assertEqual([r["순위"] for r in rows], [1, 2, 3, 4])
        # 점수 내림차순(med1 0.95 > a1 0.9 > a2 0.8 > a3 0.7), 각 행에 모달리티·점수·파일명·요약.
        self.assertEqual([r["파일명"] for r in rows], ["m.png", "a1.txt", "a2.txt", "a3.txt"])
        self.assertEqual([r["모달리티"] for r in rows], ["image", "text", "text", "text"])
        for r in rows:
            self.assertIn("점수", r)
            self.assertIn("요약", r)
        # 의료 자산도 이제 축약 뷰에 노출된다(도메인 제외 없음).
        self.assertIn("m.png", [r["파일명"] for r in rows])


class TestAssetDetail(unittest.TestCase):
    """``/assets/{id}`` — 상세 200 / 노출 게이트 404."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        # 065: 자산상세는 노출 통과 시 topics·same_topic_groups 를 같은 트랜잭션에서 계산하며
        # fetch_asset_topic/find_same_topic_groups(자기주제 정본 seam)를 호출한다. object() conn 단위
        # 테스트에선 fetch_asset_detail 과 동일하게 이 seam 들을 스텁한다(보강 검증은 test_portal_topics).
        for name in ("fetch_asset_topic", "find_same_topic_groups"):
            p = patch(f"service.portal.repositories.asset_repo.{name}", return_value=[])
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    @patch("service.portal.repositories.asset_repo.fetch_asset_detail")
    def test_detail_returns_200(self, mock_detail) -> None:
        detail = {
            "asset_id": "a1",
            "modality": "text",
            "domain_label": "general",
            "status": "registered",
            "core_meta": {"k": "v"},
            "ext_meta": {"summary": "요약"},
            "tags": [],
            "embedding_channels": [{"channel": "st", "chunk_count": 3}],
            "relations": [],
        }
        mock_detail.return_value = detail
        resp = self.client.get(f"/assets/{A1}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["asset_id"], "a1")
        self.assertEqual(resp.json()["embedding_channels"][0]["chunk_count"], 3)

    @patch("service.portal.repositories.asset_repo.fetch_asset_detail")
    def test_detail_none_returns_404(self, mock_detail) -> None:
        # 없음/비registered/의료(FR-014) → fetch_asset_detail None → 404.
        mock_detail.return_value = None
        resp = self.client.get(f"/assets/{NOPE}")
        self.assertEqual(resp.status_code, 404)


class TestPortalAuth(unittest.TestCase):
    """042 JWT · /me · 보호 라우트 401."""

    def setUp(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        _reset_verifier_for_tests()
        self._env = patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        self._env.stop()
        _reset_verifier_for_tests()

    def test_search_without_token_returns_401(self) -> None:
        resp = self.client.get("/search", params={"q": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_auth_token_disabled_when_auth_enabled(self) -> None:
        resp = self.client.post("/auth/token", json={"username": "alice"})
        self.assertEqual(resp.status_code, 404)

    # 운영 모드의 토큰은 로그인으로만 나온다 — 주체가 계정 표에 있어야 하고, **요청마다** 상태를 본다.
    _UID = "01a0c700-0000-7000-8000-00000000a11c"

    def _me_as(self, account: dict | None):
        from service.api import db
        from service.portal.auth.dev_issuer import issue_access_token
        from service.portal.repositories.account_repo import AccountRepository

        token = issue_access_token(user_id=self._UID)
        with patch.object(AccountRepository, "find_by_user_id", return_value=account) as found, \
                patch.object(db, "run_in_db", side_effect=lambda cb: cb(None)):
            resp = self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        return resp, found

    def test_me_with_valid_token(self) -> None:
        resp, found = self._me_as({"login_id": "alice", "display_name": "앨리스",
                                   "status": "active", "role": "user"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], self._UID)
        self.assertEqual(body["clearance"], "authorized")
        self.assertEqual(("alice", "앨리스", "user"),
                         (body["login_id"], body["display_name"], body["role"]))
        found.assert_called_once_with(self._UID)

    def test_suspended_account_is_blocked_with_a_live_token(self) -> None:
        """정지하면 **이미 나간 토큰도** 바로 막힌다 — 만료(8시간)까지 기다리지 않는다."""
        resp, _ = self._me_as({"login_id": "alice", "display_name": None,
                               "status": "suspended", "role": "user"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("정지된 계정", resp.json()["detail"])

    def test_token_for_missing_account_is_rejected(self) -> None:
        """지워진 계정의 토큰 — 계정이 있었는지 알려 주지 않고 무효 토큰과 같은 401."""
        resp, _ = self._me_as(None)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual("유효하지 않은 토큰", resp.json()["detail"])

    def test_non_uuid_subject_never_reaches_db(self) -> None:
        """계정 표 키는 UUID — 형식이 아닌 주체는 묻지 않고 거절한다(물으면 DB 형식 오류)."""
        from service.api import db
        from service.portal.auth.dev_issuer import issue_access_token

        def boom(*_a: object, **_k: object) -> None:
            raise AssertionError("UUID 가 아닌 주체로 DB 를 불렀다")

        token = issue_access_token(user_id="alice")
        with patch.object(db, "run_in_db", boom):
            resp = self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)


class TestPortalAuthDevToken(unittest.TestCase):
    """042 dev /auth/token — auth disabled 일 때만."""

    def setUp(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        _reset_verifier_for_tests()
        self._env = patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        self._env.stop()
        _reset_verifier_for_tests()

    def test_auth_token_issues_jwt(self) -> None:
        token_resp = self.client.post("/auth/token", json={"username": "alice"})
        self.assertEqual(token_resp.status_code, 200)
        token = token_resp.json()["access_token"]
        me_resp = self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_resp.json()["clearance"], "authorized")


class TestPortalOpenApiSecurity(unittest.TestCase):
    """Swagger /docs — HTTPBearer Authorize 버튼(OpenAPI securitySchemes)."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_openapi_exposes_http_bearer_security(self) -> None:
        spec = self.client.get("/openapi.json").json()
        schemes = spec.get("components", {}).get("securitySchemes", {})
        self.assertIn("HTTPBearer", schemes)
        self.assertEqual(schemes["HTTPBearer"]["scheme"], "bearer")
        search = spec["paths"]["/search"]["get"]
        self.assertIn({"HTTPBearer": []}, search.get("security", []))
        params = search.get("parameters", [])
        self.assertFalse(any(p.get("name") == "authorization" for p in params))


if __name__ == "__main__":
    unittest.main()
