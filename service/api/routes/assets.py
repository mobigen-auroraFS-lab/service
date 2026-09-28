"""사용자용 자산 라우트 — 상세 조회·주제 탐색·자산이 속한 개체.

**흐름에서의 위치**: 포탈 화면이 직접 부르는 경로들이다. 조회는 포탈 함수에 위임한다.

🔴 **원본 파일을 내주는 창구는 없다**(2026-09-28 삭제) — 원본 다운로드 · 썸네일 · 원문(글자) ·
관계 묶음 · 고른 자산 묶음은 파일 제공 방식을 다른 쪽과 **협의한 뒤 다시 설계**한다(`TODO.md`).
되살릴 때는 git 이력(`9d11d29`)의 구현과 IDD 보류 시트(IF-ASSET-03·04·05·11·12)를 참고하고,
원본 위치 · 접근 방식 · 변경 대조 규칙은 ``service/portal/asset/__init__.py`` 의 「원본 파일 전제」(연동 협의안 v1.0)를 따른다.

⚠️ **라우트 선언 순서가 동작을 가른다.** ``/assets/unclassified`` 처럼 고정된 경로를
``/assets/{asset_id}`` 보다 **먼저** 선언해야 한다 — 뒤에 두면 "unclassified" 가 자산 id 로
해석돼 영영 404 가 된다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import params
from service.portal.auth import Principal, require_principal
from service.portal.common.db_manager import DbManager

# 경로가 ``/assets`` 와 ``/topics`` 로 갈려 공통 접두사를 둘 수 없다 — 인증만 라우터에 건다.
# ⚠️ 주체가 필요한 핸들러만 ``principal`` 을 따로 선언한다.
router = APIRouter(tags=["assets"], dependencies=[Depends(require_principal)])


@router.get("/assets/unclassified")
def unclassified_assets(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """주제가 붙지 않은 자산을 페이징해 돌려준다 — 탐색 화면의 '미분류' 폴더.

    주제 트리(``/topics``)는 ``asset_topic`` 조인이라 주제 정본이 없는 자산(분류 실패·무내용)을 누락한다.
    자산을 '빠짐없이' 보이려면 이 엔드포인트로 미분류를 회수한다. 조회 전용·도메인 제외 없음·LLM 0.
    **라우트 순서**: ``/assets/{asset_id}`` catch-all 보다 먼저 등록해야 'unclassified' 가 asset_id 로
    오매칭되지 않는다(이 위치 유지).
    """
    return DbManager.read(lambda repo: repo.asset.unclassified(limit=limit, offset=offset))


@router.get("/assets/{asset_id}")
def asset_detail(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 1건 상세 — 메타·임베딩 요약·관계 미니뷰·자기주제.

    노출 여부 판정은 ``fetch_asset_detail`` 이 맡는다 — 없거나 등록 완료가 아니면 404.
    UUID 형식이 아닌 id 도 **같은 404** 다(둘을 가르면 "그 모양의 id 는 있을 수 있다"를 알려 준다).
    노출을 통과한 자산에는 주제 정보(``topics``·``same_topic_groups``)를 같은 읽기
    트랜잭션에서 함께 싣는다(신규 LLM 0). 게이트 미통과(None)면 주제 seam 미호출.
    """
    params.uuid_or_404(asset_id, detail="자산을 찾을 수 없거나 노출 대상이 아님")

    detail = DbManager.read(
        lambda repo: repo.asset.detail(asset_id=asset_id, clearance=principal.clearance))
    if detail is None:
        raise HTTPException(status_code=404, detail="자산을 찾을 수 없거나 노출 대상이 아님")
    return detail


@router.get("/topics")
def topics_list() -> dict[str, Any]:
    """주제 목록을 2단계(대주제 → 세부주제)로, 주제별 자산 수와 함께 돌려준다(조회 전용).

    ``list_topics`` 가 자기주제 정본(``asset_topic``·도메인 제외 없음)의 ``(topic_ko, subtopic_ko)`` 별 distinct
    정렬을 고정해(대주제 → 세부주제 이름순) 같은 요청이 늘 같은 순서를 낸다. 각 행에
    ``topic_asset_count``(주제 전체 distinct 자산 수) 동반(하위호환 필드).
    """
    return {"topics": DbManager.read(lambda repo: repo.asset.topics())}


@router.get("/topics/{topic}")
def topic_assets(
    topic: str,
    subtopic: str | None = Query(None, description="세부주제(주면 topic 하위로 좁힘·정확 일치)"),
    unassigned: bool = Query(
        False, description="'기타'(subtopic 미부여)만 — 값 매칭이 아닌 subtopic IS NULL"
    ),
    modality: str | None = Query(None, description="모달리티 폴더(text/image/video/audio) 필터"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """특정 주제에 속한 자산을 페이징 조회한다(조회 전용).

    ``assets_in_topic`` 이 그 주제의 자기주제 정본(``asset_topic``) 자산을 distinct·``asset_id asc`` 결정적
    정렬로 페이징한다. ``subtopic`` 미지정=topic 하위 전체·``unassigned=true``='기타'(IS NULL)만·
    ``modality`` 필터. 응답 ``modality_counts`` 는 필터 무관 전체 분포(모달리티 폴더 카운트).
    """
    return DbManager.read(
        lambda repo: repo.asset.topic_assets(
            topic_ko=topic,
            subtopic_ko=subtopic,
            unassigned_only=unassigned,
            modality=modality,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/assets/{asset_id}/mm-meta")
def asset_mm_meta(
    asset_id: str,
) -> dict[str, Any]:
    """자산 상세의 "이 파일이 속한 개체" 블록 — 코어 seam 위임.

    ⚠️ **묶음 크기 1 도 그대로 싣는다.** 목록 창구는 1 인 개체를 감추지만(자산 하나짜리는 "묶음"이
    아니다), 자산 쪽에서 보면 "이 파일이 그 개체에 속한다"는 것은 사실이다. 감출지는 화면이 정한다.

    Args:
        asset_id: 자산 UUID(문자열).

    Returns:
        ``{"items": [{entity_type, entity_uid, name, bundle_size, edge_id, status, reason}]}`` —
        종류·표기 키 오름차순. 소속이 없으면 빈 목록이다.
    """
    # 형식이 아닌 id 는 **없는 자산과 같게** 본다 — 이 창구는 미존재를 200·빈 목록으로 답한다.
    if not params.is_uuid(asset_id):
        return {"items": []}
    items = DbManager.read(lambda repo: repo.asset.entities_of(asset_id=asset_id))
    return {"items": items}
