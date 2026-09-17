"""개체(멀티모달 메타) 화면 **정형 계층** — 코어가 준 사실을 화면 응답 모양으로 바꾼다.

**흐름에서의 위치**: 라우트(`service/api/routes_mm_meta.py`)는 파라미터 검증·HTTP 코드·헤더만 하고, 무엇을
읽을지는 코어 seam 이 하고, 그 사이의 **조립**을 이 모듈이 한다. 그래서 이 파일에는 SQL 이 거의 없다 —
예외는 타입 어휘 머리 한 줄(아래 이유 참조)뿐이다.

**왜 이렇게 나누나**(093 책무 경계 · 세 질문):
    - "어디서 읽어도 같은 답이어야 하는 것"과 "잘못 짜면 조용히 틀리는 그래프 읽기"는 **코어**에 있다 —
      노출 개체의 정의(소속 엣지·상태·중복 제거·묶음 크기 하한), 라벨 이름의 정의 순서, 세는 규칙.
    - "프론트가 바뀌면 함께 바뀌는 것"은 **여기**다 — 응답 키 이름, 근거 키워드를 몇 개만 보일지,
      카드에 무엇을 얹을지, 다운로드 상한과 오류 문구.

**여기 있는 상수는 화면·다운로드 정책이다.** 값의 근거를 주석에 남긴다 — 근거 없는 숫자는 다음 사람이
못 고친다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from psycopg import Connection
from psycopg.rows import dict_row

from service.portal.download import fetch_asset_paths
from src.config import search_constants
from src.config.filename_util import display_file_name
from src.config.settings import active_embed_channel, get_current_settings
from src.mm_classify.read import label_names_of_assets
from src.relations.graph_query import (
    assets_of_entities,
    count_entities,
    count_entities_by_area,
    count_entities_by_type,
    list_entities,
    mm_meta_bundle,
)
from src.search.cursor import CursorError, decode_cursor, encode_cursor
from src.search.entity_search_os import match_entity_keys
from src.search.facets import aggregate_facets
from src.search.query_embed import embed_query_for_media_search

# ── 화면 정책 상수 ────────────────────────────────────────────────────────────

# 카드에 보일 근거 키워드 수. 개체 하나에 근거가 수십 개 붙을 수 있어 전부 보이면 카드가 글자로 찬다.
KEYWORD_TOP_N = 6

# 형식 축(085 자산 라벨)으로 보일 스킬. **하나로 고정한 것이 기본이다** — 여러 스킬 라벨을 한 목록에
# 섞으면 어느 축의 라벨인지 화면에서 구분되지 않는다(축을 나눠 보이려면 그때 화면 설계가 필요하다).
# 환경변수로 바꿀 수 있게 둔 이유: 스킬을 늘렸을 때 배포 없이 형식 축을 갈아탈 수 있어야 한다.
DEFAULT_FORM_SKILL_CODES: tuple[str, ...] = ("content_form",)
FORM_SKILLS_ENV = "PORTAL_MM_META_FORM_SKILLS"

# 개체 검색·좁히기가 **반드시 거쳐야 하는 백엔드**. 099 G5 부터 두 일이 한 구조로 접혀(spec §3-2a)
# 엔진에 낱말을 던져 **개체 키 집합**을 얻는 경로 하나만 남았다. 되돌림 값(`pg`)은 090 의 PG 벡터
# 경로였는데, 그 경로는 "상위 몇 개"(순위)만 낼 수 있어 집합을 만들지 못한다 — 설정이 그 값이면
# 조용히 엔진으로 가지도(설정 무시), 전체를 주지도(검색했는데 전부) 않고 **503 으로 끊는다**.
ENTITY_SEARCH_BACKEND = "opensearch"

# 개체 목록 커서(책갈피)의 **정렬 이름**. 097 파일 커서와 같은 토큰 규약(`src/search/cursor.py`)을 쓰되
# 이름이 달라야 한다 — 파일 목록에서 받은 책갈피를 개체 목록에 쓰면 엉뚱한 자리에서 조용히 이어진다.
# 값은 실제 정렬(`confirmed_count DESC, entity_uid ASC`)을 그대로 읽은 것이다.
ENTITY_CURSOR_SORT = "confirmed_count_desc"
# 그 정렬이 쓰는 **정렬값 개수** — `(confirmed_count, entity_uid)` 둘이다. 구버전·위조 토큰이 반쪽
# 책갈피로 들어오면 첫 쪽을 다시 읽어 **중복**이 나는데 오류가 없어 화면은 그것을 알 수 없다.
ENTITY_CURSOR_ARITY = 2

# 카드 한 장을 zip 으로 내보낼 때의 자산 수 상한. 묶음이 커도 응답이 무한정 커지지 않게 막는다.
CARD_BUNDLE_MAX_ASSETS = 200

# 좁힌 대상 전부를 zip 으로 받을 때의 용량 상한. **건수가 아니라 용량**으로 막는 이유: 자산 하나가
# 영상 수십 MB 에서 텍스트 수십 KB 까지라 건수로는 예측이 안 된다. 브라우저가 메모리에 담는 구조라
# 이 선을 넘으면 탭이 버틴다는 보장이 없다.
ENTITIES_BUNDLE_MAX_BYTES = 500 * 1024 * 1024

_LOG = logging.getLogger(__name__)

# 검색 엔진 **연결** 실패로 볼 예외들. 라우트(`routes_file_search.py`)와 같은 방어적 import 를 쓴다 —
# 클라이언트가 안 깔린 환경(순수 단위 테스트)에서는 빈 튜플이라 ``except ()`` 가 아무것도 잡지 않는다.
# 🔴 연결 실패만 골라 잡는다. 나머지 예외(코드 결함)까지 삼키면 결함이 "엔진 장애"로 둔갑해 운영자가
#    엉뚱한 곳을 본다(2026-09-09 리뷰에서 파일 검색이 같은 이유로 정리됐다).
try:
    from opensearchpy.exceptions import ConnectionError as _OSConnectionError
except ImportError:  # pragma: no cover - 라이브러리 미설치 환경 방어
    _OSConnectionError = None  # type: ignore[assignment,misc]

_OS_CONN_ERRORS: tuple[type[BaseException], ...] = (
    (_OSConnectionError,) if _OSConnectionError is not None else ()
)

# 타입 어휘 머리(이름·판) + 정의문. **화면용 단순 조회라 여기 둔다**(093 규칙 ④) — 그래프를 읽지 않고,
# 파이프는 이 값을 쓰지 않는다. 🔴 **어휘 행이 없으면 빈 목록**이며 코드 프리셋으로 채우지 않는다.
# 판정 경로는 폴백하지만(배치가 죽으면 안 된다) 화면이 폴백하면 "등록 안 했는데 왜 보이나"를 조사하게 된다.
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
    """코어 목록 행 → 화면 목록 항목(순수).

    코어는 근거 키워드를 **원문 전부** 준다. 화면은 그중 **짧고 대표적인 것부터** 몇 개만 보인다 —
    ``제주도`` 가 ``제주도 포토스팟`` 보다 개체를 잘 대표하기 때문이다. 그 판단이 화면 몫이라 여기 있다.

    ``node_id`` 는 응답에 싣지 않는다 — 내부 식별자이고 화면이 쓸 일이 없다(개체는
    ``(entity_type, entity_uid)`` 로 가리킨다).

    Args:
        row: ``graph_query.list_entities`` 가 준 행. 원본을 바꾸지 않는다.

    Returns:
        응답 항목 dict. ``confirmed_count`` 는 화면에서 **"확인된 N건"** 으로 표기하고,
        ``total_count`` 는 필터와 무관한 전량이라 둘이 다를 때만 함께 보인다.
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
    after_count: int | None = None,
    after_uid: str | None = None,
    uid_allow: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """노출 개체 목록을 읽어 화면 항목으로 정형한다(선택: 책갈피부터 이어 읽기 · 검색 집합 안에서만).

    Args:
        conn: DB 커넥션.
        entity_type: 종류 필터. ``None`` 이면 전체.
        areas: 갈래 이름들 — 모두 가진 개체만(AND). ``None``·빈 목록이면 필터 없음.
        min_bundle_size: 노출 임계(구성 자산 수 하한).
        limit: 이 **쪽**에 담을 개체 수(커서가 생긴 뒤의 뜻 — 전체 상한이 아니다).
        after_count: 이어읽기 책갈피 — 직전 쪽 마지막 개체의 ``confirmed_count``. ``None`` 이면 첫 쪽.
        after_uid: 이어읽기 책갈피의 ``entity_uid``(동점 무더기를 가르는 유일 키).
            ``after_count`` 와 **둘 다** 주거나 둘 다 생략한다(반쪽이면 코어가 ``ValueError``).
        uid_allow: 찾아오기·좁히기가 정한 개체 화이트리스트(``search_and_refine`` 의 결과).
            🔴 ``None`` = **필터 없음(전체)** · 빈 집합 = **0건**. 둘을 섞으면 "검색했는데 전체가
            나오는" 조용한 오류가 된다.

    Returns:
        정형된 목록(구성 자산 수 내림차순 → 표기 키 오름차순).
    """
    rows = list_entities(
        conn,
        entity_type=entity_type,
        area_names=list(areas) if areas else None,
        min_bundle_size=min_bundle_size,
        limit=limit,
        form_skill_codes=list(form_skill_codes()),
        after_count=after_count,
        after_uid=after_uid,
        uid_allow=uid_allow,
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
    """지금 걸린 조건으로 **노출 개체가 모두 몇 개인지** 센다 — 화면의 "N건 중 M건"에서 N.

    🔴 목록(``fetch_list``)이 돌려준 개수를 세면 모수가 아니라 **쪽 크기**가 나온다. 200개만 받아 놓고
    "200건"이라 적던 것이 그 오해였다(실측 노출 대상 822개). 서랍에서 서류 200장을 꺼내 놓고
    "서랍에 200장 있다"고 말하는 셈이다 — 서랍은 따로 세어야 한다.

    ⚠️ 조건은 목록과 **같게** 줘야 한다. 다르면 "822건 중 200건"의 822 가 목록과 다른 모수를 말한다.

    Args:
        conn: DB 커넥션.
        entity_type: 종류 필터. ``None`` 이면 전체(목록과 같은 값).
        areas: 갈래 이름들 — 모두 가진 개체만(AND). ``None``·빈 목록이면 필터 없음(목록과 같은 값).
        min_bundle_size: 노출 임계(구성 자산 수 하한 · 목록과 같은 값).
        uid_allow: 개체 화이트리스트 — **목록과 같은 값**을 줘야 한다(다르면 "822건 중 3건"의 822 가
            목록과 다른 모수를 말한다). 🔴 ``None`` = 필터 없음 · 빈 집합 = 0건.
            ``scope_total``(좁히기 이전)을 셀 때는 ``EntityScope.scope_allow`` 를, ``total``
            (좁히기 이후)을 셀 때는 ``EntityScope.uid_allow`` 를 준다.

    Returns:
        개체 수(0 이상). 상한·쪽 크기에 걸리지 않는 모수다.
    """
    return count_entities(
        conn,
        entity_type=entity_type,
        area_names=list(areas) if areas else None,
        min_bundle_size=min_bundle_size,
        uid_allow=uid_allow,
    )


def decode_entity_cursor(token: str) -> tuple[int, str]:
    """개체 목록 커서(책갈피)를 풀어 ``(구성 자산 수, 표기 키)`` 로 돌려준다.

    풀이: 커서는 "여기까지 읽었다"를 적어 둔 **책갈피**다. 책 페이지 번호(offset)와 달리 앞쪽에 줄이
    끼어들어도 자리가 밀리지 않는다 — 어느 줄 **다음**인지를 적어 두기 때문이다.

    🔴 정렬 이름·정렬값 개수를 코어가 함께 검사한다(``expect_sort``·``expect_arity``). 검사를 빼면
    파일 목록에서 받은 책갈피나 구버전 토큰이 조용히 통과해 **엉뚱한 자리에서 이어진다** — 사용자는
    목록이 틀린 줄 모르고 나중에 "그 개체가 왜 없지"로 나타난다.

    Args:
        token: 직전 응답의 ``next_cursor`` 문자열.

    Returns:
        ``(after_count, after_uid)`` — ``fetch_list`` 에 그대로 넘길 책갈피 두 값.

    Raises:
        CursorError: 토큰이 깨졌거나 · 정렬이 어긋나거나 · 정렬값 개수·타입이 다를 때
            (호출부가 **400** 으로 바꾼다 — 서버 오류가 아니라 입력 오류다).
    """
    values = decode_cursor(token, expect_sort=ENTITY_CURSOR_SORT, expect_arity=ENTITY_CURSOR_ARITY)
    raw_count, raw_uid = values[0], values[1]
    try:
        # 수가 아닌 값(위조 토큰의 ``"abc"``·``None``)이 그대로 SQL 로 흘러가면 DB 오류 → HTTP 500 이
        # 된다. 문 앞에서 CursorError 로 바꿔 400 으로 나가게 한다.
        after_count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise CursorError(f"커서의 구성 자산 수가 숫자가 아니다: {raw_count!r}") from exc
    if not isinstance(raw_uid, str) or not raw_uid:
        raise CursorError(f"커서의 표기 키가 비었거나 문자열이 아니다: {raw_uid!r}")
    return after_count, raw_uid


def next_entity_cursor(rows: Sequence[Mapping[str, Any]], *, page_size: int) -> str | None:
    """이번 쪽의 마지막 행으로 **다음 책갈피**를 만든다(마지막 쪽이면 ``None``).

    🔴 **이번 쪽이 꽉 찼을 때만** 준다 — 덜 찼으면 더 없다는 뜻이라 ``None`` 을 준다. 그래야 화면이
    빈 쪽을 한 번 더 받으러 가지 않는다(097 파일 목록과 같은 규율).

    ⚠️ 여기 넘기는 것은 **DB 가 준 쪽 그대로**여야 한다. 응답 직전에 파이썬이 한 번 더 거른 행으로
    만들면, 걸러진 꼬리 행들을 다음 쪽이 건너뛴다(누락). 099 G5 부터 찾아오기·좁히기는 **SQL 이**
    하므로(화이트리스트) 이 쪽은 이미 걸러진 결과이고, 파이썬이 다시 거를 일이 없다.

    ⚠️ 커서는 **정렬 자리**(구성 자산 수·표기 키)만 담는다 — 어떤 질의에서 나온 책갈피인지는 모른다.
    그래서 ``q``·``refine`` 이 바뀌면 화면이 커서를 버려야 하고, 서버는 그것을 강제하지 못한다.

    Args:
        rows: 이번 쪽의 목록 행들(정형 전후 무관 · ``confirmed_count``·``entity_uid`` 만 읽는다).
        page_size: 이번 요청의 쪽 크기(``limit``). 행 수가 이 값과 같아야 꽉 찬 쪽이다.

    Returns:
        다음 쪽을 요청할 커서 문자열. 마지막 쪽이면 ``None``.
    """
    if not rows or len(rows) != int(page_size):
        return None
    last = rows[-1]
    return encode_cursor(
        ENTITY_CURSOR_SORT, [int(last["confirmed_count"]), str(last["entity_uid"])])


class EntitySearchUnavailable(RuntimeError):
    """개체 **집합 판정**을 할 수 없다 — 호출부(라우트)가 503 으로 바꾼다.

    🔴 이 예외가 필요한 이유가 이번 설계의 핵심이다. 종전 구조는 엔진이 죽으면 문자열 결과로
    **되돌렸다**. 새 구조에서 같은 되돌림을 하면 화이트리스트가 ``None``(=필터 없음)이 되어
    **"검색했는데 전체 822개가 나온다"** 가 된다. 반대로 빈 집합으로 접으면 "자료가 없다"와
    "검색이 죽었다"가 같아진다. 둘 다 사용자를 속이므로 **끊는 쪽**을 고른다 —
    파일 검색이 엔진 연결 실패를 503 으로 내는 것과 같은 규율(`routes_file_search.py`).
    """


class EntityScope(NamedTuple):
    """이번 요청이 볼 **개체 집합**(찾아오기·좁히기 판정 결과 · spec 099 §3-2a).

    책 찾기에 비유하면, ``scope_allow`` 는 "요리 책장"(찾아온 범위)이고 ``uid_allow`` 는 그 책장에서
    "표지에 배추가 있는 책"(좁힌 결과)이다. 화면의 "N건 중 M건"에서 N 이 앞의 것, M 이 뒤의 것이다.

    Attributes:
        uid_allow: 결과 집합 ``A ∩ B`` 의 개체 키들. 🔴 ``None`` = **필터 없음(전체)** ·
            빈 집합 = **0건**. 파이썬에서는 둘 다 거짓값이라 ``if not uid_allow`` 한 줄이 사고를
            만든다 — 반드시 ``is None`` 으로 가른다.
        scope_allow: 좁히기 **이전** 집합 ``A``(= ``q`` 만 적용). ``q`` 가 없으면 ``None``(전체).
        refined: 좁히기(refine)가 걸렸는지. 걸리지 않았으면 두 집합이 같은 값이라 총계를 한 번만 센다.
    """

    uid_allow: set[tuple[str, str]] | None
    scope_allow: set[tuple[str, str]] | None
    refined: bool


def search_and_refine(*, q: str | None, refine: str | None) -> EntityScope:
    """찾아오기(``q``)와 좁히기(``refine``)를 **한 구조**로 판정해 볼 개체 집합을 정한다(099 G5).

    ```
    q 있으면      → 엔진 집합 판정 → 개체 키 집합 A
    refine 있으면 → 엔진 집합 판정 → 개체 키 집합 B
    결과 집합     = A ∩ B (한쪽만 있으면 그것만 · 둘 다 없으면 None = 전체)
    ```

    **왜 둘이 한 경로인가**(spec §3-2a): 개체 좁히기도 낱말 단위로 맞추기로 하면서(2026-09-17 결정)
    ``q`` 와 refine 이 **같은 일**(엔진에 낱말을 던져 매칭 개체 집합을 얻기)이 됐다. 한 경로로 접으면
    한쪽만 고쳐지는 사고가 원리상 사라지고, 정렬이 언제나 DB(구성 자산 수)라 커서가 ``q`` 유무와
    무관하게 성립한다.

    🔴 **refine 은 질의가 아니라 집합 필터다**(spec §3-1 · FR-002). ``A`` 는 refine 과 무관하게 한 번만
    판정하므로 kNN 게이트가 다시 돌지 않고, 좁힌 결과는 언제나 좁히기 전 결과의 **부분집합**이다
    (없던 개체가 나타나지 않는다 — 091 이 서버 재질의를 거부했던 근거 ③의 해소).

    ⚠️ **집합이 바뀌면 커서(책갈피)는 뜻을 잃는다.** ``q``·refine 을 고치면 화면은 커서를 버리고
    처음부터 받아야 한다 — 서버는 옛 커서인지 알 방법이 없어 강제하지 못한다(계약으로만 정한다).

    Args:
        q: 찾아오기 검색어. ``None``·공백뿐이면 "안 물어봤다"(집합을 만들지 않는다).
        refine: 결과 내 재검색 낱말들. ``None``·공백뿐이면 좁히지 않는다.

    Returns:
        ``EntityScope`` — 목록·총계 질의에 그대로 넘길 화이트리스트 두 개와 좁히기 여부.

    Raises:
        EntitySearchUnavailable: 집합을 만들 수 없을 때(엔진·임베딩 연결 실패 · 되돌림 백엔드).
            🔴 **전체로도 빈 결과로도 되돌리지 않는다** — 위 클래스 설명 참조.
    """
    q_keys = _matching_keys(q) if (q and q.strip()) else None
    refine_keys = _matching_keys(refine) if (refine and refine.strip()) else None
    if refine_keys is None:
        # 좁히기가 없으면 결과 집합과 좁히기 이전 집합이 **같은 값**이다(총계도 한 번만 센다).
        return EntityScope(uid_allow=q_keys, scope_allow=q_keys, refined=False)
    if q_keys is None:
        # 찾아온 적이 없으니 "지우면 몇 건"의 답은 조건 없는 목록 전체다 → scope 는 None(전체).
        return EntityScope(uid_allow=refine_keys, scope_allow=None, refined=True)
    return EntityScope(uid_allow=q_keys & refine_keys, scope_allow=q_keys, refined=True)


def _matching_keys(query: str) -> set[tuple[str, str]]:
    """낱말 하나 묶음을 엔진에 던져 **매칭 개체 키 집합**을 받는다(순위 아님).

    판정은 전부 코어(`match_entity_keys`)가 한다 — 낱말끼리 AND·필드끼리 OR, 그리고 게이트를 넘긴
    의미(kNN) 결과와의 합집합이다. 여기서 하는 일은 셋뿐이다: 되돌림 백엔드 차단, 질의 임베딩,
    그리고 **연결 실패를 뜻이 분명한 예외로 바꾸기**.

    Args:
        query: 낱말들(공백 구분). 빈 값은 호출부가 이미 걸렀다 — 코어는 빈 질의를 ``ValueError``
            로 막는다("0건"과 "안 물어봤다"가 섞이지 않게).

    Returns:
        ``{(entity_type, entity_uid), …}``. 매칭이 없으면 **빈 집합**(= 0건이며 전체가 아니다).

    Raises:
        EntitySearchUnavailable: 되돌림 백엔드이거나 임베딩·엔진에 닿지 못했을 때.
    """
    backend = get_current_settings().mm_meta.search_backend
    if backend != ENTITY_SEARCH_BACKEND:
        _LOG.warning("개체 집합 판정 불가 — 되돌림 백엔드(search_backend=%r)에는 집합 경로가 없다",
                     backend)
        raise EntitySearchUnavailable(
            f"개체 검색을 쓸 수 없습니다 — 되돌림 백엔드(MM_META_SEARCH_BACKEND={backend})는"
            " 집합 판정을 지원하지 않습니다")

    # 🔴 채널을 반드시 넘긴다. 개체 벡터는 배치가 활성 채널로 만들었다 — 다른 모델의 벡터를 견주면
    #    유사도가 뜻을 잃는다(실측: 채널을 빼면 `발효`→훈민정음 0.09 처럼 무관한 결과가 나왔다).
    try:
        query_vector = embed_query_for_media_search(query, channel=active_embed_channel())
    except (RuntimeError, ValueError) as exc:
        # 임베딩 서버 장애와 엔진 장애는 **다른 원인**이라 문구를 나눈다(뭉개면 운영자가 엉뚱한 곳을 본다).
        _LOG.warning("개체 질의 임베딩 실패: %s", exc, exc_info=True)
        raise EntitySearchUnavailable("임베딩 서버에 연결할 수 없습니다") from exc

    from src.search.opensearch_sync import get_client

    try:
        return match_entity_keys(
            get_client(), search_constants.ENTITY_INDEX_DEFAULT,
            query=query, query_vector=query_vector,
        )
    except _OS_CONN_ERRORS as exc:
        _LOG.warning("개체 집합 판정 — 검색 엔진 연결 실패: %s", exc, exc_info=True)
        raise EntitySearchUnavailable("검색 엔진에 연결할 수 없습니다") from exc


# ── 카드 ─────────────────────────────────────────────────────────────────────


def fetch_card(
    conn: Connection[Any], *, entity_type: str, entity_uid: str
) -> dict[str, Any] | None:
    """개체 카드 — 모달리티별 구성 자산 + 자산별 형식 라벨 + 형식 집계.

    묶음만 보면 "송강호 자료 5건"까지만 알 수 있고 인터뷰인지 연기 분석인지는 구별되지 않는다. 자료
    성격은 분류 스킬이 이미 판정해 두었으니 카드가 그것을 함께 보인다 — 축 둘이 교차하는 지점이 이
    기능의 실질 가치다. 자산 id 를 모아 **한 번에** 읽는다(자산마다 묻지 않는다).

    Args:
        conn: DB 커넥션.
        entity_type: 개체 종류(닫힌 어휘). 같은 표기·다른 종류는 별개 개체다.
        entity_uid: 표기 키. 원표기를 줘도 코어가 정규화해 흡수한다.

    Returns:
        코어 묶음 반환 + ``confirmed_count`` 별칭 + 자산별 ``forms`` + ``form_counts``.
        개체 자체가 없으면 ``None``(호출부는 404). 개체는 있고 자산이 0건이면 ``total`` 이 0 이다 —
        "없는 개체"와 "빈 개체"를 가른다.
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
    """카드 머리에 쓸 형식 집계 — 어떤 성격의 자료가 몇 건인지.

    세는 규칙은 코어 정본(``aggregate_facets``)을 쓴다. 단위는 **자산 하나**이고, 자산이 라벨 여럿을
    가질 수 있어 합이 자산 수보다 클 수 있다. 하한·상한을 두지 않는다 — 카드 안 목록이라 짧다.

    Args:
        labels: ``{asset_id: [라벨 이름…]}``.

    Returns:
        ``[{name, count}]`` — 건수 내림차순 → 이름 오름차순.
    """
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
    """좁히기 축 둘의 건수 — 종류(타입)와 갈래(개체 라벨).

    두 축의 공통점이 이 화면 설계의 핵심이다 — **둘 다 개체에 붙어 있다.** 그래서 좁혀도 개체가
    쪼개지지 않는다. 자산 라벨로 좁히던 옛 화면은 한 개체가 갈래마다 나뉘어 보였다.

    🔴 **축의 성격에 따라 세는 범위가 다르다**(spec 087 2차 정정):
        - **종류 칩(단일 선택)** — 아무 조건도 적용하지 않는다. 종류는 좁히는 축이 아니라 **갈아타는
          축**이라 "이 종류로 갈아타면 몇 개"가 필요하다. 갈래 조건을 적용하면 갈아탈 칩이 0건으로
          감춰져 사용자가 막힌다(실제로 겪었다). 화면은 종류를 바꿀 때 갈래 선택을 초기화한다.
        - **갈래 칩(다중 선택·AND)** — 고른 종류와 이미 고른 갈래를 적용한 뒤 센다 → **누르면 나올 수**다.

    Args:
        conn: DB 커넥션.
        entity_type: 지금 고른 종류. ``None`` 이면 종류 조건 없음.
        areas: 지금 고른 갈래들. ``None``·빈 목록이면 갈래 조건 없음.
        min_bundle_size: 노출 임계. 적힌 숫자와 클릭 결과가 같아야 하므로 목록과 **같은 값**을 준다.

    Returns:
        ``{vocab, types, areas, min_members, scoped_by}``. ``types`` 는 어휘에 적힌 **순서 그대로**이고
        개체가 없는 종류도 0 으로 담는다(어휘가 정본). ``areas`` 는 0건 갈래도 담는다 — 0 은 "이 갈래엔
        아직 자료가 없다"는 커버리지 갭 신호라 API 가 지우지 않는다(감추는 것은 화면 몫).
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
    """타입 어휘의 머리(이름·판)와 정의문 목록을 읽는다(화면용 단순 조회 · 093 규칙 ④).

    정의문을 함께 내리는 이유: 화면이 "인물이란 무엇인가"를 보여 줄 수 있어야 사람이 판정을 검증한다.
    문구의 정본은 DB 행이며 코드에 사본을 두지 않는다.

    Args:
        conn: DB 커넥션.

    Returns:
        ``{"vocab": {name, version} | None, "types": [{name, definition, boundary}]}``.
        어휘 행이 없으면 ``vocab`` 은 ``None``, ``types`` 는 빈 목록이다(프리셋으로 채우지 않는다).
    """
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
    """카드 한 장의 구성 자산을 zip 대상으로 모은다.

    노출 기준을 두 겹으로 둔다 — 구성 자산은 코어 묶음이 준 것만(화면에 보이는 것과 같은 목록)이고,
    경로 조회가 등록 자산만 통과시킨다(비노출 자산이 zip 으로 새지 않게).

    Args:
        conn: DB 커넥션.
        entity_type: 개체 종류.
        entity_uid: 표기 키.

    Returns:
        ``(대상 목록, 개체 이름, 잘렸는지)``. 개체가 없으면 ``None``. 상한을 넘으면 **앞에서부터 잘라**
        담고 잘렸음을 함께 알린다(조용히 자르지 않는다).
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
    """좁힌 개체들의 구성 자산을 zip 대상으로 모은다(용량 판정용 크기 포함).

    화면의 좁히기 축과 다운로드 축이 **같아야** "지금 보고 있는 것을 받는다"가 성립한다. 달랐던 것이
    옛 화면의 혼동 원인이었다.

    Args:
        conn: DB 커넥션.
        entity_type: 종류 필터.
        areas: 갈래 이름들(AND).
        min_bundle_size: 노출 임계.
        exclude_video: 참이면 영상을 뺀다(용량이 크게 준다).

    Returns:
        ``[{asset_id, modality, file_name, file_size, fs_path}]`` — 자산 id 순.
    """
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
    """zip 파일명을 ASCII 로만 만든다.

    한글 파일명은 브라우저·운영체제 조합에 따라 헤더에서 깨진다. 개체 이름은 표기 키가 아니라 사람이
    읽는 이름이라 그대로 실으면 특히 위험하다.

    Args:
        parts: 파일명에 넣고 싶은 조각들(종류·갈래·개체 이름 등).
        fallback: 남는 글자가 없을 때 쓸 이름.
        count: 담긴 파일 수(이름 끝에 붙는다).

    Returns:
        ``<이름>_<N>files.zip``.
    """
    safe = "".join(c for c in "_".join(parts) if c.isascii() and c.isalnum())
    return f"{safe or fallback}_{count}files.zip"
