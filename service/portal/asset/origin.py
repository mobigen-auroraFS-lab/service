"""원본 파일을 읽는 계층 — 다운로드 · 원문 · 묶음 · 기동 점검이 모두 여기를 거친다.

``PORTAL_ORIGIN_BACKEND`` 로 구현을 고른다.
  · ``local``(기본) — DB 의 ``fs_path`` 를 마운트 경로로 직접 연다(로컬 시험 · 마운트가 있는 서버).
  · ``storage_api`` — 스토리지 스트리밍 API 로 받는다(협의안 구조). 사양이 정해지기 전이라 아직 없다.

바이트를 가져오는 일만 다르다. 권한 · 링크 · 감사 · 헤더 · 봉투는 구현과 무관하게 한 벌이다.
열 수 없으면 구현과 무관하게 ``OSError`` 를 던진다(호출부가 410 으로 답한다).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO, Protocol

BACKEND_ENV = "PORTAL_ORIGIN_BACKEND"
# 조각 크기(1MiB) — 64KiB 보다 CPU 가 적게 든다(experiments/RESULTS.md). 요청당 메모리는 조각 하나뿐이다.
CHUNK = 1024 * 1024


class OriginFile(Protocol):
    size: int
    mtime_ns: int

    def read_range(self, start: int, end: int, *, window: int = 1) -> Iterator[bytes]:
        """``start``~``end``(둘 다 포함) 구간을 조각내어 내준다. 파일이 도중에 짧아지면 거기서 멈춘다. ``window`` > 1 이면 조각 여러 개를 동시에 읽는다(지연이 큰 저장소용)."""

    def read_head(self, n: int) -> bytes: ...

    def close(self) -> None: ...


class OriginReader(Protocol):
    def open(self, path: str | None) -> OriginFile:
        """원본을 연다. 경로가 비었거나 없거나 일반 파일이 아니면 OSError."""

    def stat(self, path: str | None) -> tuple[int, int]:
        """``(크기, 수정 시각 ns)``. 읽을 수 없으면 OSError."""


class LocalFile:
    def __init__(self, fh: BinaryIO, size: int, mtime_ns: int) -> None:
        self._fh = fh
        self.size, self.mtime_ns = size, mtime_ns

    def read_range(self, start: int, end: int, *, window: int = 1) -> Iterator[bytes]:
        if window > 1 and end - start + 1 >= 2 * CHUNK:
            yield from self._windowed(start, end, window)
            return
        remaining = end - start + 1
        self._fh.seek(start)
        while remaining > 0:
            b = self._fh.read(min(CHUNK, remaining))
            if not b:
                return
            remaining -= len(b)
            yield b

    def _windowed(self, start: int, end: int, window: int) -> Iterator[bytes]:
        fd = self._fh.fileno()
        with ThreadPoolExecutor(window) as ex:
            pending: list = []
            nxt = start
            while nxt <= end or pending:
                while nxt <= end and len(pending) < window:
                    pending.append(ex.submit(os.pread, fd, min(CHUNK, end - nxt + 1), nxt))
                    nxt += CHUNK
                b = pending.pop(0).result()
                if b:
                    yield b

    def read_head(self, n: int) -> bytes:
        self._fh.seek(0)
        return self._fh.read(n)

    def close(self) -> None:
        self._fh.close()


class LocalFileReader:
    def open(self, path: str | None) -> LocalFile:
        if not path:
            raise OSError("원본 경로가 비어 있다")
        fh = open(path, "rb")  # noqa: SIM115 — 응답 스트림이 닫을 때까지 살아 있어야 한다(호출부가 닫는다)
        try:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("일반 파일이 아니다")
        except BaseException:
            fh.close()
            raise
        return LocalFile(fh, info.st_size, info.st_mtime_ns)

    def stat(self, path: str | None) -> tuple[int, int]:
        if not path:
            raise OSError("경로 없음")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("일반 파일이 아님")
        if not os.access(path, os.R_OK):
            raise OSError("읽기 권한 없음")
        return info.st_size, info.st_mtime_ns


class StorageApiReader:
    def __init__(self) -> None:
        raise NotImplementedError("storage_api 는 스토리지 스트리밍 API 사양이 정해진 뒤에 구현한다 — 지금은 local 만 쓴다")


_LOCAL = LocalFileReader()


def get_reader() -> OriginReader:
    """환경 설정에 맞는 읽기 구현. 알 수 없는 값이나 아직 없는 구현이면 기동 때 바로 알리려고 예외를 던진다."""
    backend = os.environ.get(BACKEND_ENV, "local").strip().lower() or "local"
    if backend == "local":
        return _LOCAL
    if backend == "storage_api":
        return StorageApiReader()
    raise ValueError(f"{BACKEND_ENV} 는 local · storage_api 중 하나여야 한다: {backend!r}")
