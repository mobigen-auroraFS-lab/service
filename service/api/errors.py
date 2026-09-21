"""예외 → HTTP 응답 변환(단일 책임: 장애를 코드로 구분해 알리기)."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

_LOG = logging.getLogger("meta_extract.portal_api")


# 검색 엔진 연결 실패는 **503** 으로 응답한다 — 코드 버그(500)와 구분해야 운영자가
# 운영 알람·관측 구분용(ConnectionTimeout 은 ConnectionError 하위라 함께 잡힘). opensearchpy 는
# 알람을 나눌 수 있다. 라이브러리가 없는 환경에서도 앱이 뜨도록 import 는 지연·방어적으로 한다.
try:
    from opensearchpy.exceptions import ConnectionError as OSConnectionError
except ImportError:  # 미설치 환경 방어(검색 요청 시점에 별도 ImportError 로 드러남)
    OSConnectionError = None  # type: ignore[assignment,misc]


async def os_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """OpenSearch 연결 실패 → 503(코드버그 500 과 구분·운영 알람용). __init__ 이 app 에 등록."""
    _LOG.warning("OpenSearch 연결 실패(503 반환): %s %s — %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "검색 엔진(OpenSearch) 연결 실패 — 잠시 후 다시 시도해 주세요."},
    )
