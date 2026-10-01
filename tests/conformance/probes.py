"""대조에 쓰는 요청 레시피(단일 책임: 어떤 요청을 보낼지 정하기).

세 가지를 공급한다:
  · ``FOUND``/``NONE``/``GONE``  상황별 픽스처 세트
  · ``happy_request``            정상 200 을 얻기 위한 요청 인자
  · ``ERROR_PROBES``             선언된 에러 코드를 유발하는 요청들

판정은 하지 않는다 — 여기는 "무엇을 보낼까"만 답한다.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.conformance.simulator import Fixture, fixture

ASSET_ID = "0191aaaa-bbbb-cccc-dddd-eeeeffff0000"


def fake_settings() -> SimpleNamespace:
    """설정 대역 — 조회 경로가 읽는 값만 담는다.

    ⚠️ ``init_settings`` 를 부르지 않는 이유: 그건 **프로세스 전역**을 고정해 뒤에 도는 다른
    테스트의 튜닝 기본값을 바꾼다(실제로 깨뜨렸다). 대역은 이 테스트 안에서만 산다.
    """
    return SimpleNamespace(
        opensearch=SimpleNamespace(index="assets"),
        relations=SimpleNamespace(persist_min_conf_similarity=0.0),
        # 개체 검색 backend: 엔진을 띄우지 않으므로 되돌림 백엔드를 고른다. 검색어 없는 목록은
        #   집합 판정을 타지 않아 그대로 200 이고, 검색어를 주면 503 이다(IF-ENTITY-01 에러 대조가 쓴다).
        mm_meta=SimpleNamespace(search_backend="db"),
    )


# 설정 대역에서 **빼는** 모듈 — 미초기화를 이미 정상 경로로 다룬다.
#   routes_search: ``_base_tuning``·``_tag_facet_limits`` 가 RuntimeError 를 잡아 코어 상수
#                  기본값으로 접는다. 대역을 씌우면 그 폴백 경로를 덮어 **실제와 다른 값**을 본다.
#   bootstrap:     기동 전용이라 요청 경로에서 돌지 않는다.
_SETTINGS_PATCH_SKIP = frozenset({"service.api.routes.search", "service.bootstrap"})


def settings_patches() -> list:
    """``get_current_settings`` 를 들여온 **모든 service 모듈**에 대역을 건다.

    한 곳씩 쫓아가면 새 소비처가 생길 때마다 이 테스트가 깨진다 — 들여온 모듈을 찾아 한 번에
    건다(실제로 세 번 연속 깨지고 나서 이렇게 바꿨다).
    """
    import importlib
    import pkgutil
    from unittest.mock import patch

    import service

    out = []
    for info in pkgutil.walk_packages(service.__path__, "service."):
        try:
            mod = importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 — 선택 의존이 없는 모듈은 건너뛴다
            continue
        if info.name in _SETTINGS_PATCH_SKIP:
            continue
        if hasattr(mod, "get_current_settings"):
            out.append(patch.object(mod, "get_current_settings", fake_settings))
    return out


# 다운로드·썸네일은 **디스크에 실제 파일이 있어야** 200 이 난다(없으면 410 이 계약이다).
_TMPDIR = Path(tempfile.mkdtemp(prefix="idd_conformance_"))
IMAGE_PATH = _TMPDIR / f"{ASSET_ID}__photo.jpg"
MISSING_PATH = _TMPDIR / "deleted_by_someone.jpg"


def ensure_image() -> Path:
    """썸네일·다운로드 200 경로에 필요한 실제 JPEG 를 만든다(한 번만)."""
    if not IMAGE_PATH.exists():
        from PIL import Image

        Image.new("RGB", (48, 32), (120, 160, 200)).save(IMAGE_PATH, "JPEG")
    return IMAGE_PATH


_DETAIL_SQL = r"LEFT JOIN asset_metadata m ON m\.asset_id = a\.asset_id\s+WHERE a\.asset_id = %s"
# 원본 다운로드(IF-ASSET-03)의 대상 질의 · 원문(IF-ASSET-11)의 질의 — 2026-10-01 다시 구현했다.
_TARGET_SQL = (r"SELECT asset_id, fs_path, fs_uri, file_size, modality, domain_label, status"
               r"\s+FROM asset\s+WHERE asset_id = %s")


def _target_row(path: Path) -> list[dict[str, Any]]:
    return [{"asset_id": ASSET_ID, "fs_path": str(path), "fs_uri": f"file://{path}",
             "file_size": 512, "modality": "image", "domain_label": "general",
             "status": "registered"}]


# [2026-09-21 신설 창구] 원문 · 태그 · 추천은 각자 다른 질의를 쓴다.
#   ⚠️ 원문 질의는 상세 질의와 같은 조인 문구를 써서 ``_DETAIL_SQL`` 에도 걸린다 — **먼저** 둬야
#      제 픽스처를 만난다(FakeConn 은 첫 일치를 쓴다).
_CONTENT_SQL = r"SELECT a\.asset_id, a\.modality, a\.status, a\.fs_path, m\.ext_meta"
_SELECTION_SQL = r"SELECT asset_id, fs_path, file_size\s+FROM asset\s+WHERE asset_id = ANY"
_TAGS_SQL = r"jsonb_array_elements_text\(COALESCE\(m\.ext_meta->'keywords'"
_SUGGEST_SQL = r"SELECT t\.topic_ko AS value, 'topic' AS kind"


def found() -> list[Fixture]:
    """행도 있고 파일도 있다 — 정상 200·400·422 유발용."""
    img = ensure_image()
    return [
        # 원문 — 받아쓰기가 있는 소리 자산(파일을 읽지 않고도 200 이 난다).
        fixture(_CONTENT_SQL, [{"asset_id": ASSET_ID, "modality": "audio", "status": "registered",
                                "fs_path": str(img), "ext_meta": {"stt": "받아쓴 글자"}}]),
        fixture(_SELECTION_SQL, [{"asset_id": ASSET_ID, "fs_path": str(img), "file_size": 512}]),
        fixture(_TARGET_SQL, _target_row(img)),
        fixture(_TAGS_SQL, [{"tag": "한옥", "count": 3}]),
        fixture(_SUGGEST_SQL, [{"value": "역사·문화유산", "kind": "topic", "count": 5}]),
        fixture(_DETAIL_SQL, [{"asset_id": ASSET_ID, "modality": "image",
                               "domain_label": "general", "status": "registered",
                               "fs_path": str(img), "core_meta": {}, "ext_meta": {}, "tags": []}]),
    ]


NONE: list[Fixture] = []                                   # 행 없음 → 404


# 개체 묶음(IF-ENTITY-04)의 대상 질의 — 용량 판정에 쓰는 행을 여기서 공급한다.
_ENTITY_ASSETS_SQL = (r"SELECT DISTINCT a\.asset_id::text AS asset_id, a\.modality, a\.fs_path")


def entity_assets_huge() -> list[Fixture]:
    """개체 구성 자산이 **상한을 넘는 용량**으로 잡힌다 → 413."""
    return [fixture(_ENTITY_ASSETS_SQL,
                    [{"asset_id": ASSET_ID, "modality": "video", "fs_path": "/tmp/huge.mp4",
                      "file_size": 900 * 1024 * 1024}])]


def entity_card_empty() -> list[Fixture]:
    """개체는 있는데 **구성 자산이 하나도 없다** → 409(빈 zip 대신 오류로 알린다)."""
    return [fixture(r"SELECT node_id,\s+COALESCE\(NULLIF\(canonical->>'name'",
                    [{"node_id": 1, "name": "u1", "canonical": {}}])]


def entity_assets_pathless() -> list[Fixture]:
    """행은 있는데 **경로를 아는 파일이 하나도 없다** → 409(빈 zip 대신 오류)."""
    return [fixture(_ENTITY_ASSETS_SQL,
                    [{"asset_id": ASSET_ID, "modality": "image", "fs_path": None,
                      "file_size": 10}])]


def selection_huge() -> list[Fixture]:
    """고른 자산의 크기 합이 **상한을 넘는다** → 413(건수만으로는 못 막는 자리)."""
    return [fixture(_SELECTION_SQL,
                    [{"asset_id": ASSET_ID, "fs_path": "/tmp/huge.bin",
                      "file_size": 600 * 1024 * 1024}])]


def gone_content() -> list[Fixture]:
    """원문을 읽을 문서인데 **원본 파일이 사라졌다** → 410(404 와 가른다)."""
    return [fixture(_CONTENT_SQL, [{"asset_id": ASSET_ID, "modality": "text",
                                    "status": "registered", "fs_path": str(MISSING_PATH),
                                    "ext_meta": {}}])]


def gone() -> list[Fixture]:
    """행은 있고 디스크 파일이 없다 → 410."""
    return [fixture(_TARGET_SQL, _target_row(MISSING_PATH))]


# 경로 파라미터 자리를 채우는 값
PATH_VALUES = {"{asset_id}": ASSET_ID, "{modality}": "image", "{topic}": "요리",
               "{kind_code}": "some_kind", "{id}": "1",
               "{entity_type}": "person", "{entity_uid}": "u1"}

# 200 을 얻으려면 값이 필요한 엔드포인트
HAPPY_QUERY = {"/search": {"q": "김치"}, "/admin/asset-stats": {"snapshot_buckets": "true"},
               "/file-search/suggest": {"q": "역사"}}
HAPPY_BODY = {"/auth/token": {"user_id": "conformance"},
              "/assets/bundle": {"asset_ids": [ASSET_ID]},
              # 계정 창구는 아직 501 이라 본문이 형식만 맞으면 된다(저장소가 생기면 계약이 산다).
              "/auth/signup": {"login_id": "conformance", "password": "x"},
              "/auth/login": {"login_id": "conformance", "password": "x"},
              "/admin/relations/approve": {"edge_ids": ["e1"]},
              "/admin/relations/reject": {"edge_ids": ["e1"]},
              "/admin/relations/revise": {"edge_id": "e1", "to_status": "active"}}

# (설명, 픽스처 종류, query, body, headers) — 하나라도 선언 코드를 내면 재현으로 본다.
# 픽스처 종류: "found" | "none" | "gone"(다운로드 원본 없음) | "gone_content"(원문 원본 없음)
ERROR_PROBES: dict[str, list[tuple[str, str, dict, dict | None, dict]]] = {
    "IF-ASSET-06": [("mode 오타", "found", {"q": "x", "mode": "bogus"}, None, {}),
                    ("modality 오타", "found", {"q": "x", "modalities": "bogus"}, None, {}),
                    ("preset 오타", "found", {"q": "x", "preset": "bogus"}, None, {}),
                    ("날짜 형식 오류", "found", {"q": "x", "created_from": "notadate"}, None, {})],
    # 공백만 보내면 400 — 기본값(dev-user)으로 흡수하지 않는다는 계약(IDD IF-AUTH-03)
    "IF-AUTH-03": [("공백만 보낸 user_id", "found", {}, {"user_id": "   "}, {})],
    # /file-search — 커서·시작위치 동시 지정은 400, 닫힌 어휘·범위 위반은 422(IDD IF-ASSET-09)
    "IF-ASSET-09": [("offset·cursor 동시 지정", "found", {"offset": "10", "cursor": "abc"}, None, {}),
                    ("관련도 정렬 + cursor", "found",
                     {"sort": "relevance", "cursor": "abc"}, None, {}),
                    ("modality 닫힌 어휘 위반", "found", {"modality": "bogus"}, None, {}),
                    ("limit 범위 밖", "found", {"limit": "9999"}, None, {}),
                    ("날짜 형식 오류", "found", {"created_from": "notadate"}, None, {}),
                    # 크기 구간은 자리만 있다 — 주면 501(조용히 무시하지 않는다는 계약).
                    ("크기 구간(미구현)", "found", {"size_bucket": "under1"}, None, {}),
                    ("모르는 크기 구간", "found", {"size_bucket": "bogus"}, None, {})],
    # /mm-meta — 깨진 커서는 400, 집합 판정 불가는 503(IDD IF-ENTITY-01). 503 은 설정 대역이 고른
    #   되돌림 백엔드(``fake_settings`` 의 ``search_backend="db"``)에서 검색어를 주면 난다.
    "IF-ENTITY-01": [("깨진 커서", "found", {"cursor": "abc"}, None, {}),
                     ("되돌림 백엔드에서 검색", "found", {"q": "x"}, None, {}),
                     ("limit 범위 밖", "found", {"limit": "0"}, None, {}),
                     ("질의 길이 상한 초과", "found", {"q": "김" * 400}, None, {})],
    "IF-ASSET-01": [("행 없음", "none", {}, None, {})],
    "IF-ADMIN-13": [("행 없음", "none", {}, None, {})],
    "IF-ADMIN-09": [("snapshot_bucket 오타", "found", {"snapshot_bucket": "bogus"}, None, {}),
                    ("날짜 형식 오류", "found", {"created_from": "notadate"}, None, {})],
    "IF-ADMIN-16": [("status 오타", "found", {"status": "bogus"}, None, {}),
                    ("confidence 범위 밖", "found", {"min_confidence": "5"}, None, {}),
                    ("날짜 형식 오류", "found", {"from": "notadate"}, None, {})],
    "IF-ADMIN-17": [("status 오타", "found", {"status": "bogus"}, None, {})],
    # 개체 묶음·카드 — 좁힌 결과가 비면 404, 용량 상한 초과면 413, 경로를 아는 파일이 없으면 409
    "IF-ENTITY-05": [("개체 없음", "none", {}, None, {})],
    # 원본 다운로드 · 원문 · 묶음 네 창구(2026-10-01 다시 구현 — 묶음은 임시 파일 없이 만들면서 흘려 보낸다). 썸네일(IF-ASSET-04)만 자리만(501)이라 표본이 없다.
    "IF-ASSET-03": [("행 없음", "none", {}, None, {}),
                    ("파일 없음", "gone", {}, None, {}),
                    ("Range 위반", "found", {}, None, {"Range": "bytes=99999-"})],
    "IF-ASSET-11": [("행 없음", "none", {}, None, {}),
                    ("원본 파일 없음", "gone_content", {}, None, {})],
    "IF-ASSET-05": [("seed 없음", "none", {}, None, {})],
    "IF-ASSET-12": [("빈 asset_ids", "found", {}, {"asset_ids": []}, {}),
                    ("UUID 아님", "found", {}, {"asset_ids": ["nope"]}, {}),
                    ("전부 노출 대상 아님", "none", {}, {"asset_ids": [ASSET_ID]}, {}),
                    ("용량 상한 초과", "selection_huge", {}, {"asset_ids": [ASSET_ID]}, {})],
    "IF-ENTITY-04": [("좁힌 결과 없음", "none", {}, None, {}),
                     ("용량 상한 초과", "entity_huge", {}, None, {}),
                     ("전부 경로 미상", "entity_pathless", {}, None, {})],
    "IF-ENTITY-06": [("개체 없음", "none", {}, None, {}),
                     ("구성 자산 없음", "entity_card_empty", {}, None, {})],
    "IF-ASSET-13": [("q 누락", "found", {"q": ""}, None, {}),
                    ("limit 범위 밖", "found", {"q": "가", "limit": "0"}, None, {})],
    "IF-ASSET-14": [("모르는 축", "found", {"axis": "bogus"}, None, {}),
                    ("날짜 형식 오류", "found", {"created_from": "notadate"}, None, {})],
    "IF-CAT-01": [("status 오타", "found", {"status": "bogus"}, None, {})],
    "IF-CAT-02": [("limit 범위 밖", "found", {"limit": "0"}, None, {})],
    # 미구현 창구 — 형식이 맞는 요청은 501 로 끊긴다(그것이 지금의 계약이다).
    "IF-AUTH-01": [("빈 값", "found", {}, {"login_id": "", "password": ""}, {})],
    "IF-AUTH-02": [("빈 값", "found", {"login_id": ""}, None, {})],
    # 로그인 — 없는 아이디·틀린 비밀번호는 같은 401, 빈 값은 422(형식 검증이 먼저).
    "IF-AUTH-05": [("없는 아이디", "found", {}, {"login_id": "nobody", "password": "x"}, {}),
                   ("빈 값", "found", {}, {"login_id": "", "password": ""}, {})],
    "IF-REVIEW-01": [("빈 edge_ids", "found", {}, {"edge_ids": []}, {})],
    "IF-REVIEW-02": [("빈 edge_ids", "found", {}, {"edge_ids": []}, {})],
    "IF-REVIEW-03": [("to_status 오타", "found", {}, {"edge_id": "e", "to_status": "bogus"}, {})],
}
# 기간·버킷 파라미터를 가진 관리자 조회는 같은 방식으로 422 를 유발한다.
for _api in ("IF-ADMIN-02", "IF-ADMIN-03", "IF-ADMIN-04", "IF-ADMIN-05", "IF-ADMIN-06",
             "IF-ADMIN-07", "IF-ADMIN-08", "IF-ADMIN-10", "IF-ADMIN-11", "IF-ADMIN-12",
             "IF-ADMIN-14", "IF-ADMIN-15"):
    ERROR_PROBES[_api] = [("날짜 형식 오류(from)", "found", {"from": "notadate"}, None, {}),
                          ("날짜 형식 오류(created_from)", "found",
                           {"created_from": "notadate"}, None, {}),
                          ("interval 허용목록 밖", "found", {"interval": "week"}, None, {}),
                          ("monthly_interval 밖", "found", {"monthly_interval": "week"}, None, {})]

# ── 이 레포만으로는 맞출 수 없는 계약 ─────────────────────────────────────────
# 이 레포는 **서빙만** 한다(README §설계). 검색 질의 조립·색인 매핑은 코어(``src.*``)가 소유하므로,
# 코어가 축을 열어 주기 전에는 서비스가 파라미터만 받아도 필터가 성립하지 않는다.
# ⚠️ 숨기는 것이 아니라 **적어 두는 것**이다 — 코어가 지원하기 시작하면 아래 stale 검사가
#    "이제 되니 목록에서 빼라"고 실패한다. 목록이 조용히 낡는 것을 막는다.
CROSS_REPO_GAPS: dict[tuple[str, str], str] = {
    # 현재 비어 있다 — 상류가 tag 까지 구현하면서 마지막 항목이 해소됐다.
    # 다시 채울 일이 생기면 **반드시 이유를 적는다**. 위 stale 검사가 낡은 면제를 걷어낸다.
}

# ── 코드가 먼저 늘었고 IDD 반영을 기다리는 응답 필드 ─────────────────────────────
# 스펙 결정으로 응답에 필드가 더해졌는데 IDD.xlsx 가 아직 따라오지 않은 것. 코드를 되돌릴 일이
# 아니라 **문서를 고칠 일**이므로 대조에서 "여분"으로 세지 않되, 무엇을 왜 기다리는지 남긴다.
# ⚠️ IDD 에 올라가면 stale 검사가 "이제 계약에 있으니 빼라"고 실패한다(위 CROSS_REPO_GAPS 와 같은 규율).
IDD_PENDING_RESP: dict[tuple[str, str], str] = {
    # 현재 비어 있다 — IDD v2.1(2026-09-21)이 scope_total·next_cursor 를 반영해 해소됐다.
    # 다시 채울 일이 생기면 **반드시 이유를 적는다**. stale 검사가 낡은 항목을 걷어낸다.
}

# 이 단계에서 만들 수 없는 상황 — 이유를 남겨 "왜 안 봤나"가 기록으로 남게 한다.
UNREACHABLE = {
    ("IF-AUTH-03", "404"): "운영 모드(PORTAL_AUTH_DISABLED=0) 전용 — 인증 계약 테스트가 따로 확인한다",
    # [2026-09-21] 가입 409 는 **같은 아이디가 이미 있어야** 나는데, 계약 테스트는 가짜 커넥션이라
    #   유일 인덱스가 없다 — 단위 테스트(tests/api/routes/test_new_portal_routes.py)가 본다.
    ("IF-AUTH-01", "409"): "유일 인덱스 위반이 있어야 나는 코드 — 가짜 커넥션에서는 만들 수 없다",
    ("IF-ASSET-14", "503"): "검색 엔진·임베딩 장애 전용 — 계약 테스트는 집계를 대역으로 두므로 여기선 "
                            "만들 수 없다(실측 하네스가 장애 서버로 확인한다)",
}


def fixtures_for(kind: str) -> list[Fixture]:
    return {"found": found(), "none": NONE, "gone": gone(), "gone_content": gone_content(),
            "entity_huge": entity_assets_huge(), "entity_pathless": entity_assets_pathless(),
            "entity_card_empty": entity_card_empty(), "selection_huge": selection_huge()}[kind]


def fill_path(url: str) -> str:
    for token, value in PATH_VALUES.items():
        url = url.replace(token, value)
    return url
