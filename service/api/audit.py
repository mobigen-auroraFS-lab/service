"""접근 기록 미들웨어(단일 책임: 누가 무엇을 봤는지 남기기).

**추가만** 한다 — 감사 자료라 수정·삭제 경로를 두지 않는다. 기록은 응답 경로에서 분리한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from service.api import db
from service.portal.auth import authenticate_token
from service.portal.history.access_log import derive_access_action, record_access

_LOG = logging.getLogger("meta_extract.portal_api")


def user_id_from_request(request: Request) -> str:
    """best-effort: ``Authorization: Bearer <token>`` → user_id. 없거나 검증 실패면 ``anonymous``.

    기록(감사) 용 식별이라 인증 실패가 응답을 막아선 안 된다 — 어떤 예외든 삼키고 anonymous 로.
    실제 접근 인가는 라우트의 ``require_principal`` 이 이미 책임진다(여기선 기록 라벨링만).
    미들웨어는 라우트 의존성 주입 전에 돌아 route 의 Principal 을 못 받으므로 토큰을 여기서 다시 파싱한다.
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            return authenticate_token(auth[7:].strip()).user_id
        except Exception:  # noqa: BLE001 — 기록용 best-effort, 인증 실패가 응답을 막지 않음
            return "anonymous"
    return "anonymous"


def record_access_safe(method: str, path: str, status_code: int, user_id: str) -> None:
    """성공한 데이터 조회를 접근 이력에 한 행 남긴다.

    **DB에 쓴다.** 실패해도 예외를 올리지 않는다(감사는 최선 노력 — 기록 때문에 요청이 깨지면 안 된다).

    Args:
        method: HTTP 메서드.
        path: 요청 경로. 여기서 동작 이름과 대상 자산을 도출한다.
        status_code: 응답 코드. **400 이상이면 기록하지 않는다** — 실패한 접근은 감사 대상이
            아니다(무엇을 봤는지가 아니라 못 봤다는 뜻이므로).
        user_id: 요청 주체.
    """
    if status_code >= 400:
        return
    derived = derive_access_action(method, path)
    if derived is None:
        return
    action, asset_id = derived
    db.run_in_db_write(
        lambda conn: record_access(conn, action=action, user_id=user_id, asset_id=asset_id)
    )


# fire-and-forget 기록 태스크 강참조 보관(GC 로 중도 소멸 방지). 완료 시 자동 제거.
_PENDING_TASKS: set[asyncio.Task] = set()


async def _record_access_bg(method: str, path: str, status_code: int, user_id: str) -> None:
    """기록 작업을 **응답과 분리해** 뒤에서 수행한다(스레드풀 경유).

    동기 DB 쓰기를 응답 경로에서 기다리면 DB 가 느릴 때 모든 요청이 함께 느려진다.
    어떤 예외도 삼키고 경고만 남긴다.

    Args:
        method: HTTP 메서드.
        path: 요청 경로.
        status_code: 응답 코드.
        user_id: 요청 주체.
    """
    try:
        await run_in_threadpool(record_access_safe, method, path, status_code, user_id)
    except Exception:  # noqa: BLE001 — 감사 기록 실패가 서비스에 전파되면 안 됨(best-effort·D2)
        _LOG.warning("access_log 기록 실패(무시): %s %s", method, path)


async def access_log_middleware(request: Request, call_next: Callable) -> object:
    """접근 이력을 **추가만** 하는 방식으로 적재한다(수정·삭제 없음 — 감사 기록이므로).

    기록을 **응답 critical path 에서 분리**(fire-and-forget)한다 — 응답을 먼저 반환하고 기록은
    ``create_task`` 로 뒤에서 수행한다. 동기 DB write 를 await 하면 DB 지연/풀 고갈 시 모든 데이터
    응답이 지연되므로(best-effort 감사가 서비스 지연을 유발), await 하지 않는다(D2). 기록 실패·지연은
    응답 상태·지연 어디에도 영향이 없다.

    Args:
        request: 들어온 요청.
        call_next: 다음 처리 단계. 이 결과를 **그대로** 돌려준다(응답을 건드리지 않는다).

    Returns:
        아래 단계가 만든 응답 객체 그대로.
    """
    response = await call_next(request)
    try:
        user_id = user_id_from_request(request)
        task = asyncio.create_task(
            _record_access_bg(request.method, request.url.path, response.status_code, user_id)
        )
        _PENDING_TASKS.add(task)
        task.add_done_callback(_PENDING_TASKS.discard)
    except Exception:  # noqa: BLE001 — 기록 스케줄 실패조차 응답을 깨면 안 됨(best-effort)
        _LOG.warning("access_log 기록 스케줄 실패(무시): %s %s", request.method, request.url.path)
    return response


async def drain_pending() -> None:
    """아직 끝나지 않은 기록 작업을 모두 기다린다(종료 훅 전용).

    실패는 삼킨다 — 각 작업이 이미 경고를 남겼고, 종료를 막아서는 안 된다.
    ⚠️ DB 풀을 닫기 **전에** 불러야 한다(``lifespan`` 이 순서를 보장한다).
    """
    if _PENDING_TASKS:
        await asyncio.gather(*_PENDING_TASKS, return_exceptions=True)
