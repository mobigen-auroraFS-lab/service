"""전송 방식 비교 측정 — 실험 서버(``experiments.lab.app``)를 띄워 같은 데이터로 방식마다 잰다(⚠️ 로컬 · 파일이 OS 캐시에 있어 **상한값**이다).

  A 단건 원본: 보내는 방식 4종 × 동시 1 · 8        B 묶음: 형식 · 압축 · 조각 크기 · 미리 읽기 × 데이터 3종 × 동시 1 · 8
  C 저장소 지연 모사: 조각마다 5ms 를 기다리게 해 미리 읽기가 도움이 되는가      D 비동기 작업(완성본 만든 뒤 받기 · 이어받기)
  E 브라우저가 묶기(client-zip, Node)               F 서버 구성(h11/httptools · asyncio/uvloop)

측정값: 첫 바이트(s) · 총 시간(s) · 처리량(MB/s = 원본 바이트 / 총 시간) · 서버 CPU(s) · 서버 최대 RSS(MB) · 산출물 크기. 3회 중앙값(동시 8 은 1회).
사용: ``../.venv/bin/python experiments/bench.py [--only A,B] [--out experiments/results.json]``
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import psutil

HERE = Path(__file__).resolve().parent
SVC = HERE.parent
DATA = Path(os.environ.get("EXP_DATA", "/tmp/exp_data"))
PY = sys.executable
RESULTS: list[dict] = []


# ── 데이터 ──────────────────────────────────────────────────────────────────────
def text_blob(rng: random.Random, n: int) -> bytes:
    words = ["김치", "데이터", "포털", "검색", "자산", "관계", "metadata", "index", "value", "status", "2026-10-01", "서울", "부산"]
    out, size = [], 0
    while size < n:
        line = f'{{"id": {rng.randrange(10**7)}, "name": "{rng.choice(words)}_{rng.choice(words)}", "score": {rng.random():.6f}, "tags": ["{rng.choice(words)}", "{rng.choice(words)}"]}}\n'
        out.append(line)
        size += len(line.encode())
    return "".join(out).encode()[:n]


def make_data() -> None:
    marker = DATA / ".done"
    if marker.exists():
        return
    shutil.rmtree(DATA, ignore_errors=True)
    rng = random.Random(7)
    sm = DATA / "small_mixed"
    sm.mkdir(parents=True)
    for i in range(100):
        (sm / f"문서_{i:03d}.txt").write_bytes(text_blob(rng, rng.randrange(20_000, 200_000)))
    for i in range(100):
        (sm / f"사진_{i:03d}.jpg").write_bytes(os.urandom(rng.randrange(50_000, 400_000)))
    for i in range(60):
        (sm / f"보고서_{i:03d}.pdf").write_bytes(os.urandom(rng.randrange(100_000, 600_000)))
    for i in range(40):
        (sm / f"서식_{i:03d}.docx").write_bytes(os.urandom(rng.randrange(30_000, 200_000)))
    lm = DATA / "large_media"
    lm.mkdir()
    for i in range(4):
        (lm / f"영상_{i}.mp4").write_bytes(os.urandom(120 * 1024 * 1024))
    th = DATA / "text_heavy"
    th.mkdir()
    for i in range(40):
        (th / f"로그_{i:02d}.txt").write_bytes(text_blob(rng, 3 * 1024 * 1024))
    marker.write_text("ok")


def dir_bytes(rel: str) -> int:
    return sum(p.stat().st_size for p in (DATA / rel).rglob("*") if p.is_file())


# ── 서버 ────────────────────────────────────────────────────────────────────────
class Server:
    def __init__(self, port: int, *, http: str = "auto", loop: str = "auto", env: dict | None = None):
        e = {**os.environ, "EXP_ROOT": str(DATA), "EXP_TMP": str(DATA / "_jobs"), **(env or {})}
        self.port = port
        self.proc = subprocess.Popen([PY, "-m", "uvicorn", "experiments.lab.app:app", "--host", "127.0.0.1", "--port", str(port),
                                      "--http", http, "--loop", loop, "--log-level", "warning", "--no-access-log"],
                                     cwd=SVC, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        import httpx
        for _ in range(100):
            try:
                if httpx.get(f"{self.base}/x/health", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("서버가 뜨지 않았다")
        self.ps = psutil.Process(self.proc.pid)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def cpu(self) -> float:
        t = self.ps.cpu_times()
        return t.user + t.system


class Probe:
    """한 번의 측정 동안 서버 CPU 증가 · 최대 RSS 를 모은다."""

    def __init__(self, s: Server):
        self.s, self.peak, self._stop = s, 0.0, threading.Event()

    def __enter__(self):
        self.c0 = self.s.cpu()
        self.rss0 = self.s.ps.memory_info().rss / 1e6
        self.peak = self.rss0

        def run():
            while not self._stop.is_set():
                self.peak = max(self.peak, self.s.ps.memory_info().rss / 1e6)
                time.sleep(0.05)
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self.t.join()
        self.cpu_s = self.s.cpu() - self.c0


def curl(url: str, out: str = "/dev/null") -> dict:
    p = subprocess.run(["curl", "-sS", "-o", out, "-w", "%{http_code} %{size_download} %{time_starttransfer} %{time_total}", url],
                       capture_output=True, text=True)
    code, size, ttfb, total = p.stdout.split()[:4]
    return {"code": int(code), "size": int(size), "ttfb": float(ttfb), "total": float(total)}


def measure(s: Server, url: str, src_bytes: int, *, conc: int = 1, reps: int = 3) -> dict:
    runs = []
    for _ in range(reps if conc == 1 else 1):
        with Probe(s) as pr:
            t0 = time.time()
            if conc == 1:
                rs = [curl(url)]
            else:
                with cf.ThreadPoolExecutor(conc) as ex:
                    rs = list(ex.map(lambda _: curl(url), range(conc)))
            wall = time.time() - t0
        ok = all(r["code"] == 200 for r in rs)
        runs.append({"ok": ok, "wall": wall, "ttfb": statistics.median(r["ttfb"] for r in rs), "out": sum(r["size"] for r in rs) / conc,
                     "cpu": pr.cpu_s, "rss": pr.peak, "mbps": conc * src_bytes / 1e6 / wall})
    med = lambda k: statistics.median(r[k] for r in runs)  # noqa: E731
    return {"ok": all(r["ok"] for r in runs), "ttfb_s": round(med("ttfb"), 3), "total_s": round(med("wall"), 2), "mbps": round(med("mbps")),
            "cpu_s": round(med("cpu"), 2), "rss_mb": round(max(r["rss"] for r in runs)), "out_mb": round(med("out") / 1e6, 1),
            "cpu_per_gb": round(med("cpu") / (conc * src_bytes / 1e9), 2)}


def add(scn: str, variant: str, dataset: str, conc: int, m: dict, **extra) -> None:
    row = {"scenario": scn, "variant": variant, "dataset": dataset, "conc": conc, **m, **extra}
    RESULTS.append(row)
    print(f"[{scn}] {variant:<26} {dataset:<12} x{conc} " + " ".join(f"{k}={v}" for k, v in m.items()), flush=True)


def verify_archive(url: str, fmt: str, rel_dir: str, tmp: Path) -> tuple[bool, str]:
    out = tmp / f"verify.{fmt}"
    r = curl(url, str(out))
    if r["code"] != 200:
        return False, f"HTTP {r['code']}"
    want = {p.name: p.stat().st_size for p in (DATA / rel_dir).rglob("*") if p.is_file()}
    try:
        if fmt == "zip":
            with zipfile.ZipFile(out) as z:
                bad = z.testzip()
                got = {i.filename: i.file_size for i in z.infolist()}
        else:
            with tarfile.open(out) as t:
                got = {m.name: m.size for m in t.getmembers()}
                bad = None
    except Exception as e:  # noqa: BLE001
        return False, repr(e)
    ok = bad is None and got == want
    return ok, f"{len(got)}개" + ("" if ok else f" · 불일치 bad={bad}")


# ── 시나리오 ────────────────────────────────────────────────────────────────────
def scn_a(s: Server) -> None:
    rel = "large_media/영상_0.mp4"
    size = (DATA / rel).stat().st_size
    for v in ("custom1m", "custom64k", "fileresponse", "anyio1m"):
        url = f"{s.base}/x/file/{v}?path={quote(rel)}"
        add("A", v, "120MB 1개", 1, measure(s, url, size))
        add("A", v, "120MB 1개", 8, measure(s, url, size, conc=8))


BUNDLES = [("main(기준선)", "/x/bundle-main?dir={d}", "zip"),
           ("zip auto L6", "/x/bundle?dir={d}&method=auto&level=6", "zip"),
           ("zip auto L1", "/x/bundle?dir={d}&method=auto&level=1", "zip"),
           ("zip auto L3", "/x/bundle?dir={d}&method=auto&level=3", "zip"),
           ("zip auto L9", "/x/bundle?dir={d}&method=auto&level=9", "zip"),
           ("zip ext L6", "/x/bundle?dir={d}&method=ext&level=6", "zip"),
           ("zip smart L6", "/x/bundle?dir={d}&method=smart&level=6", "zip"),
           ("zip smart L1", "/x/bundle?dir={d}&method=smart&level=1", "zip"),
           ("zip stored", "/x/bundle?dir={d}&method=stored", "zip"),
           ("zip deflate L6(전부)", "/x/bundle?dir={d}&method=deflate&level=6", "zip"),
           ("zip auto L6 +미리읽기4", "/x/bundle?dir={d}&method=auto&level=6&readahead=4", "zip"),
           ("tar", "/x/bundle?dir={d}&fmt=tar", "tar")]


def scn_b(s: Server, tmp: Path) -> None:
    for d in ("small_mixed", "large_media", "text_heavy"):
        sb = dir_bytes(d)
        for name, tpl, fmt in BUNDLES:
            url = s.base + tpl.format(d=quote(d))
            ok, note = verify_archive(url, fmt, d, tmp)
            m = measure(s, url, sb)
            add("B", name, d, 1, m, verified=ok, note=note)
            if d != "large_media" or name in ("zip auto L6", "tar"):
                add("B", name, d, 8, measure(s, url, sb, conc=8))
    # 조각 크기
    d, sb = "large_media", dir_bytes("large_media")
    for ch in (64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
        url = f"{s.base}/x/bundle?dir={d}&chunk={ch}"
        add("B", f"zip auto 조각 {ch//1024}KiB", d, 1, measure(s, url, sb))


def scn_c() -> None:
    s = Server(8062, env={"EXP_LATENCY_MS": "5"})
    try:
        d, sb = "large_media", dir_bytes("large_media")
        for name, qs in (("zip auto (미리읽기 없음)", ""), ("zip auto +미리읽기 2", "&readahead=2"), ("zip auto +미리읽기 8", "&readahead=8")):
            add("C", name + " · 조각당 5ms", d, 1, measure(s, f"{s.base}/x/bundle?dir={d}{qs}", sb, reps=2))
        for name, qs in (("zip auto +파일 4개 동시 읽기", "&files_ahead=4"), ("zip auto +파일 3개 동시 읽기+조각 2", "&files_ahead=3&readahead=2"),
                         ("zip auto +한 파일 안에서 4조각 동시 읽기", "&parallel_read=4"), ("zip auto +한 파일 안에서 8조각 동시 읽기", "&parallel_read=8"),
                         ("zip auto +8조각 동시 읽기 + 파일 3개 앞서", "&parallel_read=8&files_ahead=3")):
            add("C", name + " · 조각당 5ms", d, 1, measure(s, f"{s.base}/x/bundle?dir={d}{qs}", sb, reps=2))
        d, sb = "small_mixed", dir_bytes("small_mixed")
        for name, qs in (("zip auto (미리읽기 없음)", ""), ("zip auto +미리읽기 4", "&readahead=4"), ("zip auto +파일 4개 동시 읽기", "&files_ahead=4"), ("zip auto +파일 8개 동시 읽기", "&files_ahead=8")):
            add("C", name + " · 조각당 5ms", d, 1, measure(s, f"{s.base}/x/bundle?dir={d}{qs}", sb, reps=2))
    finally:
        s.stop()


def scn_d(s: Server, tmp: Path) -> None:
    import httpx
    for d in ("small_mixed", "large_media", "text_heavy"):
        sb = dir_bytes(d)
        with Probe(s) as pr:
            t0 = time.time()
            jid = httpx.post(f"{s.base}/x/jobs?dir={quote(d)}").json()["job"]
            while True:
                st = httpx.get(f"{s.base}/x/jobs/{jid}").json()
                if st["state"] != "running":
                    break
                time.sleep(0.05)
            ready = time.time() - t0
        r = curl(f"{s.base}/x/jobs/{jid}/download", str(tmp / "job.zip"))
        # 이어받기: 앞 40% 를 받고 나머지를 Range 로 이어 붙인다
        size = st["size"]
        cut = int(size * 0.4)
        part = tmp / "part.bin"
        part.write_bytes((tmp / "job.zip").read_bytes()[:cut])
        rr = subprocess.run(["curl", "-sS", "-H", f"Range: bytes={cut}-", "-w", "%{http_code}", f"{s.base}/x/jobs/{jid}/download", "-o", str(tmp / "rest.bin")],
                            capture_output=True, text=True).stdout
        joined = part.read_bytes() + (tmp / "rest.bin").read_bytes()
        resume_ok = rr == "206" and joined == (tmp / "job.zip").read_bytes()
        add("D", "작업 방식(완성본 → 받기)", d, 1, {"ok": st["state"] == "done", "ready_s": round(ready, 2), "ttfb_s": r["ttfb"], "dl_s": r["total"],
                                              "total_s": round(ready + r["total"], 2), "mbps": round(sb / 1e6 / (ready + r["total"])), "cpu_s": round(pr.cpu_s, 2),
                                              "rss_mb": round(pr.peak), "out_mb": round(size / 1e6, 1)}, resume_ok=resume_ok, disk_mb=round(size / 1e6, 1))


def scn_e(s: Server, tmp: Path) -> None:
    mjs = HERE / "client_zip.mjs"
    if not (HERE / "node_modules" / "client-zip").exists():
        subprocess.run(["npm", "install", "--silent", "client-zip"], cwd=HERE, check=True)
    for d in ("small_mixed", "large_media", "text_heavy"):
        sb = dir_bytes(d)
        for k in (1, 4, 8):
            out = tmp / f"cz_{d}_{k}.zip"
            t0 = time.time()
            p = subprocess.run(["node", str(mjs), s.base, d, str(k), str(out)], capture_output=True, text=True)
            wall = time.time() - t0
            try:
                info = json.loads(p.stdout.strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                info = {"error": p.stderr[-200:]}
            ok = False
            if out.exists():
                with zipfile.ZipFile(out) as z:
                    want = {q.name: q.stat().st_size for q in (DATA / d).rglob("*") if q.is_file()}
                    ok = z.testzip() is None and {i.filename: i.file_size for i in z.infolist()} == want
                out.unlink()
            add("E", f"브라우저 client-zip 병렬 {k}", d, 1, {"ok": ok, "total_s": round(wall, 2), "mbps": round(sb / 1e6 / wall), "ttfb_s": info.get("first_byte_s"),
                                                          "cpu_s": None, "rss_mb": None}, requests=info.get("requests"))


def scn_f(tmp: Path) -> None:
    combos = [("auto", "auto"), ("h11", "asyncio"), ("httptools", "asyncio"), ("h11", "uvloop"), ("httptools", "uvloop")]
    rel = "large_media/영상_0.mp4"
    size = (DATA / rel).stat().st_size
    for http, loop in combos:
        try:
            s = Server(8063, http=http, loop=loop)
        except Exception as e:  # noqa: BLE001
            print("[F] 건너뜀", http, loop, e)
            continue
        try:
            add("F", f"{http}/{loop} 단건 custom1m", "120MB 1개", 8, measure(s, f"{s.base}/x/file/custom1m?path={quote(rel)}", size, conc=8))
            d, sb = "small_mixed", dir_bytes("small_mixed")
            add("F", f"{http}/{loop} zip auto", d, 8, measure(s, f"{s.base}/x/bundle?dir={d}", sb, conc=8))
        finally:
            s.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="A,B,C,D,E,F")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    a = ap.parse_args()
    only = set(a.only.split(","))
    make_data()
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"expbench_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    s = Server(8061)
    try:
        if "A" in only:
            scn_a(s)
        if "B" in only:
            scn_b(s, tmp)
        if "D" in only:
            scn_d(s, tmp)
        if "E" in only:
            scn_e(s, tmp)
    finally:
        s.stop()
    if "C" in only:
        scn_c()
    if "F" in only:
        scn_f(tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    Path(a.out).write_text(json.dumps({"host": {"cpus": os.cpu_count(), "python": sys.version.split()[0]}, "results": RESULTS}, ensure_ascii=False, indent=1))
    bad = [r for r in RESULTS if r.get("ok") is False or r.get("verified") is False]
    print(f"\n측정 {len(RESULTS)}건 · 실패/불일치 {len(bad)}건 → {a.out}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
