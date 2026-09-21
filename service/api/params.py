"""요청 파라미터 검증 의존성(단일 책임: 입력을 믿을 수 있는 값으로 바꾸기).

여러 라우트가 같은 검사를 복제하면 한 곳만 고쳐져 서로 어긋난다 — 공용 의존성으로 둔다.
"""

from __future__ import annotations

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
