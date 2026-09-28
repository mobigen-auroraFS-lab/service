"""IDD 계약 ↔ 실제 API 대조(단일 책임: 판정).

계약을 읽는 일은 ``tests.conformance.contract``, DB 를 모의하는 일은 ``…simulator``,
어떤 요청을 보낼지는 ``…probes`` 가 맡는다. 여기서는 **어긋났는지만** 판정한다.

이 테스트가 지키는 것 — 문서와 코드가 따로 노는 것을 CI 가 막는다:
  ① 요청 파라미터  IDD 가 선언한 Query·Path 가 실제 라우트에 있는가
  ② 인증          보호 라우트가 토큰 없이 401 인가
  ③ 응답 본문      200 응답의 최상위 키가 IDD 선언과 같은가(DB 모의기 위에서 실제 코드 실행)
  ④ 예외          IDD 가 선언한 에러 코드를 실제로 낼 수 있는가

⚠️ **미구현 인터페이스는 대조 대상이 아니다** — 계약만 있고 라우트가 없으면 조용히 건너뛴다.
   구현되면 자동으로 대조 범위에 들어온다.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, db, errors  # noqa: E402
from service.api.routes import file_search as routes_file_search  # noqa: E402
from service.api.routes import search as routes_search  # noqa: E402
from tests.conformance import contract, probes  # noqa: E402
from tests.conformance.simulator import FakeConn  # noqa: E402

CONTRACT = contract.load()
PATHS = app.openapi()["paths"]
# 검색은 OpenSearch·임베딩을 타므로 대역을 세운다 — 여기서 볼 것은 응답 **형식**이지 검색 품질이
# 아니다. ⚠️ 설정을 초기화해 대신하지 말 것 — ``init_settings`` 는 **프로세스 전역**을 고정해
# 뒤에 도는 다른 테스트의 튜닝 기본값을 바꾼다(실제로 깨뜨렸다).
# ⚠️ 빈 결과를 주면 중첩(행 키) 검사가 **무력해진다** — 행이 하나도 없으면 볼 것이 없다.
#    그래서 원시 OS 히트를 하나 넣어 진짜 조립 경로(group_ranked→_shape)를 태운다.
SEARCH_STUB = {
    "results": {"text_documents": [{
        "id": probes.ASSET_ID, "similarity": 0.82, "summary": "김치 담그는 법",
        "file_uri": f"/archive/{probes.ASSET_ID}__kimchi.txt", "domain_label": "general",
        "topics": ["요리"], "subtopics": ["한식"], "topic_pairs": ["요리>한식"],
    }]},
    "meta": {},
}
FILE_SEARCH_STUB = {
    "rows": [], "total": 0, "scope_total": 0, "total_capped": False, "facets": {},
    "from": 0, "size": 50, "sort": "created_desc", "next_cursor": None,
}


def _external_patches(*, mm_meta_stubs: bool = True):
    """조회 경로가 타는 **외부 자원 대역** — 임베딩·검색 엔진·전역 설정을 부르지 않게 한다.

    설정은 전역 초기화 대신 대역으로 준다(``probes.fake_settings`` 주석 참조).

    Args:
        mm_meta_stubs: 개체 카드·묶음 조회를 대역으로 덮을지. 응답 **모양**을 볼 때는 참이어야
            200 이 나지만, **오류** 계약을 볼 때는 거짓이어야 한다 — 참이면 조회가 언제나 성공해
            404·409·413 을 만들 길이 막힌다(2026-09-21: 그래서 세 창구의 오류가 선언조차 없었다).
    """
    from service.portal import mm_meta
    from src.search import opensearch_sync

    mm_stubs = (
        # 개체 카드·묶음 — 실제 행이 있어야 200 이 난다. 없으면 404 로 빠져 **대조에서 조용히
        # 사라진다**(실제로 세 엔드포인트가 그렇게 빠져 있었다).
        patch.object(mm_meta, "fetch_card", lambda *a, **k: {
            "entity_type": "person", "entity_uid": "u1", "name": "이순신",
            "source": "auto", "total": 1, "modalities": [],
        }),
    ) if mm_meta_stubs else ()
    return (
        *mm_stubs,
        # ⚠️ ``get_client`` 는 핸들러 **안에서** import 된다 — 라우트 모듈이 아니라 원본을 패치해야
        #    잡힌다(모듈 경유 참조가 아니라 호출 시점 import 라서).
        patch.object(opensearch_sync, "get_client", lambda *a, **k: object()),
        # 채널 조회는 **코어의** 설정을 읽어 설정 대역(서비스 모듈에만 걸린다)을 비껴간다 — 따로 막지
        #   않으면 검색어가 있는 요청이 엔진에 닿기 전에 "임베딩 실패 503" 으로 빠진다.
        patch.object(routes_file_search, "active_embed_channel", lambda: "text"),
        patch.object(routes_file_search, "embed_query_for_media_search", lambda *a, **k: [0.0] * 8),
        patch.object(routes_file_search, "search_files", lambda *a, **k: dict(FILE_SEARCH_STUB)),
        # 추가 칩(IF-ASSET-14)은 검색 엔진에 직접 집계를 던진다 — 대역 클라이언트(object())로는
        #   못 가므로 계산 자체를 대역으로 둔다(응답 **모양**만 보는 자리다).
        patch.object(routes_file_search, "extra_facets", lambda *a, **k: {
            "axes": {"file_ext": [{"key": "txt", "count": 1}]}, "total": 1,
            "as_of": "2026-09-21T00:00:00+00:00"}),
        patch.object(routes_file_search, "browse_files", lambda *a, **k: dict(FILE_SEARCH_STUB)),
        # 설정 대역은 **들여온 모든 모듈**에 건다 — 소비처를 하나씩 쫓지 않는다.
        *probes.settings_patches(),
    )


def implemented() -> list[tuple[str, dict]]:
    """IDD 계약 중 **앱에 실제 등록된** 것만 추린다."""
    return [(api, s) for api, s in sorted(CONTRACT.items())
            if s["url"] in PATHS and s["method"].lower() in PATHS[s["url"]]]


class TestContractFreshness(unittest.TestCase):
    """굳힌 계약(JSON)이 엑셀과 같은지 — 엑셀이 있는 환경에서만."""

    def test_frozen_matches_xlsx(self) -> None:
        if not contract.DEFAULT_XLSX.exists():
            self.skipTest(f"IDD.xlsx 없음({contract.DEFAULT_XLSX}) — 굳힌 계약으로만 대조한다")
        live = contract.contract_from_xlsx(contract.DEFAULT_XLSX)["interfaces"]
        self.assertEqual(
            live, CONTRACT,
            "IDD.xlsx 가 굳힌 계약과 다릅니다 — `python scripts/regen_idd_contract.py` 후 함께 커밋하십시오")


class TestRequestParams(unittest.TestCase):
    """① IDD 가 선언한 요청 파라미터가 실제 라우트에 있는가."""

    def test_declared_params_exist(self) -> None:
        missing: list[str] = []
        for api, spec in implemented():
            declared = contract.declared_params(spec)
            actual = {p["name"] for p in
                      PATHS[spec["url"]][spec["method"].lower()].get("parameters", [])}
            gap = sorted(n for n in declared - actual
                         if (api, n) not in probes.CROSS_REPO_GAPS)
            if gap:
                missing.append(f"{api} {spec['method']} {spec['url']}: {gap}")
        self.assertEqual([], missing, "IDD 가 선언했으나 코드에 없는 파라미터:\n  " + "\n  ".join(missing))

    def test_cross_repo_gap_list_is_not_stale(self) -> None:
        """코어가 지원하기 시작했는데 목록에 남아 있으면 실패시킨다.

        면제 목록은 **낡는 순간 거짓말**이 된다 — 여기서 막지 않으면 이미 되는 것을 안 된다고
        적어 둔 채로 굳는다.
        """
        stale = []
        for (api, param), reason in probes.CROSS_REPO_GAPS.items():
            spec = CONTRACT.get(api)
            if not spec or spec["url"] not in PATHS:
                continue
            actual = {p["name"] for p in
                      PATHS[spec["url"]][spec["method"].lower()].get("parameters", [])}
            if param in actual:
                stale.append(f"{api}.{param} — 이제 구현돼 있다. CROSS_REPO_GAPS 에서 빼라 ({reason})")
        self.assertEqual([], stale, "면제 목록이 낡았다:\n  " + "\n  ".join(stale))


def _schema_types(schema: dict) -> set[str]:
    """OpenAPI 파라미터 스키마에서 **자료형 후보**를 모은다.

    선택 파라미터는 ``anyOf: [{type: X}, {type: "null"}]`` 로 표현되므로 한 겹 풀어서 본다.
    """
    if not isinstance(schema, dict):
        return set()
    if "anyOf" in schema:
        out: set[str] = set()
        for sub in schema["anyOf"]:
            out |= _schema_types(sub)
        return out - {"null"}
    t = schema.get("type")
    return {t} if isinstance(t, str) else set()


class TestRequestTypes(unittest.TestCase):
    """①-b 요청 파라미터의 **자료형·필수 여부**가 IDD 선언과 같은가.

    이름만 맞고 타입이 어긋나면 화면은 "있는데 못 쓰는" 파라미터를 받는다 — 이름 대조만으로는
    안 잡힌다.
    """

    def test_param_types_match(self) -> None:
        wrong: list[str] = []
        for api, spec in implemented():
            params = {p["name"]: p for p in
                      PATHS[spec["url"]][spec["method"].lower()].get("parameters", [])}
            for name, meta in spec["req_meta"].items():
                if meta["loc"] == "Body" or name not in params:
                    continue          # Body 는 pydantic 모델이라 별도 · 없는 이름은 ① 이 잡는다
                want = contract.openapi_type(meta["type"])
                if want is None:
                    continue          # 모르는 표기는 대조 제외
                got = _schema_types(params[name].get("schema", {}))
                if got and want not in got:
                    wrong.append(f"{api} {spec['url']} ?{name}: IDD={meta['type']}→{want} 실제={sorted(got)}")
        self.assertEqual([], wrong, "요청 파라미터 자료형 불일치:\n  " + "\n  ".join(wrong))

    def test_param_required_matches(self) -> None:
        wrong: list[str] = []
        for api, spec in implemented():
            params = {p["name"]: p for p in
                      PATHS[spec["url"]][spec["method"].lower()].get("parameters", [])}
            for name, meta in spec["req_meta"].items():
                if meta["loc"] == "Body" or name not in params:
                    continue
                got = bool(params[name].get("required", False))
                if got != meta["required"]:
                    wrong.append(f"{api} {spec['url']} ?{name}: IDD 필수={meta['required']} 실제={got}")
        self.assertEqual([], wrong, "요청 파라미터 필수 여부 불일치:\n  " + "\n  ".join(wrong))


def _schema_leaf(schema: dict) -> dict:
    """선택 파라미터의 ``anyOf`` 를 한 겹 풀어 **실제 제약이 있는 가지**를 돌려준다."""
    if isinstance(schema, dict) and "anyOf" in schema:
        for sub in schema["anyOf"]:
            if isinstance(sub, dict) and sub.get("type") != "null":
                return sub
    return schema if isinstance(schema, dict) else {}


class TestRequestBounds(unittest.TestCase):
    """①-c 요청 파라미터의 **허용 범위·기본값**이 IDD 선언과 같은가.

    ``1~200 · 기본 50`` 같은 표기는 화면이 그대로 믿고 입력 위젯을 만든다 — 어긋나면 화면이
    허용하는 값을 서버가 422 로 막는다.
    """

    def test_bounds_and_defaults_match(self) -> None:
        wrong: list[str] = []
        for api, spec in implemented():
            params = {p["name"]: p for p in
                      PATHS[spec["url"]][spec["method"].lower()].get("parameters", [])}
            for name, meta in spec["req_meta"].items():
                if name not in params:
                    continue
                sc = _schema_leaf(params[name].get("schema", {}))
                # 스키마에 **제약이 있을 때만** 대조한다 — 없다고 위반은 아니다. 핸들러가 직접
                # 검사해 400 을 내는 축(confidence 0~1 등)이 있고, 그건 에러 계약 검사가 덮는다.
                if meta["min"] is not None and sc.get("minimum") is not None \
                        and sc["minimum"] != meta["min"]:
                    wrong.append(f"{api} ?{name} 하한: IDD={meta['min']} 실제={sc['minimum']}")
                if meta["max"] is not None and sc.get("maximum") is not None \
                        and sc["maximum"] != meta["max"]:
                    wrong.append(f"{api} ?{name} 상한: IDD={meta['max']} 실제={sc['maximum']}")
                want = meta["default"]
                if want is None:
                    continue
                got = params[name].get("schema", {}).get("default", sc.get("default"))
                if got is None:
                    continue                      # 기본값을 스키마에 안 싣는 형태는 대조 제외
                # 산문 기본값(``전체`` 등)은 비교하지 않는다 — 숫자·참거짓·단어만 본다.
                if want.lower() in ("true", "false"):
                    ok = isinstance(got, bool) and str(got).lower() == want.lower()
                elif want.isdigit():
                    ok = str(got) == want
                else:
                    ok = str(got) == want or not str(want).isascii()
                if not ok:
                    wrong.append(f"{api} ?{name} 기본값: IDD={want} 실제={got!r}")
        self.assertEqual([], wrong, "요청 범위·기본값 불일치:\n  " + "\n  ".join(wrong))


class TestRequestBody(unittest.TestCase):
    """①-d **요청 본문(Body)** 필드가 IDD 선언과 같은가 — 이름·자료형·필수."""

    def test_body_fields_match(self) -> None:
        schemas = app.openapi().get("components", {}).get("schemas", {})
        wrong: list[str] = []
        checked = 0
        for api, spec in implemented():
            declared = {n: m for n, m in spec["req_meta"].items() if m["loc"] == "Body"}
            if not declared:
                continue
            op = PATHS[spec["url"]][spec["method"].lower()]
            ref = (op.get("requestBody", {}).get("content", {})
                     .get("application/json", {}).get("schema", {}).get("$ref", ""))
            model = schemas.get(ref.rsplit("/", 1)[-1], {}) if ref else {}
            props, required = model.get("properties", {}), set(model.get("required", []))
            if not props:
                continue
            checked += 1
            for name, meta in declared.items():
                if name not in props:
                    wrong.append(f"{api} body.{name}: IDD 에만 있고 모델에 없음")
                    continue
                want = contract.openapi_type(meta["type"])
                got = _schema_types(props[name])
                if want and got and want not in got:
                    wrong.append(f"{api} body.{name} 자료형: IDD={meta['type']}→{want} 실제={sorted(got)}")
                if meta["required"] != (name in required):
                    wrong.append(f"{api} body.{name} 필수: IDD={meta['required']} 실제={name in required}")
            for extra in sorted(set(props) - set(declared)):
                wrong.append(f"{api} body.{extra}: 모델에만 있고 IDD 에 없음")
        self.assertEqual([], wrong, "요청 본문 계약 위반:\n  " + "\n  ".join(wrong))
        self.assertGreater(checked, 0, "Body 대조가 한 건도 실행되지 않았다")


class TestAuthContract(unittest.TestCase):
    """② 인증 계약 — 보호 라우트는 토큰 없이 401."""

    # 운영 모드에서 dev 발급기는 404. 계정 창구는 **로그인 전에** 부르므로 인증을 걸지 않는다 —
    # 지금은 저장소가 없어 501(가입·로그인)·422(필수 파라미터 없음)로 답한다.
    # 계정 창구는 **로그인 전에** 부르므로 인증을 걸지 않는다. 이 검사는 빈 요청을 보내므로
    # 본문·파라미터 검증에서 먼저 422 가 난다 — 요점은 **401 이 아니라는 것**이다.
    OPEN = {"/health": 200, "/auth/token": 404,
            "/auth/signup": 422, "/auth/login": 422, "/auth/login-id/availability": 422}

    def test_protected_routes_401(self) -> None:
        env = {"PORTAL_AUTH_DISABLED": "0",
               "PORTAL_JWT_SECRET": "conformance-secret-key-at-least-32-bytes"}
        from service.portal.auth import verifier
        with patch.dict(os.environ, env, clear=False):
            verifier._reset_verifier_for_tests()
            client = TestClient(app)
            wrong = []
            for api, spec in implemented():
                want = self.OPEN.get(spec["url"], 401)
                got = client.request(spec["method"], probes.fill_path(spec["url"]),
                                     json={} if spec["method"] == "POST" else None).status_code
                if got != want:
                    wrong.append(f"{api} {spec['url']}: 기대 {want} · 실제 {got}")
            verifier._reset_verifier_for_tests()
        self.assertEqual([], wrong, "인증 계약 위반:\n  " + "\n  ".join(wrong))

    def _enter_lifespan(self, env: dict[str, str]) -> None:
        """기동 절차만 돌린다 — 설정 적재(``bootstrap_env``)는 대역으로 막는다.

        ⚠️ 진짜 ``bootstrap_env`` 를 돌리면 **전역 설정을 고정**해, 뒤에 도는 다른 테스트의 튜닝
        기본값이 바뀐다(실제로 깨졌다). 여기서 볼 것은 "인증 설정을 기동 때 확인하느냐"뿐이다.
        """
        import asyncio

        from service import bootstrap
        from service.api import lifespan

        async def _run() -> None:
            async with lifespan.lifespan(app):
                pass

        with patch.dict(os.environ, env, clear=False), \
                patch.object(bootstrap, "bootstrap_env", lambda *_a, **_k: None), \
                patch.object(db, "align_thread_limit_to_pool", lambda: None), \
                patch.object(db, "close_db", lambda: None):
            if "PORTAL_JWT_SECRET" not in env:
                os.environ.pop("PORTAL_JWT_SECRET", None)
            asyncio.run(_run())

    def test_missing_jwt_secret_fails_startup(self) -> None:
        """IDD IF-AUTH-03 · 공통규약 — 운영 모드에 서명 키가 없으면 **서버가 뜨지 않는다**(fail-fast).

        종전에는 기동이 성공하고 헬스 체크도 200 인 채 첫 인증 요청부터 500 이었다 — 배포가 성공한
        것처럼 보이다가 사용자 요청에서 터졌다(2026-09-22 기동 시 확인으로 바꿨다).
        """
        with self.assertRaises(ValueError):
            self._enter_lifespan({"PORTAL_AUTH_DISABLED": "0"})

    def test_startup_passes_with_secret_or_in_dev(self) -> None:
        """키가 있거나 개발 모드면 기동 확인을 통과한다(검사가 지나치게 막지 않는다)."""
        self._enter_lifespan({"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "k" * 32})
        self._enter_lifespan({"PORTAL_AUTH_DISABLED": "1"})


class TestResponseBody(unittest.TestCase):
    """③ 200 응답의 최상위 키가 IDD 선언과 같은가."""

    def test_response_keys_match(self) -> None:
        client = TestClient(app)
        problems: list[str] = []
        with contextlib.ExitStack() as stack:
            for pt in (*_external_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(db, "run_in_db_write",
                                    lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(routes_search, "search_hybrid", lambda *a, **k: SEARCH_STUB)):
                stack.enter_context(pt)
            for api, spec in implemented():
                r = client.request(spec["method"], probes.fill_path(spec["url"]),
                                   params=probes.HAPPY_QUERY.get(spec["url"]),
                                   json=probes.HAPPY_BODY.get(spec["url"]))
                if r.status_code != 200:
                    continue                                   # 픽스처로 200 을 못 만드는 경로
                if "application/json" not in r.headers.get("content-type", ""):
                    continue                                   # 바이너리 응답은 키 대조 대상 아님
                body = r.json()
                if not isinstance(body, dict):
                    continue
                got = set(body)
                alts = spec["resp_alt"].get("200")
                if alts:                                       # 단일/멀티처럼 형상이 갈리는 응답
                    if not any(set(a) == got for a in alts):
                        problems.append(f"{api} {spec['url']}: 택일 형상 불일치 "
                                        f"응답={sorted(got)} 후보={alts}")
                    continue
                need, opt = contract.declared_resp(spec)
                if not need:
                    continue                                   # IDD 응답 칸이 필드명이 아님
                pending = {f for (a, f) in probes.IDD_PENDING_RESP if a == api}
                gap, extra = sorted(need - got), sorted(got - need - opt - pending)
                if gap or extra:
                    problems.append(f"{api} {spec['url']}: 빠짐={gap} 여분={extra}")
                # 값의 **자료형**까지 본다 — 키만 맞고 타입이 다르면 화면이 그대로 깨진다.
                for field, idd_type in spec["resp_meta"].get("200", {}).items():
                    want = contract.openapi_type(idd_type)
                    if want is None or field not in body:
                        continue
                    actual = contract.value_type(body[field])
                    if actual == "null":
                        continue      # 값 없음은 타입 위반이 아니다(비어 있을 수 있는 필드)
                    if actual != want:
                        problems.append(
                            f"{api} {spec['url']}.{field}: IDD={idd_type}→{want} 실제={actual}")
        self.assertEqual([], problems, "응답 형식 위반:\n  " + "\n  ".join(problems))

    def test_idd_pending_list_is_not_stale(self) -> None:
        """IDD 에 올라간 필드가 대기 목록에 남아 있으면 실패시킨다(면제가 낡아 거짓말이 되지 않게)."""
        stale = []
        for (api, field), reason in probes.IDD_PENDING_RESP.items():
            spec = CONTRACT.get(api)
            if spec is None:
                stale.append(f"{api} — IDD 에 없는 인터페이스다({reason})")
                continue
            need, opt = contract.declared_resp(spec)
            if field in need | opt:
                stale.append(f"{api}.{field} — 이제 IDD 에 있다. IDD_PENDING_RESP 에서 빼라 ({reason})")
        self.assertEqual([], stale, "IDD 반영 대기 목록이 낡았다:\n  " + "\n  ".join(stale))


class TestResponseKind(unittest.TestCase):
    """③-b 필드명이 없는 응답의 **타입 계약** — 미디어 타입·최상위 자료형.

    IDD 는 ``(zip 스트림) | application/zip`` 처럼 필드명 대신 타입만 적는 응답이 있다. 필드명이
    없다고 건너뛰면 그 엔드포인트가 대조에서 통째로 빠진다(실제로 5개가 빠져 있었다).
    """

    def test_declared_response_kind(self) -> None:
        client = TestClient(app)
        problems: list[str] = []
        with contextlib.ExitStack() as stack:
            for pt in (*_external_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(db, "run_in_db_write",
                                    lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(routes_search, "search_hybrid", lambda *a, **k: SEARCH_STUB)):
                stack.enter_context(pt)
            for api, spec in implemented():
                kind = spec["resp_kind"].get("200")
                if not kind:
                    continue
                r = client.request(spec["method"], probes.fill_path(spec["url"]),
                                   params=probes.HAPPY_QUERY.get(spec["url"]),
                                   json=probes.HAPPY_BODY.get(spec["url"]))
                if r.status_code != 200:
                    problems.append(f"{api} {spec['url']}: 200 을 못 만듦({r.status_code}) — 픽스처 필요")
                    continue
                ctype = r.headers.get("content-type", "").split(";")[0].strip()
                if "/" in kind:                      # application/zip · image/jpeg 같은 미디어 타입
                    if ctype != kind:
                        problems.append(f"{api} {spec['url']}: IDD={kind} 실제={ctype}")
                    continue
                want = contract.openapi_type(kind)   # object · array
                if want is None:
                    continue
                actual = contract.value_type(r.json())
                if actual != want:
                    problems.append(f"{api} {spec['url']}: IDD={kind}→{want} 실제={actual}")
        self.assertEqual([], problems, "응답 타입 계약 위반:\n  " + "\n  ".join(problems))


def _dig(body: object, path: str) -> list:
    """``relations.edges`` 같은 경로를 따라가 **대조할 dict 들**을 모은다.

    중간에 배열을 만나면 원소마다 내려간다. 값이 없으면 빈 목록(검사 대상 없음).
    """
    cur: list = [body]
    for part in path.split("."):
        nxt: list = []
        for node in cur:
            if isinstance(node, dict) and part in node:
                nxt.append(node[part])
        cur = []
        for v in nxt:
            cur.extend(v) if isinstance(v, list) else cur.append(v)
        # 모달리티 버킷처럼 dict-of-list 면 한 겹 더 편다
        flat: list = []
        for v in cur:
            if isinstance(v, dict) and v and all(isinstance(x, list) for x in v.values()):
                for lst in v.values():
                    flat.extend(lst)
            else:
                flat.append(v)
        cur = flat
    return [c for c in cur if isinstance(c, dict)]


class TestNestedShape(unittest.TestCase):
    """③-c **중첩 구조** — 행 하나하나의 키가 IDD 선언과 같은가.

    최상위만 보면 ``results`` 가 있다는 것만 알 뿐, 그 안 행의 모양이 바뀌어도 안 잡힌다.
    화면이 그리는 것은 바로 그 행이다.
    """

    def test_nested_keys_match(self) -> None:
        client = TestClient(app)
        problems: list[str] = []
        checked = 0
        with contextlib.ExitStack() as stack:
            for pt in (*_external_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(db, "run_in_db_write",
                                    lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(routes_search, "search_hybrid", lambda *a, **k: SEARCH_STUB)):
                stack.enter_context(pt)
            for api, spec in implemented():
                rules = spec["resp_nested"].get("200") or []
                if not rules:
                    continue
                r = client.request(spec["method"], probes.fill_path(spec["url"]),
                                   params=probes.HAPPY_QUERY.get(spec["url"]),
                                   json=probes.HAPPY_BODY.get(spec["url"]))
                if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
                    continue
                body = r.json()
                for rule in rules:
                    nodes = _dig(body, rule["field"])
                    if not nodes:
                        continue          # 그 경로에 값이 없다 — 볼 것이 없음
                    want = set(rule["keys"])
                    for node in nodes[:5]:
                        checked += 1
                        got = set(node)
                        if rule["kind"] == "child_opt":
                            continue      # 조건부 자식은 있어도 없어도 된다
                        gap = sorted(want - got)
                        if gap:
                            problems.append(
                                f"{api} {spec['url']} {rule['field']}[]: 빠짐={gap}")
                            break
        self.assertEqual([], problems, "중첩 구조 위반:\n  " + "\n  ".join(problems))
        self.assertGreater(checked, 0, "중첩 검사가 한 건도 실행되지 않았다 — 픽스처가 행을 못 만든다")


class TestErrorContract(unittest.TestCase):
    """④ IDD 가 선언한 에러 코드를 실제로 낼 수 있는가."""

    def test_declared_errors_reproducible(self) -> None:
        client = TestClient(app)
        unreproduced: list[str] = []
        for api, spec in implemented():
            declared = {c for c in spec["errors"] if c != "401"}
            if not declared:
                continue
            url = probes.fill_path(spec["url"])
            seen: set[str] = set()
            for _desc, kind, q, body, hdr in probes.ERROR_PROBES.get(api, []):
                fx = probes.fixtures_for(kind)
                with contextlib.ExitStack() as stack:
                    # 개체 카드·묶음은 대역을 걷어야 오류가 난다 — 대역이 조회를 늘 성공시킨다.
                    stubs = api not in ("IF-ENTITY-04", "IF-ENTITY-05", "IF-ENTITY-06")
                    for pt in (*_external_patches(mm_meta_stubs=stubs),
                               patch.object(db, "run_in_db",
                                            lambda cb, _f=fx: cb(FakeConn(_f))),
                               patch.object(db, "run_in_db_write",
                                            lambda cb, _f=fx: cb(FakeConn(_f))),
                               patch.object(routes_search, "search_hybrid",
                                            lambda *a, **k: SEARCH_STUB)):
                        stack.enter_context(pt)
                    seen.add(str(client.request(spec["method"], url, params=q or None,
                                                json=body, headers=hdr).status_code))
            if "503" in declared:
                seen.add(str(self._probe_search_unavailable(client, url)))
            for code in sorted(declared - seen):
                if (api, code) in probes.UNREACHABLE:
                    continue                                   # 다른 테스트가 확인하는 상황
                unreproduced.append(f"{api} {spec['url']} → {code} (관측: {sorted(seen)})")
        self.assertEqual([], unreproduced,
                         "선언했으나 유발되지 않은 에러 코드:\n  " + "\n  ".join(unreproduced))

    @staticmethod
    def _probe_search_unavailable(client: TestClient, url: str) -> int:
        """검색 엔진 장애를 주입해 503 계약을 확인한다.

        ⚠️ 엔진을 부르는 진입점 **전부**에 같은 장애를 건다 — 한 곳(``search_hybrid``)에만 걸면
        다른 창구는 엔진 장애가 아니라 엉뚱한 이유(설정 미초기화·임베딩 실패)로 503 을 내거나
        터져도 통과·실패가 갈린다. 외부 자원 대역 위에 장애만 얹어 **연결 실패 경로**를 태운다.
        """
        from service.portal import mm_meta

        if errors.OSConnectionError is None:
            return 0

        def boom(*_a: object, **_k: object) -> None:
            # opensearchpy 예외는 (status, error, info) 3인자다 — 1개만 주면 내부에서 IndexError.
            raise errors.OSConnectionError("N/A", "simulated", Exception("simulated"))

        with contextlib.ExitStack() as stack:
            for pt in (*_external_patches(),
                       patch.object(db, "run_in_db", lambda cb: cb(FakeConn(probes.found()))),
                       patch.object(routes_search, "search_hybrid", boom),
                       patch.object(routes_file_search, "search_files", boom),
                       patch.object(routes_file_search, "browse_files", boom),
                       patch.object(mm_meta, "match_entity_keys", boom)):
                stack.enter_context(pt)
            return client.request("GET", url, params={"q": "x"}).status_code


if __name__ == "__main__":
    unittest.main()
