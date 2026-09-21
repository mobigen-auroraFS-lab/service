"""화면이 **고를 값의 목록**을 받아 가는 창구 — 관계 종류·태그.

검색 결과에서 세는 칩과 다르다. 칩은 "지금 결과 안에서 몇 건"이고 여기는 "무엇을 고를 수 있나"다.
화면이 태그를 주제로 좁혀 보여 주거나 관계 종류를 한글 이름으로 적으려면, 결과와 무관한 목록이
필요해서 따로 둔다.

⚠️ 관계 종류는 관리자 창구(`/admin/relation-kinds`)에도 있다. 같은 조회를 포탈 경로로 여는 이유:
   상세 화면이 관계를 그릴 때 코드(``same_domain``) 대신 한글 이름을 써야 하는데, 그 하나 때문에
   화면이 관리자 경로를 부르면 **권한 경계가 흐려진다**(운영 화면과 사용자 화면이 같은 창구를 쓴다).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api.routes.file_search import _QUERY_MAX_LEN as QUERY_MAX_LEN
from service.portal.auth import require_principal
from service.portal.common.db_manager import DbManager

router = APIRouter(tags=["catalog"], dependencies=[Depends(require_principal)])

# 관계 종류 상태 어휘 — 관리자 창구와 **같은 값**을 쓴다(둘이 갈리면 화면이 다른 목록을 본다).
_RELATION_KIND_STATUSES = ("active", "inactive")

# 태그 목록 기본·최대 개수. 화면은 상위 몇 개만 칩으로 보여 주고 나머지는 '전체 보기'에서 고른다.
_TAG_LIMIT_DEFAULT = 50
_TAG_LIMIT_MAX = 500


@router.get("/relation-kinds")
def relation_kinds(
    status: str | None = Query(None, description="관계 종류 상태: active | inactive(생략=전체)"),
) -> dict[str, Any]:
    """관계 종류 통제 어휘를 조회한다(조회 전용).

    Returns:
        ``{rows: [{kind_code, kind_name_ko, description, status}], total}`` — kind_code 오름차순.

    Raises:
        HTTPException: 허용 목록 밖 ``status`` 는 400.
    """
    if status is not None and status not in _RELATION_KIND_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 status: {status!r} (허용: {list(_RELATION_KIND_STATUSES)})")
    # 같은 조회를 관리자 저장소에서 가져다 쓴다 — 권한 경계는 **라우트**가 지키고(위 주석),
    # 저장소는 표 단위로만 갈린다(같은 질의를 두 벌 두면 결과가 갈릴 수 있다).
    return DbManager.read(lambda repo: repo.admin.relation_kinds(status=status))


@router.get("/tags")
def tags(
    topic: list[str] = Query(default=[], description="주제로 좁히기(반복 가능 · 여럿이면 또는)"),
    subtopic: list[str] = Query(default=[], description="하위주제로 좁히기(반복 가능 · 여럿이면 또는)"),
    q: str | None = Query(None, max_length=QUERY_MAX_LEN,
                          description="태그 이름 부분 일치(대소문자 무시)"),
    limit: int = Query(_TAG_LIMIT_DEFAULT, ge=1, le=_TAG_LIMIT_MAX),
) -> dict[str, Any]:
    """고를 수 있는 태그(키워드)를 건수와 함께 돌려준다.

    주제·하위주제를 주면 그 안에서 쓰인 태그만 남는다 — 화면의 '주제 → 하위주제 → 태그' 잠금
    구조가 서버 값으로 성립한다.

    🔴 건수는 **자산 수**다(한 자산이 같은 태그를 여러 번 달아도 1). 검색 결과의 태그 칩과는
    세는 대상이 다르다 — 이쪽은 등록된 자산 전체이고, 칩은 지금 검색 결과다.

    Returns:
        ``{rows: [{tag, count}], total}`` — 건수 내림차순, 같으면 이름 오름차순.
    """
    rows = DbManager.read(
        lambda repo: repo.catalog.tags(topics=topic, subtopics=subtopic, q=q, limit=limit))
    return {"rows": rows, "total": len(rows)}
