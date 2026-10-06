"""묶음(zip) 응답 만들기 — 네 묶음 창구가 함께 쓴다.

시작 전에 계획(``plan_bundle``)으로 건수 · 용량 헤더를 정하고, ZIP 은 임시 파일 없이 흘려 보낸다. 동시에 만드는 묶음 수는 프로세스당 제한한다.
헤더: X-Bundle-Count(또는 X-Bundle-Files) 담길 수 · X-Bundle-Missing 빠진 수(이름은 ZIP 끝 _manifest.json) · X-Bundle-Bytes 원본 합계 · X-Bundle-Truncated 카드가 200건에서 잘렸을 때.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from service.portal.asset.bundle_stream import BundlePlan, plan_bundle, stream_zip

_LOG = logging.getLogger("meta_extract.portal_api")

# 동시 묶음 수 제한 — 압축이 있는 묶음 하나가 코어 하나를 쓴다. 넘으면 기다리게 하지 않고 곧바로 503.
# 요청이 스레드를 잡고 줄을 서면 다른 요청이 굶는다.
BUNDLE_MAX_ENV = "PORTAL_BUNDLE_MAX_CONCURRENT"
RETRY_AFTER_SECONDS = 5


def bundle_max_concurrent() -> int:
    """프로세스당 동시 묶음 수 — ``PORTAL_BUNDLE_MAX_CONCURRENT``, 기본은 코어 수의 절반(2~8). 0 이면 제한 없음."""
    raw = os.getenv(BUNDLE_MAX_ENV, "").strip()
    if raw:
        try:
            return max(int(raw), 0)
        except ValueError:
            _LOG.warning("%s=%r 는 정수가 아니다 — 기본값을 쓴다", BUNDLE_MAX_ENV, raw)
    return min(8, max(2, (os.cpu_count() or 4) // 2))


class _Gate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0

    def acquire(self, limit: int) -> bool:
        with self._lock:
            if limit and self.active >= limit:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)


GATE = _Gate()


class _Slot:
    """자리 하나. 여러 곳에서 반납해도 한 번만 돌려준다."""

    def __init__(self) -> None:
        self._done = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        GATE.release()


def make_plan(targets: list[dict[str, Any]]) -> BundlePlan:
    """대상의 존재 · 크기를 확인한다(응답 시작 전에 부른다)."""
    return plan_bundle(targets)


def zip_response(plan: BundlePlan, *, content_disposition: str, headers: dict[str, str]) -> StreamingResponse:
    """계획대로 ZIP 을 만들면서 흘려 보내는 응답(``Content-Length`` 없음). 동시 묶음 수가 상한이면 503 + ``Retry-After``."""
    if not GATE.acquire(bundle_max_concurrent()):
        raise HTTPException(status_code=503, detail="묶음 내려받기가 몰려 있습니다 — 잠시 후 다시 시도해 주세요.",
                            headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
    slot = _Slot()
    inner = stream_zip(plan)

    def stream():
        try:
            yield from inner
        finally:
            inner.close()
            slot.release()

    def done() -> None:
        stream_gen.close()
        slot.release()

    stream_gen = stream()
    weakref.finalize(stream_gen, slot.release)
    return StreamingResponse(
        stream_gen,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition, "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff", **headers},
        # 받다가 끊겨도 원본 핸들이 닫히도록 응답이 끝날 때 닫는다.
        background=BackgroundTask(done),
    )
