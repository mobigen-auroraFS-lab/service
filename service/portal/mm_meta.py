"""개체(멀티모달 메타) 화면 정형 계층 — 코어가 준 사실을 화면 응답 모양으로 바꾼다.

라우트(``routes/mm_meta.py``)는 파라미터 검증 · HTTP 코드 · 헤더만 하고, 무엇을 읽을지는 코어가 정하고, 그 사이의 조립을 이 모듈이 한다(SQL 은 타입 어휘 머리 한 줄뿐).
어디서 읽어도 같은 답이어야 하는 것(노출 개체의 정의 · 라벨 순서 · 세는 규칙)은 코어에, 프론트와 함께 바뀌는 것(응답 키 이름 · 근거 키워드 개수 · 카드 구성 · 다운로드 상한과 문구)은 여기에 있다.
여기 상수는 화면 · 다운로드 정책이라 값의 근거를 주석에 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from psycopg import Connection
from psycopg.rows import dict_row

from service.portal.asset.download import fetch_asset_paths
from service.portal.common.stage_timer import stage
from src.config import search_constants
from src.config.filename_util import display_file_name
from src.config.settings import active_embed_channel, get_current_settings

# 표기 키 정규화는 코어 정본 하나를 쓴다 — 여기서 새 규칙을 만들면 "이름이 같다"의 뜻이 화면과 저장소에서 갈라진다(``entity_uid`` 자체가 이 함수의 결과다).
from src.domain.text_norm import normalize_text_key
from src.mm_classify.read import label_names_of_assets

# 「걸린 이유」 문구는 코어 상수 그대로 쓴다 — 백엔드가 새로 만들면 같은 사실이 화면마다 다르게 적힌다.
from src.mm_meta.entity_search import REASON_SEMANTIC, REASON_TEXT_MATCH
from src.relations.graph_query import (
    assets_of_entities,
    count_entities,
    count_entities_by_area,
    count_entities_by_type,
    list_entities,
    mm_meta_bundle,
)
from src.search.cursor import CursorError, decode_cursor, encode_cursor
from src.search.entity_search_os import EntityMatchSet, match_entity_keys
from src.search.facets import aggregate_facets
from src.search.query_embed import embed_query_for_media_search

# ── 화면 정책 상수 ────────────────────────────────────────────────────────────

# 카드에 보일 근거 키워드 수. 개체 하나에 근거가 수십 개 붙을 수 있어 전부 보이면 카드가 글자로 찬다.
KEYWORD_TOP_N = 6

# 형식 축으로 보일 스킬 — 하나로 고정한 것이 기본이다(여러 스킬 라벨을 한 목록에 섞으면 어느 축인지 구분되지 않는다). 환경변수로 배포 없이 갈아탈 수 있다.
DEFAULT_FORM_SKILL_CODES: tuple[str, ...] = ("content_form",)
FORM_SKILLS_ENV = "PORTAL_MM_META_FORM_SKILLS"

# 개체 검색 · 좁히기가 반드시 거쳐야 하는 백엔드(엔진에 낱말을 던져 개체 키 집합을 얻는 경로). 되돌림 값(``pg``)은 순위만 낼 수 있어 집합을 만들지 못하므로,
# 설정이 그 값이면 엔진으로 가지도 전체를 주지도 않고 503 으로 끊는다.
ENTITY_SEARCH_BACKEND = "opensearch"

# 개체 목록 커서의 정렬 이름 — 파일 커서와 같은 토큰 규약을 쓰되 이름이 달라야 한다(파일 목록의 책갈피를 쓰면 엉뚱한 자리에서 이어진다). 실제 정렬(confirmed_count DESC, entity_uid ASC)과 같다.
ENTITY_CURSOR_SORT = "confirmed_count_desc"
# 정렬값 개수 — (우선 티어, confirmed_count, entity_uid) 셋. 옛 2값 토큰은 여기서 400 으로 끊긴다(통과시키면 반쪽 책갈피로 목록에 구멍이 난다).
ENTITY_CURSOR_ARITY = 4

_LOG = logging.getLogger(__name__)

# 검색 엔진 연결 실패로 볼 예외들(클라이언트가 없는 환경에서는 빈 튜플). 연결 실패만 잡는다 — 나머지 예외까지 삼키면 결함이 "엔진 장애"로 둔갑한다.
try:
    from opensearchpy.exceptions import ConnectionError as _OSConnectionError
except ImportError:  # pragma: no cover - 라이브러리 미설치 환경 방어
    _OSConnectionError = None  # type: ignore[assignment,misc]

_OS_CONN_ERRORS: tuple[type[BaseException], ...] = (
    (_OSConnectionError,) if _OSConnectionError is not None else ()
)

# 타입 어휘 머리 + 정의문 — 화면용 단순 조회라 여기 둔다. 어휘 행이 없으면 빈 목록이며 코드 프리셋으로 채우지 않는다(화면이 폴백하면 "등록 안 했는데 왜 보이나"를 조사하게 된다).
_TYPE_VOCAB_SQL = """
SELECT name, version, types
  FROM mm_meta_type_vocab
 WHERE vocab_code = 'default' AND status = 'active'
 LIMIT 1
"""


def form_skill_codes() -> tuple[str, ...]:
    """형식 축으로 읽을 스킬 코드들을 정한다(환경변수 우선 · 기본은 하나).

    Returns:
        스킬 코드 튜플. 환경변수가 비어 있거나 없으면 ``DEFAULT_FORM_SKILL_CODES``.
    """
    raw = os.getenv(FORM_SKILLS_ENV, "")
    codes = tuple(c.strip() for c in raw.split(",") if c.strip())
    return codes or DEFAULT_FORM_SKILL_CODES


# ── 목록 ─────────────────────────────────────────────────────────────────────


def shape_list_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """코어 목록 행 → 화면 목록 항목(순수). 근거 키워드는 짧고 대표적인 것부터 몇 개만 보인다. ``node_id`` 는 싣지 않는다(개체는 ``(entity_type, entity_uid)`` 로 가리킨다).

    ``confirmed_count`` 는 화면에서 "확인된 N건"으로 표기하고, ``total_count`` 는 필터와 무관한 전량이라 둘이 다를 때만 함께 보인다.
    """
    return {
        "entity_type": row["entity_type"],
        "entity_uid": row["entity_uid"],
        "name": row["name"],
        "source": row["source"],
        "description": row["description"],
        "confirmed_count": row["confirmed_count"],
        "total_count": row["total_count"],
        "modalities": list(row["modalities"]),
        "keywords": sorted(row["keywords"], key=lambda x: (len(x), x))[:KEYWORD_TOP_N],
        "topics": list(row["topics"]),
        "forms": list(row["forms"]),
        "areas": list(row["areas"]),
    }


def fetch_list(
    conn: Connection[Any],
    *,
    entity_type: str | None,
    areas: Sequence[str] | None,
    min_bundle_size: int,
    limit: int,
    after_tier: int | None = None,
    after_count: int | None = None,
    after_uid: str | None = None,
    after_type: str | None = None,
    uid_allow: set[tuple[str, str]] | None = None,
    uid_first: set[tuple[str, str]] | None = None,
    uid_semantic: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """노출 개체 목록을 읽어 화면 항목으로 정형한다(책갈피부터 이어 읽기 · 검색 집합 안에서만).

    ``after_tier`` · ``after_count`` · ``after_uid`` · ``after_type`` 은 직전 쪽 마지막 개체의 정렬 자리이며 함께 주거나 함께 생략한다(일부만 주면 코어가 ValueError).
    ``uid_allow`` 는 검색이 정한 개체 화이트리스트다 — ``None`` 은 필터 없음(전체), 빈 집합은 0건이다(둘을 섞으면 "검색했는데 전체가 나온다").
    ``uid_semantic`` · ``uid_first`` 는 앞세울 개체 집합(순서만 바꾸고 거르지 않는다; 이름 일치가 뜻 일치보다 앞선다).
    정렬: 우선 티어 내림차순 → 구성 자산 수 내림차순 → 표기 키 오름차순.
    """
    rows = list_entities(
        conn,
        entity_type=entity_type,
        area_names=list(areas) if areas else None,
        min_bundle_size=min_bundle_size,
        limit=limit,
        form_skill_codes=list(form_skill_codes()),
        after_tier=after_tier,
        after_count=after_count,
        after_uid=after_uid,
        after_type=after_type,
        uid_allow=uid_allow,
        uid_first=uid_first,
        uid_semantic=uid_semantic,
    )
    return [shape_list_item(r) for r in rows]


def fetch_total(
    conn: Connection[Any],
    *,
    entity_type: str | None,
    areas: Sequence[str] | None,
    min_bundle_size: int,
    uid_allow: set[tuple[str, str]] | None = None,
) -> int:
    """지금 걸린 조건으로 노출 개체가 모두 몇 개인지 센다 — 화면 "N건 중 M건"의 N. 목록이 돌려준 개수(쪽 크기)가 아니라 모수다.

    조건은 목록과 같게 줘야 한다(다르면 모수가 어긋난다). ``uid_allow`` 는 ``None`` 이 필터 없음, 빈 집합이 0건이며, 좁히기 이전을 셀 때는 ``EntityScope.scope_allow``,
    이후를 셀 때는 ``EntityScope.uid_allow`` 를 준다.
    """
    return count_entities(
        conn,
        entity_type=entity_type,
        area_names=list(areas) if areas else None,
        min_bundle_size=min_bundle_size,
        uid_allow=uid_allow,
    )


def entity_cursor_scope(
    *, q: str | None, refine: str | None, entity_type: str | None,
    areas: Sequence[str] | None, min_bundle_size: int,
) -> str:
    """이번 결과 집합을 정의하는 조건 전부를 문자열 하나로 모은다(커서 조건 지문 재료). 코어는 이 문자열의 지문만 커서에 싣는다.

    넣은 것: ``q`` · ``refine``(결과 집합을 정한다) · ``entity_type`` · ``areas``(정렬해 담는다) · ``min_bundle_size``. 우선 티어는 ``q``/``refine`` 에서 파생되므로 넣지 않는다.
    뺀 것: ``limit``(집합을 바꾸지 않는다) · 정렬 이름(코어 커서가 따로 대조한다).
    ⚠️ 조건을 늘리면 여기도 늘린다 — 빠뜨리면 그 조건만 바뀐 커서가 조용히 통과해 자료가 빠진다.
    """
    material = {
        "q": (q or "").strip(),
        "refine": (refine or "").strip(),
        "entity_type": entity_type or "",
        "areas": sorted(str(a) for a in (areas or [])),
        "min_bundle_size": int(min_bundle_size),
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 뜻으로 앞세울 개체 수 — 화면 정책이라 여기서 정한다. 크게 잡으면 뜻 10등처럼 먼 것까지 앞으로 오고, 줄이면 앞자리가 거의 안 바뀐다.
SEMANTIC_FIRST_N = 5


def semantic_first_keys(ranked: tuple[tuple[str, str], ...]) -> set[tuple[str, str]]:
    """뜻 순위 상위 ``SEMANTIC_FIRST_N`` 개를 앞세울 집합으로 고른다(순수). 코어는 "이 안에 들면 티어 1"만 보므로 순위를 SQL 로 옮기지 않고 승급만 시킨다."""
    return set(ranked[:SEMANTIC_FIRST_N])


def name_first_keys(
    *, q: str | None, refine: str | None, keys: set[tuple[str, str]] | None
) -> set[tuple[str, str]]:
    """이름이 정확히 같은 개체를 고른다 — 목록 맨 앞에 세울 대상. 정렬이 구성 자산 수뿐이라 큰 개체가 늘 위로 오는 것을 바로잡는다.

    표기 비교는 코어 정본(``normalize_text_key``)으로 한다(``entity_uid`` 가 그 함수로 만든 키다). 부분 일치 · 낱말 단위 일치(`숭례문 화재`)는 앞세우지 않는다.
    ``q`` · ``refine`` 어느 쪽에 쳤든 함께 본다. ``keys`` 가 ``None``(검색 없음)이면 우선 대상도 없다 — 종전 순서 그대로다.
    """
    if keys is None:
        return set()
    wanted = {normalize_text_key(text) for text in (q, refine) if text and text.strip()}
    wanted.discard("")
    if not wanted:
        return set()
    return {(etype, uid) for etype, uid in keys if uid in wanted}


# 카드 한 장을 zip 으로 내보낼 때의 자산 수 상한.
CARD_BUNDLE_MAX_ASSETS = 200

# 좁힌 대상 전부를 zip 으로 받을 때의 용량 상한 — 자산 크기가 제각각이라 건수가 아니라 용량으로 막는다.
ENTITIES_BUNDLE_MAX_BYTES = 500 * 1024 * 1024


def decode_entity_cursor(token: str, *, scope: str) -> tuple[int, int, str, str]:
    """개체 목록 커서(책갈피)를 풀어 ``(우선 티어, 구성 자산 수, 표기 키, 종류)`` 로 돌려준다.

    정렬 이름 · 정렬값 개수 · 조건 지문을 코어가 함께 검사한다 — 빼면 파일 목록에서 받은 커서나 조건이 바뀐 커서가 조용히 통과해 엉뚱한 자리에서 이어진다.
    깨졌거나 어긋나면 ``CursorError``(호출부가 400).
    """
    values = decode_cursor(token, expect_sort=ENTITY_CURSOR_SORT,
                           expect_arity=ENTITY_CURSOR_ARITY, expect_scope=scope)
    raw_tier, raw_count, raw_uid, raw_type = values[0], values[1], values[2], values[3]
    try:
        # 수가 아닌 값(위조 토큰의 "abc" · None)이 SQL 로 흘러가면 500 이 되므로 문 앞에서 CursorError(400)로 바꾼다.
        after_tier, after_count = int(raw_tier), int(raw_count)
    except (TypeError, ValueError) as exc:
        raise CursorError(f"커서의 정렬 자리가 숫자가 아니다: {(raw_tier, raw_count)!r}") from exc
    if not isinstance(raw_uid, str) or not raw_uid:
        raise CursorError(f"커서의 표기 키가 비었거나 문자열이 아니다: {raw_uid!r}")
    # 종류도 같은 강도로 본다 — 빈 문자열이 SQL 로 가면 같은 쪽을 다시 낸다.
    if not isinstance(raw_type, str) or not raw_type:
        raise CursorError(f"커서의 종류가 비었거나 문자열이 아니다: {raw_type!r}")
    return after_tier, after_count, raw_uid, raw_type


def next_entity_cursor(
    rows: Sequence[Mapping[str, Any]], *, page_size: int, scope: str,
    uid_first: set[tuple[str, str]] | None = None,
    uid_semantic: set[tuple[str, str]] | None = None,
) -> str | None:
    """이번 쪽의 마지막 행으로 다음 책갈피를 만든다. 이번 쪽이 꽉 찼을 때만 주고 덜 찼으면 ``None``(화면이 빈 쪽을 받으러 가지 않게).

    ``rows`` 는 DB 가 준 쪽 그대로여야 한다(파이썬이 다시 거른 행으로 만들면 걸러진 꼬리를 다음 쪽이 건너뛴다). 커서에는 정렬 자리 넷과 조건 지문이 담긴다 —
    종류까지 싣는 것은 자연키가 (종류, 표기) 둘이기 때문이다. ``uid_semantic`` · ``uid_first`` 는 목록 질의에 준 것과 같은 값이어야 한다(다르면 티어가 어긋난다).
    """
    if not rows or len(rows) != int(page_size):
        return None
    last = rows[-1]
    # 우선 티어는 SQL 의 CASE 와 같은 순서 · 같은 값으로 되짚는다(갈라지면 다음 쪽이 엉뚱한 자리에서 이어진다). 이름(2)이 뜻(1)보다 앞선다.
    key = (str(last["entity_type"]), str(last["entity_uid"]))
    if uid_first and key in uid_first:
        tier = 2
    elif uid_semantic and key in uid_semantic:
        tier = 1
    else:
        tier = 0
    return encode_cursor(
        ENTITY_CURSOR_SORT,
        [tier, int(last["confirmed_count"]), str(last["entity_uid"]), str(last["entity_type"])],
        scope=scope)


class EntitySearchUnavailable(RuntimeError):
    """개체 집합 판정을 할 수 없다 — 호출부가 503 으로 바꾼다. 엔진이 죽었을 때 전체(필터 없음)나 빈 결과로 되돌리면 사용자를 속이므로 끊는다(파일 검색과 같은 규율)."""


class EntityScope(NamedTuple):
    """이번 요청이 볼 개체 집합(찾아오기 · 좁히기 판정 결과).

    uid_allow: 결과 집합 ``A ∩ B``. ``None`` = 필터 없음(전체), 빈 집합 = 0건 — 반드시 ``is None`` 으로 가른다.
    scope_allow: 좁히기 이전 집합 ``A``(``q`` 만 적용 · 없으면 ``None``).
    refined: 좁히기가 걸렸는지(아니면 두 집합이 같아 총계를 한 번만 센다).
    text_keys: 글자로 걸린 개체 키(질의가 둘이면 교집합). semantic_ranked: 뜻 갈래의 코사인 내림차순 키(질의가 둘이면 ``q`` 쪽). semantic_keys: 뜻으로 걸린 키(질의가 둘이면 합집합).
    """

    uid_allow: set[tuple[str, str]] | None
    scope_allow: set[tuple[str, str]] | None
    refined: bool
    # 기본값: 이 둘은 표시용 부가 정보라 집합만 필요한 호출부가 세 값만으로 만들 수 있어야 한다. 비어 있으면 "근거를 모른다"이고 ``match_reason`` 은 ``None`` 이다.
    text_keys: frozenset[tuple[str, str]] = frozenset()
    semantic_keys: frozenset[tuple[str, str]] = frozenset()
    semantic_ranked: tuple[tuple[str, str], ...] = ()


def search_and_refine(*, q: str | None, refine: str | None) -> EntityScope:
    """찾아오기(``q``)와 좁히기(``refine``)를 한 구조로 판정해 볼 개체 집합을 정한다.

        q 있으면 → 엔진 판정 → 집합 A / refine 있으면 → 엔진 판정 → 집합 B / 결과 = A ∩ B(한쪽만 있으면 그것만 · 둘 다 없으면 None = 전체)

    둘이 한 경로라 한쪽만 고쳐지는 사고가 없고, 정렬이 언제나 DB(구성 자산 수)라 커서가 ``q`` 유무와 무관하게 성립한다. refine 은 질의가 아니라 집합 필터라
    좁힌 결과는 항상 좁히기 전 결과의 부분집합이다. 집합이 바뀌면 커서의 조건 지문이 달라져 옛 커서는 400 이다(``decode_entity_cursor``).

    「걸린 이유」도 함께 싣는다 — 뜻으로 걸린 개체는 카드에 검색어가 없어 근거가 없으면 사용자가 검색을 의심한다. 질의가 둘일 때 합침은 보수적이다:
    글자 일치(``text_keys``)는 모든 질의에서 글자로 걸려야 하므로 교집합, 뜻(``semantic_keys``)은 어느 쪽이든 설명이 필요하므로 합집합.

    ``EntitySearchUnavailable``: 집합을 만들 수 없을 때(엔진 · 임베딩 연결 실패 · 되돌림 백엔드). 전체로도 빈 결과로도 되돌리지 않는다.
    """
    q_match = _matching_keys(q) if (q and q.strip()) else None
    refine_match = _matching_keys(refine) if (refine and refine.strip()) else None
    text_keys, semantic_keys = _merge_match_reasons(q_match, refine_match)
    # 순서의 주인은 ``q`` 다(좁히기는 거르기). ``q`` 가 없으면 좁히기 쪽 순위를 쓴다.
    ranked = (q_match or refine_match).semantic_ranked if (q_match or refine_match) else ()
    # 걸러진 뒤에도 살아남은 것만 앞세운다 — 화면에 없는 것을 앞세우면 티어가 헛돈다.

    if refine_match is None:
        # 좁히기가 없으면 결과 집합과 좁히기 이전 집합이 **같은 값**이다(총계도 한 번만 센다).
        keys = None if q_match is None else set(q_match.keys)
        return EntityScope(uid_allow=keys, scope_allow=keys, refined=False,
                           text_keys=text_keys, semantic_keys=semantic_keys,
                           semantic_ranked=ranked)
    if q_match is None:
        # 찾아온 적이 없으니 "지우면 몇 건"의 답은 조건 없는 목록 전체다 → scope 는 None(전체).
        return EntityScope(uid_allow=set(refine_match.keys), scope_allow=None, refined=True,
                           text_keys=text_keys, semantic_keys=semantic_keys,
                           semantic_ranked=tuple(k for k in ranked if k in refine_match.keys))
    allow = set(q_match.keys & refine_match.keys)
    return EntityScope(uid_allow=allow,
                       scope_allow=set(q_match.keys), refined=True,
                       text_keys=text_keys, semantic_keys=semantic_keys,
                       semantic_ranked=tuple(k for k in ranked if k in allow))


def _merge_match_reasons(
    q_match: EntityMatchSet | None, refine_match: EntityMatchSet | None
) -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """질의 둘의 「걸린 이유」를 항목 표시용 한 쌍으로 합친다(순수). 글자는 교집합, 뜻은 합집합 — ``search_and_refine`` 참고."""
    parts = [m for m in (q_match, refine_match) if m is not None]
    if not parts:
        return frozenset(), frozenset()
    text = parts[0].text_keys
    for part in parts[1:]:
        text &= part.text_keys
    semantic = frozenset().union(*(part.semantic_keys for part in parts))
    return text, semantic


def attach_match_reason(
    items: Sequence[Mapping[str, Any]], *, scope: EntityScope
) -> list[dict[str, Any]]:
    """검색 결과 항목에 왜 걸렸는지를 얹는다(순수 · 검색 경로 전용). 뜻(kNN)으로 걸린 결과는 카드 어디에도 검색어가 없어 근거를 같이 보여 줘야 한다.

    불린(``by_text`` · ``by_semantic``)이 실질이고 ``match_reason`` 은 표시용 문구다(코어 상수 ``REASON_TEXT_MATCH`` · ``REASON_SEMANTIC``). 검색어도 좁히기도 없는 목록에는 싣지 않는다.
    갈래 어디에도 없는 항목은 불린 둘이 거짓이고 ``match_reason`` 이 ``None`` 이다(거짓 문구를 적지 않는다 — 판정이 정상이면 나오지 않으며 나오면 경고 로그).
    """
    out = [dict(item) for item in items]
    if scope.uid_allow is None:
        return out
    unexplained = 0
    for item in out:
        key = (str(item.get("entity_type") or ""), str(item.get("entity_uid") or ""))
        by_text = key in scope.text_keys
        by_semantic = key in scope.semantic_keys
        if not (by_text or by_semantic):
            unexplained += 1
        item["by_text"] = by_text
        item["by_semantic"] = by_semantic
        # 둘 다면 글자를 앞세운다 — 더 확실한 근거이고 화면에서 눈으로 확인할 수 있다.
        item["match_reason"] = (REASON_TEXT_MATCH if by_text
                                else REASON_SEMANTIC if by_semantic else None)
    if unexplained:
        _LOG.warning("검색 결과 %d건의 걸린 이유를 알 수 없다 — 판정 갈래가 항목까지 오지 않았다",
                     unexplained)
    return out


def _matching_keys(query: str) -> EntityMatchSet:
    """낱말 묶음을 엔진에 던져 매칭 개체 키 집합과 걸린 갈래(``EntityMatchSet``)를 받는다. 판정은 전부 코어(``match_entity_keys``)가 하고 여기서는 되돌림 백엔드 차단 · 질의 임베딩 · 연결 실패 변환만 한다.

    매칭이 없으면 ``keys`` 가 빈 집합이다(0건이며 전체가 아니다). 빈 질의는 호출부가 이미 걸렀다. 되돌림 백엔드이거나 임베딩 · 엔진에 못 닿으면 ``EntitySearchUnavailable``.
    """
    backend = get_current_settings().mm_meta.search_backend
    if backend != ENTITY_SEARCH_BACKEND:
        _LOG.warning("개체 집합 판정 불가 — 되돌림 백엔드(search_backend=%r)에는 집합 경로가 없다",
                     backend)
        raise EntitySearchUnavailable(
            f"개체 검색을 쓸 수 없습니다 — 되돌림 백엔드(MM_META_SEARCH_BACKEND={backend})는"
            " 집합 판정을 지원하지 않습니다")

    # 채널을 반드시 넘긴다 — 개체 벡터는 활성 채널로 만들었고, 다른 모델의 벡터를 견주면 유사도가 뜻을 잃는다.
    try:
        with stage("embed"):
            query_vector = embed_query_for_media_search(query, channel=active_embed_channel())
    except (RuntimeError, ValueError) as exc:
        # 임베딩 서버 장애와 엔진 장애는 **다른 원인**이라 문구를 나눈다(뭉개면 운영자가 엉뚱한 곳을 본다).
        _LOG.warning("개체 질의 임베딩 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise EntitySearchUnavailable("임베딩 서버에 연결할 수 없습니다") from exc

    from service.api.search_health import get_client

    try:
        with stage("engine"):
            return match_entity_keys(
                get_client(), search_constants.ENTITY_INDEX_DEFAULT,
                query=query, query_vector=query_vector,
            )
    except _OS_CONN_ERRORS as exc:
        _LOG.warning("개체 집합 판정 — 검색 엔진 연결 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise EntitySearchUnavailable("검색 엔진에 연결할 수 없습니다") from exc


# ── 카드 ─────────────────────────────────────────────────────────────────────


def fetch_card(
    conn: Connection[Any], *, entity_type: str, entity_uid: str
) -> dict[str, Any] | None:
    """개체 카드 — 모달리티별 구성 자산 + 자산별 형식 라벨 + 형식 집계(자산 id 를 모아 한 번에 읽는다).

    코어 묶음 반환 + ``confirmed_count`` 별칭 + 자산별 ``forms`` + ``form_counts``. 개체가 없으면 ``None``(404), 개체는 있고 자산이 0건이면 ``total`` 이 0 이다.
    """
    bundle = mm_meta_bundle(conn, entity_type=entity_type, entity_uid=entity_uid)
    if bundle is None:
        return None
    card = dict(bundle)
    # 화면 문구가 "확인된 N건" 이라 키 이름으로도 그 뜻을 못 박는다("전체 N건" 오해를 줄인다).
    card["confirmed_count"] = card.get("total", 0)

    asset_ids: list[str] = []
    for group in card.get("modalities") or []:
        for item in group.get("assets") or []:
            aid = str(item.get("asset_id") or "")
            if aid and aid not in asset_ids:
                asset_ids.append(aid)

    labels = label_names_of_assets(conn, asset_ids, skill_codes=list(form_skill_codes()))
    for group in card.get("modalities") or []:
        for item in group.get("assets") or []:
            item["forms"] = labels.get(str(item.get("asset_id") or ""), [])
    card["form_counts"] = _form_counts(labels)
    return card


def _form_counts(labels: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    """카드 머리의 형식 집계 — 어떤 성격의 자료가 몇 건인지. 코어 ``aggregate_facets`` 규칙(자산 하나가 단위라 자산이 라벨 여럿이면 합이 자산 수보다 클 수 있다), 건수 내림차순 → 이름 오름차순."""
    rows = [{"asset_id": aid, "forms": list(names)} for aid, names in labels.items()]
    out = aggregate_facets(
        rows,
        keys_of=lambda r: ((n, n) for n in r["forms"]),
        unit_of=lambda r: str(r["asset_id"]),
        label_of=lambda originals: originals[0],
    )
    return [{"name": item["label"], "count": item["count"]} for item in out["items"]]


# ── 좁히기 칩(종류·갈래) ──────────────────────────────────────────────────────


def fetch_facets(
    conn: Connection[Any],
    *,
    entity_type: str | None,
    areas: Sequence[str] | None,
    min_bundle_size: int,
) -> dict[str, Any]:
    """좁히기 축 둘의 건수 — 종류(타입)와 갈래(개체 라벨). 둘 다 개체에 붙어 있어 좁혀도 개체가 쪼개지지 않는다.

    종류 칩(단일 선택)은 조건을 적용하지 않고 센다(갈아타는 축이라 "이 종류로 갈아타면 몇 개"가 필요하다). 갈래 칩(다중 · AND)은 고른 종류와 갈래를 적용한 뒤 센다(누르면 나올 수).
    ``min_bundle_size`` 는 목록과 같은 값을 준다. ``{vocab, types, areas, min_members, scoped_by}`` — ``types`` 는 어휘 순서 그대로이고 개체가 없는 종류도 0 으로 담으며, 0건 갈래도 담는다(커버리지 갭 신호).
    """
    picked = [str(a) for a in (areas or []) if str(a).strip()]
    head = _fetch_type_vocab_head(conn)
    type_counts = count_entities_by_type(conn, min_bundle_size=min_bundle_size)
    area_rows = count_entities_by_area(
        conn, entity_type=entity_type, area_names=picked or None,
        min_bundle_size=min_bundle_size,
    )
    return {
        "vocab": head["vocab"],
        "types": [
            {
                "name": t["name"],
                "definition": t["definition"],
                "boundary": t["boundary"],
                "entities": type_counts.get(t["name"], 0),
            }
            for t in head["types"]
        ],
        "areas": [
            {"name": r["name"], "skill": r["skill"], "skill_code": r["skill_code"],
             "entities": r["count"]}
            for r in area_rows
        ],
        "min_members": min_bundle_size,
        "scoped_by": {"entity_type": entity_type, "areas": picked},
    }


def _fetch_type_vocab_head(conn: Connection[Any]) -> dict[str, Any]:
    """타입 어휘의 머리(이름 · 판)와 정의문 목록을 읽는다(화면용 단순 조회). 문구의 정본은 DB 행이다. 어휘 행이 없으면 ``vocab`` 은 ``None``, ``types`` 는 빈 목록이다."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_TYPE_VOCAB_SQL)
        row = cur.fetchone()
    if row is None:
        return {"vocab": None, "types": []}
    types = [
        {
            "name": str(t.get("name") or ""),
            "definition": str(t.get("definition") or ""),
            # 어휘 JSON 의 키는 `not` 이다(파이썬 예약어라 응답에서는 boundary 로 바꾼다).
            "boundary": str(t.get("not") or ""),
        }
        for t in (row["types"] or [])
    ]
    return {"vocab": {"name": str(row["name"]), "version": int(row["version"])}, "types": types}


# ── 다운로드 ─────────────────────────────────────────────────────────────────


def card_zip_targets(
    conn: Connection[Any], *, entity_type: str, entity_uid: str
) -> tuple[list[dict[str, Any]], str, bool] | None:
    """카드 한 장의 구성 자산을 zip 대상으로 모은다. ``(대상, 개체 이름, 잘렸는지)`` — 개체가 없으면 ``None``.

    코어 묶음이 준 구성 자산 중 경로 조회를 통과하는(등록 완료) 것만 담고, 상한을 넘으면 앞에서부터 잘라 잘렸음을 알린다.
    """
    bundle = mm_meta_bundle(conn, entity_type=entity_type, entity_uid=entity_uid)
    if bundle is None:
        return None
    asset_ids: list[str] = []
    # 🔴 키 이름은 ``assets`` 다 — 틀리면 조용히 빈 목록이 되어 "받았는데 비었다"가 된다.
    for group in bundle.get("modalities") or []:
        for item in group.get("assets") or []:
            aid = str(item.get("asset_id") or "")
            if aid and aid not in asset_ids:
                asset_ids.append(aid)
    truncated = len(asset_ids) > CARD_BUNDLE_MAX_ASSETS
    asset_ids = asset_ids[:CARD_BUNDLE_MAX_ASSETS]
    paths = fetch_asset_paths(conn, asset_ids)
    targets = [
        {"asset_id": aid, "fs_path": paths[aid], "file_name": display_file_name(paths[aid])}
        for aid in asset_ids
        if aid in paths
    ]
    return targets, str(bundle.get("name") or entity_uid), truncated


def entities_zip_rows(
    conn: Connection[Any],
    *,
    entity_type: str | None,
    areas: Sequence[str] | None,
    min_bundle_size: int,
    exclude_video: bool,
) -> list[dict[str, Any]]:
    """좁힌 개체들의 구성 자산을 zip 대상으로 모은다(용량 판정용 크기 포함, 자산 id 순). 화면의 좁히기 축(종류 · 갈래)과 같은 축을 쓴다."""
    rows = assets_of_entities(
        conn, entity_type=entity_type, area_names=list(areas) if areas else None,
        min_bundle_size=min_bundle_size, statuses=None, exclude_video=exclude_video,
    )
    return [
        {"asset_id": r["asset_id"], "modality": r["modality"],
         "file_name": display_file_name(r["fs_path"]), "file_size": r["file_size"],
         "fs_path": r["fs_path"]}
        for r in rows
    ]


def ascii_zip_name(parts: Sequence[str], *, fallback: str, count: int) -> str:
    """zip 파일명을 ASCII 로만 만든다(한글은 헤더에서 깨진다). ``<이름>_<N>files.zip`` — 남는 글자가 없으면 ``fallback``."""
    safe = "".join(c for c in "_".join(parts) if c.isascii() and c.isalnum())
    return f"{safe or fallback}_{count}files.zip"
