"""묶음(zip) 응답 만들기 — 네 묶음 창구가 함께 쓴다.

시작 전에 계획(``plan_bundle``)으로 건수 · 용량 헤더를 정하고, ZIP 은 임시 파일 없이 흘려 보낸다. 동시에 만드는 묶음 수는 프로세스당 제한한다.
🔴 [2026-10-21] 자리는 **대상 조회 · 파일 확인(계획)부터** 잡는다(``BundleSlot`` · HEAD 포함) — 전체 묶음은 확인 단계만 수 분이 걸려
   (웹 점검 #4 · #48: GET 198초 · HEAD 152초), 자리를 스트림 시작 때 잡으면 동시 제한이 가장 오래 걸리는 단계를 막지 못했다.
헤더: X-Bundle-Count 와 X-Bundle-Files(같은 값 — 창구마다 달랐던 이름을 둘 다 준다) 담길 수 · X-Bundle-Missing 빠진 수(이름은 ZIP 끝 _manifest.json) · X-Bundle-Bytes 원본 합계.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from service.portal.asset.bundle_stream import BundlePlan, plan_bundle, stream_zip

_LOG = logging.getLogger("meta_extract.portal_api")

# 동시 묶음 수 제한 — 압축이 있는 묶음 하나가 코어 하나를 쓴다. 넘으면 기다리게 하지 않고 곧바로 503.
# 요청이 스레드를 잡고 줄을 서면 다른 요청이 굶는다.
BUNDLE_MAX_ENV = "PORTAL_BUNDLE_MAX_CONCURRENT"
RETRY_AFTER_SECONDS = 5


def bundle_headers(plan: BundlePlan, *, missing: int | None = None) -> dict[str, str]:
    """묶음 응답 헤더 — 담길 수(``X-Bundle-Count`` = ``X-Bundle-Files``) · 빠진 수 · 원본 합계. ``missing`` 은 계획 밖에서 뺀 것이 있을 때 합산해 넘긴다."""
    packed = str(len(plan.packed))
    return {"X-Bundle-Count": packed, "X-Bundle-Files": packed,
            "X-Bundle-Missing": str(len(plan.unreadable) if missing is None else missing), "X-Bundle-Bytes": str(plan.total_bytes)}


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


def _busy() -> HTTPException:
    """자리가 없을 때의 503 — 기다리게 하지 않고 곧바로 돌려보낸다."""
    return HTTPException(status_code=503, detail="묶음 내려받기가 몰려 있습니다 — 잠시 후 다시 시도해 주세요.",
                         headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


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


class BundleSlot:
    """동시 묶음 자리 하나 — 라우트가 대상 조회 · 파일 확인 **전에** 잡는다(HEAD 포함). 자리가 없으면 만들 때 503.

    ``with`` 를 나가면 놓는다(HEAD · 오류 · 404 등). ``zip_response(slot=...)`` 로 응답에 넘기면 응답이 끝날 때(끊겨도) 놓는다.
    """

    def __init__(self) -> None:
        if not GATE.acquire(bundle_max_concurrent()):
            raise _busy()
        self._slot = _Slot()
        self._handed = False

    def __enter__(self) -> BundleSlot:
        return self

    def __exit__(self, *_exc: object) -> bool:
        if not self._handed:
            self._slot.release()
        return False

    def hand_over(self) -> _Slot:
        """응답 스트림이 자리를 넘겨받는다 — 이 뒤로는 ``with`` 를 나가도 놓지 않는다."""
        self._handed = True
        return self._slot


def make_plan(targets: list[dict[str, Any]]) -> BundlePlan:
    """대상의 존재 · 크기를 확인한다(응답 시작 전에 부른다)."""
    return plan_bundle(targets)


def zip_response(plan: BundlePlan, *, content_disposition: str, headers: dict[str, str],
                 slot: BundleSlot | None = None) -> StreamingResponse:
    """계획대로 ZIP 을 만들면서 흘려 보내는 응답(``Content-Length`` 없음).

    ``slot`` 을 주면 라우트가 확인 단계부터 잡아 둔 자리를 넘겨받는다. 안 주면 여기서 잡는다 — 동시 묶음 수가 상한이면 503 + ``Retry-After``.
    """
    if slot is not None:
        held = slot.hand_over()
    else:
        if not GATE.acquire(bundle_max_concurrent()):
            raise _busy()
        held = _Slot()
    inner = stream_zip(plan)

    def stream():
        try:
            yield from inner
        finally:
            inner.close()
            held.release()

    def done() -> None:
        stream_gen.close()
        held.release()

    stream_gen = stream()
    weakref.finalize(stream_gen, held.release)
    return StreamingResponse(
        stream_gen,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition, "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", **headers},
        # 받다가 끊겨도 원본 핸들이 닫히도록 응답이 끝날 때 닫는다.
        background=BackgroundTask(done),
    )


def zip_head_response(*, content_disposition: str, headers: dict[str, str]) -> Response:
    """``HEAD`` — 본문 없이 GET 과 같은 머리(담길 수 · 빠진 수 · 원본 합계). 접근 기록은 남기지 않는다(링크를 열기 전 확인용).
    확인하는 동안은 라우트가 동시 묶음 자리를 잡는다(``BundleSlot`` — 꽉 차면 HEAD 도 503)."""
    return Response(
        status_code=200, media_type="application/zip",
        headers={"Content-Disposition": content_disposition, "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", **headers},
    )
