"""로그 설정(단일 책임: 어디로 · 어떤 모양으로 · 어느 수준부터 남기는가).

**원칙** — 흔한 서버 로그 관례를 따른다.
  · 로그는 **표준출력**으로 낸다(파일 회전·수집은 프로세스 관리자·컨테이너 런타임의 몫).
  · 수준과 형식은 **환경변수**로 고른다 — ``PORTAL_LOG_LEVEL``(기본 INFO) · ``PORTAL_LOG_FORMAT``(``text`` 기본 · ``json``).
    운영에서 로그 수집기(ELK · Loki 등)에 넣으려면 ``json`` 이 편하다(한 줄이 JSON 한 건).
  · 모든 줄에 **요청 ID** 가 붙는다 — 한 요청이 남긴 줄을 ID 하나로 모은다(``request_log`` 가 정한다).
    ``text`` 는 눈으로 읽기 쉽게 줄인다(날짜 · 시각만, 로거는 끝 이름만, ID 는 줄 끝에 — 요청 밖의 줄에는 ID 를 안 붙인다).
    ``json`` 은 줄이는 것 없이 전부 싣는다(수집기가 읽는다).
  · uvicorn 로그도 **같은 형식**으로 모은다. uvicorn 의 접근 로그(URL 전체 + 쿼리 문자열)는 끄고 앱이 한 줄로 남긴다
    (``request_log``) — 쿼리 문자열에 검색어 · 커서가 그대로 실려 줄이 길고 사용자 입력이 로그에 남기 때문이다.
  · 잡음이 큰 라이브러리 로그(HTTP 클라이언트 · 검색 엔진 클라이언트)는 WARNING 이상만 남긴다.

**적용 시점** — ``service.api`` 를 불러올 때 한 번 적용한다(``configure_from_env``). uvicorn 은 앱을 불러오기 **전에**
자기 로그를 세우므로 이 설정이 그 위에 덮인다. 테스트는 ``PORTAL_LOG_CONFIGURE=0`` 으로 건너뛴다(pytest 의 로그 포획을 건드리지 않는다).
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.config
import os
import re
import secrets
import threading
import time
import warnings
from datetime import UTC, datetime
from typing import Any

LOG_LEVEL_ENV = "PORTAL_LOG_LEVEL"
LOG_FORMAT_ENV = "PORTAL_LOG_FORMAT"
LOG_CONFIGURE_ENV = "PORTAL_LOG_CONFIGURE"

DEFAULT_LEVEL = "INFO"
DEFAULT_FORMAT = "text"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
FORMATS = ("text", "json")
_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}

# 읽기 쉬운 한 줄: ``10-01 08:59:49 INFO  access: GET /health 200 2.4ms 127.0.0.1 [b311300b24e2]``
#   ``lvl``(WARN · CRIT 로 줄임) · ``short``(끝 이름) · ``rid``(요청 안에서만 `` [ID]``)는 ``RequestIdFilter`` 가 채운다.
TEXT_FORMAT = "%(asctime)s %(lvl)-5s %(short)s: %(message)s%(rid)s"
TEXT_DATE_FORMAT = "%m-%d %H:%M:%S"

# 요청 밖(기동 · 종료 · 백그라운드)에서 남기는 줄의 요청 ID
NO_REQUEST = "-"
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default=NO_REQUEST)

# 요청 ID 로 받아 주는 모양 — 길이와 문자를 제한해 로그 줄 위조(개행 · 제어문자)를 막는다.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def new_request_id() -> str:
    """새 요청 ID(12자리 16진수 · 48비트) — 눈으로 읽고 옮겨 적을 수 있는 길이다(하루 요청에서 겹칠 일이 없다)."""
    return secrets.token_hex(6)


def valid_request_id(raw: str | None) -> str | None:
    """앞단(프록시)이 보낸 요청 ID 가 쓸 만한 모양이면 그대로, 아니면 ``None``."""
    if raw and _REQUEST_ID_RE.match(raw):
        return raw
    return None


_LEVEL_SHORT = {"WARNING": "WARN", "CRITICAL": "CRIT"}


def short_logger_name(name: str) -> str:
    """로거 이름을 읽기 쉽게 줄인다 — ``meta_extract.portal_api.access`` → ``access`` · ``uvicorn.error`` → ``uvicorn``."""
    if name == "meta_extract.portal_api":
        return "api"
    if name == "uvicorn" or name.startswith("uvicorn."):
        return "uvicorn"
    if name == "py.warnings":
        return "warn"
    return name.rsplit(".", 1)[-1]


class RequestIdFilter(logging.Filter):
    """모든 줄에 ``request_id`` 를 싣고, 읽기 쉬운 text 형식이 쓸 칸(``lvl`` · ``short`` · ``rid``)도 채운다.

    줄에 직접 준 ``request_id``(``extra``)가 있으면 그것을 쓴다.

    요청 안에서 만든 스레드 · 태스크는 문맥 변수를 복사해 가므로(``run_in_threadpool`` · ``create_task``)
    뒤에서 도는 감사 기록의 경고에도 같은 ID 가 붙는다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        record.lvl = _LEVEL_SHORT.get(record.levelname, record.levelname)
        record.short = short_logger_name(record.name)
        record.rid = "" if record.request_id == NO_REQUEST else f" [{record.request_id}]"
        return True


# 연결 실패를 알리는 외부 라이브러리 로거 — 같은 경고가 트레이스백과 함께 수십 번 쌓인다. 로거 이름의 어느 마디든 이 이름으로 시작하면 대상.
NOISY_LIBS = ("opensearch", "postgres_util", "psycopg", "urllib3")
DEDUP_WINDOW_SECONDS = 30.0
_NUMBERS = re.compile(r"[\d.]+")


class LibraryNoiseFilter(logging.Filter):
    """외부 라이브러리 로그의 트레이스백을 예외 한 줄로 줄이고, 숫자만 다른 같은 문장은 ``DEDUP_WINDOW_SECONDS`` 안에 한 번만 남긴다(다음에 「같은 경고 N건 생략」)."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: dict[tuple[str, int, str], tuple[float | None, int]] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if not any(part.startswith(NOISY_LIBS) for part in record.name.split(".")):
            return True
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            text = f"{text} — {type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''}".rstrip(": ")
            record.exc_info = None
            record.exc_text = None
        key = (record.name, record.levelno, _NUMBERS.sub("N", text)[:200])
        now = time.monotonic()
        with self._lock:
            last, skipped = self._seen.get(key, (None, 0))
            if last is not None and now - last < DEDUP_WINDOW_SECONDS:
                self._seen[key] = (last, skipped + 1)
                return False
            self._seen[key] = (now, 0)
            if len(self._seen) > 500:
                self._seen = {k: v for k, v in self._seen.items() if v[0] is not None and now - v[0] < DEDUP_WINDOW_SECONDS}
        record.msg = text + (f" (같은 경고 {skipped}건 생략)" if skipped else "")
        record.args = ()
        return True


# LogRecord 가 원래 갖는 속성 — 이 밖의 속성(``extra`` 로 준 값)만 JSON 에 따로 싣는다.
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName", "lvl", "short", "rid"}   # 뒤 셋은 text 형식 전용 칸(필터가 채운다)


class JsonFormatter(logging.Formatter):
    """한 줄 = JSON 한 건. ``ts``(UTC) · ``level`` · ``logger`` · ``msg`` · ``request_id`` + ``extra`` 로 준 칸 + ``exc``."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", NO_REQUEST),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key != "request_id":
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def _log_warning(message: Any, category: type[Warning], filename: str, lineno: int, file: Any = None, line: Any = None) -> None:
    """파이썬 경고를 **한 줄**로 남긴다 — 표준 방식(``logging.captureWarnings``)은 소스 줄까지 두 줄로 찍는다."""
    logging.getLogger("py.warnings").warning("%s: %s (%s:%d)", category.__name__, message, os.path.basename(filename), lineno)


def resolve_level(raw: str | None) -> tuple[str, str | None]:
    """환경변수 값 → (로그 수준, 잘못된 값이면 경고 문장). 빈 값은 기본 INFO."""
    text = (raw or "").strip().upper()
    if not text:
        return DEFAULT_LEVEL, None
    text = _LEVEL_ALIASES.get(text, text)
    if text in LEVELS:
        return text, None
    return DEFAULT_LEVEL, f"{LOG_LEVEL_ENV}={raw!r} 는 모르는 수준이다({', '.join(LEVELS)}) — {DEFAULT_LEVEL} 로 둔다"


def resolve_format(raw: str | None) -> tuple[str, str | None]:
    """환경변수 값 → (형식, 잘못된 값이면 경고 문장). 빈 값은 기본 text."""
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_FORMAT, None
    if text in FORMATS:
        return text, None
    return DEFAULT_FORMAT, f"{LOG_FORMAT_ENV}={raw!r} 는 모르는 형식이다({', '.join(FORMATS)}) — {DEFAULT_FORMAT} 로 둔다"


def build_config(level: str, fmt: str) -> dict[str, Any]:
    """``dictConfig`` 에 줄 설정. 핸들러는 표준출력 하나이고 나머지는 전부 그리로 흘려 보낸다."""
    noisy = {"level": "WARNING"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_id": {"()": RequestIdFilter}, "lib_noise": {"()": LibraryNoiseFilter}},
        "formatters": {
            "text": {"format": TEXT_FORMAT, "datefmt": TEXT_DATE_FORMAT},
            "json": {"()": JsonFormatter},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": fmt,
                "filters": ["request_id", "lib_noise"],
            },
        },
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            # uvicorn 은 자기 핸들러를 따로 달아 두므로 비우고 루트로 흘린다(같은 형식 · 같은 수준).
            "uvicorn": {"handlers": [], "level": level, "propagate": True},
            "uvicorn.error": {"handlers": [], "level": level, "propagate": True},
            # 접근 로그는 앱(``request_log``)이 한 줄로 남긴다 — uvicorn 것은 끈다.
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
            "opensearch": noisy,
            "urllib3": noisy,
            "httpx": noisy,
            "httpcore": noisy,
        },
    }


def configure_logging(level: str | None = None, fmt: str | None = None) -> tuple[str, str]:
    """로그를 세운다(여러 번 불러도 같은 결과). 수준 · 형식을 안 주면 환경변수에서 읽는다.

    Returns:
        실제로 적용한 ``(수준, 형식)``.
    """
    level_name, level_warn = resolve_level(level if level is not None else os.getenv(LOG_LEVEL_ENV))
    fmt_name, fmt_warn = resolve_format(fmt if fmt is not None else os.getenv(LOG_FORMAT_ENV))
    logging.config.dictConfig(build_config(level_name, fmt_name))
    # 파이썬 경고(라이브러리가 ``warnings`` 로 내는 것)도 같은 형식 **한 줄**로 모은다 — 기본은 소스 줄까지 두 줄로 따로 찍힌다.
    warnings.showwarning = _log_warning
    log = logging.getLogger("meta_extract.portal_api")
    for warn in (level_warn, fmt_warn):
        if warn:
            log.warning(warn)
    log.info("로그 수준 %s · 형식 %s", level_name, fmt_name)
    return level_name, fmt_name


def configure_from_env() -> bool:
    """``service.api`` 가 불러올 때 부르는 입구 — ``PORTAL_LOG_CONFIGURE=0`` 이면 건너뛴다(테스트 · 외부 설정 사용).

    Returns:
        로그를 세웠으면 ``True``.
    """
    if os.getenv(LOG_CONFIGURE_ENV, "1").strip() == "0":
        return False
    configure_logging()
    return True
