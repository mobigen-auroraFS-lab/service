"""099 G5 — 개체 **찾아오기(q)·좁히기(refine) 통합 집합 구조** 라우트 계약(DB·검색엔진·임베딩 없음).

무엇을 봉인하나:
① **`q` 와 refine 이 같은 일을 한다** — 둘 다 엔진에 낱말을 던져 **개체 키 집합**을 얻고, 결과 집합은
   그 둘의 교집합(`A ∩ B`)이다. 한쪽만 있으면 그것만, 둘 다 없으면 **필터 없음**(전체).
② 🔴 **`None`(필터 없음)과 빈 집합(0건)은 다른 값**이다. 섞으면 "검색했는데 전체가 나오는" 조용한
   오류가 된다 — 서랍을 다 뒤졌는데 아무것도 없을 때 "서랍 전부"를 내미는 셈이다.
③ **건수는 모수**다(spec §3-6) — `scope_total` = 좁히기 **이전** 집합 크기("지우면 N건"),
   `total` = 좁히기 **이후** 집합 크기. 둘 다 돌려준 개수가 아니다.
④ **커서와 `q` 를 함께 쓸 수 있다**(T022a) — 정렬이 언제나 DB(구성 자산 수)라 이어 읽을 자리가 있다.
   다만 `q`·refine 이 바뀌면 집합 자체가 바뀌므로 화면은 커서를 버리고 처음부터 받아야 한다.
⑤ 🔴 **엔진이 죽으면 503** — 집합을 만들 수 없는데 "필터 없음"으로 접으면 전량이 새어 나가고,
   빈 집합으로 접으면 "자료가 없다"와 "검색이 죽었다"가 같아진다. 둘 다 금지이며 여기서 못 박는다.
⑥ **SC-002** 상위 200 밖 개체가 좁히기만으로 나온다 · **SC-003** 좁힌 결과는 좁히기 전의 부분집합이다
   (= refine 이 `q` 질의를 바꾸지 않는다 · 게이트 재판정 없음).

대역(fake)은 코어 SQL 과 **같은 규칙**으로 동작한다 — `uid_allow` 의 `None`/빈 집합 구분, 커서 keyset
조건, 상한. 실제 SQL 이 그 규칙을 지키는지는 코어 단위(`tests/test_graph_query_entity_allow.py` 11건)가
따로 봉인한다.
"""

from __future__ import annotations

import inspect
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app, routes_mm_meta
from service.portal import mm_meta
from src.search.entity_search_os import EntityMatchSet

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}

# 노출 개체 모수 — dev 실측(2026-09-17)과 같은 수를 대역에도 쓴다. SC-002 가 "201~822위"를 말하므로
# 모수가 그보다 작으면 시험 자체가 성립하지 않는다.
_TOTAL = 822
_TYPE = "장소"


def _row(idx: int) -> dict[str, Any]:
    """코어 ``list_entities`` 행 대역 하나.

    Args:
        idx: 정렬 순위(0 이 맨 위). 구성 자산 수는 3건씩 동점 무더기를 이루도록 만든다 —
            동점 tiebreak(표기 키)이 실제로 도는지 커서 시험이 볼 수 있어야 한다.

    Returns:
        코어 목록 행 모양의 dict.
    """
    uid = f"e{idx:03d}"
    count = (_TOTAL - idx) // 3 + 1
    return {
        "entity_type": _TYPE, "entity_uid": uid, "node_id": str(idx), "name": uid,
        "source": "auto", "description": None, "confirmed_count": count, "total_count": count,
        "modalities": ["text"], "keywords": [uid], "topics": [], "forms": [], "areas": [],
    }


# DB 정렬(구성 자산 수 내림차순 → 표기 키 오름차순) 그대로의 순서다.
_TABLE: list[dict[str, Any]] = [_row(i) for i in range(_TOTAL)]


def _key(idx: int) -> tuple[str, str]:
    """순위 ``idx`` 개체의 키 ``(종류, 표기 키)``."""
    return (_TYPE, f"e{idx:03d}")


def _fake_list_entities(
    _conn: object,
    *,
    entity_type: str | None = None,
    area_names: list[str] | None = None,
    min_bundle_size: int = 0,
    limit: int = 200,
    statuses: list[str] | None = None,
    form_skill_codes: list[str] | None = None,
    after_tier: int | None = None,
    after_count: int | None = None,
    after_uid: str | None = None,
    after_type: str | None = None,
    uid_allow: set[tuple[str, str]] | None = None,
    uid_first: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """코어 ``list_entities`` 대역 — 화이트리스트·커서·상한을 **SQL 과 같은 규칙**으로 흉내 낸다.

    Args:
        _conn: 커넥션 자리(쓰지 않는다).
        entity_type: 종류 필터(대역은 쓰지 않는다).
        area_names: 갈래 필터(대역은 쓰지 않는다).
        min_bundle_size: 노출 임계(대역은 쓰지 않는다).
        limit: 이 쪽의 행 수.
        statuses: 소속 엣지 상태(대역은 쓰지 않는다).
        form_skill_codes: 형식 축 스킬(대역은 쓰지 않는다).
        after_tier: 직전 쪽 마지막 개체의 우선 티어(099 G7 · 정렬 첫 키).
        after_count: 직전 쪽 마지막 개체의 구성 자산 수.
        after_uid: 직전 쪽 마지막 개체의 표기 키.
        after_type: 직전 쪽 마지막 개체의 종류(표기까지 같은 자리를 가른다 · 2026-09-18).
        uid_allow: 개체 화이트리스트. 🔴 ``None`` 이면 조건 없음 · 빈 집합이면 **0건**이다.
        uid_first: 맨 앞에 세울 개체 집합(099 G7 · 순서만 바꾸고 거르지 않는다).

    Returns:
        정렬(티어 ↓ → 구성 자산 수 ↓ → 표기 키 ↑) 기준 다음 ``limit`` 행.

    Raises:
        ValueError: 책갈피를 일부만 준 경우(코어와 같은 계약).
    """
    first = uid_first or set()

    def _tier(row: dict[str, Any]) -> int:
        """행의 우선 티어(1=앞세운 개체 · 0=나머지).

        Args:
            row: 목록 행.

        Returns:
            티어 값.
        """
        return 1 if (row["entity_type"], row["entity_uid"]) in first else 0

    rows = sorted(_rows_of(uid_allow),
                  key=lambda r: (-_tier(r), -int(r["confirmed_count"]),
                                 str(r["entity_uid"]), str(r["entity_type"])))
    book = (after_tier, after_count, after_uid, after_type)
    if any(v is not None for v in book) and any(v is None for v in book):
        raise ValueError("이어읽기 책갈피는 네 값을 함께 줘야 한다")
    if after_count is not None:
        rows = [
            r for r in rows
            if _tier(r) < int(after_tier)
            or (_tier(r) == int(after_tier)
                and (int(r["confirmed_count"]) < int(after_count)
                     or (int(r["confirmed_count"]) == int(after_count)
                         and (str(r["entity_uid"]) > str(after_uid)
                              or (str(r["entity_uid"]) == str(after_uid)
                                  and str(r["entity_type"]) > str(after_type))))))
        ]
    return [dict(r) for r in rows[: int(limit)]]


def _rows_of(uid_allow: set[tuple[str, str]] | None) -> list[dict[str, Any]]:
    """화이트리스트를 적용한 행들(정렬 순서 유지).

    Args:
        uid_allow: 개체 화이트리스트. ``None`` = 조건 없음 · 빈 집합 = 0건.

    Returns:
        조건에 맞는 행 목록.
    """
    if uid_allow is None:
        return _TABLE
    return [r for r in _TABLE if (r["entity_type"], r["entity_uid"]) in uid_allow]


def _cfg(backend: str = "opensearch") -> SimpleNamespace:
    """``get_current_settings`` 대역 — 정형 계층이 읽는 필드만."""
    return SimpleNamespace(
        mm_meta=SimpleNamespace(search_backend=backend, semantic_gate_eps=0.15,
                                semantic_gate_enabled=True),
        embed=SimpleNamespace(api_model="bge-m3"),
    )


def _as_match(keys: set[tuple[str, str]]) -> EntityMatchSet:
    """개체 키 집합을 코어 반환 모양으로 감싼다(대역용).

    이 파일이 보는 것은 **집합이 어디에 쓰이는가**라, 갈래는 "전부 글자로 걸렸다"로 둔다
    (「걸린 이유」 자체는 `test_mm_meta_match_reason.py` 가 따로 본다).

    Args:
        keys: 매칭된 개체 키들.

    Returns:
        ``EntityMatchSet`` — ``keys`` 와 ``text_keys`` 가 같고 의미 갈래는 비어 있다.
    """
    frozen = frozenset(keys)
    return EntityMatchSet(keys=frozen, text_keys=frozen, semantic_keys=frozenset(),
                          semantic_gate_passed=False)


class _Engine:
    """``match_entity_keys`` 대역 — 질의별 답을 미리 정해 두고 호출을 기록한다."""

    def __init__(self) -> None:
        self.answers: dict[str, set[tuple[str, str]]] = {}
        self.error: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    def __call__(self, client: object, index: str, **kwargs: Any) -> EntityMatchSet:
        """질의 하나에 대한 개체 키 집합을 돌려준다(또는 정해 둔 예외를 던진다).

        Args:
            client: 엔진 클라이언트 자리.
            index: 개체 인덱스 이름.
            **kwargs: 코어와 같은 키워드(``query``·``query_vector`` 등) — 그대로 기록한다.

        Returns:
            미리 정해 둔 키 집합을 담은 ``EntityMatchSet``(없으면 빈 집합).

        Raises:
            BaseException: ``error`` 를 세워 둔 경우 그 예외.
        """
        self.calls.append({"index": index, **kwargs})
        if self.error is not None:
            raise self.error
        return _as_match(self.answers.get(str(kwargs.get("query")), set()))

    def calls_for(self, query: str) -> list[dict[str, Any]]:
        """특정 질의로 들어온 호출만 골라 돌려준다."""
        return [c for c in self.calls if c.get("query") == query]


class _SetCase(unittest.TestCase):
    """대역을 세운 라우트 호출 기반 케이스(DB·엔진·임베딩 없음)."""

    backend = "opensearch"

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.engine = _Engine()
        self.count_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

        def _fake_count(_conn: object, **kw: Any) -> int:
            """코어 ``count_entities`` 대역 — 목록과 **같은 규칙**으로 모수를 센다."""
            self.count_calls.append(kw)
            return len(_rows_of(kw.get("uid_allow")))

        def _spying_list(conn: object, **kw: Any) -> list[dict[str, Any]]:
            """목록 대역 + 호출 인자 기록."""
            self.list_calls.append(kw)
            return _fake_list_entities(conn, **kw)

        for target, repl in (
            ("service.api._infra._run_in_db", lambda fn: fn(object())),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.portal.mm_meta.list_entities", _spying_list),
            ("service.portal.mm_meta.count_entities", _fake_count),
            ("service.portal.mm_meta.match_entity_keys", self.engine),
            ("service.portal.mm_meta.embed_query_for_media_search", lambda *_a, **_k: [0.0]),
            ("service.portal.mm_meta.active_embed_channel", lambda: "st_api"),
            ("service.portal.mm_meta.get_current_settings", lambda: _cfg(self.backend)),
            ("src.search.opensearch_sync.get_client", lambda: object()),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def _get(self, **params: Any) -> dict[str, Any]:
        """``/mm-meta`` 를 부르고 200 을 확인한 뒤 본문을 돌려준다."""
        resp = self.client.get("/mm-meta", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _uids(self, body: dict[str, Any]) -> list[str]:
        """응답 항목의 표기 키만 순서대로."""
        return [i["entity_uid"] for i in body["items"]]

    def _drain(self, *, limit: int, **params: Any) -> list[str]:
        """커서를 따라 끝까지 훑어 표기 키를 순서대로 모은다."""
        uids: list[str] = []
        cursor: str | None = None
        for _ in range(60):
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}), **params)
            uids.extend(self._uids(body))
            cursor = body["next_cursor"]
            if cursor is None:
                return uids
        self.fail("커서가 끝나지 않는다 — 마지막 쪽에서 None 이 나와야 한다")
        return uids


class TestUnifiedSetStructure(_SetCase):
    """T022 — ``q`` 와 refine 이 한 구조로 접힌다(spec §3-2a)."""

    def test_q_와_refine_이_같은_엔진_판정을_쓰고_교집합이_된다(self) -> None:
        self.engine.answers = {
            "김치": {_key(1), _key(2), _key(3)},
            "전통": {_key(2), _key(3), _key(9)},
        }
        body = self._get(q="김치", refine="전통", limit=200)
        # 두 질의 모두 **엔진**을 거쳤다 — 한쪽만 파이썬으로 처리하면 규칙이 두 벌이 된다.
        self.assertEqual([c["query"] for c in self.engine.calls], ["김치", "전통"])
        self.assertEqual(self._uids(body), ["e002", "e003"], "결과 집합은 A ∩ B 다")
        self.assertEqual(self.list_calls[0]["uid_allow"], {_key(2), _key(3)})

    def test_한쪽만_있으면_그_집합만_쓴다(self) -> None:
        self.engine.answers = {"김치": {_key(4), _key(5)}, "전통": {_key(7)}}
        self.assertEqual(self._uids(self._get(q="김치", limit=200)), ["e004", "e005"])
        self.assertEqual(self._uids(self._get(refine="전통", limit=200)), ["e007"])

    def test_둘_다_없으면_필터가_없다(self) -> None:
        # 🔴 빈 집합이 아니라 None 이어야 한다 — 빈 집합이면 첫 화면이 통째로 0건이 된다.
        body = self._get(limit=3)
        self.assertEqual(self._uids(body), ["e000", "e001", "e002"])
        self.assertIsNone(self.list_calls[0]["uid_allow"])
        self.assertEqual(self.engine.calls, [], "물어보지 않았으면 엔진을 부르지 않는다")

    def test_매칭이_없으면_빈_집합이지_전체가_아니다(self) -> None:
        # 🔴 이 구분이 이번 구조의 핵심이다 — None 으로 접으면 "검색했는데 822개가 나온다"가 된다.
        self.engine.answers = {"없는낱말": set()}
        body = self._get(q="없는낱말", limit=200)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)
        self.assertEqual(self.list_calls[0]["uid_allow"], set())
        self.assertIsNotNone(self.list_calls[0]["uid_allow"])

    def test_좁히기도_모수_전체에서_판정한다(self) -> None:
        # 종전에는 이번 쪽(파이썬 refine_rows)만 봤다 — 쪽 밖 개체는 좁히기로 닿을 수 없었다.
        self.engine.answers = {"전통": {_key(700)}}
        body = self._get(refine="전통", limit=10)
        self.assertEqual(self._uids(body), ["e700"])

    def test_파이썬_좁히기_경로는_은퇴했다(self) -> None:
        # 🔴 코어 함수 자체는 남아 있다(파일 검색 ``/search`` 가 쓴다) — 개체 경로에서 **호출만** 뗐다.
        for gone in ("refine_rows", "entity_refine_fields", "narrow_entities"):
            self.assertFalse(hasattr(mm_meta, gone), f"{gone} 은 개체 경로에서 은퇴했다")
        from service.api import routes_search
        from src.search.refine import refine_rows as core_refine_rows
        self.assertIs(routes_search.refine_rows, core_refine_rows, "다른 경로는 그대로 쓴다")

    def test_엔진_호출에_질의_벡터가_함께_간다(self) -> None:
        # 🔴 벡터를 빠뜨리면 의미(kNN) 갈래가 조용히 사라진다(`발효`→김치를 못 찾는다).
        self.engine.answers = {"발효": {_key(3)}}
        self._get(q="발효", limit=10)
        self.assertEqual(self.engine.calls[0]["query_vector"], [0.0])

    def test_검색_경로에만_걸린_이유_세_키가_더_실린다(self) -> None:
        # 🔴 **정책 개정(2026-09-17 사용자 결정)** — 099 G5 는 "항목 키를 경로와 무관하게 통일"했으나
        #    그러면 뜻(kNN)으로 걸린 결과를 화면이 설명할 수 없다. `왕실 무덤` 으로 `영릉` 이 나오는데
        #    카드 어디에도 그 글자가 없어 "검색이 고장났나"가 된다(089·090·092 가 세운 설명 가능성).
        #    → 검색 경로에만 ``by_text``·``by_semantic``·``match_reason`` 을 되살린다.
        # 이 단언은 종전보다 **강하다**: 목록 키 집합을 그대로 못 박고(전과 동일), 검색 경로가 더 싣는
        # 키가 **정확히 그 셋**임을 함께 못 박는다 — 넷째 키가 몰래 끼어드는 것도 잡힌다.
        self.engine.answers = {"김치": {_key(1)}}
        listed = set(self._get(limit=1)["items"][0])
        searched = set(self._get(q="김치", limit=1)["items"][0])
        self.assertEqual(listed, set(mm_meta.shape_list_item(_TABLE[0])))
        self.assertEqual(searched - listed, {"by_text", "by_semantic", "match_reason"})
        self.assertEqual(listed - searched, set(), "검색 경로가 목록 키를 잃으면 안 된다")


class TestFacetsUntouched(_SetCase):
    """🔴 좁히기 칩(종류·갈래)은 검색에 **좌우되지 않는다**(087 결정 · 현행 유지)."""

    def test_칩_창구는_검색어를_받지_않는다(self) -> None:
        # 종류는 **갈아타는 축**이라 검색으로 좁히면 갈아탈 칩이 사라진다. 그 규율을 파라미터가 없다는
        # 사실로 못 박는다(있으면 언젠가 얹히게 된다).
        params = inspect.signature(routes_mm_meta.mm_meta_facets).parameters
        for gone in ("q", "refine", "cursor"):
            self.assertNotIn(gone, params)


class TestCounts(_SetCase):
    """T022 — 건수의 뜻(spec §3-6): 둘 다 **모수**이고 항상 실린다."""

    def test_scope_total_은_좁히기_이전_모수다(self) -> None:
        self.engine.answers = {
            "김치": {_key(i) for i in range(10)},
            "전통": {_key(i) for i in range(5)},
        }
        body = self._get(q="김치", refine="전통", limit=2)
        self.assertEqual(len(body["items"]), 2, "쪽 크기는 2 다")
        self.assertEqual(body["total"], 5, "좁히기 **이후** 집합 크기(모수)")
        self.assertEqual(body["scope_total"], 10, "좁히기 **이전** 집합 크기 — 지우면 N건")
        self.assertEqual(body["refine"], "전통")

    def test_q_경로_총계가_돌려준_개수가_아니다(self) -> None:
        # 🔴 G2 까지는 검색 경로의 total 이 len(items) 였다(화면에 "2건"이 찍혔다).
        self.engine.answers = {"김치": {_key(i) for i in range(10)}}
        body = self._get(q="김치", limit=2)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["total"], 10)
        self.assertEqual(body["scope_total"], 10, "좁히기가 없으면 둘이 같다")

    def test_refine_만_주면_scope_total_은_전체_모수다(self) -> None:
        # "지우면 몇 건"의 답은 조건 없는 목록 전체다(찾아오기를 안 했으므로).
        self.engine.answers = {"전통": {_key(3), _key(500)}}
        body = self._get(refine="전통", limit=200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["scope_total"], _TOTAL)

    def test_목록_경로_총계는_모수_그대로다(self) -> None:
        body = self._get(limit=3)
        self.assertEqual(body["total"], _TOTAL)
        self.assertEqual(body["scope_total"], _TOTAL)

    def test_총계는_목록과_같은_조건으로_센다(self) -> None:
        # 조건이 다르면 "822건 중 3건"의 822 가 목록과 다른 모수를 말한다.
        self.engine.answers = {"김치": {_key(1)}}
        self._get(q="김치", entity_type=_TYPE, areas="여행·명소", limit=3)
        # 코어 seam 이 받는 이름으로 확인한다(``count_entities`` 는 ``area_names`` 로 받는다).
        self.assertEqual(self.count_calls[0]["entity_type"], _TYPE)
        self.assertEqual(self.count_calls[0]["area_names"], ["여행·명소"])
        self.assertEqual(self.count_calls[0]["uid_allow"], {_key(1)})
        self.assertEqual(self.count_calls[0]["min_bundle_size"], routes_mm_meta._MIN_BUNDLE_SIZE)

    def test_좁히기가_없으면_총계를_두_번_세지_않는다(self) -> None:
        # 같은 값을 두 번 묻는 것은 DB 왕복 낭비다 — 좁히기가 있을 때만 모수가 둘로 갈린다.
        self.engine.answers = {"김치": {_key(1)}}
        self._get(q="김치", limit=3)
        self.assertEqual(len(self.count_calls), 1)
        self.count_calls.clear()
        self.engine.answers["전통"] = {_key(1)}
        self._get(q="김치", refine="전통", limit=3)
        self.assertEqual(len(self.count_calls), 2)


class TestCursorWithQuery(_SetCase):
    """T022a — 커서와 ``q`` 를 함께 쓸 수 있다(G2 가 임시로 막아 둔 제약 해제)."""

    def test_커서와_검색어를_함께_줄_수_있다(self) -> None:
        self.engine.answers = {"김치": {_key(i) for i in range(5)}}
        first = self._get(q="김치", limit=2)
        self.assertEqual(self._uids(first), ["e000", "e001"])
        second = self._get(q="김치", limit=2, cursor=first["next_cursor"])
        self.assertEqual(self._uids(second), ["e002", "e003"], "이어 읽기가 성립한다")

    def test_검색_경로도_커서로_완주한다(self) -> None:
        self.engine.answers = {"김치": {_key(i) for i in range(7)}}
        walked = self._drain(limit=2, q="김치")
        self.assertEqual(walked, [f"e{i:03d}" for i in range(7)], "중복 0·누락 0")

    def test_좁히기_경로도_커서로_완주한다(self) -> None:
        self.engine.answers = {"전통": {_key(i) for i in (300, 400, 500)}}
        self.assertEqual(self._drain(limit=2, refine="전통"), ["e300", "e400", "e500"])

    def test_커서_설명이_질의가_바뀌면_처음부터임을_알린다(self) -> None:
        # 서버는 강제할 수단이 없다 — 계약 문구로 알린다(집합이 바뀌면 책갈피의 자리도 뜻을 잃는다).
        param = inspect.signature(routes_mm_meta.list_mm_meta).parameters["cursor"].default
        desc = str(param.description)
        self.assertIn("처음부터", desc)
        self.assertNotIn("함께 줄 수 없", desc, "G2 의 임시 제약 문구가 남아 있으면 안 된다")
        self.assertIn("처음부터", str(routes_mm_meta.list_mm_meta.__doc__))


class TestEngineFailure(_SetCase):
    """🔴 집합을 만들 수 없을 때 — **되돌리지 않는다**(전체도, 빈 결과도 아니다)."""

    def _conn_error(self) -> BaseException:
        """검색 엔진 **연결** 실패 예외(라이브러리가 없으면 시험을 건너뛴다)."""
        try:
            from opensearchpy.exceptions import ConnectionError as OSConnectionError
        except ImportError:  # pragma: no cover - 라이브러리 미설치 환경
            self.skipTest("opensearchpy 미설치 — 연결 실패 예외를 만들 수 없다")
        return OSConnectionError("N/A", "conn refused", None)

    def test_엔진_연결_실패는_503_이다(self) -> None:
        self.engine.error = self._conn_error()
        resp = self.client.get("/mm-meta", params={"q": "김치", "limit": 200})
        self.assertEqual(resp.status_code, 503, resp.text)

    def test_503_일_때_전체_목록이_새지_않는다(self) -> None:
        # 🔴 종전 구조는 실패를 문자열 결과로 되돌렸다. 새 구조에서 같은 되돌림은 uid_allow=None
        #    이 되어 "검색했는데 822개가 나온다" 가 된다.
        self.engine.error = self._conn_error()
        resp = self.client.get("/mm-meta", params={"q": "김치", "limit": 200})
        self.assertNotIn("items", resp.json())
        self.assertEqual(self.list_calls, [], "집합을 못 만들었으면 목록을 읽지 않는다")

    def test_좁히기_엔진_실패도_503_이다(self) -> None:
        self.engine.error = self._conn_error()
        resp = self.client.get("/mm-meta", params={"refine": "전통", "limit": 200})
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.list_calls, [])

    def test_임베딩_실패는_임베딩_503_이다(self) -> None:
        # 임베딩 서버 장애와 엔진 장애는 다른 원인 — 문구로 구분해야 운영자가 엉뚱한 곳을 안 본다.
        with patch("service.portal.mm_meta.embed_query_for_media_search",
                   side_effect=RuntimeError("임베딩 API 호출 실패")):
            resp = self.client.get("/mm-meta", params={"q": "김치", "limit": 200})
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertIn("임베딩", resp.json()["detail"])
        self.assertEqual(self.list_calls, [])

    def test_코드_결함은_503으로_둔갑하지_않는다(self) -> None:
        # 연결 실패만 503 — 그 밖의 예외는 그대로 올라가 500 으로 구분된다(파일 검색과 같은 규율).
        self.engine.error = KeyError("코드 결함 흉내")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/mm-meta", params={"q": "김치", "limit": 200})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.list_calls, [], "코드 결함일 때도 전체 목록으로 되돌리지 않는다")

    def test_엔진이_죽어도_질의_없는_목록은_그대로다(self) -> None:
        # 첫 화면(찾아오기 없음)은 엔진이 필요 없다 — 엔진 장애로 목록까지 막지 않는다.
        self.engine.error = self._conn_error()
        body = self._get(limit=3)
        self.assertEqual(self._uids(body), ["e000", "e001", "e002"])


class TestFallbackBackend(_SetCase):
    """되돌림 백엔드(``MM_META_SEARCH_BACKEND=pg``)는 집합 판정을 하지 못한다."""

    backend = "pg"

    def test_되돌림_백엔드에서_검색하면_503_이다(self) -> None:
        # 🔴 조용히 엔진으로 가면 설정이 무시되고, 조용히 전체를 주면 "검색했는데 전부"가 된다.
        resp = self.client.get("/mm-meta", params={"q": "김치", "limit": 200})
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.list_calls, [])
        self.assertIn("pg", resp.json()["detail"])

    def test_되돌림_백엔드에서도_목록은_된다(self) -> None:
        body = self._get(limit=3)
        self.assertEqual(self._uids(body), ["e000", "e001", "e002"])


class TestSC002(_SetCase):
    """SC-002 — 질의 없이 refine 만 줬을 때 **상위 200 밖(201~822위)** 개체가 나온다."""

    def test_상위_200_밖_개체가_좁히기만으로_나온다(self) -> None:
        target = 500  # 501위 — 종전 구조에서는 상위 200 안에 없어 좁히기로 닿을 수 없었다.
        first_page = self._uids(self._get(limit=200))
        self.assertEqual(len(first_page), 200)
        self.assertNotIn(f"e{target:03d}", first_page, "상위 200 안에는 없는 개체여야 시험이 성립한다")

        self.engine.answers = {"전통": {_key(target)}}
        body = self._get(refine="전통", limit=200)
        self.assertEqual(self._uids(body), [f"e{target:03d}"])
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["scope_total"], _TOTAL, "지우면 822건")

    def test_꼬리_개체도_닿는다(self) -> None:
        self.engine.answers = {"전통": {_key(_TOTAL - 1)}}
        self.assertEqual(self._uids(self._get(refine="전통", limit=200)), [f"e{_TOTAL - 1:03d}"])


class TestSC003(_SetCase):
    """SC-003 — 좁힌 결과는 좁히기 전 결과의 **부분집합**이다(게이트 재판정 없음)."""

    def setUp(self) -> None:
        super().setUp()
        self.engine.answers = {
            "김치": {_key(i) for i in (1, 4, 9, 300, 700)},
            # 🔴 refine 집합에 ``q`` 밖 개체(e800)를 일부러 섞는다 — 교집합이 아니라 합집합이면
            #    없던 개체가 나타나 부분집합이 깨진다.
            "전통": {_key(4), _key(700), _key(800)},
        }

    def test_좁히면_좁히기_전_결과의_부분집합이다(self) -> None:
        plain = set(self._uids(self._get(q="김치", limit=500)))
        refined = set(self._uids(self._get(q="김치", refine="전통", limit=500)))
        self.assertTrue(refined <= plain, f"부분집합이 아니다: {refined - plain}")
        self.assertEqual(refined, {"e004", "e700"})
        self.assertNotIn("e800", refined, "좁히기가 없던 개체를 데려오면 안 된다")

    def test_좁히기가_질의_판정을_바꾸지_않는다(self) -> None:
        # 🔴 refine 은 **질의가 아니라 집합 필터**다(spec §3-1). ``q`` 판정 호출이 두 실행에서
        #    글자 그대로 같아야 게이트가 다시 돌지 않았다고 말할 수 있다.
        self._get(q="김치", limit=500)
        without = list(self.engine.calls_for("김치"))
        self.engine.calls.clear()
        self._get(q="김치", refine="전통", limit=500)
        with_refine = self.engine.calls_for("김치")
        self.assertEqual(with_refine, without, "refine 이 q 질의 호출을 바꿨다")
        self.assertEqual(len(with_refine), 1, "q 판정은 한 번만 돈다")

    def test_좁히기_전후_모수가_포함관계를_말한다(self) -> None:
        body = self._get(q="김치", refine="전통", limit=500)
        self.assertEqual(body["scope_total"], 5)
        self.assertEqual(body["total"], 2)
        self.assertLessEqual(body["total"], body["scope_total"])


if __name__ == "__main__":
    unittest.main()
