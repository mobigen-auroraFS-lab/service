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
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from service.portal.download import fetch_asset_paths
from src.config import search_constants
from src.config.filename_util import display_file_name
from src.config.settings import active_embed_channel, get_current_settings
from src.mm_classify.read import label_names_of_assets
from src.mm_meta.entity_embedding import find_similar_entities
from src.mm_meta.entity_search import (
    REASON_SEMANTIC,
    REASON_TEXT_MATCH,
    entity_refine_fields,
    fuse_entity_results,
    gate_semantic_hits,
    narrow_entities,
)
from src.relations.graph_query import (
    assets_of_entities,
    count_entities_by_area,
    count_entities_by_type,
    list_entities,
    mm_meta_bundle,
)
from src.search.entity_search_os import search_entities_hybrid
from src.search.facets import aggregate_facets
from src.search.query_embed import embed_query_for_media_search
from src.search.refine import refine_rows

# ── 화면 정책 상수 ────────────────────────────────────────────────────────────

# 카드에 보일 근거 키워드 수. 개체 하나에 근거가 수십 개 붙을 수 있어 전부 보이면 카드가 글자로 찬다.
KEYWORD_TOP_N = 6

# 형식 축(085 자산 라벨)으로 보일 스킬. **하나로 고정한 것이 기본이다** — 여러 스킬 라벨을 한 목록에
# 섞으면 어느 축의 라벨인지 화면에서 구분되지 않는다(축을 나눠 보이려면 그때 화면 설계가 필요하다).
# 환경변수로 바꿀 수 있게 둔 이유: 스킬을 늘렸을 때 배포 없이 형식 축을 갈아탈 수 있어야 한다.
DEFAULT_FORM_SKILL_CODES: tuple[str, ...] = ("content_form",)
FORM_SKILLS_ENV = "PORTAL_MM_META_FORM_SKILLS"

# 의미 검색으로 더할 개체 수(spec 090). **되돌림 경로(`MM_META_SEARCH_BACKEND=pg`) 전용**이다.
# 3 은 측정으로 정한 값이다 — 5 로 늘리면 재현율은 그대로이고 노이즈만 두 배, 1 로 줄이면 재현율이
# 절반으로 떨어졌다. 코어 기본값(5)과 다른 것은 의도된 차이다: 그 값은 OpenSearch 융합 경로에서
# 정답이 4~5위에 있다는 실측으로 정한 것이라 이 경로에는 근거가 없다.
SEMANTIC_TOP_N = 3

# 게이트가 볼 표본 크기(090 후속). **상위 몇 개만 넘기면 안 된다** — 게이트의 기준선이 "하위 절반 평균"
# 이라 상위권만 주면 배경이 상위권 평균이 되어 신호가 죽는다. 노출 개체 전량이 담기는 여유값이다.
# ⚠️ 개체가 이 수를 넘기면 표본이 잘려 임계의 근거가 흔들린다 — 그때는 다시 측정한다.
SEMANTIC_GATE_SAMPLE = 500

# 카드 한 장을 zip 으로 내보낼 때의 자산 수 상한. 묶음이 커도 응답이 무한정 커지지 않게 막는다.
CARD_BUNDLE_MAX_ASSETS = 200

# 좁힌 대상 전부를 zip 으로 받을 때의 용량 상한. **건수가 아니라 용량**으로 막는 이유: 자산 하나가
# 영상 수십 MB 에서 텍스트 수십 KB 까지라 건수로는 예측이 안 된다. 브라우저가 메모리에 담는 구조라
# 이 선을 넘으면 탭이 버틴다는 보장이 없다.
ENTITIES_BUNDLE_MAX_BYTES = 500 * 1024 * 1024

_LOG = logging.getLogger(__name__)

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
) -> list[dict[str, Any]]:
    """노출 개체 목록을 읽어 화면 항목으로 정형한다.

    Args:
        conn: DB 커넥션.
        entity_type: 종류 필터. ``None`` 이면 전체.
        areas: 갈래 이름들 — 모두 가진 개체만(AND). ``None``·빈 목록이면 필터 없음.
        min_bundle_size: 노출 임계(구성 자산 수 하한).
        limit: 최대 개체 수. 검색·좁히기 **전** 집계 상한이다.

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
    )
    return [shape_list_item(r) for r in rows]


def search_and_refine(
    items: list[dict[str, Any]],
    *,
    q: str | None,
    refine: str | None,
    run_in_db: Callable[[Callable[[Any], Any]], Any],
) -> tuple[list[dict[str, Any]], int]:
    """목록을 검색어로 좁히고(선택) 결과 안에서 글자로 한 번 더 좁힌다.

    검색 **판단은 전부 코어 함수**가 한다(문자열 매칭·융합·게이트·좁히기). 이 함수가 하는 일은 셋이다 —
    설정에 따라 어느 경로를 탈지 고르고, 그 경로가 실패하면 되돌리고, 좁히기를 마지막에 얹는다.

    경로가 둘인 이유(spec 092): OpenSearch 경로는 형태소 BM25 와 벡터 kNN 을 융합해 **문자열 매칭을
    대체**한다. 두 방식을 겹쳐 쓰면 같은 개체가 두 근거로 두 번 올라온다. 되돌림 경로(``pg``)는 089 의
    부분 문자열 매칭 위에 의미 결과를 **얹기만** 한다.

    🔴 **실패하면 문자열 결과로 되돌린다.** 임베딩 서버나 검색 엔진이 죽었을 때 개체 검색 전체가 오류가
    되면, 문자열로 이미 되던 것까지 못 쓰게 된다.

    ⚠️ 좁히기는 **융합 뒤**에 한다 — 문자열·의미로 모인 결과 전체가 좁히기 대상이다. 의미로 걸린 개체는
    그 글자가 없어서 들어온 것이라 여기서 반드시 떨어진다(정상 동작 — 글자로 골라내기).

    Args:
        items: 정형된 목록 전체.
        q: 검색어. 없거나 공백뿐이면 좁히지 않는다(전체 목록).
        refine: 결과 내 재검색 글자. 공백으로 쪼갠 낱말이 **모두** 있는 행만 남는다.
        run_in_db: DB 작업을 트랜잭션 안에서 돌려 주는 호출자의 함수(라우트가 넘긴다). 되돌림 경로의
            의미 검색이 커넥션을 필요로 해 주입받는다 — 이 모듈이 인프라를 직접 잡지 않게.

    Returns:
        ``(좁혀진 목록, 좁히기 전 건수)``. 뒤 값은 화면이 "지우면 N건"을 띄우는 재료다.
    """
    # 매칭은 코어 한 곳에 있다 — 표기 정규화 규칙이 두 벌이 되면 표기 해석이 갈린다.
    string_hits = narrow_entities(items, q)
    has_query = bool(q and q.strip())
    backend = get_current_settings().mm_meta.search_backend

    if backend == "opensearch" and has_query:
        items = _fuse_via_opensearch(items, string_hits, q or "")
    elif has_query and backend != "opensearch":
        items = _overlay_semantic(items, string_hits, q or "", run_in_db)
    else:
        items = string_hits

    scope_total = len(items)
    if refine and refine.strip():
        items = refine_rows(items, refine, fields_of=entity_refine_fields)
    return items, scope_total


def _fuse_via_opensearch(
    items: Sequence[Mapping[str, Any]],
    string_hits: list[dict[str, Any]],
    q: str,
) -> list[dict[str, Any]]:
    """OpenSearch 융합 결과로 목록을 갈아치운다(실패 시 문자열 결과 반환).

    Args:
        items: 정형된 목록 전체. 융합 결과는 키만 주므로 화면에 보일 행을 여기서 되살린다.
        string_hits: 문자열 매칭 결과(되돌림용).
        q: 검색어.

    Returns:
        융합 목록, 또는 실패 시 ``string_hits``.
    """
    try:
        from src.search.opensearch_sync import get_client

        channel = active_embed_channel()
        query_vector = embed_query_for_media_search(q, channel=channel)
        hits = search_entities_hybrid(
            get_client(),
            search_constants.ENTITY_INDEX_DEFAULT,
            query=q,
            query_vector=query_vector,
        )
    except Exception:  # noqa: BLE001
        _LOG.warning("개체 검색(OpenSearch) 실패 — 문자열 결과로 되돌린다", exc_info=True)
        return string_hits

    by_key = {(it["entity_type"], it["entity_uid"]): it for it in items}
    fused: list[dict[str, Any]] = []
    for hit in hits:
        row = by_key.get((str(hit.get("entity_type")), str(hit.get("entity_uid"))))
        if row is None:
            continue  # 목록에 없는 개체(임계 아래거나 색인이 낡음) — 목록이 정본이다
        fused.append({**dict(row), **_reason_of(hit)})
    return fused


def _reason_of(hit: Mapping[str, Any]) -> dict[str, Any]:
    """융합 결과 한 건의 "왜 걸렸나"를 응답 필드로 만든다.

    문구는 **코어 상수를 그대로** 쓴다(백엔드가 문구를 새로 만들지 않는다). 함께 싣는 불린이 실질이다 —
    화면이 "글자로 걸린 것 / 뜻으로 걸린 것"을 가르려고 문구를 파싱하면 문구를 고칠 때 조용히 깨진다.
    둘 다 맞으면 글자를 앞세운다(더 확실한 근거).

    Args:
        hit: ``search_entities_hybrid`` 결과 한 건(``by_text``·``by_semantic``·``cosine``).

    Returns:
        ``{match_reason, by_text, by_semantic}``.
    """
    by_text = bool(hit.get("by_text"))
    if by_text:
        reason = REASON_TEXT_MATCH
    else:
        cosine = hit.get("cosine")
        reason = (f"{REASON_SEMANTIC} ({float(cosine):.2f})"
                  if isinstance(cosine, (int, float)) else REASON_SEMANTIC)
    return {"match_reason": reason, "by_text": by_text,
            "by_semantic": bool(hit.get("by_semantic"))}


def _overlay_semantic(
    items: Sequence[Mapping[str, Any]],
    string_hits: list[dict[str, Any]],
    q: str,
    run_in_db: Callable[[Callable[[Any], Any]], Any],
) -> list[dict[str, Any]]:
    """되돌림 경로 — 문자열 결과 **위에** 의미 결과를 얹는다(실패 시 문자열 결과).

    🔴 **채널을 반드시 넘긴다.** 개체 벡터는 배치가 활성 채널로 만들었다. 다른 모델의 벡터를 견주면
    유사도가 뜻을 잃는다(실측: 채널을 빼면 `발효`→훈민정음 0.09 처럼 무관한 결과가 나왔다).

    Args:
        items: 정형된 목록 전체.
        string_hits: 문자열 매칭 결과.
        q: 검색어.
        run_in_db: DB 작업 실행 함수.

    Returns:
        융합 목록, 또는 실패 시 ``string_hits``.
    """
    try:
        cfg = get_current_settings()
        channel = active_embed_channel()
        query_vector = embed_query_for_media_search(q, channel=channel)
        ranked = run_in_db(
            lambda conn: find_similar_entities(
                conn, query_vector=query_vector, top_n=SEMANTIC_GATE_SAMPLE,
                model_name=cfg.embed.api_model)
        )
        # 정답이 없으면 아무것도 얹지 않는다 — 게이트 없이는 무관한 질의에도 상위 몇 건이 나간다.
        semantic_hits = gate_semantic_hits(
            ranked, eps=cfg.mm_meta.semantic_gate_eps, top_n=SEMANTIC_TOP_N,
            enabled=cfg.mm_meta.semantic_gate_enabled)
        fused = fuse_entity_results(items, string_hits, semantic_hits)
    except Exception:  # noqa: BLE001
        _LOG.warning("개체 의미 검색 실패 — 문자열 결과만 돌려준다", exc_info=True)
        return string_hits
    # 두 경로가 같은 계약을 내보내게 불린을 채운다. 문자열 결과가 항상 위에 오는 계층이라(090 설계)
    # 이유 문구로 층을 가를 수 있다.
    for row in fused:
        is_semantic = str(row.get("match_reason") or "").startswith(REASON_SEMANTIC)
        row["by_semantic"] = is_semantic
        row["by_text"] = not is_semantic
    return fused


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
