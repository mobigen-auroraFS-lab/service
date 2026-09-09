"""검색 라우트 — 검색 실행 + 권한별 필드 가리기 + 주제 패싯 집계.

**흐름에서의 위치**: 요청을 코어 검색 함수에 넘기고, 돌아온 결과를 **요청자 권한에 맞게**
가린 뒤 화면이 쓸 패싯까지 붙여 응답한다. 검색 알고리즘 자체는 코어에 있다.

**권한 투영은 응답 직전에 한다** — 색인이나 검색 자체를 건드리지 않는다. 그래야 권한 정책이
바뀌어도 색인을 다시 만들 필요가 없다.

인프라 함수는 ``from ... import`` 가 아니라 **모듈 경유**로 쓴다 — 그래야 테스트가 이 모듈의
이름을 갈아끼워 DB 없이 검증할 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import _infra
from service.portal.access_project import project_grouped
from service.portal.auth import Principal, require_principal
from service.portal.search_group import asset_refine_fields, group_ranked
from service.portal.search_presets import DEFAULT_PRESET, PRESETS, resolve_tuning, tuning_meta
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

router = APIRouter()

# 배제할 도메인 목록. **지금은 비어 있다**(모든 도메인을 균일하게 노출) — 특정 도메인을
# 다시 가려야 할 때 여기에 넣으면 결과 조립 단계가 그 행들을 걷어낸다.
# 의료 특수 트랙 미운용. 의료 복귀(3년차) 시 frozenset({"medical"}) 로 되돌린다.
_EXCLUDE_DOMAINS: frozenset[str] = frozenset()

# search_hybrid 의 버킷당 후보 풀 **기본값**. /search 의 limit_per_bucket 로 요청마다 덮어쓴다.
# 응답은 모달리티별 상위 N개만 내보내지만, 후보 풀은 그보다 깊게 받아야 한다 — (a)배제된 행
# 드롭 후 승격 여지가 생긴다 — 핸들러가 max(풀, size)로 하한을 걸어 풀<size 회귀를 막는다.
_SEARCH_LIMIT_PER_BUCKET_DEFAULT = 50
# 풀 상한(요청 남용·OS 부하 방어). size 상한(100)보다 넉넉히 둬 승격 여지를 남긴다.
_SEARCH_LIMIT_PER_BUCKET_MAX = 500

# 간략 보기에서 요약을 자를 길이(고정값 — 요청 파라미터로 받지 않는다).
_COMPACT_SUMMARY_CHARS = 160


def _topic_pairs_of(row: Mapping[str, Any]) -> list[str]:
    """행이 나르는 주제 짝(``topic_pairs``)을 읽는다.

    짝이 없는 옛 행(색인에 짝이 없던 문서)은 부모 주제(``topics``)만으로 센다 — 없는 조합을
    만들어 내지 않는다.

    Args:
        row: 결과 행(``_shape`` 가 만든 응답 모양). 아무것도 바꾸지 않는다.

    Returns:
        ``"부모>자식"`` 또는 ``"부모"`` 문자열 목록(빈 값 제외).
    """
    pairs = [str(p) for p in (row.get("topic_pairs") or []) if p]
    if not pairs:
        pairs = [str(t) for t in (row.get("topics") or []) if t]
    return pairs


def _topic_keys_of(row: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """주제 축의 "묶는 법" — 짝의 첫 ``>`` 앞이 부모 주제이고, 키가 곧 라벨이다.

    Args:
        row: 결과 행.

    Yields:
        ``(부모 주제, 부모 주제)``. 첫 ``>`` 로만 자른다 — 세부주제에 ``>`` 가 섞여도 부모가
        어긋나지 않게. 부모가 빈 짝은 내지 않는다.
    """
    for pair in _topic_pairs_of(row):
        parent = pair.split(">", 1)[0]
        if parent:
            yield parent, parent


def _subtopic_keys_of(row: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """세부주제 축의 "묶는 법" — 짝 문자열 자체가 키다(부모 아래에만 귀속돼 교차곱이 생기지 않는다).

    Args:
        row: 결과 행.

    Yields:
        ``(짝, 짝)``. 부모나 자식이 빈 짝(``">x"``·``"x>"``)은 내지 않는다.
    """
    for pair in _topic_pairs_of(row):
        idx = pair.find(">")
        if idx > 0 and pair[idx + 1 :]:
            yield pair, pair


def _asset_unit(row: Mapping[str, Any]) -> str:
    """계수 단위 = 자산 id — 같은 자산이 여러 모달리티 버킷에 있어도 1건으로 센다.

    Args:
        row: 결과 행.

    Returns:
        자산 id. 없으면 빈 문자열(그 행은 세지 않는다).
    """
    return str(row.get("asset_id") or "")


def _first_original(originals: list[str]) -> str:
    """닫힌 어휘 축의 표시 라벨 = 키 그대로(표기 투표·공백 다듬기 없음 — 종전 응답과 글자까지 같게).

    Args:
        originals: 같은 키로 묶인 원문들(주제 축은 전부 같은 값이다).

    Returns:
        첫 원문.
    """
    return originals[0]


def _search_topic_facet(grouped: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """검색 결과에 걸린 자산들이 공유하는 주제를 세어 패싯으로 만든다.

    행에 이미 실려 온 **부모-자식 짝**을 그대로 쓴다 — 자산마다 DB 를 다시 묻지 않는다.
    짝을 쓰는 이유: 부모 목록과 자식 목록을 따로 받아 곱하면, 자산 하나가 여러 주제에
    걸릴 때 있지도 않은 조합이 생긴다.

    **세는 규칙은 코어 정본 하나**(``aggregate_facets`` — 자산당 1회 계수·건수 내림차순·동수는
    이름순)를 부르고, 여기서는 주제 축의 "묶는 법"과 응답 모양(부모 아래 자식 목록)만 정한다
    (093 2단계 · 규칙 ① "어디서 세어도 같은 건수" 는 코어, 규칙 ② 화면 모양은 백엔드).

    Args:
        grouped: 모달리티별 검색 결과 행. 각 행의 주제 짝만 읽고 아무것도 바꾸지 않는다.
            짝이 없는 행은 부모 주제만으로 센다(조합을 만들어 내지 않는다).

    Returns:
        주제별 자산 수와 그 아래 세부주제 분포. 자산 수 내림차순, 동수는 이름순으로
        갈라 순서를 고정한다.
    """
    rows = [row for bucket_rows in grouped.values() for row in bucket_rows]
    parents = aggregate_facets(
        rows, keys_of=_topic_keys_of, unit_of=_asset_unit, label_of=_first_original
    )
    pairs = aggregate_facets(
        rows, keys_of=_subtopic_keys_of, unit_of=_asset_unit, label_of=_first_original
    )
    # 짝 목록은 이미 (건수 내림차순 → 짝 문자열 오름차순)이라, 부모별로 나눠 담기만 하면
    # 부모 안 순서가 (건수 내림차순 → 세부주제 이름순)이 된다 — 앞부분 "부모>" 가 같기 때문.
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
    """검색 모드 값을 검증한다.

    Args:
        mode: 요청 값. 빈 값이면 ``auto`` 로 본다.

    Returns:
        소문자로 정규화된 모드.

    Raises:
        HTTPException: 허용 밖 값이면 400 — 오타를 기본 모드로 흡수하면 사용자가 의도한
            검색과 다른 결과를 보게 된다.
    """
    m = (mode or "auto").strip().lower()
    if m not in ("auto", "keyword"):
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 mode: {mode!r} (허용: auto, keyword)",
        )
    return m


def _parse_modalities(modalities: str | None) -> list[str] | None:
    """콤마 구분 모달리티 문자열을 검증된 리스트로 파싱한다(미지정=None=전체).

    파싱은 공용 파서 하나만 쓴다(같은 규칙이 두 곳에 생기지 않게).

    Args:
        modalities: 콤마로 이은 모달리티 문자열. ``None``·빈 값이면 **전체**를 뜻한다.

    Returns:
        검증된 목록, 또는 전체를 뜻하는 ``None``.

    Raises:
        HTTPException: 모르는 모달리티가 섞이면 400(조용히 무시하면 사용자는 그 모달리티가
            검색된 줄 안다).
    """
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
    """검색 튜닝 프리셋 이름을 검증한다(닫힌 어휘).

    Args:
        preset: 요청 값. 빈 값이면 ``default`` 로 본다.

    Returns:
        소문자로 정규화된 프리셋 이름.

    Raises:
        HTTPException: 허용 밖 값이면 400 — 오타를 기본 프리셋으로 흡수하면 사용자는 다른 눈금으로
            검색된 줄 안다(``mode`` 검증과 같은 원칙).
    """
    name = (preset or DEFAULT_PRESET).strip().lower()
    if name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 preset: {preset!r} (허용: {', '.join(PRESETS)})",
        )
    return name


def _base_tuning() -> SearchTuning:
    """서버 설정에서 기본 튜닝 묶음을 해소한다 — 코어가 ``tuning`` 인자 없이 하는 것과 **같은 규칙**.

    같은 설정 객체에 같은 classmethod 를 쓰므로 값이 코어의 자체 해소와 같다. 그래서 ``default``
    프리셋을 명시적으로 넘겨도 응답은 인자를 안 넘긴 종전과 완전히 같다. 설정 미초기화(순수 단위
    테스트)면 상수 기본값 — 이것도 코어 폴백과 같다.

    Returns:
        설정에서 해소한 ``SearchTuning``.
    """
    try:
        cfg = get_current_settings()
    except RuntimeError:
        return SearchTuning()
    return SearchTuning.from_settings(cfg)


def _tag_facet_limits() -> tuple[int, int]:
    """태그 패싯의 상위 노출 개수·노출 하한을 설정에서 읽는다.

    Returns:
        ``(top_n, min_count)``. 설정 미초기화면 코어 상수 기본값(12·2) — 설정 spec 의 기본과 같은 정본.
    """
    try:
        search_cfg = get_current_settings().search
    except RuntimeError:
        return TAG_FACET_TOP_N_DEFAULT, TAG_FACET_MIN_COUNT_DEFAULT
    return search_cfg.tag_facet_top_n, search_cfg.tag_facet_min_count


def _tag_facets(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """응답에 실리는 결과 행 전체(모달리티 합)에서 태그 패싯을 센다 — 결과-스코프(083 전달물 §1).

    세는 규칙은 코어 ``aggregate_tag_facets`` 그대로다(자체 집계 금지 — 건수 계약이 코어 테스트에
    묶여 있다). 여기서는 입력(지금 보이는 행)과 응답 모양만 정한다.

    Args:
        grouped: 모달리티별 결과 행(권한 투영·좁히기까지 끝난 것 — 화면에 보이는 그 행들).

    Returns:
        ``{"items": [{"label", "count"}, …], "has_more": bool}``. ``label_by_key`` 는 내부 산출물이라
        싣지 않는다.
    """
    rows = [row for bucket_rows in grouped.values() for row in bucket_rows]
    top_n, min_count = _tag_facet_limits()
    out = aggregate_tag_facets(rows, top_n=top_n, min_count=min_count)
    return {"items": out["items"], "has_more": out["has_more"]}


# ── 디버그 뷰(no_cutoff·compact) — 기본 off = 기존 grouped 응답 불변 ──────────────
# 간략 보기는 이미 계산된 결과를 다시 쓰기만 한다 — 검색을 두 번 돌리지 않는다.
# 된 grouped 위에서 계산 — 원시 search_hybrid 버킷 사용 시 tier 미투영 요약 유출이라 정제 후 입력(도메인 배제는 dormant).


def _clip_text(text: str, max_chars: int) -> str:
    """요약을 한 줄로 펴고 길면 잘라 준다.

    Args:
        text: 원본 텍스트(줄바꿈·연속 공백이 있어도 된다).
        max_chars: 최대 길이. **0 이하면 자르지 않는다**.

    Returns:
        한 줄로 정규화된 텍스트(잘렸으면 끝에 ``…``).
    """
    one_line = " ".join(text.split())
    if max_chars > 0 and len(one_line) > max_chars:
        return one_line[: max_chars - 1].rstrip() + "…"
    return one_line


def _compact_view(
    grouped: dict[str, list[dict[str, Any]]], query: str, limit: int
) -> dict[str, Any]:
    """모달리티 버킷 결과를 한눈에 보기 좋은 단일 랭킹으로 축약한다(디버그·순수).

    각 행은 순위·모달리티·점수·파일명·요약만 남긴다. 점수 내림차순, 동점은 자산 id 순으로
    갈라 순서를 고정한다. 모달리티를 합쳐 상위 ``limit`` 건만 낸다.

    Args:
        grouped: 모달리티별 결과. **이미 권한 투영을 거친 것**을 넣어야 한다 — 원시 결과를
            넣으면 가려야 할 요약이 그대로 노출된다.
        query: 표시용 질의 문자열.
        limit: 낼 행 수 상한.

    Returns:
        축약된 단일 랭킹 dict. 입력 ``grouped`` 는 이미 clearance
    projection 된 portal 결과라 tier 미투영 유출이 없다(도메인 배제는 dormant).
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
    q: str = Query(..., description="검색 질의(한국어)"),
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
    """하이브리드 검색 결과를 **모달리티별 그룹**으로 돌려준다.

    모달리티끼리 점수 척도가 달라 **하나의 순위로 합치지 않는다** — 합치면 특정 모달리티가
    통째로 밀려난다. 대신 섹션마다 독립 순위를 매겨 상위 N개씩 내보낸다(전체
    코퍼스 페이징은 아직 없다).
    """
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

    # 풀 하한: 요청 풀이 노출 size 보다 얕으면 size 로 끌어올린다(size 계약 보장 + 승격 여지 확보).
    effective_pool = max(limit_per_bucket, size)
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

    # 모달리티별로 독립 순위를 매겨 상위 N개씩 담는다(배제 목록은 현재 비어 있다).
    grouped_raw = group_ranked(result, limit_per_modality=size, exclude_domains=_EXCLUDE_DOMAINS)

    # 권한 투영과 주제 패싯을 **한 트랜잭션에서** 끝낸다 — 연결을 두 번 잡지 않기 위해서다.
    def _project_and_facet(conn: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        """권한별 필드 가리기와 주제 패싯 계산을 **한 번의 조회**로 끝낸다(연결을 두 번 잡지 않게)."""
        projected = project_grouped(conn, grouped_raw, clearance=principal.clearance)
        facet = _search_topic_facet(projected)
        return projected, facet

    grouped, topic_facets = _infra._run_in_db(_project_and_facet)

    # ── 결과 내 재검색(091) ────────────────────────────────────────────────────
    # 🔴 **서버에 다시 묻지 않는다.** 질의를 바꿔 재검색하면 게이트가 다시 판정해 원 결과에
    #    없던 것이 나타난다(083 실측: 재검색 방식은 93항목 중 80%만 일치). 이번 결과만 걸러
    #    "47건 중 12건"이 정의상 성립하게 한다. 판단 로직은 코어 순수 함수 한 곳에 있다.
    # ⚠️ 좁히기는 compact 뷰 **앞**에 둔다 — compact 가 요약을 자르므로, 뒤에 두면 잘린 글자로
    #    걸러져 "화면엔 보이는데 안 걸림"이 생긴다(spec 091 §2-5). 여기서는 원문을 본다.
    scope_counts = {modality: len(rows) for modality, rows in grouped.items()}
    refine_applied = bool(refine and refine.strip())
    if refine_applied:
        grouped = {
            modality: refine_rows(rows, refine, fields_of=asset_refine_fields)
            for modality, rows in grouped.items()
        }
        # 패싯은 "지금 보이는 결과" 스코프다(083 SC-02) — 좁힌 뒤로 다시 센다. 순수 계산이라
        # DB 를 다시 잡지 않는다.
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
    # 이번 검색에 실제 적용된 튜닝(프리셋 이름 + 값 전부) — 서버 설정이 나중에 바뀌어도 이 응답이
    # 왜 이렇게 나왔는지 그대로 재현할 수 있다(헌법 3조 · ADR §6-3).
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
