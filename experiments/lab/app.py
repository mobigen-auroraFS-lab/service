"""실험 서버 — DB · 인증 없이 ``EXP_ROOT`` 아래 파일을 여러 방식으로 내준다(⚠️ 실험 전용 · 운영에 올리지 않는다).

경로는 항상 ``EXP_ROOT`` 안으로 가둔다(``..`` · 심볼릭 링크 탈출 거부). 변형은 URL 로 고른다.

  단건   GET /x/file/{variant}?path=            variant: custom1m | custom64k | fileresponse | anyio1m   (custom* 는 Range · If-Range · ETag 지원)
  묶음   GET /x/bundle?dir=&fmt=&method=&level=&chunk=&readahead=&files_ahead=&latency_ms=   (zip/tar 스트리밍 · 임시 파일 없음)
         GET /x/bundle-main?dir=             (main 에 있는 엔진 ``stream_zip`` 그대로 — 기준선)
  작업   POST /x/jobs?dir=  →  GET /x/jobs/{id}  →  GET /x/jobs/{id}/download   (서버가 완성본을 디스크에 만들고 Range 로 내준다)
  목록   GET /x/manifest?dir=                (브라우저가 직접 묶을 때 쓰는 파일 목록 — 이름 · 크기 · 주소)
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from experiments.lab import engines
from experiments.lab.engines import Item, Opts
from service.portal.asset import bundle_stream
from service.portal.asset.download import (
    if_range_matches,
    make_etag,
    make_last_modified,
    open_original,
    parse_range_header,
)

ROOT = Path(os.environ.get("EXP_ROOT", "/nonexistent")).resolve()
TMP = Path(os.environ.get("EXP_TMP", "/tmp/exp_jobs"))
LATENCY = float(os.environ.get("EXP_LATENCY_MS", "0"))
app = FastAPI(title="파일 전송 방식 실험")


def safe(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if ROOT != p and ROOT not in p.parents:
        raise HTTPException(400, "EXP_ROOT 밖의 경로")
    return p


def items_of(dir_rel: str) -> list[Item]:
    d = safe(dir_rel)
    if not d.is_dir():
        raise HTTPException(404, "디렉터리 없음")
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            out.append(Item(name=p.name, path=str(p), size=p.stat().st_size))
    if not out:
        raise HTTPException(404, "파일 없음")
    return out


@app.get("/x/health")
def health() -> dict:
    return {"ok": True, "root": str(ROOT)}


# ── 단건 ────────────────────────────────────────────────────────────────────────
def _iter_range(fh, start: int, end: int, chunk: int, latency_ms: float) -> Iterator[bytes]:
    remaining = end - start + 1
    try:
        fh.seek(start)
        while remaining > 0:
            if latency_ms:
                time.sleep(latency_ms / 1000.0)
            b = fh.read(min(chunk, remaining))
            if not b:
                break
            remaining -= len(b)
            yield b
    finally:
        fh.close()


async def _aiter_range(path: str, start: int, end: int, chunk: int) -> Iterator[bytes]:
    remaining = end - start + 1
    async with await anyio.open_file(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            b = await f.read(min(chunk, remaining))
            if not b:
                break
            remaining -= len(b)
            yield b


@app.get("/x/file/{variant}")
def file_variant(variant: str, path: str, request: Request):
    p = safe(path)
    if not p.is_file():
        raise HTTPException(404, "없음")
    if variant == "fileresponse":
        return FileResponse(p, media_type="application/octet-stream", filename=p.name)
    if variant not in ("custom1m", "custom64k", "anyio1m"):
        raise HTTPException(404, "모르는 변형")
    chunk = 64 * 1024 if variant == "custom64k" else 1024 * 1024
    fh, size, mtime_ns = open_original(str(p))
    etag, lm = make_etag(size, mtime_ns), make_last_modified(mtime_ns)
    headers = {"Accept-Ranges": "bytes", "ETag": etag, "Last-Modified": lm,
               "Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}"}
    rv = request.headers.get("range")
    if rv is not None and not if_range_matches(request.headers.get("if-range"), etag, lm):
        rv = None
    try:
        rng = parse_range_header(rv, size)
    except ValueError as exc:
        fh.close()
        raise HTTPException(416, "범위", headers={"Content-Range": f"bytes */{size}"}) from exc
    start, end, code = (0, size - 1, 200) if rng is None else (rng[0], rng[1], 206)
    if code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(end - start + 1, 0))
    if variant == "anyio1m":
        fh.close()
        return StreamingResponse(_aiter_range(str(p), start, end, chunk), status_code=code, headers=headers,
                                 media_type="application/octet-stream")
    return StreamingResponse(_iter_range(fh, start, end, chunk, LATENCY), status_code=code, headers=headers,
                             media_type="application/octet-stream", background=BackgroundTask(fh.close))


# ── 묶음(스트리밍) ──────────────────────────────────────────────────────────────
def _stream_headers(items: list[Item], name: str) -> dict:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Bundle-Bytes": str(sum(i.size for i in items)),
            "X-Bundle-Count": str(len(items)), "Cache-Control": "private, no-store"}


@app.get("/x/bundle")
def bundle(dir: str, fmt: str = "zip", method: str = "auto", level: int = 6, chunk: int = 1024 * 1024,
           readahead: int = 0, parallel_read: int = 0, files_ahead: int = 0, latency_ms: float = LATENCY):
    items = items_of(dir)
    o = Opts(fmt=fmt, method=method, level=level, chunk=chunk, readahead=readahead, parallel_read=parallel_read, files_ahead=files_ahead, latency_ms=latency_ms)
    gen = engines.bundle_stream(items, o)
    return StreamingResponse(gen, media_type="application/zip" if fmt == "zip" else "application/x-tar",
                             headers=_stream_headers(items, f"bundle.{fmt}"), background=BackgroundTask(gen.close))


@app.get("/x/bundle-main")
def bundle_main(dir: str):
    items = items_of(dir)
    plan = bundle_stream.plan_bundle([{"asset_id": i.name, "fs_path": i.path, "file_name": i.name} for i in items])
    gen = bundle_stream.stream_zip(plan)
    return StreamingResponse(gen, media_type="application/zip", headers=_stream_headers(items, "bundle.zip"),
                             background=BackgroundTask(gen.close))


# ── 비동기 작업(완성본을 디스크에 만든 뒤 Range 로 내준다) ───────────────────────────
JOBS: dict[str, dict] = {}


def _run_job(jid: str, items: list[Item], o: Opts) -> None:
    job = JOBS[jid]
    try:
        TMP.mkdir(parents=True, exist_ok=True)
        dest = str(TMP / f"{jid}.zip")
        t0 = time.time()
        size = engines.build_zip_file(items, dest, o)
        job.update(state="done", path=dest, size=size, build_s=round(time.time() - t0, 3))
    except Exception as exc:  # noqa: BLE001
        job.update(state="failed", error=repr(exc))


@app.post("/x/jobs")
def job_create(dir: str, method: str = "auto", level: int = 6, latency_ms: float = LATENCY):
    items = items_of(dir)
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"state": "running", "created": time.time()}
    threading.Thread(target=_run_job, args=(jid, items, Opts(method=method, level=level, latency_ms=latency_ms)), daemon=True).start()
    return JSONResponse({"job": jid}, status_code=202)


@app.get("/x/jobs/{jid}")
def job_status(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "없음")
    return {k: v for k, v in j.items() if k != "path"}


@app.get("/x/jobs/{jid}/download")
def job_download(jid: str):
    j = JOBS.get(jid)
    if not j or j.get("state") != "done":
        raise HTTPException(409, "아직")
    return FileResponse(j["path"], media_type="application/zip", filename="bundle.zip")     # Range · ETag 는 Starlette 가 처리


# ── 목록(브라우저가 직접 묶을 때) ────────────────────────────────────────────────
@app.get("/x/manifest")
def manifest(dir: str):
    items = items_of(dir)
    return {"items": [{"name": i.name, "size": i.size, "url": f"/x/file/custom1m?path={quote(os.path.relpath(i.path, ROOT))}"} for i in items]}
