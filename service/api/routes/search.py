"""검색 라우트 — 검색 실행 + 권한별 필드 가리기 + 주제 패싯 집계. 검색 알고리즘은 코어에 있다.

권한 투영은 응답 직전에 한다(색인을 건드리지 않으므로 정책이 바뀌어도 재색인이 필요 없다). 인프라 함수는 모듈 경유로 써서 테스트가 DB 없이 이름을 갈아끼운다.
새 화면은 ``GET /file-search`` 를 쓰고, 이 라우트는 기존 멀티모달 검색 화면(종류별 그룹 응답)과 과제 산출물 근거로 남긴다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import search_health
from service.portal.auth import Principal, require_principal
from service.portal.common.db_manager import DbManager
from service.portal.common.stage_timer import stage
from service.portal.search.group import asset_refine_fields, group_ranked
from service.portal.search.presets import DEFAULT_PRESET, PRESETS, resolve_tuning, tuning_meta
from src.config.search_constants import TAG_FACET_MIN_COUNT_DEFAULT, TAG_FACET_TOP_N_DEFAULT
from src.config.search_modalities import VALID_SEARCH_MODALITIES, parse_modalities_csv
from src.config.settings import get_current_settings
from src.domain.numeric import safe_float
from src.search.facets import aggregate_facets
from src.search.refine import refine_rows
from src.search.search_filters import parse_search_filters
from src.search.search_service import search_hybrid
from src.search.search_tuning import SearchTuning
from src.search.tag_facets import aggregate_tag_facets

router = APIRouter(tags=["search"])

_LOG = logging.getLogger("meta_extract.portal_api")

# 질의 임베딩 실패를 가려내는 표식 — 코어가 전용 예외 없이 RuntimeError("임베딩 API 호출 실패…")로 올린다. 코어에 예외 형이 생기면 그것으로 바꾼다.
_EMBED_FAIL_MARK = "임베딩"

# 질의 길이 상한 — 검색 엔진이 낱말마다 절을 만들고 1024개에서 거절한다. 너무 긴 질의는 사용자가 고칠 문제라 422 로 앞에서 끊는다.
_QUERY_MAX_LEN = 300

# 배제할 도메인 목록 — 지금은 비어 있다(모든 도메인을 균일하게 노출). 가려야 할 도메인이 생기면 여기에 넣는다.
_EXCLUDE_DOMAINS: frozenset[str] = frozenset()

# search_hybrid 의 버킷당 후보 풀 기본값(요청마다 limit_per_bucket 으로 덮어쓴다). 응답 개수보다 깊게 받아야 배제된 행을 드롭한 뒤 승격할 여지가 생긴다.
_SEARCH_LIMIT_PER_BUCKET_DEFAULT = 50
# 풀 상한(요청 남용 · 엔진 부하 방어).
_SEARCH_LIMIT_PER_BUCKET_MAX = 500

# 간략 보기에서 요약을 자를 길이(고정값 — 요청 파라미터로 받지 않는다).
_COMPACT_SUMMARY_CHARS = 160


def _topic_pairs_of(row: Mapping[str, Any]) -> list[str]:
    """행이 나르는 주제 짝(``topic_pairs``)을 읽는다. 짝이 없는 옛 행은 부모 주제만으로 센다(없는 조합을 만들지 않는다)."""
    pairs = [str(p) for p in (row.get("topic_pairs") or []) if p]
    if not pairs:
        pairs = [str(t) for t in (row.get("topics") or []) if t]
    return pairs


def _topic_keys_of(row: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """주제 축의 묶는 법 — 짝의 첫 ``>`` 앞이 부모 주제이고 키가 곧 라벨이다. 부모가 빈 짝은 내지 않는다."""
    for pair in _topic_pairs_of(row):
        parent = pair.split(">", 1)[0]
        if parent:
            yield parent, parent


def _subtopic_keys_of(row: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """세부주제 축의 묶는 법 — 짝 문자열 자체가 키다(교차곱이 생기지 않는다). 부모나 자식이 빈 짝은 내지 않는다."""
    for pair in _topic_pairs_of(row):
        idx = pair.find(">")
        if idx > 0 and pair[idx + 1 :]:
            yield pair, pair


def _asset_unit(row: Mapping[str, Any]) -> str:
    """계수 단위 = 자산 id — 같은 자산이 여러 모달리티 버킷에 있어도 1건으로 센다. 없으면 빈 문자열(세지 않는다)."""
    return str(row.get("asset_id") or "")


def _first_original(originals: list[str]) -> str:
    """닫힌 어휘 축의 표시 라벨 = 키 그대로(종전 응답과 글자까지 같게). 같은 키로 묶인 원문 중 첫 번째."""
    return originals[0]


def _search_topic_facet(grouped: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """검색 결과에 걸린 자산들이 공유하는 주제를 세어 패싯으로 만든다.

    행에 실려 온 부모-자식 짝을 그대로 쓴다(자산마다 DB 를 다시 묻지 않고, 부모 · 자식 목록을 곱해 없는 조합을 만들지 않는다).
    세는 규칙은 코어 ``aggregate_facets`` 하나(자산당 1회 · 건수 내림차순 · 동수는 이름순)이고, 여기서는 묶는 법과 응답 모양(부모 아래 자식 목록)만 정한다.
    """
    rows = [row for bucket_rows in grouped.values() for row in bucket_rows]
    parents = aggregate_facets(
        rows, keys_of=_topic_keys_of, unit_of=_asset_unit, label_of=_first_original
    )
    pairs = aggregate_facets(
        rows, keys_of=_subtopic_keys_of, unit_of=_asset_unit, label_of=_first_original
    )
    # 짝 목록이 이미 (건수 내림차순 → 짝 문자열 오름차순)이라 부모별로 나눠 담기만 하면 부모 안 순서도 (건수 → 이름)이 된다.
    subs_by_topic: dict[str, list[dict[str, Any]]] = {}
    for item in pairs["items"]:
        parent, sub = item["label"].split(">", 1)
        subs_by_topic.setdefault(parent, []).append(
            {"subtopic_ko": sub, "asset_count": item["count"]}
        )
    return [
        {
            "topic_ko": item["label"],
            "asset_count": item["count"],
            "subtopics": subs_by_topic.get(item["label"], []),
        }
        for item in parents["items"]
    ]


def _parse_search_mode(mode: str) -> str:
    """검색 모드 값을 검증한다(빈 값은 ``auto``). 허용 밖 값은 400 — 오타를 기본 모드로 흡수하면 의도와 다른 결과를 보게 된다."""
    m = (mode or "auto").strip().lower()
    if m not in ("auto", "keyword"):
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 mode: {mode!r} (허용: auto, keyword)",
        )
    return m


def _parse_modalities(modalities: str | None) -> list[str] | None:
    """콤마 구분 모달리티 문자열을 검증된 리스트로 파싱한다(미지정 = ``None`` = 전체). 모르는 모달리티는 400."""
    mods = parse_modalities_csv(modalities)
    if mods is None:
        return None
    unknown = [m for m in mods if m not in VALID_SEARCH_MODALITIES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 modality: {unknown} (허용: {list(VALID_SEARCH_MODALITIES)})",
        )
    return mods or None


def _parse_preset(preset: str) -> str:
    """검색 튜닝 프리셋 이름을 검증한다(닫힌 어휘 · 빈 값은 ``default``). 허용 밖 값은 400."""
    name = (preset or DEFAULT_PRESET).strip().lower()
    if name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 preset: {preset!r} (허용: {', '.join(PRESETS)})",
        )
    return name


def _base_tuning() -> SearchTuning:
    """서버 설정에서 기본 튜닝 묶음을 해소한다 — 코어가 ``tuning`` 없이 하는 것과 같은 규칙이라 ``default`` 프리셋을 명시해도 응답이 같다."""
    try:
        cfg = get_current_settings()
    except RuntimeError:
        return SearchTuning()
    return SearchTuning.from_settings(cfg)


def _tag_facet_limits() -> tuple[int, int]:
    """태그 패싯의 상위 노출 개수 · 노출 하한 ``(top_n, min_count)`` 을 설정에서 읽는다(미초기화면 코어 기본값 12 · 2)."""
    try:
        search_cfg = get_current_settings().search
    except RuntimeError:
        return TAG_FACET_TOP_N_DEFAULT, TAG_FACET_MIN_COUNT_DEFAULT
    return search_cfg.tag_facet_top_n, search_cfg.tag_facet_min_count


def _tag_facets(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """응답에 실리는 결과 행 전체(모달리티 합)에서 태그 패싯을 센다 — 세는 규칙은 코어 ``aggregate_tag_facets`` 그대로다.

    ``{"items": [{"label", "count"}, …], "has_more": bool}``.
    """
    rows = [row for bucket_rows in grouped.values() for row in bucket_rows]
    top_n, min_count = _tag_facet_limits()
    out = aggregate_tag_facets(rows, top_n=top_n, min_count=min_count)
    return {"items": out["items"], "has_more": out["has_more"]}


# 디버그 뷰(no_cutoff · compact) — 기본 off 라 기존 grouped 응답이 그대로다. 이미 계산된(권한 투영을 거친) grouped 위에서만 계산해 검색을 두 번 돌리지 않고 tier 유출이 없다.


def _clip_text(text: str, max_chars: int) -> str:
    """요약을 한 줄로 펴고 ``max_chars`` 를 넘으면 ``…`` 로 자른다(0 이하면 자르지 않는다)."""
    one_line = " ".join(text.split())
    if max_chars > 0 and len(one_line) > max_chars:
        return one_line[: max_chars - 1].rstrip() + "…"
    return one_line


def _compact_view(
    grouped: dict[str, list[dict[str, Any]]], query: str, limit: int
) -> dict[str, Any]:
    """모달리티 버킷 결과를 단일 랭킹으로 축약한다(디버그). 점수 내림차순 · 동점은 자산 id 순으로 상위 ``limit`` 건.

    ``grouped`` 는 권한 투영을 거친 것이어야 한다(원시 결과를 넣으면 가려야 할 요약이 노출된다).
    """
    flat: list[tuple[float, str, dict[str, Any]]] = []
    for modality, rows in grouped.items():
        for r in rows:
            score = round(safe_float(r.get("similarity")), 4)  # 정화 규칙은 코어 정본 하나(093 1단계)
            iid = str(r.get("asset_id", ""))
            flat.append(
                (
                    score,
                    iid,
                    {
                        "모달리티": modality,
                        "점수": score,
                        "파일명": str(r.get("file_name", "")),
                        "요약": _clip_text(str(r.get("summary", "")), _COMPACT_SUMMARY_CHARS),
                    },
                )
            )
    flat.sort(key=lambda t: (-t[0], t[1]))
    top = [{"순위": i, **row} for i, (_s, _id, row) in enumerate(flat[:limit], start=1)]
    return {"query": query, "건수": len(top), "결과": top}


@router.get("/search")
def search(
    q: str = Query(..., max_length=_QUERY_MAX_LEN, description="검색 질의(한국어)"),
    modalities: str | None = Query(
        None, description="콤마 구분: text,image,video,audio (미지정=전체)"
    ),
    size: int = Query(20, ge=1, le=100, description="모달리티별 최대 결과 수(top-N)"),
    limit_per_bucket: int = Query(
        _SEARCH_LIMIT_PER_BUCKET_DEFAULT,
        ge=1,
        le=_SEARCH_LIMIT_PER_BUCKET_MAX,
        description=(
            "버킷당 후보 풀 깊이(top-N=size 캡 이전). 크게 줄수록 컷·도메인배제(dormant) 잔여·074 승격 여지↑, "
            "OS 부하↑. 실제 풀 = max(이 값, size)"
        ),
    ),
    mode: str = Query("auto", description="검색 모드: auto(기본) | keyword(단어 포함 문서)"),
    file_ext: list[str] | None = Query(None, description="파일 확장자 필터(반복 가능, 예: txt,pdf)"),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    created_to: str | None = Query(None, description="생성일 상한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    topic: str | None = Query(None, description="주제(topic) 정확 일치 필터"),
    subtopic: str | None = Query(None, description="세부주제(subtopic) 정확 일치 필터"),
    tag: list[str] | None = Query(
        None,
        description=(
            "태그 필터(반복 가능 · 화면에 보인 라벨 원문 그대로). 태그끼리 OR, 다른 축(주제·기간·확장자)과"
            " AND. 패싯 클릭 좁히기는 이 파라미터가 아니라 받은 결과 행의 tags 로 화면이 거른다(083 ⓐ)"
            " — 이 파라미터는 태그 단독 탐색(전 코퍼스 조회)용이다."
        ),
    ),
    no_cutoff: bool = Query(
        False, description="true 면 모달리티별 적합도 컷오프를 무시(약한 매칭까지 노출·디버그용·기본 off)"
    ),
    refine: str | None = Query(
        None,
        max_length=_QUERY_MAX_LEN,
        description=(
            "결과 내 재검색(글자 좁히기). 공백으로 쪼갠 낱말이 **모두** 들어 있는 행만 남긴다"
            "(파일명·요약·태그 대상). 서버에 다시 묻지 않고 **이번 결과 안에서만** 좁히므로"
            " 상위 N 밖은 대상이 아니다. 미지정·빈 값이면 좁히지 않는다."
        ),
    ),
    compact: bool = Query(
        False,
        description="true 면 전 모달리티를 합쳐 점수순 top-K(=size)로 축약(순위·모달리티·점수·파일명·요약·기본 off)",
    ),
    preset: str = Query(
        DEFAULT_PRESET,
        description=(
            "검색 튜닝 프리셋(닫힌 이름 · 현재 default 하나 = 서버 설정값 그대로). 원시 숫자는 받지 않는다."
            " 적용된 값은 meta.tuning 에 그대로 기록된다(같은 질의 + 같은 프리셋 = 같은 결과)."
        ),
    ),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """하이브리드 검색 결과를 모달리티별 그룹으로 돌려준다. 점수 척도가 달라 하나의 순위로 합치지 않고 섹션마다 독립 순위로 상위 N개씩 낸다."""
    mods = _parse_modalities(modalities)
    search_mode = _parse_search_mode(mode)
    preset_name = _parse_preset(preset)
    # 손잡이는 코어(tuning=), 눈금은 백엔드(프리셋) — default 는 설정값 그대로라 종전과 같다.
    tuning = resolve_tuning(preset_name, _base_tuning())
    try:
        search_filters = parse_search_filters(
            file_ext=file_ext,
            created_from=created_from,
            created_to=created_to,
            topic=topic,
            subtopic=subtopic,
            tag=tag,  # 정규화·가공 없이 그대로(정규화는 코어 몫 · 083 전달물 §1)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    # 풀 하한: 요청 풀이 노출 size 보다 얕으면 size 로 끌어올린다. 공백뿐인 질의는 묻지 않은 것이라 422(조용히 0건을 주면 '자료가 없다'로 읽힌다).
    if not q.strip():
        raise HTTPException(status_code=422, detail="검색어(q)가 비어 있습니다 — 찾을 말을 주십시오")

    effective_pool = max(limit_per_bucket, size)
    search_health.check()           # 검색 엔진이 죽어 있으면 임베딩부터 기다리지 않고 곧바로 503
    try:
        with stage("search"):       # 임베딩 + 검색 엔진을 한 번에 부른다 — 요청 로그에는 합쳐서 남는다
            result = search_hybrid(
                q,
                modalities=mods,
                limit_per_bucket=effective_pool,
                search_mode=search_mode,
                search_filters=search_filters,
                # 디버그용 우회. 기본은 꺼져 있어 평소 호출에는 영향이 없다.
                disable_os_cutoff=no_cutoff,
                tuning=tuning,
            )
    except RuntimeError as exc:
        # 임베딩 서버 장애는 검색 엔진 장애와 따로 알린다. 그 밖의 RuntimeError(설정 미초기화 등)는 코드 결함이라 그대로 올린다.
        if _EMBED_FAIL_MARK not in str(exc):
            raise
        _LOG.warning("검색 — 질의 임베딩 실패: %s", exc, exc_info=_LOG.isEnabledFor(logging.DEBUG))
        raise HTTPException(status_code=503, detail="임베딩 서버에 연결할 수 없습니다") from exc

    # 모달리티별로 독립 순위를 매겨 상위 N개씩 담는다(배제 목록은 현재 비어 있다).
    grouped_raw = group_ranked(result, limit_per_modality=size, exclude_domains=_EXCLUDE_DOMAINS)

    # 권한 투영과 주제 패싯을 **한 트랜잭션에서** 끝낸다 — 연결을 두 번 잡지 않기 위해서다.
    def _project_and_facet(repo: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        """권한별 필드 가리기와 주제 패싯 계산을 **한 번의 조회**로 끝낸다(연결을 두 번 잡지 않게)."""
        projected = repo.search.project_grouped(grouped_raw, clearance=principal.clearance)
        facet = _search_topic_facet(projected)
        return projected, facet

    grouped, topic_facets = DbManager.read(_project_and_facet)

    # ── 결과 내 재검색(091) ────────────────────────────────────────────────────
    # 결과 내 재검색은 서버에 다시 묻지 않는다 — 질의를 바꾸면 게이트가 다시 판정해 원 결과에 없던 것이 나타난다. 이번 결과만 걸러 "47건 중 12건"이 성립하게 한다.
    # 좁히기는 compact 뷰 앞에 둔다 — compact 가 요약을 자르므로 뒤에 두면 잘린 글자로 걸러져 "보이는데 안 걸림"이 생긴다.
    scope_counts = {modality: len(rows) for modality, rows in grouped.items()}
    refine_applied = bool(refine and refine.strip())
    if refine_applied:
        grouped = {
            modality: refine_rows(rows, refine, fields_of=asset_refine_fields)
            for modality, rows in grouped.items()
        }
        # 주제 패싯은 "지금 보이는 결과" 스코프라 좁힌 뒤로 다시 센다(순수 계산이라 DB 를 다시 잡지 않는다).
        topic_facets = _search_topic_facet(grouped)

    # 태그 패싯도 "지금 보이는 결과" 스코프다(083 SC-02) — 좁힌 뒤의 행으로 센다.
    tag_facets = _tag_facets(grouped)

    # 디버그 opt-in(기본 off): compact 뷰는 이미 clearance projection 된 grouped 위에서 계산(tier 유출 0·도메인 배제 dormant).
    if compact:
        return _compact_view(grouped, q, size)

    counts = {modality: len(rows) for modality, rows in grouped.items()}

    meta: dict[str, Any] = {
        "query": q,
        "modalities": mods,
        "size": size,
        "counts": counts,
        # 이번 결과 안에서만 주제를 센다 — 화면에서 주제를 누르면 그 값으로 다시 필터한다.
        "topic_facets": topic_facets,
        # 태그 축(083) — 주제 축과 분리된 단일 목록(상위 N + has_more). 건수는 화면이 재계산하지 않는다.
        "tag_facets": tag_facets,
    }
    # 파라미터를 준 요청에만 실린다 — 안 주면 응답이 091 이전과 **완전히 같다**(되돌림의 실질).
    if refine_applied:
        meta["refine"] = {
            "q": refine,
            "scope_counts": scope_counts,
            "scope_total": sum(scope_counts.values()),
            "total": sum(counts.values()),
        }
    search_plan = (result.get("meta") or {}).get("search_plan")
    if search_plan is not None:
        meta["search_plan"] = search_plan
    # 이번 검색에 실제 적용된 튜닝(프리셋 이름 + 값 전부) — 서버 설정이 바뀌어도 응답을 재현할 수 있다.
    meta["tuning"] = tuning_meta(preset_name, tuning)
    # 검색이 남긴 관측치(게이트·검증·정규화)를 응답에 실어 준다 — 있을 때만 넣는다.
    for obs_key in ("os_gate", "llm_verify", "query_norm"):
        obs_val = (result.get("meta") or {}).get(obs_key)
        if obs_val is not None:
            meta[obs_key] = obs_val
    if search_filters is not None:
        meta["filters"] = {
            "file_ext": list(search_filters.file_exts),
            "created_from": search_filters.created_from.isoformat()
            if search_filters.created_from is not None
            else None,
            "created_to": search_filters.created_to.isoformat()
            if search_filters.created_to is not None
            else None,
            "topic": search_filters.topic,
            "subtopic": search_filters.subtopic,
            "tag": list(search_filters.tags),
        }

    return {
        "query": q,
        "results": grouped,
        "meta": meta,
    }
