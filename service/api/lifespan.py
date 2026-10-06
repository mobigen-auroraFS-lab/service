"""앱 수명 주기(단일 책임: 기동과 종료 순서).

⚠️ 종료 순서가 중요하다 — 감사 기록 태스크를 **먼저** 비운 뒤 DB 풀을 닫는다. 반대로 하면
아직 쓰기 중인 태스크가 닫힌 풀을 잡는다.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from service.api import audit, db, db_health, posture, search_health, warmup

ENV = os.getenv("PORTAL_API_ENV", "dev")


def _start_dependency_checks() -> None:
    """기동 직후 뒤에서 DB · 검색 엔진 접속 시험과 자기 점검을 한다(``PORTAL_STARTUP_CHECK=0`` 이면 건너뜀). files 역할은 검색 엔진을 보지 않는다."""
    if os.getenv("PORTAL_STARTUP_CHECK", "1").strip() == "0":
        return
    checks = [db_health.BREAKER.startup_check]
    if os.getenv("PORTAL_ROLE", "all").strip().lower() != "files":
        checks.append(search_health.BREAKER.startup_check)
    role = os.getenv("PORTAL_ROLE", "all").strip().lower()
    checks.append(lambda: posture.run_all(role=role))
    for fn in checks:
        threading.Thread(target=fn, name="startup-check", daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """앱의 수명 주기 — 기동 시 설정을 확정하고, 종료 시 남은 작업과 DB 풀을 정리한다.

    ⚠️ 부트스트랩은 **백엔드 전용**을 쓴다(코어 것이 아니라). 코어 부트스트랩은 코어 레포의
    ``.env`` 를 읽기 때문에, 그걸 쓰면 백엔드가 남의 설정으로 뜬다.

    종료 순서가 중요하다 — 감사 기록 태스크를 **먼저** 비운 뒤 DB 풀을 닫는다. 반대로 하면
    아직 쓰기 중인 태스크가 닫힌 풀을 잡는다.
    """
    from service.bootstrap import bootstrap_env
    from service.portal.auth.config import load_portal_auth_config

    bootstrap_env(ENV)
    # 🔴 인증 설정을 **기동할 때** 확정한다(fail-fast). 종전에는 첫 인증 요청 때에야 읽어서, 운영
    #    모드에 서명 키가 없어도 서버가 떴고 헬스 체크도 200 이었다 — 배포가 성공한 것처럼 보이다가
    #    사용자 요청마다 500 이 났다. 이제는 서버가 아예 뜨지 않는다(키가 없으면 ValueError).
    #    ⚠️ ``bootstrap_env`` **뒤**여야 한다 — 서명 키가 ``.env.{ENV}`` 에서 들어올 수 있다.
    load_portal_auth_config()
    db.align_thread_limit_to_pool()
    _start_dependency_checks()
    warmup.start()          # 뒤에서 — 첫 사용자가 DB 연결 · 태그 캐시를 만드는 시간을 치르지 않게(기동은 막지 않는다)
    yield
    # 종료 시 남은 감사 기록 작업을 먼저 비운다(응답과 분리돼 뒤에서 돌던 것들).
    await audit.drain_pending()
    # 그다음에 DB 풀을 닫는다 — **순서가 중요하다**. 먼저 닫으면 아직 쓰는 중인 감사 작업이 실패한다.
    db.close_db()
