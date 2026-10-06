"""CORS 허용 요청 헤더와 노출 응답 헤더의 정본. 미들웨어와 미처리 500 처리기가 같은 목록을 써야 정상 응답과 500 이 같은 헤더를 노출한다.

요청 헤더에는 이어받기(``Range`` · ``If-Range``)가 들어 있어야 다른 오리진에서 사전 요청이 통과한다.
응답 헤더는 요청 ID · 내려받기(``Content-Disposition`` · ``Content-Range`` · ``Accept-Ranges`` · ``ETag``) · 묶음(``X-Bundle-*``) — 노출하지 않으면 다른 오리진의 화면이 읽지 못한다.
"""

from __future__ import annotations

from service.api.request_log import REQUEST_ID_HEADER

CORS_ALLOW_HEADERS: tuple[str, ...] = ("Authorization", "Content-Type", "Range", "If-Range")
CORS_EXPOSE_HEADERS: tuple[str, ...] = (
    REQUEST_ID_HEADER, "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag",
    # 묶음(zip) 응답: 담긴 수 · 빠진 수 · 원본 합계 · 잘림 — 화면이 받은 zip 이 온전한지 맞춰 본다(``bundle_response``).
    "X-Bundle-Count", "X-Bundle-Files", "X-Bundle-Missing", "X-Bundle-Bytes", "X-Bundle-Truncated",
)
