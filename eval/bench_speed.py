"""Speed benchmark: upload several documents AT THE SAME TIME to an isolated local server and
record how long each takes and where the time goes. Makes REAL LLM calls (the server uses
backend/.env's key) - set --call-cap to bound the spend.

  python eval/bench_speed.py run --label auto --env LLM_PAGE_INPUT=auto --out eval/benchmarks/run_auto.json
  python eval/bench_speed.py snapshot-prod --out eval/benchmarks/baseline_render_prod.json
  python eval/bench_speed.py report eval/benchmarks/run_auto.json [--baseline eval/benchmarks/baseline_render_prod.json]

The server runs against a fresh SQLite file (deleted afterwards) with 2 workers, like Render.
CPU seconds / peak memory of the server process are measured on Windows via the Win32 API
(skipped elsewhere).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DOCS = ROOT / "docs"
DEFAULT_FILES = [
    DOCS / "pdf&rendition=1.pdf",
    DOCS / "sterling-accuris-pathology-sample-report-unlocked.pdf",
    DOCS / "sample test docs" / "sterling-accuris-pathology-sample-report-unlocked-3.pdf",
    DOCS / "sample test docs" / "sterling-accuris-pathology-sample-report-unlocked-6.pdf",
    DOCS / "sample test docs" / "sterling-accuris-pathology-sample-report-unlocked-18.pdf",
    DOCS / "sample test docs" / "sterling-accuris-pathology-sample-report-unlocked-19.pdf",
]
PROD_API = "https://healthpilot-api-2b2m.onrender.com"
TERMINAL = {"complete", "failed", "cancelled"}


# ----------------------------------------------------------------------------- http helpers
def http_json(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def upload(base, path: Path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return http_json(f"{base}/reports", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


# ----------------------------------------------------------------------------- process stats (Windows)
def _children(pid):
    """The venv's python.exe on Windows is a launcher that starts the real interpreter as a child."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"], text=True, timeout=30)
        return [int(x) for x in out.split()]
    except Exception:
        return []


def proc_stats(pid):
    """CPU seconds + peak memory of the server (the process and its direct children)."""
    if os.name != "nt":
        return None
    parts = [x for x in (_proc_stats_one(p) for p in [pid, *_children(pid)]) if x]
    if not parts:
        return None
    return {"cpu_seconds": round(sum(x["cpu_seconds"] for x in parts), 2), "peak_mb": max(x["peak_mb"] for x in parts)}


def _proc_stats_one(pid):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wt.HANDLE
    h = k.OpenProcess(0x1000 | 0x0010, False, pid)     # QUERY_LIMITED_INFORMATION | VM_READ
    if not h:
        return None
    ct, et, kt, ut = (wt.FILETIME() for _ in range(4))
    k.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    k.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut))
    to_s = lambda f: ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    k.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    k.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    k.CloseHandle(h)
    return {"cpu_seconds": round(to_s(kt) + to_s(ut), 2), "peak_mb": round(pmc.PeakWorkingSetSize / 1e6)}


# ----------------------------------------------------------------------------- run
def summarize(detail):
    p = detail.get("progress") or []
    t = lambda e: datetime.fromisoformat(e["t"])
    t0 = t(p[0]) if p else None
    off = lambda pred: next(((t(e) - t0).total_seconds() for e in p if pred(e["msg"])), None)
    ai = [float(m.group(1)) for e in p if (m := re.search(r"the AI found .*\(([\d.]+)s\)", e["msg"]))]
    return {
        "opened_at": off(lambda m: m.startswith("Opened")),
        "sending_at": off(lambda m: m.startswith("Sending")),
        "first_page_at": off(lambda m: "the AI found" in m),
        "ai_page_seconds": ai,
        "final_line": p[-1]["msg"] if p else None,
    }


def cmd_run(a):
    files = [Path(f) for f in a.files] if a.files else DEFAULT_FILES
    for f in files:
        if not f.exists():
            sys.exit(f"missing file: {f}\n(docs/pdf&rendition=1.pdf is a real patient's report and is deliberately "
                     "not in the repository; put your own copy there or pass --files.)")
    db = BACKEND / f"hp_bench_{a.label}.db"
    db.unlink(missing_ok=True)
    port = a.port
    env = dict(os.environ, DATABASE_URL=f"sqlite:///./{db.name}", LLM_CALL_CAP=str(a.call_cap),
               WORKER_CONCURRENCY="2", SQLITE_WORKER_CONCURRENCY="2", PYTHONUNBUFFERED="1",
               RATE_LIMIT_PER_MINUTE="0", RATE_LIMIT_UPLOADS_PER_MINUTE="0")   # the harness polls often
    for kv in a.env:
        k, v = kv.split("=", 1)
        env[k] = v
    py = BACKEND / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    log = open(BACKEND / f"hp_bench_{a.label}.log", "w")
    server = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                              cwd=BACKEND, env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if http_json(f"{base}/health", timeout=3).get("status") == "ok":
                    break
            except Exception:
                time.sleep(1)
        else:
            sys.exit("server did not start")
        time.sleep(a.settle)            # let the one-time LOINC index warm-up finish, like a warmed Render instance
        before = proc_stats(server.pid)

        ids, t_up, t_done = {}, {}, {}
        start = time.monotonic()

        def do_upload(f):
            t_up[f.name] = time.monotonic() - start
            ids[f.name] = upload(base, f)["id"]

        threads = [threading.Thread(target=do_upload, args=(f,)) for f in files]
        [t.start() for t in threads]
        [t.join() for t in threads]
        while len(t_done) < len(files):
            for name, i in ids.items():
                if name in t_done:
                    continue
                st = http_json(f"{base}/reports/{i}")
                if st["status"] in TERMINAL:
                    t_done[name] = time.monotonic() - start
            if time.monotonic() - start > a.timeout:
                break
            time.sleep(0.5)
        wall = time.monotonic() - start
        after = proc_stats(server.pid)

        docs = []
        for f in files:
            d = http_json(f"{base}/reports/{ids[f.name]}")
            docs.append({
                "file": f.name, "pages": d["num_pages"], "status": d["status"], "rows": len(d["observations"]),
                "used_fallback": d["used_fallback"], "processing_seconds": d["processing_seconds"],
                "wall_seconds_from_upload": round(t_done.get(f.name, float("nan")), 1),
                "queue_wait_seconds": round(max(0.0, t_done.get(f.name, 0) - (d["processing_seconds"] or 0)), 1),
                **summarize(d),
                "stages": {s: sum(1 for o in d["observations"] if o["mapping_stage"] == s)
                           for s in {o["mapping_stage"] for o in d["observations"]}},
                "observations": [{"page": o["page_number"], "name": o["original_test_name"], "value": o["value"],
                                  "unit": o["unit"], "loinc": o["loinc_code"], "stage": o["mapping_stage"],
                                  "status": o["mapping_status"]} for o in d["observations"]],
            })
        result = {
            "label": a.label, "env": a.env, "total_wall_seconds": round(wall, 1),
            "server_cpu_seconds": round(after["cpu_seconds"] - before["cpu_seconds"], 2) if before and after else None,
            "server_peak_mb": after["peak_mb"] if after else None, "documents": docs,
            "when": datetime.now().isoformat(timespec="seconds"),
        }
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "documents"}, indent=1))
        for d in docs:
            print(f'{d["file"][-42:]:42s} {d["pages"]:2d}p  rows={d["rows"]:3d}  proc={d["processing_seconds"]}s  '
                  f'wall={d["wall_seconds_from_upload"]}s  {d["status"]}')
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except Exception:
            server.kill()
        log.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            for _ in range(20):                    # the OS may hold the file for a moment after the server exits
                try:
                    Path(str(db) + suffix).unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.5)


# ----------------------------------------------------------------------------- baseline from the deployed app
def cmd_snapshot(a):
    names = {f.name for f in DEFAULT_FILES}
    lst = http_json(f"{PROD_API}/reports")
    chosen = {}
    for r in lst:                                   # newest first; keep the newest clean completed run per file
        if r["filename"] in names and r["filename"] not in chosen and r["status"] == "complete" and r["processing_seconds"]:
            chosen[r["filename"]] = r["id"]
    docs = []
    for name, i in chosen.items():
        d = http_json(f"{PROD_API}/reports/{i}")
        docs.append({
            "file": name, "pages": d["num_pages"], "status": d["status"], "rows": len(d["observations"]),
            "processing_seconds": d["processing_seconds"], **summarize(d),
            "observations": [{"page": o["page_number"], "name": o["original_test_name"], "value": o["value"],
                              "unit": o["unit"], "loinc": o["loinc_code"], "stage": o["mapping_stage"],
                              "status": o["mapping_status"]} for o in d["observations"]],
        })
    out = {"label": "render-prod-before", "note": "previous version, Render free tier, 6 documents uploaded together",
           "when": datetime.now().isoformat(timespec="seconds"), "documents": docs}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"saved {len(docs)} documents -> {a.out}")


# ----------------------------------------------------------------------------- report / comparison
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _same_row(b, n):
    """Same page and same printed value, and one name contains the other (image mode may print
    'RBC Count Optical impedance' where text mode keeps 'RBC Count' - same row)."""
    if b["page"] != n["page"] or norm(b["value"]) != norm(n["value"]):
        return False
    x, y = norm(b["name"]), norm(n["name"])
    return x == y or (x and y and (x in y or y in x))


def agreement(base_doc, new_doc):
    """How many of the baseline's rows the new run reproduced, and for those how many got the same
    LOINC code. The baseline is the previous version's output, NOT ground truth."""
    unused = list(new_doc["observations"])
    matched = same_code = 0
    for o in base_doc["observations"]:
        hit = next((n for n in unused if _same_row(o, n)), None)
        if hit is not None:
            unused.remove(hit)
            matched += 1
            same_code += (hit["loinc"] == o["loinc"])
    return matched, same_code, len(base_doc["observations"])


def cmd_report(a):
    run = json.loads(Path(a.run).read_text(encoding="utf-8"))
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8")) if a.baseline else None
    bmap = {d["file"]: d for d in base["documents"]} if base else {}
    print(f'### {run["label"]}  (env: {", ".join(run["env"]) or "defaults"})\n')
    print(f'Total wall time for all documents: **{run["total_wall_seconds"]} s**'
          + (f' - server CPU {run["server_cpu_seconds"]} s, peak memory {run["server_peak_mb"]} MB' if run.get("server_cpu_seconds") is not None else "") + "\n")
    print("| Document | Pages | Rows | Opened at (s) | First page back (s) | Slowest AI page (s) | Processed in (s) | Wall from upload (s) | LOINC stages |")
    print("|---|---|---|---|---|---|---|---|---|")
    for d in run["documents"]:
        ai = d["ai_page_seconds"]
        short = d["file"].replace("sterling-accuris-pathology-sample-report-unlocked", "sterling")
        stages = ", ".join(f"{k} {v}" for k, v in sorted(d["stages"].items()))
        print(f'| {short} | {d["pages"]} | {d["rows"]} | {d["opened_at"]} | {d["first_page_at"]} | {max(ai) if ai else "-"} | {d["processing_seconds"]} | {d["wall_seconds_from_upload"]} | {stages} |')
    if base:
        print("\n| Document | Before (Render) s | Now s | Rows before/now | Rows reproduced | Same LOINC code |")
        print("|---|---|---|---|---|---|")
        for d in run["documents"]:
            b = bmap.get(d["file"])
            if not b:
                continue
            if b.get("redacted"):
                print(f'| {d["file"]} | {b["processing_seconds"]} | {d["processing_seconds"]} | {b["rows"]}/{d["rows"]} | (rows redacted: real patient report) | |')
                continue
            m, c, t = agreement(b, d)
            short = d["file"].replace("sterling-accuris-pathology-sample-report-unlocked", "sterling")
            print(f'| {short} | {b["processing_seconds"]} | {d["processing_seconds"]} | {b["rows"]}/{d["rows"]} | {m}/{t} | {c}/{m} |')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--env", action="append", default=[], help="KEY=VALUE for the server (repeatable)")
    r.add_argument("--files", nargs="*")
    r.add_argument("--port", type=int, default=8020)
    r.add_argument("--call-cap", type=int, default=800)
    r.add_argument("--settle", type=float, default=20, help="seconds to let startup warm-up finish")
    r.add_argument("--timeout", type=float, default=600)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("snapshot-prod")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_snapshot)
    p = sub.add_parser("report")
    p.add_argument("run")
    p.add_argument("--baseline")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
