"""099 G7 — `/mm-meta` 개체 목록의 **조건 지문**과 **이름 우선**(결함 A·B) · DB·엔진 없음.

무엇을 막나(실측 2026-09-17 · 실 DB·실 OS):

**결함 A — 조건이 다른 커서로 이어 읽으면 자료가 조용히 빠진다.**
``q=사찰`` 로 받은 커서를 ``q=석탑`` 요청에 넣자 **200** 으로 답했고, 정상 1쪽의 석굴암·경주시가
통째로 사라졌다. 계약 주석에는 "q·refine 을 고치면 커서를 버려라"라고 적혀만 있었다 — 서버가
강제하지 않으면 그것은 계약이 아니라 **바람**이다.

**결함 B — 이름으로 찾아도 그 개체가 위에 오지 않는다.**
``숭례문`` 은 **7위**(1~3위: 서울특별시·운문사·화엄사), ``경포대`` 는 **15위**였다. 정렬이 구성
자산 수 하나뿐이라 **큰 개체가 늘 위**로 오기 때문이다. 이름을 정확히 친 사람에게는 "그 개체"가
가장 먼저여야 한다 — 전화번호부에서 이름을 정확히 아는데 두꺼운 항목부터 보여 주는 셈이었다.

여기서 보는 것은 **배선**이다(판정 규칙·SQL 은 코어 단위가 본다):
    ① 라우트가 조건 지문을 만들어 커서에 싣고, 조건이 바뀐 커서를 400 으로 끊는다.
    ② 옛 2값 토큰은 거부된다(정렬 키가 셋이 되었다 · 의도된 깨는 변경).
    ③ **이름이 정확히 같은 개체**를 코어에 ``uid_first`` 로 넘겨 맨 앞에 세운다.
    ④ 검색이 없으면 우선 대상도 없다(목록은 종전 순서 그대로 · 회귀).
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app
from service.portal import mm_meta
from src.search.cursor import encode_cursor
from src.search.entity_search_os import EntityMatchSet

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}

# 실측 장면을 그대로 옮긴 대역 표 — 이름을 정확히 친 개체(숭례문)가 **가장 작다**.
# 구성 자산 수만으로 줄을 세우면 숭례문은 꼴찌가 된다(= 실측의 7위·15위와 같은 구조).
_TABLE: list[tuple[str, str, int]] = [
    ("장소", "서울특별시", 30),
    ("장소", "운문사", 20),
    ("장소", "화엄사", 10),
    ("장소", "숭례문", 4),
]


def _item(entity_type: str, uid: str, count: int) -> dict[str, Any]:
    """정형된 목록 항목 대역(``shape_list_item`` 이 낸 모양).

    Args:
        entity_type: 개체 종류.
        uid: 표기 키.
        count: 구성 자산 수.

    Returns:
        항목 dict.
    """
    return {
        "entity_type": entity_type, "entity_uid": uid, "name": uid, "source": "auto",
        "description": None, "confirmed_count": count, "total_count": count,
        "modalities": ["image"], "keywords": [uid], "topics": [], "forms": [], "areas": [],
    }


def _fake_fetch_list(
    _conn: object,
    *,
    entity_type: str | None = None,
    areas: list[str] | None = None,
    min_bundle_size: int = 0,
    limit: int = 200,
    after_tier: int | None = None,
    after_count: int | None = None,
    after_uid: str | None = None,
    uid_allow: set[tuple[str, str]] | None = None,
    uid_first: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """코어 목록 대역 — **SQL 과 같은 3단 규칙**으로 줄을 세우고 이어 읽는다.

    Args:
        _conn: 커넥션 자리(쓰지 않는다).
        entity_type: 종류 필터(대역은 쓰지 않는다).
        areas: 갈래 필터(대역은 쓰지 않는다).
        min_bundle_size: 노출 임계(대역은 쓰지 않는다).
        limit: 이 쪽의 행 수.
        after_tier: 직전 쪽 마지막 개체의 우선 티어.
        after_count: 직전 쪽 마지막 개체의 구성 자산 수.
        after_uid: 직전 쪽 마지막 개체의 표기 키.
        uid_allow: 검색이 정한 화이트리스트(``None`` = 전체 · 빈 집합 = 0건).
        uid_first: 맨 앞에 둘 개체 집합(순서만 바꾼다).

    Returns:
        정렬(우선 티어 ↓ → 구성 자산 수 ↓ → 표기 키 ↑) 기준 다음 ``limit`` 행.

    Raises:
        ValueError: 책갈피 세 값을 함께 주지 않았을 때(코어와 같은 계약).
    """
    book = (after_tier, after_count, after_uid)
    if any(v is not None for v in book) and any(v is None for v in book):
        raise ValueError("이어읽기 책갈피는 세 값을 함께 줘야 한다")
    rows = [(t, u, c) for t, u, c in _TABLE
            if uid_allow is None or (t, u) in uid_allow]
    first = uid_first or set()
    ranked = sorted(((1 if (t, u) in first else 0, c, u, t) for t, u, c in rows),
                    key=lambda r: (-r[0], -r[1], r[2]))
    if after_tier is not None:
        ranked = [r for r in ranked
                  if r[0] < int(after_tier)
                  or (r[0] == int(after_tier)
                      and (r[1] < int(after_count)
                           or (r[1] == int(after_count) and r[2] > str(after_uid))))]
    return [_item(t, u, c) for _tier, c, u, t in ranked[: int(limit)]]


def _cfg() -> SimpleNamespace:
    """``get_current_settings`` 대역 — 정형 계층이 읽는 필드만.

    Returns:
        설정 대역 객체.
    """
    return SimpleNamespace(
        mm_meta=SimpleNamespace(search_backend="opensearch", semantic_gate_eps=0.15,
                                semantic_gate_enabled=True),
        embed=SimpleNamespace(api_model="bge-m3"),
    )


class _Case(unittest.TestCase):
    """대역을 세운 라우트 호출 기반 케이스."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.list_kwargs: list[dict[str, Any]] = []

        def _recording_fetch_list(conn: object, **kw: Any) -> list[dict[str, Any]]:
            """목록 대역 + 받은 인자 기록(무엇을 코어에 넘겼는지 본다).

            Args:
                conn: 커넥션 자리.
                **kw: 정형 계층이 넘긴 인자들.

            Returns:
                목록 대역의 결과.
            """
            self.list_kwargs.append(dict(kw))
            return _fake_fetch_list(conn, **kw)

        def _fake_match(_client: object, _index: str, **kw: Any) -> EntityMatchSet:
            """엔진 집합 판정 대역 — **표 전체**를 돌려준다(실측처럼 큰 개체가 함께 걸린 상태).

            Args:
                _client: 엔진 클라이언트 자리.
                _index: 색인 이름 자리.
                **kw: 질의·벡터 등.

            Returns:
                표 전체를 담은 판정 결과.
            """
            keys = frozenset((t, u) for t, u, _c in _TABLE)
            return EntityMatchSet(keys=keys, text_keys=keys, semantic_keys=frozenset(),
                                  semantic_gate_passed=False)

        for target, repl in (
            ("service.api._infra._run_in_db", lambda fn: fn(object())),
            ("service.api.routes_mm_meta.mm_meta.fetch_list", _recording_fetch_list),
            ("service.api.routes_mm_meta.mm_meta.fetch_total", lambda *_a, **_k: len(_TABLE)),
            ("service.portal.access_project.fetch_access_tiers", lambda *_a, **_k: {}),
            ("service.portal.mm_meta.match_entity_keys", _fake_match),
            ("service.portal.mm_meta.embed_query_for_media_search", lambda *_a, **_k: [0.0]),
            ("service.portal.mm_meta.active_embed_channel", lambda: "st_api"),
            ("service.portal.mm_meta.get_current_settings", _cfg),
            ("src.search.opensearch_sync.get_client", lambda: object()),
        ):
            p = patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def _get(self, **params: Any) -> dict[str, Any]:
        """``/mm-meta`` 를 부르고 200 을 확인한 뒤 본문을 돌려준다.

        Args:
            **params: 질의 파라미터.

        Returns:
            응답 본문.
        """
        resp = self.client.get("/mm-meta", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _uids(self, **params: Any) -> list[str]:
        """이번 쪽의 표기 키만 순서대로 돌려준다.

        Args:
            **params: 질의 파라미터.

        Returns:
            표기 키 목록.
        """
        return [i["entity_uid"] for i in self._get(**params)["items"]]


class 이름_우선(_Case):
    """③ 결함 B — 이름을 정확히 치면 그 개체가 **맨 앞**이다."""

    def test_이름을_정확히_치면_첫_줄에_온다(self) -> None:
        """실측 재현: 숭례문(자산 4건)이 서울특별시(30건)보다 앞."""
        self.assertEqual(self._uids(q="숭례문", limit=10)[0], "숭례문")

    def test_우선_집합을_코어에_넘긴다(self) -> None:
        """순서를 만드는 것은 SQL 이다 — 백엔드는 **누구를 앞세울지**만 정해 넘긴다(093 경계)."""
        self._get(q="숭례문", limit=10)
        self.assertEqual(self.list_kwargs[-1]["uid_first"], {("장소", "숭례문")})

    def test_표기_차이는_코어_정본_규칙으로_흡수한다(self) -> None:
        """``normalize_text_key``(083/084 공용 정본) — 공백·전각·대소문자. **새 규칙을 만들지 않는다.**"""
        self.assertEqual(self._uids(q="숭 례 문", limit=10)[0], "숭례문")

    def test_좁히기에_이름을_쳐도_앞선다(self) -> None:
        """찾아온 뒤 이름을 치는 것도 같은 뜻이다(어느 칸에 쳤든 "그 개체"를 찾는 것)."""
        self.assertEqual(self._uids(q="사찰", refine="숭례문", limit=10)[0], "숭례문")

    def test_이름이_아니면_앞세우지_않는다(self) -> None:
        """🔴 부분 일치로 앞세우면 순위가 뒤집히는 이유를 아무도 설명하지 못한다."""
        self._get(q="사찰", limit=10)
        self.assertEqual(self.list_kwargs[-1]["uid_first"], set())
        self.assertEqual(self._uids(q="사찰", limit=10)[0], "서울특별시", "종전 순서 그대로")

    def test_검색이_없으면_우선_대상도_없다(self) -> None:
        """회귀 — 목록 화면(검색어 없음)은 종전과 **완전히 같은 순서**여야 한다."""
        self.assertEqual(self._uids(limit=10),
                         ["서울특별시", "운문사", "화엄사", "숭례문"])
        self.assertIn(self.list_kwargs[-1]["uid_first"], (None, set()))


class 커서_지문(_Case):
    """① 결함 A — 조건이 바뀐 커서는 400(조용히 이어 주지 않는다)."""

    def _cursor(self, **params: Any) -> str:
        """그 조건의 첫 쪽을 받아 ``next_cursor`` 를 돌려준다.

        Args:
            **params: 질의 파라미터(쪽 크기는 1로 고정해 꽉 찬 쪽을 만든다).

        Returns:
            다음 쪽 커서.
        """
        token = self._get(limit=1, **params)["next_cursor"]
        self.assertTrue(token, "꽉 찬 쪽이면 책갈피를 줘야 한다")
        return token

    def test_같은_조건이면_이어진다(self) -> None:
        token = self._cursor(q="사찰")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "q": "사찰", "cursor": token}).status_code, 200)

    def test_검색어가_바뀌면_400(self) -> None:
        """🔴 실측 그대로 — 사찰 커서를 석탑 요청에 넣으면 석굴암 등이 통째로 빠졌다."""
        token = self._cursor(q="사찰")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "q": "석탑", "cursor": token}).status_code, 400)

    def test_좁히기가_바뀌면_400(self) -> None:
        token = self._cursor(q="사찰", refine="운문")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "q": "사찰", "refine": "화엄",
                                "cursor": token}).status_code, 400)

    def test_종류_필터가_바뀌면_400(self) -> None:
        token = self._cursor(entity_type="장소")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "entity_type": "작품",
                                "cursor": token}).status_code, 400)

    def test_갈래_필터가_바뀌면_400(self) -> None:
        token = self._cursor(areas="여행·명소")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "areas": "자연", "cursor": token}).status_code, 400)

    def test_갈래를_고른_순서만_다르면_200(self) -> None:
        """🔴 과잉 거부도 결함이다 — 같은 갈래를 다른 순서로 적었을 뿐이면 집합이 같다."""
        token = self._cursor(areas="여행·명소,자연")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "areas": "자연,여행·명소",
                                "cursor": token}).status_code, 200)

    def test_쪽_크기만_달라도_200(self) -> None:
        token = self._cursor(q="사찰")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 2, "q": "사찰", "cursor": token}).status_code, 200)


class 옛_토큰_거부(_Case):
    """② 정렬 키가 셋이 되었다 — 옛 2값 토큰은 **의도적으로** 끊는다."""

    def test_두_값짜리_옛_토큰은_400(self) -> None:
        """통과시키면 반쪽 책갈피로 엉뚱한 자리에서 이어져 목록에 구멍이 난다."""
        # 지문까지 맞춘 **옛 규약** 토큰(정렬값 2개) — 개수 검사만이 이것을 막는다.
        scope = mm_meta.entity_cursor_scope(
            q=None, refine=None, entity_type=None, areas=[],
            min_bundle_size=3)
        token = encode_cursor(mm_meta.ENTITY_CURSOR_SORT, [4, "숭례문"], scope=scope)
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "cursor": token}).status_code, 400)

    def test_지문이_없는_옛_토큰은_400(self) -> None:
        import base64
        import json
        raw = json.dumps({"o": mm_meta.ENTITY_CURSOR_SORT, "s": [0, 4, "숭례문"]}).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(self.client.get(
            "/mm-meta", params={"limit": 1, "cursor": token}).status_code, 400)


class 우선_티어_커서_경계(_Case):
    """🔴 회귀의 핵심 — 우선 무리와 평범한 무리의 **경계**에서 끊겨도 겹치거나 빠지지 않는다."""

    def _drain(self, *, limit: int, **params: Any) -> list[str]:
        """커서를 따라 끝까지 훑어 표기 키를 모은다.

        Args:
            limit: 한 쪽의 크기.
            **params: 질의 파라미터.

        Returns:
            훑은 순서 그대로의 표기 키 목록.
        """
        uids: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}), **params)
            uids.extend(i["entity_uid"] for i in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                return uids
        self.fail("커서가 끝나지 않는다 — 마지막 쪽에서 None 이 나와야 한다")
        return uids

    def test_우선_개체_바로_뒤에서_끊겨도_이어진다(self) -> None:
        """쪽 크기 1 이면 1쪽이 **우선 무리의 끝**이다 — 여기서 한 단이 빠지면 나머지를 통째로 잃는다."""
        self.assertEqual(self._drain(limit=1, q="숭례문"),
                         ["숭례문", "서울특별시", "운문사", "화엄사"])

    def test_쪽_크기를_바꿔도_같은_목록이다(self) -> None:
        whole = self._uids(q="숭례문", limit=10)
        self.assertEqual(self._drain(limit=2, q="숭례문"), whole)
        self.assertEqual(self._drain(limit=3, q="숭례문"), whole)

    def test_완주에_중복도_누락도_없다(self) -> None:
        walked = self._drain(limit=1, q="숭례문")
        self.assertEqual(len(set(walked)), len(walked))
        self.assertEqual(len(walked), len(_TABLE))


if __name__ == "__main__":
    unittest.main()
