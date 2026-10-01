"""기동 예열(단일 책임: 서버가 뜬 직후 첫 사용자가 느린 요청을 맞지 않게 미리 한 번 불러 둔다).

**왜 필요한가** — 서버를 갈아 끼운 직후 첫 요청은 평소와 다르게 느리다(사내 k8s 실측 2026-10-01).
  · 첫 **검색**은 32초(``/file-search``) · 8초(``/search``) — 임베딩 모델 · 서버 연결과 검색 엔진 연결을 처음 여는 비용.
  · 조건 없는 **태그 목록**은 캐시를 채우는 동안 수 초 — 그 첫 호출이 DB 연결 여는 시간(0.3~0.6초)까지 치른다.
이를 기동 직후 **뒤에서** 한 번 치러 두면 첫 사용자는 열린 연결 · 채워진 캐시를 만난다.

**원칙**
  · 기동을 막지 않는다 — 별도 스레드에서 돈다(``daemon``). 서버는 곧바로 요청을 받는다.
  · 단계는 **서로 독립**이다 — 한 단계가 실패해도(임베딩 서버가 아직 안 떴어도) 다음 단계를 한다. 실패는 경고 한 줄(단계 이름 · 예외 종류)뿐이다.
  · 캐시가 빈 뒤에는 다시 느려질 수 있다 — 주기적으로 갱신하지는 않는다(필요해지면 그때 정한다).

⚠️ 이 예열은 **프로세스 안**에서 돈다. k8s 의 준비 확인(readinessProbe)이 예열이 끝난 뒤에 통과하도록 바꾸는 것은 배포 매니페스트 몫이다
(``/health`` 는 예열을 기다리지 않고 곧바로 200 이다).

``PORTAL_WARMUP=0`` 이면 건너뛴다(테스트 · DB · 검색 엔진을 부르면 안 되는 환경).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from service.portal.common.db_manager import DbManager

WARMUP_ENV = "PORTAL_WARMUP"
# ``GET /tags`` 의 기본 개수와 같아야 그 요청이 캐시를 만난다(캐시 열쇠에 개수가 들어 있다).
TAGS_DEFAULT_LIMIT = 50

_LOG = logging.getLogger("meta_extract.portal_api")


def enabled() -> bool:
    """예열을 할지 — ``PORTAL_WARMUP=0`` 이면 하지 않는다(기본은 한다). 파일 전용 프로세스(``PORTAL_ROLE=files``)는 검색 · 태그를 안 쓰니 하지 않는다."""
    if os.getenv("PORTAL_ROLE", "all").strip().lower() == "files":
        return False
    return os.getenv(WARMUP_ENV, "1").strip() != "0"


def _warm_tags() -> str:
    """조건 없는 태그 목록을 한 번 불러 DB 연결을 열고 태그 캐시를 채운다."""
    rows = DbManager.read(
        lambda repo: repo.catalog.tags(topics=[], subtopics=[], q=None, limit=TAGS_DEFAULT_LIMIT))
    return f"태그 {len(rows)}건"


def _warm_search() -> str:
    """검색 한 건(임베딩 + 검색 엔진)을 실제 검색과 같은 모양으로 보낸다."""
    from service.api.routes.file_search import (
        warm_up_search,  # 라우트 모듈은 늦게 불러온다(순환 import 방지)
    )
    warm_up_search()
    return "검색"


# 단계 이름 · 함수 — 앞에서부터 차례로 하고, 한 단계의 실패가 다음 단계를 막지 않는다.
STEPS: tuple[tuple[str, Callable[[], str]], ...] = (("태그", _warm_tags), ("검색", _warm_search))


def warm_once() -> None:
    """예열 단계를 차례로 한 번씩 한다. 단계마다 걸린 시간을 재고, 실패는 경고만 남긴다(예외를 올리지 않는다)."""
    done: list[str] = []
    for name, step in STEPS:
        started = time.perf_counter()
        try:
            what = step()
        except Exception as exc:  # noqa: BLE001 — 예열은 최선 노력이다. 실패가 기동을 막으면 안 된다.
            _LOG.warning("기동 예열 실패(무시) — %s: %s", name, type(exc).__name__)
            continue
        done.append(f"{what} {time.perf_counter() - started:.1f}초")
    if done:
        _LOG.info("기동 예열 — %s", " · ".join(done))


def start() -> threading.Thread | None:
    """예열을 뒤에서 시작한다. 건너뛰면 ``None``, 아니면 시작한 스레드(시험이 기다릴 수 있게 돌려준다)."""
    if not enabled():
        return None
    thread = threading.Thread(target=warm_once, name="portal-warmup", daemon=True)
    thread.start()
    return thread
