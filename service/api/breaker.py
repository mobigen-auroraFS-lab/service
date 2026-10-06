"""연결 차단기 — 외부 의존(DB · 검색 엔진)이 죽어 있을 때 요청이 매달리지 않고 곧바로 실패하게 한다.

연결 실패가 나면 짧게 접속해 보고, 죽었으면 차단기를 연다. 차단 중에는 뒤에서 주기적으로 접속을 시도해 살아나면 닫는다(확인 루프가 멈추면 ``max_open`` 초 뒤에 닫는다).
접속이 되는 실패(풀이 꽉 찬 것)에는 열지 않는다. 상태가 바뀔 때만 한 줄 남긴다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

_LOG = logging.getLogger("meta_extract.portal_api")


class Breaker:
    def __init__(self, label: str, probe: Callable[[], bool], *, interval: float = 2.0, max_open: float = 30.0) -> None:
        self.label, self.probe, self.interval, self.max_open = label, probe, interval, max_open
        self._lock = threading.Lock()
        self._down_since: float | None = None
        self._last_check: float = 0.0
        self._thread: threading.Thread | None = None

    def is_down(self) -> bool:
        """차단 중인가(안전장치 시간이 지났으면 닫는다)."""
        with self._lock:
            if self._down_since is None:
                return False
            # 확인 루프가 돌며 실패하는 동안(진짜 장애)은 풀리면 안 된다.
            if time.monotonic() - max(self._down_since, self._last_check) > self.max_open:
                self._down_since = None
                _LOG.info("%s 차단기 자동 해제(%.0f초 경과) — 실제로 다시 시도한다", self.label, self.max_open)
                return False
            return True

    def reset(self) -> None:
        with self._lock:
            self._down_since = None

    def note_failure(self, exc: BaseException) -> None:
        """연결 실패를 알린다 — 짧은 접속 시험이 안 되면 차단기를 연다(되면 열지 않는다)."""
        if self.is_down() or self.probe():
            return
        with self._lock:
            if self._down_since is not None:
                return
            self._down_since = time.monotonic()
            _LOG.warning("%s 연결 불가 — 차단: 요청은 기다리지 않고 503 으로 끝낸다(%s)", self.label, type(exc).__name__)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name=f"recover-{self.label}", daemon=True)
                self._thread.start()

    def startup_check(self) -> None:
        """기동 직후 한 번 접속해 본다. 죽어 있으면 첫 요청들이 풀 대기 × 재시도로 매달리지 않고 바로 거절된다."""
        if not self.probe():
            self.note_failure(ConnectionError("기동 시 접속 시험 실패"))

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            with self._lock:
                if self._down_since is None:
                    self._thread = None
                    return
            if not self.probe():
                with self._lock:
                    self._last_check = time.monotonic()
                continue
            with self._lock:
                since, self._down_since, self._thread = self._down_since, None, None
            _LOG.warning("%s 연결 복구 — 차단 해제(%.0f초 동안 막았다)", self.label, time.monotonic() - since if since else 0)
            return
