"""예외 → HTTP 응답 변환(단일 책임: 실패를 **하나의 봉투**로 알리기).

**흐름에서의 위치**: 라우트가 던진 것이든, 프레임워크가 만든 것이든, 아무도 예상 못 한 것이든,
클라이언트로 나가기 직전 여기를 지난다. 화면은 이 모듈이 정한 모양 **하나만** 알면 된다.

봉투(실패 응답은 예외 없이 이 모양이다 · Content-Type 은 항상 ``application/json``)::

    {"detail": "<사람이 읽는 한국어 한 문장>",
     "errors": [{"loc": ["query", "size"], "msg": "1 이상이어야 합니다",
                 "type": "greater_than_equal"}]}   # 칸별 검증 실패일 때만 붙는다

``detail`` 은 **언제나 문자열 하나**다 — 화면이 그대로 띄울 수 있어야 하기 때문이다.
``errors`` 는 어느 칸이 왜 틀렸는지 기계가 읽는 부분이라, 있을 때만 붙는다(칸 단위 검증 422).

🔴 **왜 이 모듈이 필요한가(실측 2026-09-21)** — 손대기 전에는 실패 응답이 네 가지 모양이었다.
  · ``{"detail": "<문장>"}``            우리가 던진 4xx·5xx
  · ``{"detail": [{type, loc, ...}]}``  FastAPI 자동 검증 422 — ``detail`` 이 **배열**이라 모양이 다름
  · ``text/plain "Internal Server Error"``  미처리 예외 — **JSON 조차 아니었다**
  · uvicorn 프로토콜 계층 400/무응답     앱에 닿기 전이라 여기서 통일할 수 없다(문서로만 알린다)

앞의 셋을 하나로 모은다. 마지막 하나는 ASGI 서버 영역이라 손이 닿지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    """실패 응답을 **공용 봉투**로 만든다 — 실패 경로는 전부 이 함수를 지난다.

    Args:
        status_code: HTTP 상태 코드.
        detail: 화면이 그대로 띄울 수 있는 한국어 한 문장.
        errors: 칸별 검증 실패 목록(있을 때만 붙는다). 빈 목록이면 붙이지 않는다 —
            "칸 정보가 있다"와 "없다"를 키 유무로 가른다.
        headers: 응답 헤더(416 의 ``Content-Range``, 401 의 ``WWW-Authenticate`` 등을 잃지 않는다).

    Returns:
        ``{"detail": str}`` 또는 ``{"detail": str, "errors": [...]}`` 를 담은 JSON 응답.
    """
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
            "Access-Control-Expose-Headers": REQUEST_ID_HEADER, "Vary": "Origin"}


# ── 처리기 ──────────────────────────────────────────────────────────────────────

async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """``HTTPException`` → 공용 봉투. 라우트가 고른 상태 코드와 헤더를 그대로 보존한다.

    Args:
        request: 들어온 요청(로깅용).
        exc: ``starlette.exceptions.HTTPException``. FastAPI 의 ``HTTPException`` 도 이 하위형이다.

    Returns:
        ``{"detail": "<문장>"}`` 봉투. 상태 코드·헤더는 예외가 지정한 그대로다.
    """
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
    """FastAPI 자동 검증 실패(422) → 공용 봉투.

    기본 동작은 ``detail`` 에 **배열**을 담아 우리 4xx 와 모양이 달랐다. 여기서 사람이 읽는 한 문장을
    ``detail`` 로 올리고, 칸별 정보는 ``errors`` 로 내린다 — 모양은 하나가 되고 정보는 잃지 않는다.

    🔴 **받은 값(``input``)은 되돌려 보내지 않는다** — 5MB 본문이 그대로 반사되거나 남이 보낸 값이
    화면에 그려질 수 있다. 진단이 필요하면 서버 로그를 본다.

    Args:
        request: 들어온 요청.
        exc: ``RequestValidationError``.

    Returns:
        422 봉투. ``errors`` 에 ``{loc, msg, type}`` 목록이 붙는다.
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
    """아무도 예상 못 한 예외(500) → 공용 봉투.

    이 처리기가 없으면 Starlette 이 ``text/plain`` 으로 ``Internal Server Error`` 를 보낸다 —
    JSON 을 기대하고 파싱하던 화면이 그 자리에서 함께 깨진다(실측 2026-09-21).

    예외는 **로그에만** 남긴다. 처리 후 Starlette 이 예외를 다시 올려 서버 로그에도 스택이 남는다.

    Args:
        request: 들어온 요청.
        exc: 잡히지 않은 예외.

    이 응답은 CORS 미들웨어를 지나지 않으므로 허용 오리진이면 CORS 헤더를 직접 붙인다(``CORS_ORIGINS``).

    Returns:
        500 봉투(내용은 고정 문구 — 내부 정보를 싣지 않는다).
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
    _LOG.warning("OpenSearch 연결 실패(503 반환): %s %s — %s", request.method, request.url.path, exc)
    return envelope(503, "검색 엔진(OpenSearch) 연결 실패 — 잠시 후 다시 시도해 주세요.")


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
    """경로·쿼리에 NUL 바이트가 있으면 **DB 에 닿기 전에** 400 으로 끊는다.

    검사는 두 번의 부분문자열 스캔뿐이라 정상 요청에 얹히는 비용이 사실상 없다.
    경로는 ASGI 서버가 이미 퍼센트 디코딩해 주므로 날 바이트로, 쿼리는 원문이라 ``%00`` 으로 본다
    (``%2500`` 은 문자열 ``%00`` 으로 풀리는 정상 입력이라 걸리지 않는다).

    Args:
        request: 들어온 요청.
        call_next: 다음 처리 단계.

    Returns:
        NUL 이 없으면 아래 단계의 응답 그대로, 있으면 400 봉투.
    """
    path = request.scope.get("path") or ""
    query = request.scope.get("query_string") or b""
    if "\x00" in path or b"%00" in query or b"\x00" in query:
        _LOG.warning("NUL 바이트 요청 차단(400): %s %s", request.method, path.replace("\x00", "\\0"))
        return envelope(400, _NUL_DETAIL)
    return await call_next(request)
