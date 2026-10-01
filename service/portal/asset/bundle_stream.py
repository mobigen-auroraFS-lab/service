"""여러 파일을 **임시 파일 없이** 한 번에 흘려 보내는 ZIP(on-the-fly streaming ZIP).

**하는 일** — 파일을 하나씩 읽으며 ZIP 조각을 만들어 곧바로 응답으로 내보낸다. 서버는 ZIP 을 디스크에도 메모리에도 쌓지 않는다
(요청당 메모리는 조각 하나 ≈ 1MiB). 지우기 직전 구현(``9d11d29``)은 ZIP 을 임시 파일(``SpooledTemporaryFile``)에 **다 만든 뒤** 보내서
첫 바이트가 늦고 임시 디스크가 들었다 — 원본이 고속병렬저장소에 있어 서버로 복사해 묶는 방식은 맞지 않다는 판단으로 바꿨다(2026-10-01).

**지금 상황과 대안 (2026-10-01 결정 기록)**
  · 채택: 서버가 만들면서 흘려 보낸다(이 모듈). 새 인프라(큐 · 워커 · 객체 저장소)가 필요 없고 임시 저장공간이 0 이다.
  · 대안 B — 브라우저가 묶기: 웹이 단건 다운로드(``GET /assets/{id}/download``)를 4~6개 병렬로 받아 ``client-zip`` 류로 ZIP 을 만들어
    파일 저장 창(File System Access API)에 바로 쓴다. 서버가 가장 가볍지만 **HTTPS 가 필요**하고(지금 사내 배포본은 http), Chrome/Edge 전용이며,
    파일마다 요청 · DB 조회 · 감사가 따라붙는다. 웹 저장소가 준비되면 이쪽으로 옮길 수 있다 — 단건 다운로드는 이미 Range · ETag · If-Range 를 지원한다.
  · 대안 A — 서버 비동기 ZIP: 큐 → 워커 → 완성본을 객체 저장소에 두고 서명 URL 로 받는다. 모든 브라우저에서 안정적이고 이어받기가 쉽지만
    원본을 한 번 더 복사하게 되고(저장소 이중 사용) 권한이 회수된 뒤에도 완성본이 남으며 운영할 것이 많다. 수십 GB · Safari/Firefox 지원이 필요해지면 검토한다.
  · 이 방식의 한계: ZIP 크기를 미리 알 수 없어 ``Content-Length`` 가 없다(진행률은 헤더 ``X-Bundle-Bytes`` 로 어림) · **이어받기가 안 된다**(중간에 끊기면 처음부터)
    · 한 줄기로 순서대로 읽는다 · 응답을 시작한 뒤에는 오류 봉투를 못 씌운다(실패는 연결이 끊기는 것으로 드러난다).

**규칙**
  · 시작하기 전에 한다(``plan_bundle``): 각 파일의 존재 · 크기를 확인해 담을 수 있는 것과 빠지는 것을 가른다. 그래서 건수 · 용량 헤더를 **먼저** 줄 수 있고,
    상한 초과 · 전부 누락 같은 오류는 응답 시작 **전에** 봉투로 돌려준다.
  · 확인과 읽기 사이에 파일이 사라지면 그 항목만 건너뛰고 ``_manifest.json`` 에 남긴다(ZIP 끝에 쓰므로 가능하다). 한 파일을 쓰는 도중 읽기가 실패하면
    ZIP 이 이미 시작됐으므로 되돌릴 수 없다 — 예외를 올려 연결을 끊는다(조용히 깨진 ZIP 을 주지 않는다).
  · 서버 경로는 ZIP 어디에도 싣지 않는다(``_manifest.json`` 에는 자산 id · 파일명만).
  · 같은 입력이면 **같은 바이트**가 나온다 — 항목 순서 · 타임스탬프(1980-01-01) · 이름 중복 처리를 고정한다.
  · 이미 압축된 형식(이미지 · 영상 · 음성 · 압축 파일 · pdf · docx 같은 압축 컨테이너)은 압축하지 않고 담고(STORED), 나머지는 압축한다(DEFLATED).
    목록에 없는 형식(hwp · 암호화 파일)은 앞부분 64KiB 를 가장 빠른 수준으로 **시험 압축**해 거의 안 줄면(92% 이상 남으면) 압축하지 않는다.
    (2026-10-01 실험: 병목은 전송이 아니라 DEFLATE 였다 — 문서가 섞인 묶음에서 서버 CPU 가 3~5배 줄었다. ``experiments/RESULTS.md``)

**환경변수** (모두 선택 · 기본값이 안전하다)
  · ``PORTAL_ZIP_LEVEL``(1~9 · 기본 6) — 압축 수준. 텍스트에서 1 은 CPU 1/3 · 크기 +45%, 9 는 CPU 5배 · 크기 −9%. 사내망이면 1~3 도 합리적이다(정책으로 정한다).
  · ``PORTAL_ZIP_READ_WINDOW``(기본 1 = 끔) — 큰 파일 한 개를 조각 N 개로 **동시에** 읽는다(``pread`` 창). 저장소 읽기 지연이 클 때(NFS · 객체)만 의미가 있다.
  · ``PORTAL_ZIP_FILES_AHEAD``(기본 0 = 끔) — 다음 파일 N 개를 **미리** 읽어 둔다. 작은 파일이 많은 묶음에서 파일마다 생기는 지연을 겹친다.
    둘 다 요청당 메모리가 ``창 × 조각(1MiB)`` 만큼 늘고 읽기 스레드가 붙는다. 로컬 디스크에서는 이득이 없다 — 실제 저장소에서 재어 보고 켠다.
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

# 확장자 목록에는 없지만 이미 압축된 컨테이너 — 다시 압축해도 안 줄고 CPU 만 든다.
_PRECOMPRESSED_CONTAINERS = frozenset({
    "pdf", "docx", "xlsx", "pptx", "hwpx", "odt", "ods", "odp", "epub", "jar", "apk",
    "woff2", "heif", "jxl", "br", "lz4", "zstd",
})
_PRECOMPRESSED = _PRECOMPRESSED | _PRECOMPRESSED_CONTAINERS

# 목록에 없는 형식은 앞부분을 시험 압축해 가른다 — 92% 이상 남으면 압축 이득이 없다.
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
    """묶음 대상의 존재 · 크기를 **파일을 열기 전에** 확인해 계획을 세운다(읽기 전용 · 응답 시작 전).

    Args:
        targets: ``{asset_id, fs_path, file_name}`` 목록(순서가 곧 ZIP 항목 순서).

    Returns:
        ``packed``(``size`` 포함) · ``unreadable`` · ``total_bytes``. 경로가 없거나 일반 파일이 아니거나 열 수 없으면 ``unreadable``.
    """
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
    """열린 원본 한 개 — 조각을 순서대로 내준다. ``window`` > 1 이고 파일이 크면 조각 여러 개를 **동시에** 읽어 두었다가 순서대로 내보낸다.

    열기(``open``)가 실패하면 ``OSError`` — 호출부가 「읽기 직전에 사라진 파일」로 처리한다. 닫기(``close``)는 여러 번 불러도 된다.
    """

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
    """계획대로 ZIP 을 만들면서 조각을 내보낸다. 끝나면(또는 연결이 끊기면) 열린 파일은 닫힌다.

    Args:
        plan: ``plan_bundle`` 이 만든 계획.

    Yields:
        ZIP 바이트 조각. 빠진 항목이 있으면 맨 끝에 ``_manifest.json`` 을 싣는다.

    Raises:
        OSError: 한 파일을 쓰는 도중 읽기가 실패했을 때(이미 ZIP 이 시작돼 되돌릴 수 없다 — 연결이 끊긴다).
    """
    sink = _Sink()
    missing: list[dict[str, Any]] = list(plan.unreadable)
    used: set[str] = set()
    level = zip_level()
    window = _env_int(READ_WINDOW_ENV, 1, 1, 32)
    ahead = _env_int(FILES_AHEAD_ENV, 0, 0, 16)
    opened: dict[int, _Source | _Prefetch | None] = {}      # 앞서 열어 둔 항목(None = 읽기 직전에 사라짐)

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
                    # 압축할지 — 이미 압축된 형식은 무압축, 목록에 없는 형식은 앞부분을 시험 압축해 가른다.
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
        tail = sink.drain()        # 중앙 디렉터리(맨 끝)
        if tail:
            yield tail
    finally:
        for leftover in opened.values():        # 연결이 끊겨 못 읽은 항목의 읽기 스레드 · 핸들을 정리한다
            if leftover is not None:
                leftover.close()


def _chain(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    if first:
        yield first
    yield from rest
