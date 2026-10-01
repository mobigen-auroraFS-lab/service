"""CORS 가 허용하는 요청 헤더와 화면이 읽을 수 있게 노출하는 응답 헤더(단일 책임: 두 목록의 **정본**).

미들웨어(``service.api``)와 미처리 500 처리기(``errors``)가 **같은 목록**을 써야 한다 — 500 응답은 CORS 미들웨어 바깥에서 만들어져
처리기가 헤더를 직접 붙이는데, 목록이 갈리면 정상 응답과 500 이 서로 다른 헤더를 노출한다.

  · 요청 헤더: 인증 · JSON 본문 · **이어받기**(``Range`` · ``If-Range``). 이어받기 요청은 ``Authorization`` 과 함께면 브라우저가 사전 요청에
    둘 다 적는데, 허용 목록에 없으면 사전 요청이 400(「Disallowed CORS headers」)이라 다른 오리진에서는 이어받기가 아예 안 된다.
  · 응답 헤더: 요청 ID(``X-Request-ID`` · 오류 문의 열쇠) · 내려받기용(``Content-Disposition`` 파일명 · ``Content-Range`` ·
    ``Accept-Ranges`` · ``ETag``) · 묶음용(``X-Bundle-*``). 노출하지 않으면 다른 오리진의 화면은 이 값을 읽지 못한다.
"""

from __future__ import annotations

from service.api.request_log import REQUEST_ID_HEADER

CORS_ALLOW_HEADERS: tuple[str, ...] = ("Authorization", "Content-Type", "Range", "If-Range")
CORS_EXPOSE_HEADERS: tuple[str, ...] = (
    REQUEST_ID_HEADER, "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag",
    # 묶음(zip) 응답: 담긴 수 · 빠진 수 · 원본 합계 · 잘림 — 화면이 받은 zip 이 온전한지 맞춰 본다(``bundle_response``).
    "X-Bundle-Count", "X-Bundle-Files", "X-Bundle-Missing", "X-Bundle-Bytes", "X-Bundle-Truncated",
)
