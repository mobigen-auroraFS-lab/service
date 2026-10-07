"""기동 시 자기 점검 — 위험한 설정과 빠진 환경을 경고 한 줄로 알린다(기동은 막지 않는다).

인증이 꺼져 있음 · ``httptools`` 설치(uvicorn 파서가 바뀌어 잘못된 요청이 봉투 없이 평문 400 이 된다) · 원본 경로 표본 20개가 이 서버에서 보이는지(마운트 누락 확인).
"""

from __future__ import annotations

import importlib.util
import logging
import os

from service.portal.asset import origin

_LOG = logging.getLogger("meta_extract.portal_api")
SAMPLE_SIZE = 20


def warn_auth_disabled() -> None:
    from service.portal.auth.config import load_portal_auth_config
    if not load_portal_auth_config().auth_disabled:
        return
    if os.getenv("PORTAL_ALLOWED_CLIENT_CIDRS", "").strip():
        _LOG.warning("인증이 꺼져 있다(PORTAL_AUTH_DISABLED=1) — 접속 주소는 PORTAL_ALLOWED_CLIENT_CIDRS 로 제한돼 있다")
    else:
        _LOG.warning("인증이 꺼져 있다(PORTAL_AUTH_DISABLED=1) — 이 서버에 닿는 누구나 토큰 없이 자료를 볼 수 있다. "
                     "사내망에 열려 있다면(--host 0.0.0.0) PORTAL_ALLOWED_CLIENT_CIDRS 로 접속 주소를 제한하거나 127.0.0.1 로 띄운다")


def warn_http_parser() -> None:
    if importlib.util.find_spec("httptools") is not None:
        _LOG.warning("httptools 가 설치돼 있다 — uvicorn 이 이 파서를 쓰면 잘못된 HTTP 요청이 봉투 없이 평문 400 으로 끝난다(IDD 공통규약과 어긋남). "
                     "`--http h11` 로 띄우거나 이미지에서 uvicorn[standard] 를 뺀다")


def _prefix(path: str) -> str:
    return "/".join(path.split("/")[:4])


def _readable(path: str) -> bool:
    try:
        origin.get_reader().stat(path)
    except OSError:
        return False
    return True


def check_origin_paths() -> None:
    """DB 의 원본 경로 표본이 이 서버에서 보이는가. DB 가 안 되면 건너뛴다(차단기가 따로 알린다)."""
    try:
        from service.api import db_health
        from service.portal.common.db_manager import DbManager

        if db_health.is_down():
            return
        paths = DbManager.read(lambda repo: repo.asset.sample_fs_paths(SAMPLE_SIZE))
    except Exception:
        return
    if not paths:
        return
    seen = sum(1 for p in paths if _readable(p))
    prefixes = sorted({_prefix(p) for p in paths})[:2]
    if seen == 0:
        _LOG.warning("원본 경로 표본 %d개 중 0개가 보이지 않는다 — 원본 저장소가 이 서버에 마운트됐는지 확인한다(다운로드 · 원문이 410 이 된다). 경로 예: %s",
                     len(paths), ", ".join(prefixes))
    elif seen < len(paths):
        _LOG.warning("원본 경로 표본 %d개 중 %d개만 보인다(경로 예: %s)", len(paths), seen, ", ".join(prefixes))
    else:
        _LOG.info("원본 경로 표본 %d개 모두 보인다", len(paths))


def run_all(*, role: str) -> None:
    """기동 직후 뒤에서 한 번 돈다(스레드). 역할이 api 면 원본 경로는 쓰지 않으므로 보지 않는다."""
    warn_auth_disabled()
    warn_http_parser()
    if role != "api":
        check_origin_paths()
