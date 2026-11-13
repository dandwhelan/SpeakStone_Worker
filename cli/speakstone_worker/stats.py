"""Board data: the worker's own log, one nvidia-smi reading, and the public queue figures.
Ported from app/ssapp/stats.py and extended with what the live board shows (words, audio minutes, history, events)."""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAKING_EVENTS = {"claim", "resume", "run_start"}
DONE_EVENTS = {"complete", "fail", "idle", "stop", "lease_lost"}
_KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
BUCKET_SECONDS = 600
BUCKETS = 12                                        # 2 hours of 10-minute buckets


def parse_line(line):
    """'2026-11-11T10:00:00Z claim job=ab attempt=1 lines=12' -> (epoch, event, {kv}) or None."""
    parts = line.strip().split(" ", 2)
    if len(parts) < 2:
        return None
    try:
        ts = datetime.strptime(parts[0].rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    kv = {}
    for k, v in _KV.findall(parts[2] if len(parts) > 2 else ""):
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        kv[k] = v
    return ts, parts[1], kv


def _int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_events(log_dir, since=None, max_files=3):
    events = []
    for f in sorted(Path(log_dir).glob("worker_*.log"))[-max_files:]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    p = parse_line(ln)
                    if p and (since is None or p[0] >= since):
                        p[2]["job"] = p[2].get("job", "-")
                        events.append(p)
        except OSError:
            pass
    return events


def local_midnight(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def summarize(events, now, avg_job_seconds_default=60.0):
    """Reduce log events to what the board shows. Pure."""
    day0 = local_midnight(now)
    out = {"lines": 0, "words": 0, "audio_min": 0.0, "lines_hour": 0, "words_hour": 0.0, "first_today": None,
           "making": False, "job": None, "job_lines": 0, "job_started": 0.0, "avg_job_seconds": avg_job_seconds_default,
           "events": [], "spark": [0.0] * BUCKETS, "errors": 0, "dl_today": 0, "dl_job": 0}
    buckets = [0.0] * BUCKETS
    seconds = []
    dl_by_job = {}                                  # bytes the worker itself fetched, per job
    for ts, ev, kv in events:
        job = kv.get("job", "-")
        if ev == "complete":
            n = _int(kv.get("ok")) + _int(kv.get("held"))
            w = _int(kv.get("words"))
            if ts >= day0:
                out["lines"] += n
                out["words"] += w
                out["audio_min"] += _flt(kv.get("audio")) / 60.0
                out["first_today"] = out["first_today"] or ts
            if now - ts <= 3600:
                out["lines_hour"] += n
                out["words_hour"] += w
            age = int((now - ts) // BUCKET_SECONDS)
            if 0 <= age < BUCKETS:
                buckets[BUCKETS - 1 - age] += (w or n)
            if _flt(kv.get("seconds")) > 0:
                seconds.append(_flt(kv.get("seconds")))
            out["making"] = False
            out["events"].append((ts, "done", f"job {job[:8]} - {n} lines", f"{_flt(kv.get('seconds')):.0f} s"))
        elif ev in MAKING_EVENTS:
            if ev != "run_start":
                out["job"], out["job_lines"], out["job_started"] = job, _int(kv.get("lines")), ts
                if ev == "resume":
                    out["events"].append((ts, "resumed", f"job {job[:8]}", "after a restart"))
            out["making"] = True
        elif ev == "run_end" and kv.get("error"):
            out["events"].append((ts, "error", f"job {job[:8]} - {kv['error']}", ""))
        elif ev == "fail":
            out["making"] = False
            out["events"].append((ts, "retry", f"job {job[:8]} - given back", kv.get("reason", "")))
        elif ev == "lease_lost":
            out["making"] = False
            out["events"].append((ts, "dropped", f"job {job[:8]}", "the queue gave it to someone else"))
        elif ev == "ref_fetch":
            n = _int(kv.get("bytes"))
            if n:
                dl_by_job[job] = dl_by_job.get(job, 0) + n
                if ts >= day0:
                    out["dl_today"] += n
        elif ev == "error":
            out["errors"] += 1
            out["events"].append((ts, "error", kv.get("where", "worker"), "will retry"))
        elif ev in ("idle", "stop"):
            out["making"] = False
    if out["making"] and now - out["job_started"] > 3 * 3600:
        out["making"] = False                       # a job cannot outlive the 3 h lease cap
    if seconds:
        out["avg_job_seconds"] = sum(seconds[-10:]) / len(seconds[-10:])
    out["spark"] = buckets
    out["dl_job"] = dl_by_job.get(out["job"], 0)
    out["events"] = out["events"][-8:][::-1]
    out["audio_min"] = round(out["audio_min"])
    return out


def gpu_reading(runner=subprocess.run):
    try:
        r = runner(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,name",
                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        vals = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        t, u, mu, mt = (int(float(v)) for v in vals[:4])
        return {"temp_c": t, "util_pct": u, "mem_used_mib": mu, "mem_total_mib": mt,
                "name": vals[4] if len(vals) > 4 else "NVIDIA graphics card"}
    except Exception:
        return None


def gpu_total_mib(runner=subprocess.run):
    g = gpu_reading(runner)
    return g["mem_total_mib"] if g else None


def fetch_queue(api_base, opener=urllib.request.urlopen, timeout=8):
    """Public figures from /api/network/stats. None when unreachable."""
    try:
        with opener(api_base.rstrip("/") + "/api/network/stats", timeout=timeout) as r:
            d = json.loads(r.read(2_000_000).decode("utf-8"))
        q = d.get("queue") or {}
        return {"queued_lines": _int(q.get("queued_lines")) + _int(q.get("in_progress_lines")),
                "eta_seconds": d.get("eta_seconds")}
    except Exception:
        return None


def format_duration(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{sec // 60} m"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return f"{h} h {m:02d} m" if h < 48 else f"{h / 24:.1f} days"


def time_left(queue, words_hour_est, lines_hour):
    if queue and queue.get("eta_seconds") is not None:
        return queue["eta_seconds"]
    if queue and lines_hour > 0:
        return queue["queued_lines"] / lines_hour * 3600
    return None


def merge_summaries(parts):
    """Add several workers' board summaries into one."""
    if len(parts) == 1:
        return parts[0]
    out = dict(parts[0])
    for k in ("lines", "words", "lines_hour", "words_hour", "errors"):
        out[k] = sum(p[k] for p in parts)
    out["audio_min"] = sum(p["audio_min"] for p in parts)
    out["spark"] = [sum(v) for v in zip(*(p["spark"] for p in parts))]
    firsts = [p["first_today"] for p in parts if p["first_today"]]
    out["first_today"] = min(firsts) if firsts else None
    out["events"] = sorted((e for p in parts for e in p["events"]), key=lambda e: -e[0])[:8]
    out["avg_job_seconds"] = sum(p["avg_job_seconds"] for p in parts) / len(parts)
    making = [p for p in parts if p["making"]]
    out["making"] = bool(making)
    if making:
        best = max(making, key=lambda p: p["job_started"])
        out["job"], out["job_lines"], out["job_started"] = best["job"], best["job_lines"], best["job_started"]
    return out


# ---------------------------------------------------------------- GPU utilisation, smoothed
UTIL_WINDOW = 10.0                         # seconds of samples averaged for the board


def rolling_avg(samples, now, window=UTIL_WINDOW):
    """Mean of the (time, value) samples from the last `window` seconds; None when there are none."""
    vals = [v for t, v in samples if v is not None and now - t <= window]
    return sum(vals) / len(vals) if vals else None


_TYPEPERF_3D = r"\GPU Engine(*engtype_3D)\Utilization Percentage"
_LUID = re.compile(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", re.I)


def parse_typeperf_3d(text):
    """Windows `typeperf ... -sc 1` CSV -> 3D utilisation of the busiest adapter (sum over its engines), or None.
    Task Manager's 3D graph is that sum per adapter, so the busiest adapter is the card doing our work."""
    rows = [ln for ln in text.splitlines() if ln.startswith('"')]
    if len(rows) < 2:
        return None
    head = [c.strip('"') for c in rows[0].split('","')]
    vals = [c.strip('"') for c in rows[1].split('","')]
    per = {}
    for name, v in zip(head[1:], vals[1:]):
        m = _LUID.search(name)
        try:
            x = float(v)
        except ValueError:
            continue
        if m:
            per[m.group(1)] = per.get(m.group(1), 0.0) + x
    return min(100.0, max(per.values())) if per else None


def read_3d(runner=subprocess.run):
    try:
        r = runner(["typeperf", _TYPEPERF_3D, "-sc", "1"], capture_output=True, text=True, timeout=8,
                   creationflags=0x08000000)
        return parse_typeperf_3d(r.stdout)
    except Exception:
        return None


class GpuMonitor:
    """nvidia-smi every call, averaged over ~10 s; on Windows also the 3D engine figure Task Manager shows (read on its
    own thread because typeperf takes about a second). The 3D figure is dropped for good if it ever fails."""

    def __init__(self, reader=None, d3_reader=None, clock=time.time, windows=None, start_thread=True):
        self.reader, self.clock = reader or gpu_reading, clock
        self.d3_reader = d3_reader or read_3d
        self.util, self.d3 = [], []
        self.d3_ok = (sys.platform == "win32") if windows is None else windows
        self._last3d = 0.0
        if self.d3_ok and start_thread:
            threading.Thread(target=self._loop3d, daemon=True).start()

    def _loop3d(self):
        while self.d3_ok:
            try:
                self.sample3d()
            except Exception:                  # one bad reading must not end the thread; try again in 2 s
                pass
            time.sleep(2)

    def sample3d(self):
        v = self.d3_reader()
        if v is None:
            self.d3_ok = False
            self.d3.clear()
        else:
            self.d3.append((self.clock(), v))
            del self.d3[:-20]

    def read(self):
        g = self.reader()
        now = self.clock()
        if not g:
            return None
        self.util.append((now, g["util_pct"]))
        del self.util[:-20]
        g = dict(g)
        g["util_pct"] = round(rolling_avg(self.util, now))
        d3 = rolling_avg(self.d3, now) if self.d3_ok else None
        if d3 is not None:
            g["util3d_pct"] = round(d3)
        return g


def download_figures(s):
    """(this job MB, today MB) of the worker's OWN downloads (voice sample clips it logged), from a summary."""
    return (s.get("dl_job", 0) / 1e6, s.get("dl_today", 0) / 1e6)
