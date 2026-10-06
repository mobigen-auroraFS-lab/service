"""실험용 묶음 엔진 — 형식(zip · tar) · 압축 방식 · 읽기 병렬 · 저장소 지연 모사(``latency_ms``)를 옵션으로 고른다."""

from __future__ import annotations

import json
import os
import queue
import tarfile
import threading
import time
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from service.portal.asset.bundle_stream import (
    _FIXED_TIME,
    _PRECOMPRESSED,
    MANIFEST_NAME,
    _dedup_name,
    _Sink,
)


@dataclass(frozen=True)
class Opts:
    fmt: str = "zip"            # zip | tar
    method: str = "auto"        # auto(main 의 확장자 목록) | ext(확장자 목록 확대) | smart(확장자 목록 확대 + 앞부분을 시험 압축해 안 줄면 무압축) | stored | deflate
    level: int = 6
    chunk: int = 1024 * 1024
    readahead: int = 0          # 0 이면 끈다 · 양수면 미리 읽어 둘 조각 수(한 파일 안에서)
    parallel_read: int = 0      # 0 이면 끈다 · 양수면 한 파일 안에서 조각 N 개를 동시에 읽는다(``pread`` 창 — 지연이 큰 저장소에서 큰 파일 한 개가 직렬로 기다리지 않게)
    files_ahead: int = 0        # 0 이면 끈다 · 양수면 다음 파일 몇 개를 동시에 미리 읽는다(저장소 지연이 클 때 — 병렬 읽기)
    latency_ms: float = 0.0


@dataclass(frozen=True)
class Item:
    name: str
    path: str
    size: int


def read_chunks(path: str, chunk: int, latency_ms: float = 0.0) -> Iterator[bytes]:
    """파일을 조각으로 읽는다(조각마다 ``latency_ms`` 만큼 기다려 원격 저장소를 모사). 닫히면 파일도 닫는다."""
    with open(path, "rb") as fh:
        while True:
            if latency_ms:
                time.sleep(latency_ms / 1000.0)
            b = fh.read(chunk)
            if not b:
                return
            yield b


_END = object()


def prefetch(source: Iterator[bytes], depth: int) -> Iterator[bytes]:
    """다른 스레드가 ``depth`` 조각까지 미리 읽어 둔다. 소비자가 멈추면(연결 끊김) 읽기 스레드도 멈춘다."""
    q: queue.Queue = queue.Queue(maxsize=depth)
    stop = threading.Event()

    def run() -> None:
        try:
            for b in source:
                while not stop.is_set():
                    try:
                        q.put(b, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    return
            q.put(_END)
        except BaseException as exc:  # noqa: BLE001 — 읽기 오류를 소비자에게 넘긴다
            q.put(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        while True:
            b = q.get()
            if b is _END:
                return
            if isinstance(b, BaseException):
                raise b
            yield b
    finally:
        stop.set()
        t.join(timeout=2)


# 압축 컨테이너를 넣기 전의 확장자 목록 — 비교 기준(``auto``)을 재현하려고 고정해 둔다.
_LEGACY_PRECOMPRESSED = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "heic", "avif",
    "mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "mpg", "mpeg",
    "mp3", "m4a", "aac", "ogg", "opus", "flac", "wma",
    "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst",
})

# 목록에 없지만 이미 압축된 컨테이너 형식.
_EXTRA_PRECOMPRESSED = frozenset({"pdf", "docx", "xlsx", "pptx", "hwpx", "odt", "ods", "odp", "epub", "jar", "apk", "woff2", "heif", "jxl", "br", "lz4", "zstd"})
_SNIFF_BYTES = 64 * 1024
_SNIFF_RATIO = 0.92       # 시험 압축 결과가 원래의 92% 보다 크면 압축 이득이 없다고 본다


def looks_incompressible(sample: bytes) -> bool:
    """앞부분을 가장 빠른 수준으로 시험 압축해 거의 안 줄면 True — 확장자 목록이 놓친 형식(hwp · 암호화 · 압축 컨테이너)을 잡는다."""
    if not sample:
        return False
    return len(zlib.compress(sample[:_SNIFF_BYTES], 1)) > _SNIFF_RATIO * min(len(sample), _SNIFF_BYTES)


class _Reader:
    """파일 하나를 별도 스레드가 **만들자마자** 읽기 시작한다(조각 ``depth`` 개까지) — 다음 파일들을 앞서 읽어 둘 때 쓴다.

    (``prefetch`` 제너레이터는 처음 꺼낼 때에야 스레드가 뜬다 — 그래서 「다음 파일을 미리 읽기」에는 쓸 수 없다.)
    """

    def __init__(self, item: Item, o: Opts, depth: int = 2):
        self._q: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(item, o), daemon=True)
        self._t.start()

    def _put(self, v: object) -> bool:
        while not self._stop.is_set():
            try:
                self._q.put(v, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self, item: Item, o: Opts) -> None:
        try:
            for b in read_chunks(item.path, o.chunk, o.latency_ms):
                if not self._put(b):
                    return
            self._put(_END)
        except BaseException as exc:  # noqa: BLE001
            self._put(exc)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            b = self._q.get()
            if b is _END:
                return
            if isinstance(b, BaseException):
                raise b
            yield b

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=2)


def pread_window(path: str, chunk: int, window: int, latency_ms: float = 0.0) -> Iterator[bytes]:
    """조각 ``window`` 개를 동시에 읽어 두고 **순서대로** 내보낸다 — 조각마다 지연이 있어도 처리량이 창 크기만큼 는다. 메모리는 ``window × chunk``."""
    from concurrent.futures import ThreadPoolExecutor

    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size

        def one(off: int) -> bytes:
            if latency_ms:
                time.sleep(latency_ms / 1000.0)
            return os.pread(fd, chunk, off)

        with ThreadPoolExecutor(window) as ex:
            pending: list = []
            nxt = 0
            while nxt < size or pending:
                while nxt < size and len(pending) < window:
                    pending.append(ex.submit(one, nxt))
                    nxt += chunk
                b = pending.pop(0).result()
                if b:
                    yield b
    finally:
        os.close(fd)


def _chunks(item: Item, o: Opts) -> Iterator[bytes]:
    if o.parallel_read > 0:
        return pread_window(item.path, o.chunk, o.parallel_read, o.latency_ms)
    src = read_chunks(item.path, o.chunk, o.latency_ms)
    return prefetch(src, o.readahead) if o.readahead > 0 else src


def _ctype(name: str, o: Opts) -> int:
    if o.method == "stored":
        return zipfile.ZIP_STORED
    if o.method == "deflate":
        return zipfile.ZIP_DEFLATED
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if o.method in ("ext", "smart"):
        return zipfile.ZIP_STORED if ext in _PRECOMPRESSED or ext in _EXTRA_PRECOMPRESSED else zipfile.ZIP_DEFLATED
    return zipfile.ZIP_STORED if ext in _LEGACY_PRECOMPRESSED else zipfile.ZIP_DEFLATED


def zip_stream(items: list[Item], o: Opts) -> Iterator[bytes]:
    sink = _Sink()
    used: set[str] = set()
    readers: dict[int, _Reader] = {}

    def ahead(i: int) -> None:
        if o.files_ahead and 0 <= i < len(items) and i not in readers:
            readers[i] = _Reader(items[i], o)

    try:
        for i in range(min(o.files_ahead, len(items))):
            ahead(i)
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=o.level) as zf:
            for idx, it in enumerate(items):
                ahead(idx + o.files_ahead)
                name = _dedup_name(it.name, used)
                info = zipfile.ZipInfo(filename=name, date_time=_FIXED_TIME)
                info.compress_type = _ctype(name, o)
                info._compresslevel = o.level        # ZipInfo 를 직접 넘기면 ZipFile 의 compresslevel 이 적용되지 않는다(파이썬 동작)
                source = iter(readers.pop(idx)) if idx in readers else _chunks(it, o)
                first = next(source, b"")
                if o.method == "smart" and info.compress_type == zipfile.ZIP_DEFLATED and looks_incompressible(first):
                    info.compress_type = zipfile.ZIP_STORED
                with zf.open(info, "w", force_zip64=it.size >= 2**31 - 1) as dst:
                    for b in _chain(first, source):
                        dst.write(b)
                        out = sink.drain()
                        if out:
                            yield out
                out = sink.drain()
                if out:
                    yield out
        tail = sink.drain()
        if tail:
            yield tail
    finally:
        for r in readers.values():
            r.close()


def _chain(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    if first:
        yield first
    yield from rest


def tar_stream(items: list[Item], o: Opts) -> Iterator[bytes]:
    for it in items:
        ti = tarfile.TarInfo(name=it.name)
        ti.size = it.size
        ti.mtime = 0
        ti.mode = 0o644
        yield ti.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")
        sent = 0
        for b in _chunks(it, o):
            sent += len(b)
            yield b
        pad = (-sent) % 512
        if pad:
            yield b"\0" * pad
    yield b"\0" * 1024


def bundle_stream(items: list[Item], o: Opts) -> Iterator[bytes]:
    return tar_stream(items, o) if o.fmt == "tar" else zip_stream(items, o)


def build_zip_file(items: list[Item], dest: str, o: Opts) -> int:
    """비동기 작업 방식 — 보통의(되감을 수 있는) zip 을 디스크에 완성한다. 크기가 헤더에 들어가 일반 도구와 호환이 가장 좋다."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=o.level) as zf:
        used: set[str] = set()
        for it in items:
            name = _dedup_name(it.name, used)
            info = zipfile.ZipInfo(filename=name, date_time=_FIXED_TIME)
            info.compress_type = _ctype(name, o)
            info._compresslevel = o.level
            with open(it.path, "rb") as src, zf.open(info, "w", force_zip64=it.size >= 2**31 - 1) as dst:
                while True:
                    b = src.read(o.chunk)
                    if not b:
                        break
                    if o.latency_ms:
                        time.sleep(o.latency_ms / 1000.0)
                    dst.write(b)
        zf.writestr(zipfile.ZipInfo(MANIFEST_NAME, _FIXED_TIME), json.dumps({"missing": []}))
    return os.path.getsize(dest)
