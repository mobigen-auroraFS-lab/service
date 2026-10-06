"""여러 파일을 임시 파일 없이 ZIP 으로 만들면서 곧바로 흘려 보낸다.

서버에 쓰지 않아 첫 바이트가 바로 나가고 메모리는 조각 하나(1MiB)뿐이다. 이미 압축된 형식(이미지 · 영상 · pdf · docx 등)은 무압축으로 담고,
목록에 없는 형식은 앞 64KiB 를 시험 압축해 안 줄면 무압축으로 담는다. 항목 순서 · 타임스탬프 · 중복 이름 처리가 고정이라 같은 입력이면 같은 바이트다.

한계: Content-Length 가 없고, 이어받기가 안 되며, 응답을 시작한 뒤 읽기가 실패하면 연결이 끊긴다.
대안(브라우저에서 zip · 서버 비동기 zip + 저장소)은 TODO.md 에 있다.

환경변수: PORTAL_ZIP_LEVEL(1~9 · 기본 6) · PORTAL_ZIP_READ_WINDOW(큰 파일 조각 동시 읽기 · 기본 끔) · PORTAL_ZIP_FILES_AHEAD(다음 파일 미리 읽기 · 기본 끔).
뒤의 둘은 저장소 읽기 지연이 클 때만 의미가 있다.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import threading
import zipfile
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("meta_extract.portal_api")

# 조각 크기 — 파일 읽기 · ZIP 쓰기 단위. 단건 다운로드와 같은 값이다(조각이 작으면 서버 CPU 가 먼저 찬다).
CHUNK = 1024 * 1024

# 고정 타임스탬프 — 현재 시각을 넣으면 내용이 같아도 ZIP 바이트가 매번 달라진다.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)

# 이미 압축된 형식 — 다시 압축해도 크기가 거의 줄지 않고 CPU 만 든다.
_PRECOMPRESSED = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "heic", "avif",
    "mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "mpg", "mpeg",
    "mp3", "m4a", "aac", "ogg", "opus", "flac", "wma",
    "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst",
})

_PRECOMPRESSED_CONTAINERS = frozenset({
    "pdf", "docx", "xlsx", "pptx", "hwpx", "odt", "ods", "odp", "epub", "jar", "apk",
    "woff2", "heif", "jxl", "br", "lz4", "zstd",
})
_PRECOMPRESSED = _PRECOMPRESSED | _PRECOMPRESSED_CONTAINERS

# 시험 압축 결과가 이 비율 이상 남으면 압축 이득이 없다.
_SNIFF_BYTES = 64 * 1024
_SNIFF_RATIO = 0.92

ZIP_LEVEL_ENV = "PORTAL_ZIP_LEVEL"
READ_WINDOW_ENV = "PORTAL_ZIP_READ_WINDOW"
FILES_AHEAD_ENV = "PORTAL_ZIP_FILES_AHEAD"
DEFAULT_LEVEL = 6

MANIFEST_NAME = "_manifest.json"

# 2GiB 이상이면 항목을 ZIP64 로 쓴다 — 크기를 모르는 채 쓰는 스트림이라 미리 알려 줘야 한다.
_ZIP64_HINT = 2**31 - 1


@dataclass
class BundlePlan:
    """시작 전에 확정한 묶음 계획 — 담을 것 · 빠질 것 · 원본 합계."""

    packed: list[dict[str, Any]] = field(default_factory=list)       # 담을 항목(``size`` 가 붙는다)
    unreadable: list[dict[str, Any]] = field(default_factory=list)   # 원본이 없거나 못 열어 빠지는 항목 {asset_id, file_name}
    total_bytes: int = 0                                              # 담을 항목의 실제 크기 합


def _dedup_name(name: str, used: set[str]) -> str:
    """ZIP 안에서 이름이 겹치면 번호를 붙여 유일하게 만든다(같은 이름으로 두 번 넣으면 풀 때 하나가 덮어써진다)."""
    if name not in used:
        used.add(name)
        return name
    root, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = f"{root}_{i}{ext}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def _safe_entry_name(name: str) -> str:
    """ZIP 안 이름에서 경로 구분자 · 제어문자를 걷어 낸다 — 풀 때 폴더 밖으로 나가는 이름(``../x``)을 막는다."""
    cleaned = "".join(c if c.isprintable() and c not in '/\\:*?"<>|' else "_" for c in name).strip().strip(".")
    return cleaned or "file"


def plan_bundle(targets: list[dict[str, Any]]) -> BundlePlan:
    """담을 파일의 존재 · 크기를 응답 시작 전에 확인한다. 경로가 없거나 일반 파일이 아니거나 읽을 수 없으면 ``unreadable`` 로 뺀다."""
    plan = BundlePlan()
    for t in targets:
        fs_path = t.get("fs_path")
        name = t.get("file_name") or os.path.basename(str(fs_path or "")) or "file"
        try:
            if not fs_path:
                raise OSError("경로 없음")
            st = os.stat(fs_path)
            if not os.path.isfile(fs_path):
                raise OSError("일반 파일이 아님")
            if not os.access(fs_path, os.R_OK):
                raise OSError("읽기 권한 없음")
        except OSError as exc:
            # 🔴 서버 경로는 응답(ZIP 포함)에 싣지 않는다 — 운영자가 볼 로그에만 남긴다.
            _LOG.warning("묶음에서 원본을 열 수 없어 건너뜀: asset_id=%s fs_path=%s — %s", t.get("asset_id"), fs_path, type(exc).__name__)
            plan.unreadable.append({"asset_id": t.get("asset_id"), "file_name": name})
            continue
        plan.packed.append({**t, "file_name": name, "size": st.st_size})
        plan.total_bytes += st.st_size
    return plan


class _Sink(io.RawIOBase):
    """``zipfile`` 이 쓰는 **되감을 수 없는** 출력 — 쓴 조각을 모아 두었다가 호출부가 꺼내 간다(디스크 · 큰 버퍼 없음)."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False        # 되감을 수 없으면 zipfile 이 항목 뒤에 크기 · CRC(data descriptor)를 붙인다

    def write(self, data: Any) -> int:
        b = bytes(data)
        self._chunks.append(b)
        self._pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """환경변수 정수 — 비었거나 틀리면 기본값(경고 한 줄). 범위 밖이면 가장 가까운 끝으로 맞춘다."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning("%s=%r 는 정수가 아니다 — 기본 %d 를 쓴다", name, raw, default)
        return default
    return max(low, min(high, value))


def zip_level() -> int:
    """압축 수준(1~9) — ``PORTAL_ZIP_LEVEL``, 기본 6."""
    return _env_int(ZIP_LEVEL_ENV, DEFAULT_LEVEL, 1, 9)


def looks_incompressible(sample: bytes) -> bool:
    """앞부분을 가장 빠른 수준으로 시험 압축해 거의 안 줄면 True — 확장자 목록이 놓친 형식(hwp · 암호화 · 압축 컨테이너)을 가른다."""
    if not sample:
        return False
    head = sample[:_SNIFF_BYTES]
    return len(zlib.compress(head, 1)) > _SNIFF_RATIO * len(head)


def _precompressed(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in _PRECOMPRESSED


class _Source:
    """열린 원본 한 개. ``window`` > 1 이고 파일이 크면 조각 여러 개를 동시에 읽어 순서대로 내준다. 열기 실패는 OSError."""

    def __init__(self, path: str, size: int, window: int) -> None:
        self.fh = open(path, "rb")  # noqa: SIM115 — close() 가 닫는다
        self.size, self.window = size, window

    def chunks(self) -> Iterator[bytes]:
        if self.window > 1 and self.size >= 2 * CHUNK:
            yield from self._windowed()
            return
        while True:
            b = self.fh.read(CHUNK)
            if not b:
                return
            yield b

    def _windowed(self) -> Iterator[bytes]:
        fd = self.fh.fileno()
        with ThreadPoolExecutor(self.window) as ex:
            pending: list = []
            nxt = 0
            while nxt < self.size or pending:
                while nxt < self.size and len(pending) < self.window:
                    pending.append(ex.submit(os.pread, fd, CHUNK, nxt))
                    nxt += CHUNK
                b = pending.pop(0).result()
                if b:
                    yield b

    def close(self) -> None:
        self.fh.close()


_END = object()


class _Prefetch:
    """다음 파일을 별도 스레드가 **만들자마자** 읽기 시작해 조각 ``depth`` 개까지 쌓아 둔다(``PORTAL_ZIP_FILES_AHEAD``)."""

    def __init__(self, source: _Source, depth: int = 2) -> None:
        self.source = source
        self._q: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="zip-prefetch", daemon=True)
        self._t.start()

    def _put(self, v: object) -> bool:
        while not self._stop.is_set():
            try:
                self._q.put(v, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        try:
            for b in self.source.chunks():
                if not self._put(b):
                    return
            self._put(_END)
        except BaseException as exc:  # noqa: BLE001 — 읽기 오류는 소비하는 쪽으로 넘긴다
            self._put(exc)

    def chunks(self) -> Iterator[bytes]:
        while True:
            b = self._q.get()
            if b is _END:
                return
            if isinstance(b, BaseException):
                raise b
            yield b

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=10)
        if not self._t.is_alive():      # 읽기 스레드가 아직 읽는 중이면 핸들은 건드리지 않는다(가비지 수집이 닫는다)
            self.source.close()


def stream_zip(plan: BundlePlan) -> Iterator[bytes]:
    """계획대로 ZIP 조각을 내보낸다. 빠진 항목이 있으면 맨 끝에 ``_manifest.json`` 을 싣는다. 쓰는 도중 읽기가 실패하면 OSError 로 연결을 끊는다."""
    sink = _Sink()
    missing: list[dict[str, Any]] = list(plan.unreadable)
    used: set[str] = set()
    level = zip_level()
    window = _env_int(READ_WINDOW_ENV, 1, 1, 32)
    ahead = _env_int(FILES_AHEAD_ENV, 0, 0, 16)
    opened: dict[int, _Source | _Prefetch | None] = {}

    def open_ahead(i: int) -> None:
        """``i`` 번째 항목을 미리 연다(읽기 스레드가 바로 읽기 시작)."""
        if i in opened or not 0 <= i < len(plan.packed):
            return
        t = plan.packed[i]
        try:
            src = _Source(t["fs_path"], t["size"], window)
        except OSError:
            opened[i] = None
            return
        opened[i] = _Prefetch(src)

    try:
        for i in range(min(ahead, len(plan.packed))):
            open_ahead(i)
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as zf:
            for idx, t in enumerate(plan.packed):
                if ahead:
                    open_ahead(idx + ahead)
                    src = opened.pop(idx)
                else:
                    try:
                        src = _Source(t["fs_path"], t["size"], window)
                    except OSError:
                        src = None
                if src is None:
                    # 확인과 읽기 사이에 사라졌다 — 이 항목만 건너뛰고 끝의 목록 파일에 남긴다.
                    _LOG.warning("묶음에서 원본을 열 수 없어 건너뜀(읽기 직전): asset_id=%s fs_path=%s",
                                 t.get("asset_id"), t.get("fs_path"))
                    missing.append({"asset_id": t.get("asset_id"), "file_name": t["file_name"]})
                    continue
                name = _dedup_name(_safe_entry_name(t["file_name"]), used)
                info = zipfile.ZipInfo(filename=name, date_time=_FIXED_TIME)
                chunks = src.chunks()
                try:
                    first = next(chunks, b"")
                    stored = _precompressed(name) or looks_incompressible(first)
                    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
                    # ZipInfo 를 직접 넘기면 ZipFile 의 compresslevel 이 적용되지 않는다(파이썬 동작) — 항목에 직접 준다.
                    info._compresslevel = level  # noqa: SLF001
                    with zf.open(info, "w", force_zip64=t["size"] >= _ZIP64_HINT) as dst:
                        for chunk in _chain(first, chunks):
                            dst.write(chunk)
                            out = sink.drain()
                            if out:
                                yield out
                finally:
                    chunks.close()      # 동시 읽기 중인 조각이 끝난 **뒤에** 핸들을 닫는다(닫힌 번호를 다른 파일이 재사용하는 일을 막는다)
                    src.close()
                out = sink.drain()
                if out:
                    yield out
            if missing:
                body = json.dumps({"missing": missing}, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                minfo = zipfile.ZipInfo(filename=MANIFEST_NAME, date_time=_FIXED_TIME)
                minfo.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(minfo, body)
                out = sink.drain()
                if out:
                    yield out
        tail = sink.drain()
        if tail:
            yield tail
    finally:
        for leftover in opened.values():
            if leftover is not None:
                leftover.close()


def _chain(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    if first:
        yield first
    yield from rest
