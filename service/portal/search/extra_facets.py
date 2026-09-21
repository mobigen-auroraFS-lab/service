"""화면이 쓰는 **추가 좁히기 칩**(형식·크기 구간·기간 프리셋)의 건수 — 코어 칩 네 축 밖.

🔴 세는 집합이 검색 결과와 같아야 한다 — 코어의 집합 절(``browse_scope_clause``)을 그대로 불러
   쓰고 그 위에 집계만 얹는다. 절을 새로 짜면 "적힌 숫자 = 누르면 나오는 수"가 깨진다.

🔴 칩은 **자기 조건을 뺀 채** 센다(형식 칩은 형식 필터를, 기간 칩은 기간 필터를 빼고) — 빼지 않으면
   지금 고른 값만 남아 칩이 하나로 접힌다. 코어 ``FACET_SELF_FILTERS`` 와 같은 규칙이다.

TODO: 크기 구간은 **세기만** 한다 — 코어 ``SearchFilters`` 에 크기 축이 없어 눌러서 좁힐 수 없다
      (코어에 `file_size_min`·`file_size_max` 요청 · `TODO.md`). 그 전까지 화면은 숫자만 보이거나 칩을 감춘다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from src.search.file_search import (
    ABOUT_BRANCH_DEFAULT,
    WORD_OPERATOR_DEFAULT,
    browse_scope_clause,
    build_semantic_body,
    refine_clause,
)
from src.search.search_filters import SearchFilters, filters_to_opensearch_bool

# 셀 수 있는 축.
AXES: tuple[str, ...] = ("file_ext", "file_size", "date_preset")

# 🔴 색인의 **실제 필드 이름** — 코어가 필터 절에 쓰는 이름과 같아야 한다. 최상위 이름으로 집계하면
#    오류 없이 **빈 칩**이 나온다(2026-09-21 실호출에서 드러난 결함).
FIELD = {"file_ext": "filter_kw.file_ext",
         "file_size": "file_size",
         "date_preset": "filter_date.created_at"}

# 크기 구간(바이트) — 화면의 '1MB 미만 / 1~10MB / 10MB 이상'과 같은 경계.
MB = 1024 * 1024
SIZE_BUCKETS: tuple[tuple[str, int | None, int | None], ...] = (
    ("under1", None, MB),
    ("1to10", MB, 10 * MB),
    ("over10", 10 * MB, None),
)

# 기간 프리셋(일) — 화면의 '최근 7·30·90일'.
DATE_PRESETS: tuple[int, ...] = (7, 30, 90)

# 형식 칩에 받아 올 확장자 수 상한.
EXT_FACET_SIZE = 20


def _clauses(filters: SearchFilters | None, *, axis: str, refine: str | None) -> list[dict[str, Any]]:
    """축을 셀 때 쓸 filter 절 — **그 축의 조건은 빼고** 만든다."""
    trimmed = filters
    if filters is not None and axis == "file_ext":
        trimmed = replace(filters, file_exts=())
    elif filters is not None and axis == "date_preset":
        trimmed = replace(filters, created_from=None, created_to=None)
    clauses = list(filters_to_opensearch_bool(trimmed))
    clause = refine_clause(refine)
    if clause is not None:
        clauses.append(clause)
    return clauses


def build_axis_body(
    axis: str,
    *,
    query: str,
    semantic_ids: list[str],
    filters: SearchFilters | None,
    refine: str | None,
    now: datetime,
    operator: str = WORD_OPERATOR_DEFAULT,
    about_branch: bool = ABOUT_BRANCH_DEFAULT,
) -> dict[str, Any]:
    """한 축을 세는 검색 본문(순수 — 행은 받지 않는다).

    Args:
        semantic_ids: 뜻으로 걸린 자산 id 들. 검색어가 있을 때 코어 순위 질의와 **같은 값**이어야 한다.
        now: 기간 프리셋의 기준 시각(주입 — 테스트가 시각을 고정할 수 있게).

    Raises:
        ValueError: 모르는 축 이름.
    """
    if axis not in AXES:
        raise ValueError(f"알 수 없는 축: {axis!r} (허용: {list(AXES)})")
    if axis == "file_ext":
        aggs = {"file_ext": {"terms": {"field": FIELD["file_ext"], "size": EXT_FACET_SIZE,
                                       "order": [{"_count": "desc"}, {"_key": "asc"}]}}}
    elif axis == "file_size":
        ranges = []
        for key, start, end in SIZE_BUCKETS:
            one: dict[str, Any] = {"key": key}
            if start is not None:
                one["from"] = start
            if end is not None:
                one["to"] = end
            ranges.append(one)
        aggs = {"file_size": {"range": {"field": FIELD["file_size"], "keyed": True,
                                        "ranges": ranges}}}
    else:
        ranges = [{"key": str(days), "from": (now - timedelta(days=days - 1))
                   .replace(hour=0, minute=0, second=0, microsecond=0).isoformat()}
                  for days in DATE_PRESETS]
        aggs = {"date_preset": {"date_range": {"field": FIELD["date_preset"], "keyed": True,
                                               "ranges": ranges}}}
    return {
        "size": 0,
        "track_total_hits": True,
        "query": browse_scope_clause(
            query, semantic_ids,
            filters=_clauses(filters, axis=axis, refine=refine),
            operator=operator, about_branch=about_branch),
        "aggs": aggs,
    }


def semantic_ids_for(client: Any, index: str, *, query: str, query_vector: list[float]) -> list[str]:
    """검색어가 뜻으로 걸어 온 자산 id 들 — 코어 순위 질의와 같은 재료로 구한다.

    검색어가 없으면(훑기) 빈 목록이다.
    """
    if not (query or "").strip() or not query_vector:
        return []
    found = client.search(index=index, body=build_semantic_body(query_vector))
    ids = [str((h.get("_source") or {}).get("asset_id") or "")
           for h in ((found.get("hits") or {}).get("hits") or [])]
    return [a for a in ids if a]


def _items(axis: str, agg: dict[str, Any] | None) -> list[dict[str, Any]]:
    """집계 응답을 ``[{key, count}]`` 로 편다(축마다 모양이 달라 여기서 흡수한다)."""
    if not agg:
        return []
    if axis == "file_ext":
        return [{"key": str(b.get("key")), "count": int(b.get("doc_count") or 0)}
                for b in (agg.get("buckets") or [])]
    buckets = agg.get("buckets") or {}
    order = [k for k, _, _ in SIZE_BUCKETS] if axis == "file_size" else [str(d) for d in DATE_PRESETS]
    return [{"key": key, "count": int((buckets.get(key) or {}).get("doc_count") or 0)}
            for key in order]


def extra_facets(
    client: Any,
    index: str,
    *,
    query: str,
    query_vector: list[float],
    filters: SearchFilters | None,
    refine: str | None,
    axes: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """요청한 축들을 세어 ``{axes: {축: [{key, count}]}, total, as_of}`` 로 돌려준다(축마다 질의 하나)."""
    picked = list(axes) if axes else list(AXES)
    at = now or datetime.now(UTC)
    ids = semantic_ids_for(client, index, query=query, query_vector=query_vector)
    out: dict[str, Any] = {"axes": {}, "total": 0, "as_of": at.isoformat()}
    for axis in picked:
        body = build_axis_body(axis, query=query, semantic_ids=ids, filters=filters,
                               refine=refine, now=at)
        found = client.search(index=index, body=body)
        out["axes"][axis] = _items(axis, (found.get("aggregations") or {}).get(axis))
        out["total"] = int((((found.get("hits") or {}).get("total")) or {}).get("value") or 0)
    return out
