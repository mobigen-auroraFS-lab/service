"""검색 결과를 **모달리티별 독립 순위**로 묶는다 — 순수 함수(DB·IO 없음).

**흐름에서의 위치**: 검색이 돌려준 모달리티별 버킷을 화면이 그릴 모양으로 정리한다.

**모달리티를 하나의 순위로 합치지 않는다**
    버킷마다 점수를 내는 방식이 달라 숫자의 뜻이 서로 다르다 — 같은 0.8 이 텍스트와 영상에서
    같은 정도의 적합함을 뜻하지 않는다. 한 줄로 세우면 구조적으로 점수가 큰 모달리티가 상단을
    독식하고 나머지는 통째로 밀려난다. 그래서 **모달리티 안에서만** 비교한다. 화면도 어차피
    섹션별로 나눠 보여 준다.

**동점 순서를 못 박는다** — 점수가 같을 때 자산 id 로 갈라, 같은 질의가 매번 같은 순서를 낸다.

⚠️ **가벼운 모듈만 import 한다.** 검색 엔진·임베딩 쪽 모듈을 들여오면 모델까지 딸려 올라와
이 모듈을 쓰는 가벼운 테스트가 무거워진다. 코어에서는 **순수·의존 0 인 정본 모듈만** 가져온다
(파일명 규칙 · 버킷↔모달리티 표 · 유한 실수 정화). 규칙을 여기 사본으로 두지 않는다(093 1단계).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 파일명 규칙(경로→basename · 아카이브 asset_id 접두 제거)은 코어 정본을 그대로 쓴다 — 같은 정규식이
# 두 곳에 있으면 접두 포맷이 바뀔 때 한쪽만 고쳐진다(종전 사본 ``_ASSET_ID_PREFIX`` 제거).
from src.config.filename_util import basename_of, strip_asset_id_prefix

# 응답 버킷 키(``text_documents`` 등) → 모달리티 라벨. 코어 검색이 버킷을 만드는 표의 역표 —
# 검색 쪽이 버킷 이름을 바꾸면 여기도 자동으로 따라간다(종전 사본 ``_BUCKET_TO_MODALITY`` 제거).
from src.config.search_modalities import BUCKET_TO_MODALITY

# 점수를 유한 실수로 정화하는 규칙(NaN·무한대 → 0.0)도 코어 정본 하나만 쓴다.
from src.domain.numeric import safe_float


def _row_similarity(row: dict[str, Any]) -> float:
    """행의 점수를 **유한한 실수**로 읽는다.

    정화 규칙은 코어 ``safe_float`` 하나다(종전에는 같은 로직을 여기 다시 적었다 — 093 1단계에서
    정본 참조로 바꿈. 코어의 그 모듈은 의존 0 이라 이 모듈의 가벼움이 유지된다).

    Args:
        row: 결과 행. 점수 키가 없거나 값이 이상해도 예외를 올리지 않는다.

    Returns:
        유한 실수. 읽을 수 없거나 NaN·무한대면 0.0(정렬이 실행마다 달라지는 것을 막는다).
    """
    return safe_float(row.get("similarity"))


def display_name(uri: str) -> str:
    """file_uri(전체 경로/URI)에서 **표시용** 파일명을 뽑는다(결정적·순수).

    공통 코어 ``basename_of``(쿼리/프래그먼트 제거·백슬래시 정규화) 위에 아카이브 asset_id
    id 접두 제거를 합쳐서 처리한다. 경로에서 파일명만 뽑는 일은 코어 함수가 맡고,
    프리픽스 제거는 표시 전용 책임이라 여기서만 얹는다(색인·샘플 경로는 프리픽스를 벗기지 않음).

    Args:
        uri: 자산 경로 또는 URI.

    Returns:
        표시용 파일명. 입력이 비면 빈 문자열.

    **공개 심볼**: 상세 응답(``asset_detail``)·테스트가 모듈 경계 너머로 재사용하는 표시명 단일
    여러 모듈이 함께 쓰므로 밑줄 없는 공개 이름으로 둔다(비공개 이름을 남이 import 하지 않게).
    """
    return strip_asset_id_prefix(basename_of(uri))


def _sort_key(item: dict[str, Any]) -> tuple[float, str]:
    """버킷 안 정렬 키 — 유사도 내림차순, 동점은 자산 id 순.

    유사도를 반올림해 비교하는 이유: 부동소수 끝자리 차이로 순서가 뒤집히면 같은 질의가
    실행마다 다른 순서를 낸다.

    Args:
        item: 결과 행.

    Returns:
        정렬 키 튜플.
    """
    return (-round(_row_similarity(item), 6), str(item.get("asset_id", "")))


def _ext_of(file_name: str) -> str:
    """표시 파일명에서 확장자를 뽑는다(소문자 · 없으면 빈 문자열).

    Args:
        file_name: 표시용 파일명. 점이 없거나 점으로 끝나면 확장자가 없는 것으로 본다.

    Returns:
        확장자(``jpg``) 또는 빈 문자열.
    """
    if "." not in file_name or file_name.endswith("."):
        return ""
    return file_name.rsplit(".", 1)[-1].strip().lower()


def _shape(row: dict[str, Any], modality: str) -> dict[str, Any]:
    """원시 검색 행 → 포탈 응답 항목.

    ``domain_label``: 권한별 필드 가리기를 계산할 때 쓴다.

    Args:
        row: 검색 엔진이 준 원시 행.
        modality: 이 행이 속한 버킷 이름(행에 함께 담는다).

    Returns:
        응답 항목 dict. 도메인 라벨이 없으면 기본값으로 채운다(뒤 단계가 None 을 만나지 않게).
    """
    display = display_name(str(row.get("file_uri", "")))
    return {
        "asset_id": str(row.get("id", "")),
        "modality": modality,
        "similarity": _row_similarity(row),
        "summary": row.get("summary", "") or "",
        "file_name": display,
        "domain_label": row.get("domain_label") or "general",
        # 주제 패싯·결과 좁히기에 쓸 값을 그대로 통과시킨다 — 화면이 이미 받은 결과로
        # 이 topics 로 클라 필터(재검색 없이) → 패싯 수와 표시 수 일치·컷오프 무관.
        "topics": [str(t) for t in (row.get("topics") or [])],
        "subtopics": [str(t) for t in (row.get("subtopics") or [])],
        # 부모·자식을 따로 내리면 화면이 둘을 곱해 있지도 않은 조합을 그린다 — 짝을 그대로 내린다.
        # 색인에 짝이 없는 옛 문서는 빈 목록으로 와, 화면이 부모 목록만으로 그리게 된다.
        "topic_pairs": [str(t) for t in (row.get("topic_pairs") or [])],
        # 태그 원문 배열(083 FR-106). 항상 있는 키 — 없으면 빈 배열(화면이 키 유무를 분기하지 않게).
        # 태그 패싯(meta.tag_facets)·화면의 패싯 클릭 좁히기·결과 내 재검색(091)이 이 값을 본다.
        "tags": [str(t) for t in (row.get("tags") or [])],
        # 파일 확장자 — 표의 "종류" 칸. 색인에도 있지만 결과 행에는 오지 않아 파일명에서 뽑는다
        # (경로가 아니라 표시 파일명 기준이라 자산 id 접두가 이미 벗겨진 값이다).
        "file_ext": _ext_of(display),
    }


def group_ranked(
    search_result: dict[str, Any],
    *,
    limit_per_modality: int,
    exclude_domains: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, Any]]]:
    """모달리티 버킷 dict 를 모달리티별 독립 랭킹 ``{modality: [rows]}`` 로 묶는다.

    모달리티끼리 **점수를 섞지 않는다** — 척도가 달라 한 줄로 세우면 특정 모달리티가 통째로
    밀려난다. 그래서 버킷마다 따로 순위를 매긴다.

    Args:
        search_result: 검색이 돌려준 원시 결과.
        limit_per_modality: 버킷당 담을 최대 건수. **1 미만이면 예외** — 0을 허용하면
            빈 결과가 정상인지 설정 실수인지 구분되지 않는다.
        exclude_domains: 제외할 도메인. 기본은 비어 있다(전 도메인 노출).

    Returns:
        ``{modality: [rows]}``. **입력에 있던 버킷만** 키로 나타난다(빈 입력이면 빈 dict).

    Raises:
        ValueError: ``limit_per_modality`` 가 1 미만일 때.
    """
    if limit_per_modality < 1:
        raise ValueError("limit_per_modality 는 1 이상")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for bucket, rows in (search_result.get("results") or {}).items():
        modality = BUCKET_TO_MODALITY.get(bucket, bucket)
        shaped: list[dict[str, Any]] = []
        for row in rows or []:
            label = row.get("domain_label")
            if label is not None and label in exclude_domains:
                continue  # 배제 목록에 든 도메인
            shaped.append(_shape(row, modality))
        shaped.sort(key=_sort_key)
        grouped[modality] = shaped[:limit_per_modality]
    return grouped


def asset_refine_fields(
    row: Mapping[str, Any], *, summary: str | None = None
) -> list[str]:
    """결과 내 재검색(091)이 **글자를 찾아볼 필드**를 응답 행에서 뽑는다(순수).

    좁히는 판단 자체는 코어 ``src.search.refine.refine_rows`` 가 한다. 이 함수는 그 판단에
    "어느 글자를 보라"고 넘겨 주는 재료 추출기다. 코어가 아니라 **여기** 있는 이유는 읽는 키
    (``file_name``·``summary``·``tags``)가 위 ``_shape`` 가 만든 **응답 모양**이기 때문이다 —
    응답 모양을 정한 쪽이 그 키를 알아야 하고, 코어가 이 키를 알면 화면 모양이 바뀔 때 코어를
    함께 고쳐야 한다(093 책무 경계 규칙 ② "프론트가 바뀌면 함께 바뀌는 것은 백엔드").

    셋을 고른 이유는 **화면 카드에 실제로 보이는 값**이기 때문이다 — "보이는 것으로 걸러진다"는
    계약이 서면 사용자가 결과에 놀라지 않는다. 검색 내부용 키(``_kwtext`` 등)는 보지 않는다.

    Args:
        row: ``_shape`` 가 만든 결과 행. 세 키 중 없거나 타입이 다른 것은 그 축이 없는 것으로
            본다(행 모양이 바뀌어도 좁히기 전체가 죽지 않게).
        summary: 요약 **클립 전 원문**. 간략 보기는 요약을 자르는데, 잘린 글자로 거르면
            "화면엔 보이는데 안 걸림"이 생긴다. ``None`` 이면 행의 ``summary`` 를 쓴다.

    Returns:
        빈 값을 제외한 필드 문자열 목록(파일명 → 요약 → 태그 순).
    """
    out: list[str] = []
    file_name = row.get("file_name")
    if isinstance(file_name, str) and file_name:
        out.append(file_name)

    text = summary if summary is not None else row.get("summary")
    if isinstance(text, str) and text:
        out.append(text)

    tags = row.get("tags")
    # 문자열 하나가 오면 글자 단위로 순회돼 쓰레기 값이 생긴다 — 배열만 받는다.
    if isinstance(tags, (list, tuple)):
        out.extend(t for t in tags if isinstance(t, str) and t)
    return out
