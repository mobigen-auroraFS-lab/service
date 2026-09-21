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
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    Returns:
        500 봉투(내용은 고정 문구 — 내부 정보를 싣지 않는다).
    """
    _LOG.exception("미처리 예외(500): %s %s", request.method, request.url.path)
    return envelope(500, _INTERNAL_DETAIL)


async def os_unavailable_handler(request: Request, exc: Exception) -> Response:
    """OpenSearch 연결 실패 → 503(코드버그 500 과 구분·운영 알람용). __init__ 이 app 에 등록."""
    _LOG.warning("OpenSearch 연결 실패(503 반환): %s %s — %s", request.method, request.url.path, exc)
    return envelope(503, "검색 엔진(OpenSearch) 연결 실패 — 잠시 후 다시 시도해 주세요.")

