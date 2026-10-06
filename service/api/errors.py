"""예외 → HTTP 응답 변환 — 실패를 하나의 봉투로 알린다. 라우트가 던진 것 · 프레임워크가 만든 것 · 예상 못 한 것 모두 클라이언트로 나가기 직전 여기를 지난다.

봉투(Content-Type 은 항상 JSON)::

    {"detail": "<사람이 읽는 한국어 한 문장>",
     "errors": [{"loc": ["query", "size"], "msg": "1 이상이어야 합니다", "type": "greater_than_equal"}]}   # 칸별 검증 실패(422)일 때만

``detail`` 은 언제나 문자열 하나다. uvicorn 프로토콜 계층의 400 · 무응답은 앱에 닿기 전이라 통일할 수 없다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from service.api.cors import CORS_EXPOSE_HEADERS
from service.api.request_log import REQUEST_ID_HEADER

_LOG = logging.getLogger("meta_extract.portal_api")


# 검색 엔진 연결 실패는 **503** 으로 응답한다 — 코드 버그(500)와 구분해야 운영자가
# 운영 알람·관측 구분용(ConnectionTimeout 은 ConnectionError 하위라 함께 잡힘). opensearchpy 는
# 알람을 나눌 수 있다. 라이브러리가 없는 환경에서도 앱이 뜨도록 import 는 지연·방어적으로 한다.
try:
    from opensearchpy.exceptions import ConnectionError as OSConnectionError
except ImportError:  # 미설치 환경 방어(검색 요청 시점에 별도 ImportError 로 드러남)
    OSConnectionError = None  # type: ignore[assignment,misc]


# ── 봉투 ────────────────────────────────────────────────────────────────────────

def envelope(
    status_code: int,
    detail: str,
    *,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """실패 응답을 공용 봉투로 만든다(실패 경로는 전부 이 함수를 지난다). ``errors`` 는 있을 때만 붙고 ``headers`` 는 416 의 ``Content-Range`` · 401 의 ``WWW-Authenticate`` 등을 보존한다."""
    body: dict[str, Any] = {"detail": detail}
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body, headers=headers)


# 프레임워크가 영어로 채워 넣는 기본 문구 → 한국어. 우리 문구는 전부 한국어인데 이것만 영어로
# 섞이면 화면이 두 언어를 띄운다. **정확히 일치할 때만** 바꾼다 — 라우트가 직접 쓴 문구는 건드리지
# 않는다(우연히 같은 문장을 쓸 일이 없는, 프레임워크 고정 문자열이다).
_FRAMEWORK_DETAIL_KO = {
    "Not Found": "요청한 경로가 없습니다 — 경로와 철자를 확인하십시오(경로는 대소문자를 구분합니다)",
    "Method Not Allowed": "이 경로가 받지 않는 메서드입니다 — 허용 메서드는 Allow 헤더에 있습니다",
    "There was an error parsing the body":
        "본문을 해석할 수 없습니다 — JSON 형식과 인코딩(UTF-8)을 확인하십시오",
    "Not authenticated": "인증이 필요합니다 — Authorization: Bearer <token> 을 붙이십시오",
    "Internal Server Error": "서버 내부 오류입니다 — 잠시 후 다시 시도하십시오",
}

# 미처리 예외의 문구. **예외 내용을 싣지 않는다** — 스택·SQL·경로가 섞여 나가면 공격자에게
# 내부 구조를 알려 주는 셈이다. 진단에 필요한 것은 전부 서버 로그에 남긴다.
_INTERNAL_DETAIL = "서버 내부 오류입니다 — 잠시 후 다시 시도하고, 계속되면 관리자에게 알리십시오"

# 🔴 CORS 허용 오리진(``__init__`` 이 ``PORTAL_CORS_ORIGINS`` 로 채운다). 미처리 예외(500)의 응답은
#    Starlette 구조상 **CORS 미들웨어 바깥**(ServerErrorMiddleware)에서 만들어져 허용 헤더가 붙지 않는다.
#    그러면 다른 오리진의 화면은 500 봉투를 읽지 못하고 "CORS 오류(Failed to fetch)"만 본다
#    (2026-09-28 재현). 그래서 500 처리기만 허용 오리진에 한해 같은 헤더를 직접 붙인다.
CORS_ORIGINS: frozenset[str] = frozenset()


def _cors_headers(request: Request) -> dict[str, str] | None:
    """허용 오리진에서 온 요청이면 CORS 미들웨어가 붙였을 헤더를 돌려준다(아니면 ``None``)."""
    origin = request.headers.get("origin")
    if not origin or origin not in CORS_ORIGINS:
        return None
    return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
            "Access-Control-Expose-Headers": ", ".join(CORS_EXPOSE_HEADERS), "Vary": "Origin"}


# ── 처리기 ──────────────────────────────────────────────────────────────────────

async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """``HTTPException`` → 공용 봉투(상태 코드 · 헤더는 예외가 지정한 그대로)."""
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 — 등록 타입 보증(방어)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return envelope(
        exc.status_code,
        _FRAMEWORK_DETAIL_KO.get(detail, detail),
        headers=getattr(exc, "headers", None),
    )


# pydantic 의 오류 종류 → 한국어. 없는 종류는 원문(``msg``)을 그대로 쓴다 — 번역이 없다고
# 응답이 비면 안 되기 때문이다. ``ctx`` 의 경계값을 문구에 넣어 "무엇이 맞는 값인지"까지 알린다.
_PYDANTIC_KO = {
    "missing": "필수 값입니다",
    "json_invalid": "본문이 올바른 JSON 이 아닙니다",
    "model_attributes_type": "본문은 JSON 객체여야 합니다",
    "list_type": "배열이어야 합니다",
    "string_type": "문자열이어야 합니다",
    "int_type": "정수여야 합니다",
    "int_parsing": "정수여야 합니다",
    "float_parsing": "실수여야 합니다",
    "bool_parsing": "true 또는 false 여야 합니다",
    "enum": "허용된 값이 아닙니다",
}
_PYDANTIC_KO_BOUND = {
    "greater_than_equal": ("ge", "{} 이상이어야 합니다"),
    "less_than_equal": ("le", "{} 이하여야 합니다"),
    "greater_than": ("gt", "{} 보다 커야 합니다"),
    "less_than": ("lt", "{} 보다 작아야 합니다"),
    "string_too_long": ("max_length", "{}자 이하여야 합니다"),
    "string_too_short": ("min_length", "{}자 이상이어야 합니다"),
}


def _field_message(err: dict[str, Any]) -> str:
    """pydantic 오류 한 건을 한국어 한 줄로 바꾼다."""
    kind = str(err.get("type", ""))
    if kind in _PYDANTIC_KO:
        return _PYDANTIC_KO[kind]
    if kind in _PYDANTIC_KO_BOUND:
        key, template = _PYDANTIC_KO_BOUND[kind]
        bound = (err.get("ctx") or {}).get(key)
        if bound is not None:
            return template.format(bound)
    return str(err.get("msg") or kind or "올바르지 않은 값입니다")


def _field_path(loc: object) -> str:
    """pydantic 의 ``loc`` 을 ``query.size`` · ``body.edge_ids[0]`` 처럼 읽히게 잇는다."""
    text = ""
    for part in loc if isinstance(loc, (list, tuple)) else ():
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text = f"{text}.{part}" if text else str(part)
    return text or "요청"


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """FastAPI 자동 검증 실패(422) → 공용 봉투. 사람이 읽는 문장을 ``detail`` 로, 칸별 정보를 ``errors`` 로 내린다.
    받은 값(``input``)은 되돌려 보내지 않는다(큰 본문이 반사되거나 남이 보낸 값이 화면에 그려지지 않게).
    """
    assert isinstance(exc, RequestValidationError)  # noqa: S101 — 등록 타입 보증(방어)
    errors = [
        {"loc": list(e.get("loc", ())), "msg": _field_message(e), "type": str(e.get("type", ""))}
        for e in exc.errors()
    ]
    if not errors:  # 방어: 빈 검증 오류(정상 경로에서는 나오지 않는다)
        return envelope(422, "요청 값이 올바르지 않습니다")
    head = errors[0]
    detail = f"요청 값이 올바르지 않습니다 — {_field_path(head['loc'])}: {head['msg']}"
    if len(errors) > 1:
        detail += f" (그 밖 {len(errors) - 1}건)"
    return envelope(422, detail, errors=errors)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """아무도 예상 못 한 예외(500) → 공용 봉투(고정 문구 — 내부 정보를 싣지 않는다). 예외는 로그에만 남긴다.
    이 응답은 CORS 미들웨어를 지나지 않으므로 허용 오리진이면 CORS 헤더를 직접 붙인다.
    """
    # 스택은 여기서 찍지 않는다 — 처리 뒤 Starlette 이 예외를 다시 올려 uvicorn 이 스택을 한 번 남긴다(두 번 찍으면 길다).
    # 이 줄은 **요청 ID 와 예외 종류**를 남겨 그 스택을 찾는 열쇠가 된다(예외 메시지는 값이 섞일 수 있어 싣지 않는다).
    request_id = request.scope.get("request_id")
    _LOG.error("미처리 예외(500): %s %s — %s", request.method, request.url.path, type(exc).__name__,
               extra={"request_id": request_id} if request_id else None)
    headers = dict(_cors_headers(request) or {})
    if request_id:       # 이 응답은 요청 로그 층 바깥에서 만들어져 머리를 그 층이 못 붙인다
        headers[REQUEST_ID_HEADER] = request_id
    return envelope(500, _INTERNAL_DETAIL, headers=headers or None)


async def os_unavailable_handler(request: Request, exc: Exception) -> Response:
    """OpenSearch 연결 실패 → 503(코드버그 500 과 구분·운영 알람용). __init__ 이 app 에 등록."""
    from starlette.concurrency import run_in_threadpool

    from service.api import search_health
    # 접속 시험(최대 2초)은 스레드에서 — 이벤트 루프에서 동기로 부르면 그동안 전 요청이 멈춘다.
    await run_in_threadpool(search_health.BREAKER.note_failure, exc)
    _LOG.warning("OpenSearch 연결 실패(503 반환): %s %s — %s", request.method, request.url.path, type(exc).__name__)
    return envelope(503, "검색 엔진(OpenSearch) 연결 실패 — 잠시 후 다시 시도해 주세요.", headers={"Retry-After": "5"})


async def search_unavailable_handler(request: Request, exc: Exception) -> Response:
    """검색 엔진 차단 중 → 곧바로 503 + ``Retry-After``."""
    return envelope(503, "검색 엔진(OpenSearch)에 연결할 수 없습니다 — 잠시 후 다시 시도해 주세요.", headers={"Retry-After": "5"})


async def db_unavailable_handler(request: Request, exc: Exception) -> Response:
    """DB 연결 실패 · 질의 시간 초과 → 503 + ``Retry-After``."""
    from service.api.db_health import RETRY_AFTER_SECONDS, DatabaseSlow
    if isinstance(exc, DatabaseSlow):
        return envelope(503, "데이터베이스 응답이 너무 늦습니다 — 잠시 후 다시 시도해 주세요.", headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
    return envelope(503, "데이터베이스에 연결할 수 없습니다 — 잠시 후 다시 시도해 주세요.",
                    headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


# ── 입력 위생: NUL 바이트 ────────────────────────────────────────────────────────
# 🔴 **PostgreSQL 은 text 에 NUL(0x00)을 못 넣는다** — 자유 문자열 파라미터가 그대로 SQL 로 내려가면
#    ``psycopg.DataError`` 가 나고 그것이 **500** 으로 샜다(실측 2026-09-21: GET 창구 108개 조합 중
#    33개). 닫힌 어휘·날짜·UUID 로 검증되는 칸은 앞에서 걸렸지만, 자유 문자열 칸은 막을 사람이
#    없었다. 칸마다 검사를 붙이면 새 칸이 생길 때마다 빠뜨리므로 **입구 한 곳**에서 끊는다.
#
#    본문(JSON)은 대상이 아니다 — 쓰기 창구의 본문 칸은 UUID·닫힌 어휘 검증을 이미 거치므로
#    NUL 이 SQL 까지 닿지 않는다(``params.uuid_list_or_400`` · ``REVIEW_STATUSES``).

_NUL_DETAIL = (
    "요청에 NUL(0x00) 바이트가 들어 있습니다 — 저장소가 받지 않는 문자입니다"
    "(경로·쿼리에서 제거하고 다시 보내십시오)"
)


async def reject_nul_bytes(request: Request, call_next: Callable) -> Response:
    """경로 · 쿼리에 NUL 바이트가 있으면 DB 에 닿기 전에 400 으로 끊는다(경로는 이미 디코딩된 날 바이트, 쿼리는 원문 ``%00`` 으로 본다)."""
    path = request.scope.get("path") or ""
    query = request.scope.get("query_string") or b""
    if "\x00" in path or b"%00" in query or b"\x00" in query:
        _LOG.warning("NUL 바이트 요청 차단(400): %s %s", request.method, path.replace("\x00", "\\0"))
        return envelope(400, _NUL_DETAIL)
    return await call_next(request)
