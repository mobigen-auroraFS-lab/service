"""요청 파라미터 검증 의존성(단일 책임: 입력을 믿을 수 있는 값으로 바꾸기).

여러 라우트가 같은 검사를 복제하면 한 곳만 고쳐져 서로 어긋난다 — 공용 의존성으로 둔다.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from fastapi import HTTPException, Query

from service.portal.common.timeline import TIMELINE_INTERVALS


def parse_dt(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` 또는 ISO datetime 문자열을 ``datetime`` 으로 파싱한다.

    Args:
        value: 날짜 문자열. 빈 값·``None`` 이면 필터를 걸지 않는다는 뜻이다.

    Returns:
        파싱된 ``datetime``, 또는 값이 없으면 ``None``.

    Raises:
        HTTPException: 형식이 틀렸을 때 422. **기본값으로 넘기지 않는다** — 사용자가 의도한
            기간과 다른 결과를 조용히 보여주면 안 되기 때문이다.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"날짜 형식 오류: {value!r}") from exc


def validated_interval(
    interval: str = Query("day", description="집계 단위: hour | day | week | month"),
) -> str:
    """시계열 라우트의 버킷 단위 값을 검증한다(허용 목록 밖이면 422).

    시계열 라우트들이 공유하는 **의존성**이다 — 핸들러마다 같은 검사를 복제하면 한 곳만
    고쳐져 서로 어긋난다. 통과하면 값을 그대로 넘겨 핸들러가 주입받는다.

    Args:
        interval: 버킷 단위. 허용 목록은 시계열 공용 상수 하나뿐이다.

    Returns:
        검증을 통과한 값.

    Raises:
        HTTPException: 허용 목록 밖이면 422.
    """
    if interval not in TIMELINE_INTERVALS:
        raise HTTPException(
            status_code=422,
            detail=f"interval 은 {'|'.join(TIMELINE_INTERVALS)} 만 허용: {interval!r}",
        )
    return interval


# ── 식별자 형식 ─────────────────────────────────────────────────────────────────
# 🔴 **DB 에 물어보기 전에 형식을 본다.** asset_id·edge_id 컬럼은 uuid 라, 형식이 아닌 값을 그대로
#    넘기면 PostgreSQL 이 ``invalid input syntax for type uuid`` 로 거절하고 그것이 **500** 으로
#    새어 나간다(실측 2026-09-21: `/assets/not-a-uuid` 등 10개 창구). 500 은 "서버가 고장났다"는
#    뜻이라, 클라이언트가 잘못 보낸 것과 구분되지 않는다.


def preview(values: Sequence[object], *, max_items: int = 5, max_len: int = 60) -> str:
    """잘못된 값을 응답 문구에 실을 때 **줄여서** 싣는다.

    🔴 받은 값을 그대로 되돌리면 요청 크기가 그대로 응답 크기가 된다 — 실측 2026-09-21: 5MB 본문을
    보내자 "UUID 형식이어야 함: ['aaa…']" 에 그 5MB 가 통째로 실려 나갔다. 무엇이 틀렸는지 알아볼
    만큼만 보여 주고 나머지는 건수로 알린다.

    Args:
        values: 문구에 실을 값들.
        max_items: 보여 줄 개수. 나머지는 "외 N건" 으로 줄인다.
        max_len: 값 하나의 최대 길이. 넘으면 잘라서 ``…`` 를 붙인다.

    Returns:
        ``['abc', 'de…'] 외 3건`` 꼴의 문자열.
    """
    shown = []
    for v in list(values)[:max_items]:
        text = str(v)
        shown.append(f"{text[:max_len]}…" if len(text) > max_len else text)
    rest = len(values) - max_items
    return f"{shown!r}" + (f" 외 {rest}건" if rest > 0 else "")


def is_uuid(value: str) -> bool:
    """UUID 형식인지만 본다(존재 여부는 보지 않는다 · 순수 함수)."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def uuid_or_404(value: str, *, detail: str) -> str:
    """경로의 자산 id 형식을 본다 — 형식이 아니면 **없는 것과 같게** 404.

    없는 id(404)와 형식이 틀린 id 를 **같은 응답**으로 준다 — 둘을 가르면 "그 모양의 id 는 존재할
    수 있다"를 알려 주는 셈이고, IDD 도 이 창구들의 404 를 사유 구분 없이 하나로 적었다.

    Args:
        value: 경로에서 받은 id.
        detail: 404 응답 문구. 창구마다 다르므로(다운로드·썸네일·묶음) 호출부가 준다.

    Returns:
        형식을 통과한 값 그대로.

    Raises:
        HTTPException: 형식이 아니면 404.
    """
    if not is_uuid(value):
        raise HTTPException(status_code=404, detail=detail)
    return value


def uuid_list_or_400(values: Sequence[str], *, field: str) -> list[str]:
    """본문으로 받은 id 목록의 형식을 본다 — 하나라도 형식이 아니면 400.

    쓰기 창구(관계 검토)는 404 가 아니라 **400** 이다 — 경로가 아니라 본문이라 "그런 자원이 없다"가
    아니라 "요청이 잘못됐다"가 맞고, 어느 값이 문제인지 돌려줘야 화면이 고칠 수 있다.

    Args:
        values: 검사할 id 목록.
        field: 응답 문구에 쓸 필드 이름.

    Returns:
        형식을 통과한 목록 그대로.

    Raises:
        HTTPException: 형식이 아닌 값이 있으면 400(어느 값인지 **줄여서** 함께 알린다 — ``preview``).
    """
    bad = [v for v in values if not is_uuid(v)]
    if bad:
        raise HTTPException(status_code=400, detail=f"{field} 는 UUID 형식이어야 함: {preview(bad)}")
    return list(values)
