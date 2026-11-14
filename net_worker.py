#!/usr/bin/env python3
"""
net_worker.py - the SpeakStone worker-network client.

Click-and-go loop:  python net_worker.py run
Model hash:         python net_worker.py --print-model-hash
First-time config:  python net_worker.py init --model-dir D:\\models\\IndexTTS-2     (key from SSW_WORKER_KEY or a prompt)

It claims a job from the site, fetches the voice sample clips (cached by sha256), converts the payload to the chunk
format indextts2_worker.py already reads, runs indextts2_worker.py UNCHANGED as a subprocess, uploads the clips with
presigned URLs and posts the result manifest. Python >= 3.10, standard library only. It imports no torch / numpy.

Privacy: the worker key is never logged or printed (sswk_ + last 4 only); the text of a line is never logged.
Exit codes: 0 stopped, 2 config error, 3 key refused (401), 4 protocol refused (426), 5 model dir missing,
6 another worker is already running with this key (config file) on this PC.
"""
from __future__ import annotations

import argparse
import email.utils
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------------------------- constants
PROTOCOL = 1
WORKER_VERSION = "1.2.17"
RECIPE = "idx2-v1"
ENGINE = "indextts2"
DEFAULT_API_BASE = "https://speakstone.beanw.co.uk"
HEARTBEAT_SECONDS = 60
# Empty queue: the site's retry_after_seconds when it gives one, else 15 s doubling; never more than 60 s, so new work
# is picked up within a minute. +-20% jitter keeps many workers from asking at the same moment.
IDLE_BASE_SECONDS = 15
IDLE_CAP_SECONDS = 60
IDLE_JITTER = 0.2
RETRY_AFTER_CAP = 300     # longest wait a 429 Retry-After can ask for
# Keep the engine loaded between jobs (SSW_ENGINE_KEEP=0 turns it off). It is restarted after this many runs and after
# this long with no job, and always when local GPU work wants the card, so the memory is not held for ever.
ENGINE_MAX_JOBS = 50
ENGINE_IDLE_SECONDS = 300
# A frozen engine keeps its job (the heartbeat keeps renewing the lease) for hours. No new clip, no new line of engine
# output and no ready/done marker for this long and the engine is killed and the job goes back to the queue. Loading
# the model before the first clip can take several minutes, so the clock starts at run start and this covers it.
STALL_SECONDS = 900
STALL_MIN_SECONDS = 120
STALL_CHECK_SECONDS = 2     # how often the progress signature is looked at (a folder listing is cheap, not free)
ENGINE_READY_MARKER = "[ENGINE-READY]"
ENGINE_DONE_RE = re.compile(r"^\[JOB-DONE\] rc=(-?\d+)\s*$")
UPLOAD_THREADS = 10       # a job is about 10 small clips and each PUT opens a new TLS connection, so all go at once
FINISH_GRACE_SECONDS = 120   # a finished job may still upload and complete for this long after a stop is asked for
BACKOFF_START = 5
BACKOFF_CAP = 300
VRAM_POLL_SECONDS = 30
GPU_FATAL_WAIT_SECONDS = 60
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_REF_BYTES = 20 * 1024 * 1024
MAX_RESUME = 8
YIELD_CHECK_SECONDS = 10    # spec 12a, yield
YIELD_POLL_SECONDS = 20
PRESENCE_SECONDS = 240      # while stepping aside (yield / vram) no call reaches the site: say "still here" this often
PING_FAIL_LOG_SECONDS = 1800   # a failing presence call is logged at most this often
MAX_RERUNS = 2            # reruns of indextts2_worker.py after the first run
QC_TRIES = 3              # runpod_worker.QC_TRIES: a line held this many times is final
HELD_DIR = "_held_qc"
QC_LOG = "_qc_held.jsonl"
PROV_LOG = "generation_provenance.jsonl"
GPU_UNSAFE_MARKER = "GPU state is unsafe"
OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")      # never passed on: model files may always be fetched
KEY_RE = re.compile(r"^sswk_[A-Za-z0-9_-]{43}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
BITRATE_RE = re.compile(r"^[0-9]{2,3}k$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
LIVE_KEYS = ("gpu_util", "gpu_mem_used_mib", "vram_free_mib", "session_started_at", "session_clips",
             "session_audio_sec", "session_words", "words_per_hour", "audio_sec_per_hour")

EXIT_OK, EXIT_CONFIG, EXIT_KEY, EXIT_PROTOCOL, EXIT_MODEL = 0, 2, 3, 4, 5
EXIT_BUSY = 6                           # another worker holds this config's lock (one worker per key on a PC)
EXIT_BROKEN = 7                         # job after job, not one line could be made: stop instead of burning the queue
# A PC whose engine or audio tool is broken fails every line of every job within seconds. Without a brake it claimed
# the next job at once: 152 failed attempts across 17 jobs in 15 minutes on a rented Linux box (2026-11-13), and the
# hub gave 14 groups up. After a job with NOT ONE line made the worker waits; after this many in a row it stops.
DEAD_JOBS_LIMIT = 2
DEAD_JOB_WAIT_SECONDS = 120
# The brake only counted jobs with not one line made, and any other failure reset it, so a PC whose engine stalled on
# every job or whose uploads always failed failed a job every 15 minutes for ever (2026-11-14 review). Now ANY failed
# job counts, except the user's own stop and a yield hand-back; only a completed job resets it. The wait after a failed
# job doubles for each failure in the row (120, 240, 480 ... up to the cap), and this many in a row stop the worker.
FAILED_JOBS_LIMIT = 5
FAILED_JOB_WAIT_CAP_SECONDS = 1800
BROKEN_FILE = "CANNOT_MAKE_LINES"       # the last error, for the app to show
BUSY_MESSAGE = "Another SpeakStone Worker is already running with this key on this PC"
MAX_REFS_PER_JOB = 40                    # 8 until 1.2.8; a chunk mixes many voices and 8 closed most jobs under 20 lines
UPDATE_FILE = "UPDATE_REQUIRED"          # written when the site says this version is too old for new work: the app updates now
DRAIN_FILE = "STOP_AFTER_JOB"           # finish the job in hand, claim no more, exit (see Worker.drain_requested)


# ----------------------------------------------------------------------------------------------- exceptions
class ConfigError(Exception):
    pass


class ExitRun(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code


class Drained(Exception):
    """A "finish and leave" request arrived while the worker had nothing in hand: leave now."""


class Yield(Exception):
    """Another program holds the GPU lock: give the job back and wait."""


class Stop(Exception):
    """Ctrl+C or the STOP file."""


class LeaseLost(Exception):
    """404 / 409 / 410 from a job-scoped route: the job is no longer ours."""
    def __init__(self, status):
        super().__init__(f"lease lost (HTTP {status})")
        self.status = status


class Stalled(Exception):
    """The engine made no progress for STALL_SECONDS (raised from JobCtx.check while a run is being watched)."""
    def __init__(self, seconds):
        super().__init__(f"no progress for {seconds} s")
        self.seconds = seconds


class NetError(Exception):
    """Network failure or a transient server answer (5xx, 429): retried with backoff. retry_after (seconds) is set
    when the server said how long to wait."""
    def __init__(self, message="", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


_ERR_LINE = re.compile(r"^\s*(?:[\w.]+\.)?\w*(?:Error|Exception)\b:\s.*$")
# Home folders in every spelling a Windows, WSL, macOS or Linux error message can carry: C:\Users\x and C:/Users/x,
# /mnt/c/Users/x (WSL), /home/x, /Users/x, /root, and network shares (\\host\share, //host/share), which name a machine.
_HOME_PATHS = re.compile(
    r"(?i)(?:\\\\[^\\/\s'\"]+\\[^\\/\s'\"]+|//[^\\/\s'\"]+/[^\\/\s'\"]+|[a-z]:[\\/]users[\\/][^\\/\s'\"]+"
    r"|/mnt/[a-z]/users/[^\\/\s'\"]+|/home/[^\\/\s'\"]+|/users/[^\\/\s'\"]+|/root)(?=[\\/\s'\":]|$)")


def scrub_paths(text):
    """home-folder paths become ~ before an error line leaves the machine."""
    return _HOME_PATHS.sub("~", text)


def _machine_names():
    """This machine's host name (full and short) and the OS user name: they identify a volunteer, so they never go to
    the site. Names under 3 characters are left alone, they would match inside ordinary words."""
    names = set()
    try:
        host = socket.gethostname() or ""
        names.update({host, host.split(".")[0]})
    except Exception:
        pass
    for get in (getpass.getuser, lambda: os.environ.get("USERNAME", ""), lambda: os.environ.get("USER", "")):
        try:
            names.add(get() or "")
        except Exception:
            pass
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def safe_msg(text, names=None):
    """Text that is about to be sent to the site: every URL (presigned links are secrets, and no address is needed
    there), home-folder paths, the host name and the user name are replaced. The local log keeps its own rules."""
    s = _URL_RE.sub("<url>", str(text))
    s = scrub_paths(s)
    for n in (_machine_names() if names is None else names):
        s = re.sub(r"(?i)(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", "~", s)
    return s


def last_error_line(log_path, offset=0, max_bytes=65536):
    """The last exception-looking line written to log_path after offset, paths scrubbed, at most 160 chars; "" if none."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(offset, end - max_bytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if _ERR_LINE.match(line):
            return scrub_paths(line.strip())[:400]
    return ""


class JobFail(Exception):
    def __init__(self, reason, message, retryable, delete_dir):
        super().__init__(message)
        self.reason, self.message, self.retryable, self.delete_dir = reason, message, retryable, delete_dir


# ----------------------------------------------------------------------------------------------- small helpers
def utc_iso(ts=None):
    d = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_key(key):
    return "sswk_..." + key[-4:] if key else "(none)"


_URL_RE = re.compile(r"https?://\S+")
_HF_URL = re.compile(r"https://(?:www\.)?huggingface\.co(?:/|$)")


def describe_exc(e):
    """A short, URL-free description of an exception (presigned URLs are secrets too)."""
    return _URL_RE.sub("<url>", f"{type(e).__name__}: {e}")[:200]


def default_config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "SpeakStoneWorker"
    return Path.home() / ".config" / "speakstone-worker"


def secure_file(path):
    """User-only permissions where the OS supports it. Best effort, never fatal."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or getpass.getuser()
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def write_config(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    secure_file(tmp)
    os.replace(str(tmp), str(path))
    secure_file(path)


def atomic_write_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_jsonl(path):
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LOG_KEEP_DAYS = 14
REFS_LIMIT_BYTES = 1024 ** 3        # the downloaded reference clips are a cache; over this size the oldest-used go
REFS_TARGET_BYTES = 800 * 1024 ** 2
PART_MAX_AGE = 86400                # a half-finished download older than this is never going to be finished


def prune_logs(log_dir, now=None, days=LOG_KEEP_DAYS):
    """Delete worker_*.log files not touched for `days` days. -> how many. Never raises."""
    n = 0
    try:
        cutoff = (time.time() if now is None else now) - days * 86400
        for e in os.scandir(log_dir):
            try:
                if e.is_file() and re.fullmatch(r"worker_.*\.log", e.name) and e.stat().st_mtime < cutoff:
                    os.unlink(e.path)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def prune_refs(refs_dir, now=None, limit=REFS_LIMIT_BYTES, target=REFS_TARGET_BYTES):
    """Trim the reference-clip cache: remove *.part files older than a day, and when the rest is over `limit` bytes
    remove the least recently used (newest of access and modify time) until it is under `target`. Called only at
    start, before any job is claimed, so no job in hand needs a file that goes (a missing one is fetched again anyway).
    -> how many files were removed. Never raises."""
    removed = 0
    try:
        now = time.time() if now is None else now
        files = []
        for e in os.scandir(refs_dir):
            try:
                if not e.is_file():
                    continue
                st = e.stat()
                if e.name.endswith(".part"):
                    if now - st.st_mtime > PART_MAX_AGE:
                        os.unlink(e.path)
                        removed += 1
                    continue
                files.append((max(st.st_atime, st.st_mtime), st.st_size, e.path))
            except OSError:
                pass
        total = sum(f[1] for f in files)
        if total > limit:
            for _, size, path in sorted(files):
                if total <= target:
                    break
                try:
                    os.unlink(path)
                    total -= size
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def stall_seconds(env=None):
    """SSW_STALL_SECONDS, never below STALL_MIN_SECONDS (a typo must not kill a healthy model load)."""
    env = os.environ if env is None else env
    try:
        v = int(float(env.get("SSW_STALL_SECONDS") or STALL_SECONDS))
    except (TypeError, ValueError):
        v = STALL_SECONDS
    return max(STALL_MIN_SECONDS, v)


def kill_tree(proc):
    """Kill a subprocess and everything it started (Windows: taskkill /T, POSIX: its process group)."""
    if proc is None:
        return
    pid = proc.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def popen_tree_kwargs():
    """Popen kwargs that make kill_tree work: own process group / session."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def rmtree_retry(path):
    for attempt in range(6):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.3 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


# ----------------------------------------------------------------------------------------------- GPU (one function)
NULL_GPU = {"name": None, "vram_total_mib": None, "vram_free_mib": None, "gpu_mem_used_mib": None, "gpu_util": None}


def gpu_index(env=None):
    """Which graphics card this worker uses on a PC with several: SSW_GPU=<number as nvidia-smi lists them>, or None
    (the first card, as before). One worker drives one card, so a four-card machine runs four workers, each with its
    own number, key and work folder. Anything that is not a small whole number is ignored."""
    env = os.environ if env is None else env
    v = str(env.get("SSW_GPU") or "").strip()
    return int(v) if v.isdigit() and int(v) < 64 else None


def smi(query, env=None):
    """The nvidia-smi command for `query`, limited to this worker's own card when SSW_GPU names one. Without that, a
    worker on the second card read the FIRST card's free memory and waited for ever while its own card sat idle."""
    cmd = ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"]
    i = gpu_index(env)
    return cmd if i is None else cmd + ["-i", str(i)]


def query_gpu():
    """The only place nvidia-smi is called. Degrades to nulls when it is missing or unreadable."""
    try:
        r = subprocess.run(smi("name,memory.total,memory.free,memory.used,utilization.gpu"),
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return dict(NULL_GPU)
        parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
        if len(parts) < 5:
            return dict(NULL_GPU)

        def num(s):
            try:
                return int(float(s))
            except ValueError:
                return None
        return {"name": parts[0][:64] or None, "vram_total_mib": num(parts[1]), "vram_free_mib": num(parts[2]),
                "gpu_mem_used_mib": num(parts[3]), "gpu_util": num(parts[4])}
    except (OSError, subprocess.SubprocessError):
        return dict(NULL_GPU)


TELEMETRY_TTL = 30.0
_TELEMETRY_QUERY = "temperature.gpu,memory.used,memory.total,utilization.gpu,power.draw"
_telemetry_cache = {"t": None, "v": None}


def _bounded(s, lo, hi, as_float=False):
    try:
        x = float(str(s).strip())
    except (ValueError, TypeError):
        return None
    if x != x or x < lo or x > hi:
        return None
    return round(x, 1) if as_float else int(x)


def parse_telemetry(text):
    """One nvidia-smi csv line (first GPU) -> telemetry dict with only the keys that could be read; None when none."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return None
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) < 5:
        return None
    out = {"gpu_temp_c": _bounded(parts[0], 0, 120), "gpu_mem_used_mib": _bounded(parts[1], 0, 10 ** 7),
           "gpu_mem_total_mib": _bounded(parts[2], 0, 10 ** 7), "gpu_util_pct": _bounded(parts[3], 0, 100),
           "gpu_power_w": _bounded(parts[4], 0, 10 ** 5, as_float=True)}
    out = {k: v for k, v in out.items() if v is not None}
    return out or None


def read_telemetry(now=None, runner=subprocess.run, ttl=TELEMETRY_TTL):
    """GPU telemetry for the heartbeat. One nvidia-smi call, cached for ttl seconds (failures too),
    never raises, returns None when nothing could be read."""
    now = time.monotonic() if now is None else now
    if _telemetry_cache["t"] is not None and now - _telemetry_cache["t"] < ttl:
        return _telemetry_cache["v"]
    try:
        r = runner(smi(_TELEMETRY_QUERY), capture_output=True, text=True, timeout=5)
        v = parse_telemetry(r.stdout) if r.returncode == 0 else None
    except Exception:
        v = None
    _telemetry_cache["t"], _telemetry_cache["v"] = now, v
    return v


# ----------------------------------------------------------------------------------------------- config (8.1)
class Config:
    def __init__(self):
        self.path = None
        self.api_base = DEFAULT_API_BASE
        self.worker_key = ""
        self.work_dir = None
        self.machine_name = ""
        self.model_dir = ""
        self.python = sys.executable
        self.need_free_mib = 5200
        self.yield_lock = ""            # path of another program's GPU lock
        self.synth_script = ""          # test-only override of indextts2_worker.py (SSW_SYNTH_SCRIPT)
        self.engine_keep = True         # keep the engine loaded between jobs (SSW_ENGINE_KEEP=0: one process per job)


def load_config(path=None, env=None, require_key=True):
    env = os.environ if env is None else env
    cdir = default_config_dir()
    cpath = Path(path) if path else cdir / "config.json"
    data = {}
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read config file {cpath}: {type(e).__name__}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"config file {cpath} must hold a JSON object")
        secure_file(cpath)
    elif path:
        raise ConfigError(f"config file {cpath} not found")

    def pick(file_key, env_key, default=None):
        v = env.get(env_key) if env_key else None
        if v not in (None, ""):
            return v
        v = data.get(file_key)
        return default if v in (None, "") else v

    c = Config()
    c.path = cpath
    c.api_base = str(pick("api_base", "SSW_API_BASE", DEFAULT_API_BASE)).rstrip("/")
    c.worker_key = str(pick("worker_key", "SSW_WORKER_KEY", "") or "")
    c.vercel_bypass = str(pick("vercel_bypass", "SSW_VERCEL_BYPASS", "") or "").strip()
    c.yield_lock = str(pick("yield_lock", "SSW_YIELD_LOCK", "") or "").strip()
    c.work_dir = Path(str(pick("work_dir", "SSW_WORK_DIR", str(cdir / "work")))).expanduser().resolve()
    name = str(pick("machine_name", "SSW_MACHINE_NAME", "") or socket.gethostname().lower()).strip()
    c.machine_name = name[:64] or "worker"
    c.model_dir = str(pick("model_dir", "INDEXTTS2_DIR", "") or "")
    c.python = str(pick("python", "INDEXTTS2_PYTHON", sys.executable))
    c.synth_script = str(pick("synth_script", "SSW_SYNTH_SCRIPT", "") or "")
    c.engine_keep = str(pick("engine_keep", "SSW_ENGINE_KEEP", "1")).strip().lower() not in ("0", "false", "no", "off")
    try:
        c.need_free_mib = int(pick("need_free_mib", "SSW_NEED_FREE_MIB", 5200))
        if c.need_free_mib < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigError("need_free_mib must be a whole number of MiB (0 or more)") from None
    parts = urllib.parse.urlsplit(c.api_base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("api_base must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise ConfigError("api_base must be https (plain http is only allowed for localhost)")
    if require_key:
        if not c.worker_key:
            raise ConfigError("worker_key is required (config file or SSW_WORKER_KEY)")
        if not KEY_RE.match(c.worker_key):
            raise ConfigError("worker_key is not a valid sswk_ key (it must be sswk_ + 43 characters)")
    if not c.model_dir:
        raise ConfigError("model_dir is required (config file or INDEXTTS2_DIR)")
    return c


# ----------------------------------------------------------------------------------------------- model hash (8.2)
def list_model_files(model_dir):
    """[relpath, size, mtime_ns] for every regular file, skipping dot paths and __pycache__, sorted by UTF-8 bytes."""
    out = []
    root = str(model_dir)
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fn in files:
            if fn.startswith("."):
                continue
            full = os.path.join(cur, fn)
            try:
                if not os.path.isfile(full):
                    continue
                st = os.stat(full)
            except OSError:
                continue
            rel = Path(os.path.relpath(full, root)).as_posix()
            out.append([rel, st.st_size, st.st_mtime_ns])
    out.sort(key=lambda t: t[0].encode("utf-8"))
    return out


def compute_model_hash(model_dir, cache_path=None, hash_file=sha256_file, progress=None):
    files = list_model_files(model_dir)
    if cache_path:
        try:
            cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if cached.get("files") == files and re.match(r"^sha256:[0-9a-f]{64}$", str(cached.get("hash", ""))):
                return cached["hash"]
        except (OSError, ValueError, AttributeError):
            pass
    lines = []
    for i, (rel, size, _mt) in enumerate(files):
        if progress:
            progress(i, len(files), rel)
        lines.append(f"{hash_file(os.path.join(str(model_dir), *rel.split('/')))} {size} {rel}\n")
    h = "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    if cache_path:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(cache_path, {"files": files, "hash": h})
        except OSError:
            pass
    return h


def model_revision(model_dir):
    cfg = Path(model_dir) / "config.yaml"
    h = hashlib.sha256(cfg.read_bytes()).hexdigest()[:12] if cfg.exists() else "missing"
    return f"IndexTeam/IndexTTS-2@cfg-{h}"


# ----------------------------------------------------------------------------------------------- log
class Log:
    """One line per event: ISO time, event, job id, key=value counts. Key and line text never reach it."""

    def __init__(self, logs_dir, key="", echo=True):
        self.dir = Path(logs_dir)
        self.key = key
        self.echo = echo
        self.lock = threading.Lock()

    def _clean(self, s):
        s = str(s)
        if self.key:
            s = s.replace(self.key, mask_key(self.key))
        return _URL_RE.sub(lambda m: m.group(0) if _HF_URL.match(m.group(0)) else "<url>", s)

    def event(self, name, job=None, **kv):
        parts = [utc_iso(), name, f"job={job or '-'}"]
        for k, v in kv.items():
            if v is None:
                continue
            v = self._clean(v)
            if re.search(r"\s", v) or not v:
                v = json.dumps(v)
            parts.append(f"{k}={v}")
        line = " ".join(parts)
        with self.lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.dir / f"worker_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log", "a",
                          encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            if self.echo:
                print(line, flush=True)


# ----------------------------------------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _opener(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    handlers = [_NoRedirect()]
    if host in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


MAX_JSON_BYTES = 4 * 1024 * 1024      # a site JSON answer is never larger than this
REF_MARGIN_BYTES = 1024               # a reference may overrun its declared size by this much before we stop reading
UPLOAD_HEADERS = ("content-type", "content-length")


_transfer_api = {"host": "", "loopback": False}


def _host_is_local(host):
    """True for localhost names and for IP literals that point at this machine or its own network."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7")))


def _is_loopback(host):
    h = (host or "").strip("[]").lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def set_transfer_policy(api_base):
    """Called once when a Worker starts. Why: a transfer link comes from the site, and a hostile site could point it at
    the volunteer's own machine or network (2026-11-14 review). Until this is called the strict rule applies."""
    host = urllib.parse.urlsplit(str(api_base or "")).hostname or ""
    _transfer_api["host"] = host.lower()
    _transfer_api["loopback"] = _is_loopback(host)


def check_url(url):
    """Only https:// URLs (loopback http:// when the site address itself is loopback, for tests) may be opened. Any
    other scheme (file:, ftp:, data:, ...) is refused, and so is a link to this machine or its network unless the
    site address is that same host. -> url."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except (ValueError, AttributeError):
        raise NetError("refused: not a valid URL") from None
    if _transfer_api["loopback"] or host.lower() == _transfer_api["host"]:
        local_ok = True
    else:
        local_ok = not _host_is_local(host)
    if parts.scheme == "https" and host and local_ok:
        return url
    if parts.scheme == "http" and host in LOOPBACK and _transfer_api["loopback"]:
        return url
    if parts.scheme in ("http", "https") and host and not local_ok:
        raise NetError("refused: link points at this computer or its network")
    raise NetError(f"refused: URL scheme {parts.scheme[:12] or '?'!r} is not allowed")


def safe_upload_headers(headers):
    """Of the headers the site sends with an upload URL only Content-Type and Content-Length are used."""
    out = {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() in UPLOAD_HEADERS and isinstance(v, (str, int)):
                out[k] = str(v)
    return out


def _read_capped(r, limit):
    body = r.read(limit + 1)
    if len(body) > limit:
        raise NetError(f"response larger than {limit} bytes")
    return body


def http_request(method, url, headers=None, data=None, timeout=30, max_bytes=MAX_JSON_BYTES, resp_headers=None):
    """-> (status, body bytes). Raises NetError on connection problems, a refused URL or a body over max_bytes. Never follows redirects.
    resp_headers, when a dict is given, receives the answer's Retry-After header (the only one callers need)."""
    check_url(url)
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))

    def keep(h):
        if resp_headers is not None and h is not None:
            v = h.get("Retry-After")
            if v:
                resp_headers["retry-after"] = str(v)
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            keep(r.headers)
            return r.status, _read_capped(r, max_bytes)
    except urllib.error.HTTPError as e:
        keep(e.headers)
        try:
            body = _read_capped(e, max_bytes)
        except Exception:
            body = b""
        return e.code, body
    except NetError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise NetError(describe_exc(e)) from None
    except Exception as e:      # http.client errors
        raise NetError(describe_exc(e)) from None


def download_verified(url, dest_part, expected_bytes, expected_sha, timeout=60):
    """Stream a presigned GET to dest_part (no Authorization header). -> (bytes, sha256). Raises NetError.
    Never writes more than expected_bytes + REF_MARGIN_BYTES; a longer stream makes the returned count exceed expected_bytes."""
    check_url(url)
    req = urllib.request.Request(url, method="GET")
    h = hashlib.sha256()
    n = 0
    cap = int(expected_bytes) + REF_MARGIN_BYTES
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            try:
                declared = int(r.headers.get("Content-Length"))
            except (TypeError, ValueError):
                declared = None
            if declared is not None and declared > cap:
                return declared, ""                  # refused before reading a byte
            with open(dest_part, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > cap:                      # longer than declared: stop, the caller reports a mismatch
                        break
                    h.update(chunk)
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise NetError(describe_exc(e)) from None
    except Exception as e:
        raise NetError(describe_exc(e)) from None
    return n, h.hexdigest()


def _pid_alive(pid):
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                 text=True, timeout=20).stdout
        except Exception:
            return True
        return f'"{pid}"' in out
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def lock_held(path):
    """Another program's GPU lock: held if the file exists and its last token is a live PID, or it has
    no PID and is younger than 3 h. A lock whose text contains the word no_gpu is not held: its owner is busy
    with something that leaves the card alone (packing, uploading). Never created, held or deleted here."""
    try:
        p = Path(path)
        text, age = p.read_text(encoding="utf-8", errors="replace").split(), time.time() - p.stat().st_mtime
    except OSError:
        return False
    if "no_gpu" in text:                 # the holder says this stretch of its work does not use the card: carry on
        return False
    pid = text[-1] if text and text[-1].isdigit() else None
    return _pid_alive(pid) if pid else age < 3 * 3600


class InstanceLock:
    """One worker per config file (so one per key) on a PC. An exclusive, non-blocking OS lock on a file beside the
    config, held for the whole life of the process. The operating system drops it when the process ends, even in a
    crash, so it can never be left stuck. The first byte is the locked one; the pid is written after it for the message.
    (cli/speakstone_worker/locking.py reads the same lock from the app side: keep the two in step.)"""

    def __init__(self, path):
        self.path = Path(path)
        self.fh = None

    def acquire(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            open(self.path, "ab").close()
            fh = open(self.path, "r+b")
        except OSError:
            return True                             # cannot even open the file: do not stop the worker for that
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.write(b"L" + f"{os.getpid():>12}".encode() + b"\n")      # fixed width: nothing stale is left over
            fh.flush()
        except OSError:
            pass
        self.fh = fh
        return True

    def holder_pid(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(1)                                   # byte 0 is the locked one
                return int(f.read(20).split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def release(self):
        fh, self.fh = self.fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


class Api:
    def __init__(self, base, key, timeout=30, bypass=""):
        self.base, self.key, self.timeout = base, key, timeout
        self.bypass = bypass or ""

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.key}", "Accept": "application/json",
             "User-Agent": f"speakstone-worker/{WORKER_VERSION}"}
        if self.bypass:
            h["x-vercel-protection-bypass"] = self.bypass
        h.update(extra or {})
        return h

    @staticmethod
    def _parse(body):
        try:
            d = json.loads(body.decode("utf-8")) if body else {}
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def _finish(self, status, body, headers=None):
        if status == 401:
            raise ExitRun(EXIT_KEY, "key refused")
        if status == 426:
            raise ExitRun(EXIT_PROTOCOL, "protocol refused")
        if status == 429:
            raise NetError(f"HTTP {status}", retry_after=retry_after_seconds(headers, self._parse(body)))
        if status >= 500 or status == 408:
            raise NetError(f"HTTP {status}")
        return status, self._parse(body)

    def get_me(self, present=False):
        h = {}
        s, b = http_request("GET", self.base + "/api/worker/me" + ("?present=1" if present else ""),
                            self._headers({"X-Worker-Protocol": str(PROTOCOL)}), timeout=self.timeout, resp_headers=h)
        return self._finish(s, b, h)

    def post(self, route, body):
        body = dict(body)
        body["protocol"] = PROTOCOL
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        h = {}
        s, b = http_request("POST", f"{self.base}/api/worker/{route}",
                            self._headers({"Content-Type": "application/json"}), data=data, timeout=self.timeout,
                            resp_headers=h)
        return self._finish(s, b, h)


def retry_after_seconds(headers, data, now=None):
    """Seconds a 429 asks us to wait: the Retry-After header (seconds or an HTTP date), else the body's
    retry_after_seconds. None when neither is usable. Capped at RETRY_AFTER_CAP so a bad answer cannot park a worker."""
    v = None
    raw = (headers or {}).get("retry-after")
    if raw:
        try:
            v = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                v = when.timestamp() - (time.time() if now is None else now)
            except (TypeError, ValueError, IndexError):
                v = None
    if v is None and isinstance(data, dict):
        try:
            v = float(data.get("retry_after_seconds"))
        except (TypeError, ValueError):
            v = None
    if v is None or v != v:
        return None
    return max(0.0, min(v, float(RETRY_AFTER_CAP)))


def idle_wait(base, rnd=None):
    """Seconds to wait on an empty queue: base with +-IDLE_JITTER, at least 1 s and never over IDLE_CAP_SECONDS."""
    rnd = random.random if rnd is None else rnd
    try:
        base = float(base)
    except (TypeError, ValueError):
        base = IDLE_BASE_SECONDS
    w = max(1.0, base) * (1.0 + IDLE_JITTER * (2.0 * rnd() - 1.0))
    return round(max(1.0, min(w, float(IDLE_CAP_SECONDS))), 1)


class Backoff:
    """Network errors: 5, 10, 20 ... 300 s. Resets after any good answer."""
    def __init__(self):
        self.n = 0

    def next(self):
        d = min(BACKOFF_START * (2 ** self.n), BACKOFF_CAP)
        self.n += 1
        return d

    def ok(self):
        self.n = 0


# ----------------------------------------------------------------------------------------------- payload -> chunk (8.3 step 6)
def check_payload(p):
    """-> None if usable, else (reason, message). Recipe / version problems are unsupported_recipe (not retryable)."""
    if not isinstance(p, dict):
        return "bad_job", "payload is not an object"
    if p.get("v") != 1 or isinstance(p.get("v"), bool):
        return "unsupported_recipe", f"payload version {p.get('v')!r} is not supported"
    if p.get("recipe") != RECIPE:
        return "unsupported_recipe", f"recipe {str(p.get('recipe'))[:40]!r} is not supported"
    if p.get("engine") != ENGINE:
        return "unsupported_recipe", f"engine {str(p.get('engine'))[:40]!r} is not supported"
    try:
        br = (p.get("settings") or {}).get("bitrate")
        if not isinstance(br, str) or not BITRATE_RE.match(br):
            return "bad_job", "settings.bitrate is invalid"
        refs, lines = p.get("refs"), p.get("lines")
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REFS_PER_JOB or not isinstance(lines, list) or not lines:
            return "bad_job", "refs/lines missing"
        known = set()
        for r in refs:
            if (not isinstance(r, dict) or not HASH_RE.match(str(r.get("sha256", ""))) or r.get("ext") not in ("ogg", "wav")
                    or not isinstance(r.get("bytes"), int) or not 1 <= r["bytes"] <= MAX_REF_BYTES
                    or not isinstance(r.get("text", ""), str)):
                return "bad_job", "a reference entry is invalid"
            known.add(r["sha256"])
        for n, ln in enumerate(lines):
            if (not isinstance(ln, dict) or ln.get("i") != n or not isinstance(ln.get("text"), str) or not ln["text"]
                    or ln.get("ref") not in known or not isinstance(ln.get("seed"), int)
                    or not isinstance(ln.get("emo"), list) or len(ln["emo"]) != 8
                    or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ln["emo"])
                    or not isinstance(ln.get("post_af", ""), str)):
                return "bad_job", f"line {n} is invalid"
            why = _post_af_problem(ln.get("post_af", ""))
            if why:
                return "bad_job", f"line {n} post_af refused: {why}"
    except (TypeError, AttributeError):
        return "bad_job", "payload shape is invalid"
    return None


# post_af reaches `ffmpeg -af`, and a filter graph can read or write files
# (movie/amovie, file= options, ; and [labels]). Only a linear chain of these audio filters is allowed. The SAME rule lives in
# the site's payload check and this file (the worker must not trust the site); change all together.
POST_AF_MAX = 500
POST_AF_FILTERS = frozenset({"atempo", "rubberband", "bass", "treble", "aecho", "compand", "highpass", "equalizer"})
_POST_AF_FREQ_FILTERS = frozenset({"bass", "treble", "highpass", "equalizer"})   # where f= is a frequency, not a file
_POST_AF_DENIED_KEYS = ("file", "filename", "path", "url")
_POST_AF_RE = re.compile(r"[a-z]+(?:=[A-Za-z0-9._:=+|-]+)?(?:,[a-z]+(?:=[A-Za-z0-9._:=+|-]+)?)*")


def _post_af_problem(value):
    """None when value is an allowed post_af (empty is allowed), else a short reason."""
    if not isinstance(value, str):
        return "not a string"
    if value == "":
        return None
    if len(value) > POST_AF_MAX:
        return "too long"
    if _POST_AF_RE.fullmatch(value) is None:
        return "not a plain comma chain of name=args"
    for part in value.split(","):
        name, _, args = part.partition("=")
        if name not in POST_AF_FILTERS:
            return f"filter {name!r} is not allowed"
        for seg in args.split(":") if args else ():
            key = seg.partition("=")[0].lower() if "=" in seg else ""
            if key and (key in _POST_AF_DENIED_KEYS or key.startswith("file")):
                return f"option {key!r} is not allowed"
            if key == "f" and name not in _POST_AF_FREQ_FILTERS:
                return "option 'f' is not allowed here"
    return None


def build_chunk(p):
    """Rows for indextts2_worker.py --batch-file. Exactly the 12 keys of the spec's step 6 table."""
    refs = {r["sha256"]: r for r in p["refs"]}
    rows = []
    for ln in sorted(p["lines"], key=lambda x: x["i"]):
        r = refs[ln["ref"]]
        rows.append({
            "sound_file": f"L{ln['i']:03d}.ogg",
            "model_engine": p["engine"],
            "profile_key": "net_" + r["sha256"][:16],
            "canonical_reference_clip": f"{r['sha256']}.{r['ext']}",
            "ref_text": r.get("text", ""),
            "cleaned_text": ln["text"],
            "text_hash": hashlib.sha256(ln["text"].encode("utf-8")).hexdigest(),
            "generation_seed": ln["seed"],
            "emo_vector": ln["emo"],
            "line_emotion": "",
            "post_af": ln.get("post_af", ""),
            "npc_name": "",
        })
    return rows


# ----------------------------------------------------------------------------------------------- per-job context
class JobCtx:
    def __init__(self, job_id, lease_id, attempt, payload, jobdir):
        self.job_id, self.lease_id, self.attempt, self.payload = job_id, lease_id, attempt, payload
        self.dir = jobdir
        self.out = jobdir / "out"
        self.lost = threading.Event()
        self.lost_status = None
        self.fatal = None
        self.proc = None
        self.started_at = utc_iso()
        self.n = len(payload.get("lines", [])) if isinstance(payload, dict) and isinstance(payload.get("lines"), list) else 0
        self.worker = None
        self.grace_until = None      # set once every line is made: from then on only a lost lease may abort
        self.load_s = 0.0            # engine start to ready, summed over this job's runs (0 when a loaded one was reused)
        self.synth_s = 0.0           # time spent in run_synth, summed over this job's runs
        self.stall_limit = None      # seconds without progress that end a run; None = not watching (set by run_synth)
        self.stall_extra = None      # () -> anything that changes when the engine does something (line counter)
        self.clock = time.monotonic
        self._sig = None
        self._sig_at = self._progress_at = 0.0

    def stall_begin(self, limit, extra=None):
        """Start watching a run. The clock starts now: model loading counts as waiting, which the limit covers."""
        self.stall_limit, self.stall_extra, self._sig = limit, extra, None
        self._sig_at = 0.0
        self._progress_at = self.clock()

    def stall_end(self):
        self.stall_limit = None

    def progress_signature(self):
        """Changes whenever a clip appears, the engine log grows or the engine's marker/line counter moves."""
        try:
            log_size = (self.dir / "worker.log").stat().st_size
        except OSError:
            log_size = 0
        extra = None
        if self.stall_extra is not None:
            try:
                extra = self.stall_extra()
            except Exception:
                pass
        return (self.lines_done(), log_size, extra)

    def stalled_for(self):
        """Seconds since the last progress, or None when not watching or all is well. Looks at most every
        STALL_CHECK_SECONDS."""
        if self.stall_limit is None:
            return None
        now = self.clock()
        if self._sig is None or now - self._sig_at >= STALL_CHECK_SECONDS:
            self._sig_at = now
            sig = self.progress_signature()
            if sig != self._sig:
                self._sig, self._progress_at = sig, now
        idle = now - self._progress_at
        return int(idle) if idle >= self.stall_limit else None

    def start_grace(self, seconds=None):
        """Every line is made: a stop or a yield now waits for the upload and complete (bounded), because throwing the
        finished work away costs a whole job's GPU time while finishing costs seconds."""
        self.grace_until = time.monotonic() + (FINISH_GRACE_SECONDS if seconds is None else seconds)

    def in_grace(self):
        return self.grace_until is not None and time.monotonic() < self.grace_until

    def check(self):
        if self.fatal is not None:
            raise self.fatal
        if self.lost.is_set():
            raise LeaseLost(self.lost_status)
        if self.in_grace():
            # the job finishes, but a local program that wants the GPU still gets the idle engine's memory now
            w = self.worker
            if w is not None and w.engine_alive() and w.yield_busy():
                w.engine_shutdown("yield")
            return
        if self.worker is not None and self.worker.stop_requested():
            raise Stop()
        if self.worker is not None and self.worker.yield_busy():
            raise Yield()
        idle = self.stalled_for()
        if idle is not None:
            raise Stalled(idle)

    def lines_done(self):
        seen = set()
        for d in (self.out, self.out / HELD_DIR):
            try:
                for e in os.scandir(d):
                    m = re.match(r"^L(\d{3})\.ogg$", e.name)
                    if m and e.is_file():
                        seen.add(m.group(1))
            except OSError:
                pass
        return len(seen)


class Heartbeat(threading.Thread):
    def __init__(self, worker, ctx, interval):
        super().__init__(daemon=True, name="heartbeat")
        self.w, self.ctx, self.interval = worker, ctx, interval
        self.stop_evt = threading.Event()

    def run(self):
        while not self.stop_evt.wait(self.interval):
            self.beat()

    def beat(self):
        w, ctx = self.w, self.ctx
        try:
            body = {"job_id": ctx.job_id, "lease_id": ctx.lease_id, "lines_done": ctx.lines_done(),
                    "live": w.live_fields(ctx)}
            try:
                tel = read_telemetry()
                if tel:
                    body["telemetry"] = tel
            except Exception:
                pass                                   # telemetry must never break a heartbeat
            status, _ = w.api.post("heartbeat", body)
            if status in (404, 409, 410):
                ctx.lost_status = status
                ctx.lost.set()
                w.log.event("lease_lost", ctx.job_id, status=status, where="heartbeat")
        except ExitRun as e:
            ctx.fatal = e
        except NetError as e:
            w.log.event("backoff", ctx.job_id, where="heartbeat", error=str(e))
        except Exception as e:      # never let the thread die silently
            w.log.event("error", ctx.job_id, where="heartbeat", error=describe_exc(e))

    def stop(self):
        self.stop_evt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=35)


# ----------------------------------------------------------------------------------------------- engine kept loaded
class EngineHost:
    """One long-lived `indextts2_worker.py --serve` process, so the model (several GB) is loaded once for many jobs
    instead of once per job and once per re-run of held lines.

    The engine's stdout and stderr come back on one pipe. A reader thread copies every line into the log file of the
    job being made (the job's worker.log, which last_error_line() and log_has() read by offset, exactly as when the
    engine was a per-job process) and picks out two marker lines: READY once the model is loaded and
    "[JOB-DONE] rc=<n>" after each job. Any way of stopping a job kills the whole process tree and marks the host
    dead; the next job starts a fresh one."""

    def __init__(self, cmd, env_fn, cwd, max_jobs=ENGINE_MAX_JOBS):
        self.cmd, self.env_fn, self.cwd, self.max_jobs = list(cmd), env_fn, str(cwd), max_jobs
        self.proc = None
        self.jobs = 0
        self.last_used = time.monotonic()
        self.spawned_at = None
        self.lines_seen = 0          # every line the engine printed, markers included: the stall watchdog's pulse
        self._sink_lock = threading.Lock()
        self._sink = None
        self._ready = self._done = self._eof = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self):
        proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env_fn(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_tree_kwargs())
        ready, done, eof = threading.Event(), queue.Queue(), threading.Event()
        self.proc, self._ready, self._done, self._eof = proc, ready, done, eof
        self.jobs, self.spawned_at, self.last_used = 0, time.monotonic(), time.monotonic()
        threading.Thread(target=self._read, args=(proc, ready, done, eof), daemon=True, name="engine-reader").start()

    def _read(self, proc, ready, done, eof):
        # proc, ready, done and eof are this spawn's own: a reader left over from a killed process can never signal
        # (or write into the log of) the process that replaced it.
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                self.lines_seen += 1
                if text == ENGINE_READY_MARKER:
                    ready.set()
                    continue
                m = ENGINE_DONE_RE.match(text)
                if m:
                    done.put(int(m.group(1)))
                    continue
                with self._sink_lock:
                    if self._sink is not None and self.proc is proc:
                        try:
                            self._sink.write(raw)
                            self._sink.flush()
                        except (OSError, ValueError):
                            pass
        except (OSError, ValueError):
            pass
        finally:
            eof.set()

    def _ended(self, load_s):
        """The engine went away without a marker: wait briefly so its last words reach the log, then reap it."""
        self._eof.wait(5)
        rc = self.proc.poll() if self.proc is not None else None
        self.kill()
        return (rc if rc else 1), load_s

    def run_job(self, ctx, request, log_path, poll=0.25):
        """Send one batch request and wait for its marker, calling ctx.check() every poll seconds. -> (rc, load_s):
        load_s is the time from starting the engine to READY when this call started it, else 0. ctx.proc points at
        the engine while it runs, so every existing kill path reaches it."""
        load_s = 0.0
        with open(log_path, "ab") as lf:
            with self._sink_lock:
                self._sink = lf
            try:
                if not self.alive():
                    self.kill()
                    self._spawn()
                ctx.proc = self.proc
                if not self._ready.is_set():
                    while not self._ready.wait(poll):
                        if self._eof.is_set() or self.proc.poll() is not None:
                            return self._ended(time.monotonic() - self.spawned_at)
                        ctx.check()
                    load_s = time.monotonic() - self.spawned_at
                try:
                    self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    return self._ended(load_s)
                while True:
                    try:
                        rc = self._done.get(timeout=poll)
                        break
                    except queue.Empty:
                        pass
                    if self._eof.is_set():
                        try:
                            rc = self._done.get_nowait()       # the marker came just before the engine exited
                            break
                        except queue.Empty:
                            return self._ended(load_s)
                    ctx.check()
                self.jobs += 1
                self.last_used = time.monotonic()
                return rc, load_s
            except BaseException:
                self.kill()
                raise
            finally:
                with self._sink_lock:
                    self._sink = None
                ctx.proc = None

    def needs_recycle(self, now=None):
        now = time.monotonic() if now is None else now
        return self.alive() and (self.jobs >= self.max_jobs or now - self.last_used >= ENGINE_IDLE_SECONDS)

    def kill(self):
        """Kill the engine and everything it started (ffmpeg included) and mark the host dead."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            kill_tree(proc)
        else:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        for s in (proc.stdin, proc.stdout):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass

    def stop(self, timeout=10):
        """Polite stop between jobs: end of stdin makes the engine exit by itself; killed if it does not."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=timeout)
        except Exception:
            pass
        self.kill()


# ----------------------------------------------------------------------------------------------- the worker
class Worker:
    def __init__(self, cfg, log=None, gpu_query=query_gpu, skip_vram_wait=None, max_cycles=None,
                 install_signals=True):
        self.cfg = cfg
        set_transfer_policy(cfg.api_base)
        self.work = cfg.work_dir
        self.jobs_dir = self.work / "jobs"
        self.refs_dir = self.work / "refs"
        self.log = log or Log(self.work / "logs", cfg.worker_key)
        self.api = Api(cfg.api_base, cfg.worker_key, bypass=getattr(cfg, "vercel_bypass", ""))
        self.gpu_query = gpu_query
        self.skip_vram_wait = (os.environ.get("SSW_SKIP_VRAM_WAIT") == "1") if skip_vram_wait is None else skip_vram_wait
        self.max_cycles = max_cycles
        self.install_signals = install_signals
        self.stop_event = threading.Event()
        self.backoff = Backoff()
        self.idle_delay = IDLE_BASE_SECONDS
        self.sleep_impl = self._real_sleep       # tests replace this
        self.model_hash = ""
        self.revision = ""
        self.session_started = utc_iso()
        self.session_t0 = time.time()
        self.session_clips = 0
        self.session_audio = 0.0
        self.session_words = 0
        self._signals = 0
        self._yield_at = 0.0
        self._yield_last = False
        self.clock = time.monotonic  # tests replace this
        self._ping_at = None         # when the last presence call (or the start of this idle spell) was
        self._ping_fail_at = None
        self.engine = None           # EngineHost, created on the first job when engine_keep is on
        self.tier = None             # the key's tier from /api/worker/me

    # ---- engine kept loaded between jobs
    def engine_keep(self):
        return bool(getattr(self.cfg, "engine_keep", True))

    def engine_alive(self):
        eng = getattr(self, "engine", None)
        return eng is not None and eng.alive()

    def engine_shutdown(self, why, graceful=False):
        """Give the GPU memory back: on a yield, after ENGINE_MAX_JOBS runs, after ENGINE_IDLE_SECONDS with no job,
        and on exit."""
        eng = getattr(self, "engine", None)
        if eng is None or eng.proc is None:
            return
        was_alive = eng.alive()
        if graceful:
            eng.stop()
        else:
            eng.kill()
        if was_alive:
            self.log.event("engine", None, state="stopped", reason=why)

    def serve_command(self):
        script = self.cfg.synth_script or str(Path(__file__).resolve().with_name("indextts2_worker.py"))
        return [self.cfg.python, script, "--serve", "--ref-cache", os.path.abspath(str(self.work / "ref_wav_cache"))]

    def engine_host(self):
        if getattr(self, "engine", None) is None:
            self.engine = EngineHost(self.serve_command(), self.synth_env, os.path.abspath(str(self.work)))
        return self.engine

    # ---- stop / sleep
    def request_stop(self, why="stop"):
        self.stop_event.set()

    def _on_signal(self, signum, frame):
        self._signals += 1
        if self._signals > 1:               # second press: do not wait for anything
            ctx = getattr(self, "_current", None)
            if ctx is not None:
                kill_tree(ctx.proc)
            eng = getattr(self, "engine", None)
            if eng is not None and eng.proc is not None:
                kill_tree(eng.proc)         # a loaded engine between jobs is not ctx.proc
            os._exit(1)
        self.request_stop("signal")

    def yield_busy(self, force=False):
        """True while the configured GPU lock is held. Checked at most every YIELD_CHECK_SECONDS unless forced."""
        path = getattr(self.cfg, "yield_lock", "")
        if not path:
            return False
        now = time.time()
        if not force and now - self._yield_at < YIELD_CHECK_SECONDS:
            return self._yield_last
        self._yield_at, self._yield_last = now, lock_held(path)
        return self._yield_last

    def stop_requested(self):
        if self.stop_event.is_set():
            return True
        try:
            f = self.work / "STOP"
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
                self.stop_event.set()
                return True
        except OSError:
            pass
        return False

    def drain_requested(self):
        """STOP_AFTER_JOB in the work folder: finish the job in hand (make, upload, complete), claim nothing new, then
        exit cleanly. Unlike STOP it never gives a job back."""
        try:
            return (self.work / DRAIN_FILE).exists()
        except OSError:
            return False

    def clear_drain(self):
        try:
            (self.work / DRAIN_FILE).unlink()
        except OSError:
            pass

    def _stop_now(self, ctx=None):
        """A stop is wanted and may act now: not while a finished job is still uploading (ctx in its grace time)."""
        return self.stop_requested() and not (ctx is not None and ctx.in_grace())

    def _real_sleep(self, seconds, ctx=None):
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_now(ctx) or (ctx is not None and (ctx.lost.is_set() or ctx.fatal is not None)):
                return
            if ctx is None and self.drain_requested():      # idle or backing off: nothing in hand, so leave now
                return
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def sleep(self, seconds, ctx=None):
        self.sleep_impl(seconds, ctx)
        if self._stop_now(ctx):
            raise Stop()
        # A wait with nothing in hand ends at once when a drain is asked for (the pause, an update). It must also END
        # THE LOOP it sits in. Until 1.2.16-rc2 it only returned, so a worker waiting for graphics memory, for another
        # program's lock or for the network went round its loop about 20 times a second for as long as the request
        # stood: a full processor core and a log growing by megabytes (seen 2026-11-14: a pause from the tray while a
        # game held the card's memory).
        if ctx is None and self.drain_requested():
            raise Drained()
        if ctx is not None:
            ctx.check()

    def net_delay(self, e):
        """Backoff for a network error; at least what a 429 asked for (capped in retry_after_seconds)."""
        d = self.backoff.next()
        ra = getattr(e, "retry_after", None)
        return max(d, ra) if ra else d

    # ---- retry wrapper
    def call_retry(self, fn, ctx=None):
        while True:
            if self._stop_now(ctx):
                raise Stop()
            if ctx is not None:
                ctx.check()
            try:
                r = fn()
                self.backoff.ok()
                return r
            except NetError as e:
                d = self.net_delay(e)
                self.log.event("backoff", ctx.job_id if ctx else None, seconds=d, error=str(e))
                self.sleep(d, ctx)

    def post_job(self, route, body, ctx, retry=True):
        full = dict(body)
        full["job_id"], full["lease_id"] = ctx.job_id, ctx.lease_id

        def once():
            status, data = self.api.post(route, full)
            if status in (404, 409, 410):
                raise LeaseLost(status)
            return status, data
        if retry:
            return self.call_retry(once, ctx)
        return once()

    # ---- telemetry
    def live_fields(self, ctx):
        g = self.gpu_query() or {}
        hours = max((time.time() - self.session_t0) / 3600.0, 1e-9)
        clips = self.session_clips + (ctx.lines_done() if ctx else 0)
        live = {"gpu_util": g.get("gpu_util"), "gpu_mem_used_mib": g.get("gpu_mem_used_mib"),
                "vram_free_mib": g.get("vram_free_mib"), "session_started_at": self.session_started,
                "session_clips": clips, "session_audio_sec": round(self.session_audio, 1),
                "session_words": self.session_words,
                "words_per_hour": round(self.session_words / hours, 1),
                "audio_sec_per_hour": round(self.session_audio / hours, 1)}
        return {k: v for k, v in live.items() if v is not None and k in LIVE_KEYS}

    def claim_body(self, resume):
        g = self.gpu_query() or {}
        # a public-tier key belongs to an anonymous volunteer: their computer's name is not sent
        name = "worker" if getattr(self, "tier", None) == "public" else self.cfg.machine_name
        body = {"worker_version": WORKER_VERSION, "engines": [ENGINE], "recipes": [RECIPE],
                "model_hash": self.model_hash, "model_revision": self.revision[:80],
                "machine_name": name, "os": os_string(), "resume": resume}
        for k_body, k_g in (("gpu", "name"), ("vram_total_mib", "vram_total_mib"), ("vram_free_mib", "vram_free_mib"),
                            ("gpu_util", "gpu_util")):
            if g.get(k_g) is not None:
                body[k_body] = g[k_g]
        return body

    # ---- local job dirs
    def safe_job_dir(self, job_id):
        if not UUID_RE.match(str(job_id)):
            raise ValueError("job id is not a uuid")
        return self.jobs_dir / str(job_id)

    def delete_job_dir(self, job_id):
        try:
            d = self.safe_job_dir(job_id)
        except ValueError:
            return
        if d.exists():
            rmtree_retry(d)

    def scan_resume(self):
        ids = []
        if self.jobs_dir.is_dir():
            for d in sorted(self.jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not d.is_dir():
                    continue
                st = d / "state.json"
                ok = False
                try:
                    s = json.loads(st.read_text(encoding="utf-8"))
                    ok = isinstance(s, dict) and s.get("job_id") == d.name and UUID_RE.match(d.name)
                except (OSError, ValueError):
                    pass
                if ok:
                    ids.append(d.name)
                else:
                    rmtree_retry(d)
        # Only the newest MAX_RESUME are offered back to the site; older ones can never be resumed, so they go now
        # (they used to stay on disk for ever).
        for jid in ids[MAX_RESUME:]:
            self.delete_job_dir(jid)
        return ids[:MAX_RESUME]

    # ---- startup (8.3 step 1)
    def startup(self):
        mdir = Path(self.cfg.model_dir)
        if not mdir.is_dir() or not (mdir / "config.yaml").exists():
            raise ExitRun(EXIT_MODEL, f"model dir missing or incomplete: {mdir}")
        self.work.mkdir(parents=True, exist_ok=True)
        self.clear_drain()                      # a drain file left from an earlier run must not stop this one
        for d in (self.jobs_dir, self.refs_dir):
            d.mkdir(parents=True, exist_ok=True)
        prune_logs(self.log.dir)                # housekeeping, only here: before any job is claimed
        prune_refs(self.refs_dir)
        self.log.event("start", None, version=WORKER_VERSION, protocol=PROTOCOL, machine=self.cfg.machine_name,
                       key=mask_key(self.cfg.worker_key), api=self.cfg.api_base)
        self.model_hash = compute_model_hash(mdir, self.work / "model_hash.json")
        self.revision = model_revision(mdir)
        self.log.event("start", None, model_hash=self.model_hash, revision=self.revision)
        while True:
            try:
                status, data = self.api.get_me()
                break
            except NetError as e:
                d = self.net_delay(e)
                self.log.event("backoff", None, seconds=d, error=str(e), where="me")
                self.sleep(d)
        self.backoff.ok()
        w = data.get("worker") if isinstance(data, dict) else None
        if isinstance(w, dict):
            self.tier = w.get("tier") if isinstance(w.get("tier"), str) else None
            self.log.event("start", None, label=str(w.get("label", ""))[:40], tier=w.get("tier"), status=w.get("status"))

    def presence(self):
        """While idle for yield or vram the worker calls nothing, so the site would show the PC as offline. Once every
        PRESENCE_SECONDS, ask /api/worker/me?present=1 (the site counts that as 'seen'). Never raises, and never when a
        stop or a drain (the user's pause) is wanted."""
        now = self.clock()
        if self._ping_at is None:                  # first idle moment of this spell: the last real call was just now
            self._ping_at = now
            return
        if now - self._ping_at < PRESENCE_SECONDS or self.stop_requested() or self.drain_requested():
            return
        self._ping_at = now
        try:
            self.api.get_me(present=True)
        except BaseException as e:                 # any failure, even a refused key: the main loop deals with those
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if self._ping_fail_at is None or now - self._ping_fail_at >= PING_FAIL_LOG_SECONDS:
                self._ping_fail_at = now
                self.log.event("ping_failed", None, error=describe_exc(e))

    def wait_for_vram(self):
        try:
            self._wait_for_vram()
        finally:
            self._ping_at = None

    def _wait_for_vram(self):
        while self.yield_busy(force=True):
            self.engine_shutdown("yield")          # local GPU work gets the engine's memory back
            self.log.event("idle", None, reason="yield", wait=YIELD_POLL_SECONDS)
            self.presence()
            self.sleep(YIELD_POLL_SECONDS)
        need = self.cfg.need_free_mib
        if need <= 0 or self.skip_vram_wait:
            return
        if self.engine_alive():
            return      # our own loaded engine is what uses the memory: waiting for it to be free would never end
        while True:
            free = (self.gpu_query() or {}).get("vram_free_mib")
            if free is None or free >= need:       # no nvidia-smi: cannot know, go on
                return
            self.log.event("idle", None, reason="vram", free_mib=free, need_mib=need)
            self.presence()
            self.sleep(VRAM_POLL_SECONDS)

    # ---- main loop
    def lock_path(self):
        p = getattr(self.cfg, "path", None)
        return Path(str(p) + ".lock") if p else self.work / "worker.lock"

    def run(self):
        self.instance_lock = InstanceLock(self.lock_path())
        if not self.instance_lock.acquire():
            self.log.event("stop", None, reason="another worker is running with this key", code=EXIT_BUSY,
                           pid=self.instance_lock.holder_pid())
            print(BUSY_MESSAGE, file=sys.stderr)
            return EXIT_BUSY
        try:
            return self._run_locked()
        finally:
            self.instance_lock.release()

    def _run_locked(self):
        old = {}
        if self.install_signals and threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        old[sig] = signal.signal(sig, self._on_signal)
                    except (ValueError, OSError):
                        pass
        try:
            return self._run()
        finally:
            for sig, h in old.items():
                try:
                    signal.signal(sig, h)
                except (ValueError, OSError):
                    pass

    def _run(self):
        try:
            self.startup()
            cycles = 0
            while True:
                if self.stop_requested():
                    raise Stop()
                if self.drain_requested():
                    self.clear_drain()
                    self.log.event("stop", None, reason="drained")
                    return EXIT_OK
                if self.max_cycles is not None and cycles >= self.max_cycles:
                    self.log.event("stop", None, reason="max_cycles")
                    return EXIT_OK
                cycles += 1
                try:
                    self.cycle()
                except (Stop, ExitRun, Drained):
                    raise
                except Exception as e:
                    self.log.event("error", None, where="cycle", error=describe_exc(e))
                    self.sleep(self.backoff.next())
        except Stop:
            self.log.event("stop", None, reason="requested")
            return EXIT_OK
        except Drained:
            self.clear_drain()
            self.log.event("stop", None, reason="drained")
            return EXIT_OK
        except ExitRun as e:
            self.log.event("stop", None, reason=str(e) or "exit", code=e.code)
            return e.code
        finally:
            self.engine_shutdown("exit", graceful=True)

    def cycle(self):
        self.wait_for_vram()
        if self.drain_requested():              # asked while waiting: claim nothing, the loop exits
            return
        resume = self.scan_resume()
        try:
            status, data = self.call_retry(lambda: self._claim(resume))
        except LeaseLost:           # cannot happen on claim; keep the loop alive
            return
        job = data.get("job") if isinstance(data, dict) else None
        returned = job.get("id") if isinstance(job, dict) else None
        for jid in resume:
            if jid != returned:
                self.delete_job_dir(jid)
        if not isinstance(job, dict):
            reason = data.get("reason") or ("paused" if status == 403 else "none")
            hint = None
            if status == 200:
                try:
                    hint = float(data.get("retry_after_seconds"))
                    hint = hint if hint == hint and hint > 0 else None
                except (TypeError, ValueError):
                    hint = None
            if status != 200:
                base = IDLE_CAP_SECONDS
            elif hint is not None:
                base = hint                     # the site knows when work is due: use its figure
            else:
                base = self.idle_delay
                self.idle_delay = min(self.idle_delay * 2, IDLE_CAP_SECONDS)
            wait = idle_wait(base)
            if getattr(self, "engine", None) is not None and self.engine.needs_recycle():
                self.engine_shutdown("idle" if self.engine.jobs < self.engine.max_jobs else "max_jobs")
            if reason == "update_required":     # the site keeps new work from this version: tell the app to update now
                try:
                    (self.work / UPDATE_FILE).write_text(WORKER_VERSION, encoding="utf-8")
                except OSError:
                    pass
            self.log.event("idle", None, reason=reason, wait=wait)
            self.sleep(wait)
            return
        self.idle_delay = IDLE_BASE_SECONDS
        self.handle_job(job)

    def _claim(self, resume):
        status, data = self.api.post("claim", self.claim_body(resume))
        if status == 403:
            return status, {"job": None, "reason": "paused"}
        if status != 200:
            raise NetError(f"claim answered HTTP {status}")
        return status, data

    # ---- one job
    def handle_job(self, job):
        job_id, lease_id, payload = job.get("id"), job.get("lease_id"), job.get("payload")
        if not (UUID_RE.match(str(job_id)) and isinstance(lease_id, str) and lease_id and isinstance(payload, dict)):
            self.log.event("error", None, where="claim", error="malformed job answer")
            self.sleep(self.backoff.next())
            return
        attempt = job.get("attempt")
        try:
            jobdir = self.safe_job_dir(job_id)
            jobdir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(jobdir / "state.json", {"job_id": job_id, "lease_id": lease_id, "attempt": attempt,
                                                      "payload": payload})
        except Exception as e:
            self.setup_failed(job_id, lease_id, attempt, payload, e)
            return
        ctx = JobCtx(job_id, lease_id, attempt, payload, jobdir)
        ctx.worker = self
        self._current = ctx
        try:
            interval = float(job.get("heartbeat_every_seconds") or HEARTBEAT_SECONDS)
        except (TypeError, ValueError):
            interval = HEARTBEAT_SECONDS
        hb = Heartbeat(self, ctx, max(0.05, interval))
        self.log.event("resume" if job.get("resumed") else "claim", job_id, attempt=attempt, lines=ctx.n,
                       lease_until=job.get("lease_expires_at"))
        hb.start()
        after_wait = 0
        try:
            self._work(ctx, job)
        except LeaseLost as e:
            kill_tree(ctx.proc)
            self.log.event("lease_lost", job_id, status=e.status)
            self.delete_job_dir(job_id)
        except Stop:
            kill_tree(ctx.proc)
            self.release(ctx)
            raise
        except Yield:
            kill_tree(ctx.proc)
            self.engine_shutdown("yield")           # the local program wants the memory, not just the job
            self.release(ctx, "yielded to local GPU work")
            self.log.event("idle", job_id, reason="yield")
        except ExitRun:
            kill_tree(ctx.proc)
            self.engine_shutdown("exit")
            raise
        except JobFail as f:
            after_wait = self.after_fail(ctx, f)
        except Drained:
            raise
        except Exception as e:
            # Anything else used to escape: no fail was sent, the engine kept running and the job stayed leased with its
            # folder kept for a resume, so a job that always crashed the worker came back for ever (2026-11-14 review).
            # Only the type name is sent: never a traceback or a path.
            kill_tree(ctx.proc)
            self.log.event("error", job_id, where="job", error=type(e).__name__)
            after_wait = self.after_fail(ctx, JobFail("worker_error", f"unexpected {type(e).__name__} in the worker",
                                                      True, True))
        finally:
            hb.stop()
            self._current = None
        if after_wait:
            self.log.event("idle", None, reason="failed_job", wait=after_wait)
            self.sleep(after_wait)

    def after_fail(self, ctx, f):
        """Send a job's failure to the site and apply the brake. -> seconds to wait before the next job."""
        try:
            self.send_fail(ctx, f)
        except Stop:
            self.release(ctx)
            raise
        return self.brake(f)

    def brake(self, f):
        """Count a failed job and decide whether to wait or stop. -> seconds to wait before the next job. A failure that
        is not this PC's fault or is the user's choice (reason "released") counts for nothing."""
        if f.reason == "released":
            return 0
        self._failed_jobs = getattr(self, "_failed_jobs", 0) + 1
        none_made = bool(getattr(f, "none_made", False))
        self._dead_jobs = getattr(self, "_dead_jobs", 0) + 1 if none_made else 0
        if self._dead_jobs >= DEAD_JOBS_LIMIT or self._failed_jobs >= FAILED_JOBS_LIMIT:
            self.log.event("stop", None, reason="cannot make lines", code=EXIT_BROKEN, error=f.message)
            try:
                (self.work / BROKEN_FILE).write_text(f.message, encoding="utf-8")
            except OSError:
                pass
            self.engine_shutdown("broken")
            raise ExitRun(EXIT_BROKEN, "jobs keep failing on this PC: "
                          + ("not one line could be made" if self._dead_jobs >= DEAD_JOBS_LIMIT else "too many in a row"))
        wait = min(DEAD_JOB_WAIT_SECONDS * 2 ** (self._failed_jobs - 1), FAILED_JOB_WAIT_CAP_SECONDS)
        return max(wait, GPU_FATAL_WAIT_SECONDS if f.reason == "gpu_fatal" else 0)

    def setup_failed(self, job_id, lease_id, attempt, payload, exc):
        """The job folder could not be made or written (disk full, no permission) before the job even started. Give the
        job back as a retryable failure so it does not sit leased for its whole lease, and carry on: this must not
        crash the loop. One try only; a stop or exit still passes through."""
        self.log.event("error", job_id, where="job_setup", error=type(exc).__name__)
        ctx = JobCtx(job_id, lease_id, attempt, payload, self.jobs_dir / str(job_id))
        ctx.worker = self
        f = JobFail("worker_error", f"could not prepare the job folder ({type(exc).__name__})", True, True)
        try:
            self.log.event("fail", job_id, reason=f.reason, retryable=True, message=f.message)
            self.post_job("fail", {"reason": f.reason, "message": safe_msg(f.message)[:500], "retryable": True}, ctx,
                          retry=False)
        except (Stop, ExitRun):
            raise
        except Exception as e:
            self.log.event("fail", job_id, reason=f.reason, note=f"not delivered: {describe_exc(e)}")
        try:
            self.delete_job_dir(job_id)
        except Exception:
            pass
        self.sleep(max(self.brake(f), self.backoff.next()))

    def release(self, ctx, message="worker stopped"):
        """Ctrl+C / STOP: give the job back (attempt refunded). One try only: the user wants out."""
        try:
            self.post_job("fail", {"reason": "released", "message": safe_msg(message)[:500], "retryable": True}, ctx,
                          retry=False)
            self.log.event("fail", ctx.job_id, reason="released")
        except (LeaseLost, NetError, ExitRun) as e:
            self.log.event("fail", ctx.job_id, reason="released", note=f"not delivered: {describe_exc(e)}")

    def send_fail(self, ctx, f):
        self.log.event("fail", ctx.job_id, reason=f.reason, retryable=f.retryable, message=f.message[:200])
        try:
            # cleaned here, in the one place a failure leaves the machine, so no call site can forget it
            status, data = self.post_job("fail", {"reason": f.reason, "message": safe_msg(f.message)[:500],
                                                  "retryable": f.retryable}, ctx)
            if status == 400:
                self.log.event("error", ctx.job_id, where="fail", error=f"HTTP 400 {data.get('code', '')}")
        except LeaseLost as e:
            self.log.event("lease_lost", ctx.job_id, status=e.status, where="fail")
            self.delete_job_dir(ctx.job_id)
            return
        if f.delete_dir:
            self.delete_job_dir(ctx.job_id)

    def _work(self, ctx, job):
        bad = check_payload(ctx.payload)
        if bad:
            reason, msg = bad
            raise JobFail(reason, msg, False, True)
        self.ensure_refs(ctx, job.get("ref_urls") or {})
        chunk = build_chunk(ctx.payload)
        atomic_write_json(ctx.dir / "chunk.json", chunk)
        ctx.out.mkdir(parents=True, exist_ok=True)

        t_run = time.time()
        runs = 0
        last_err = ""
        while True:
            runs += 1
            self.log.event("run_start", ctx.job_id, run=runs, lines=ctx.n)
            log_path = ctx.dir / "worker.log"
            offset = log_path.stat().st_size if log_path.exists() else 0
            t_synth = time.time()
            rc = self.run_synth(ctx)
            ctx.synth_s += time.time() - t_synth
            st = self.scan(ctx)
            unsafe = self.log_has(log_path, offset, GPU_UNSAFE_MARKER)
            err = last_error_line(log_path, offset) if rc else ""
            if err:
                last_err = err
            extra = {"error": err} if err else {}
            self.log.event("run_end", ctx.job_id, run=runs, exit=rc,
                           ok=sum(1 for v in st.values() if v["kind"] == "ok"),
                           held=sum(1 for v in st.values() if v["kind"] == "held"),
                           missing=sum(1 for v in st.values() if v["kind"] == "missing"), **extra)
            todo = [i for i, v in st.items() if v["kind"] == "missing" or (v["kind"] == "held" and not v["final"])]
            if not todo:
                break
            if unsafe:
                raise JobFail("gpu_fatal", "GPU state is unsafe", True, False)
            if runs > MAX_RERUNS:
                break
            ctx.check()
        missing = [i for i, v in st.items() if v["kind"] == "missing"]
        if missing:
            msg = f"{len(missing)} of {ctx.n} lines were not made"
            if last_err:
                msg += f"; last error: {last_err}"
            fail = JobFail("line_failed", msg[:200], True, False)
            fail.none_made = len(missing) == ctx.n
            raise fail

        ctx.start_grace()      # every line is made: only a lost lease (or the grace running out) stops the delivery
        for upload_round in range(3):
            self.upload_all(ctx, st)
            ctx.check()
            manifest = self.build_manifest(ctx, st, t_run)
            status, data = self.post_job("complete", {"manifest": manifest}, ctx)
            if status == 200:
                break
            code = data.get("code", "")
            if status == 422 and code in ("object_missing", "object_size_mismatch") and upload_round < 2:
                self.log.event("upload", ctx.job_id, retry="complete answered " + code)
                continue
            if status == 422 and code in ("object_missing", "object_size_mismatch"):
                raise JobFail("upload_failed", f"complete refused: {code}", True, False)
            raise JobFail("other", f"complete answered HTTP {status} {code}"[:200], status == 422, status != 422)
        ok_n = sum(1 for v in st.values() if v["kind"] == "ok")
        held_n = sum(1 for v in st.values() if v["kind"] == "held")
        job_audio = sum(float(v.get("duration_sec") or 0) for v in st.values())
        job_words = sum(len(ln["text"].split()) for ln in ctx.payload["lines"])
        self.session_clips += ok_n + held_n
        self.session_audio += job_audio
        self.session_words += job_words
        # load_s / gen_s (added in 1.1.0, after the older fields so existing readers are unaffected): model load time
        # (0 when the kept engine was reused; not known in one-process-per-job mode, where it is inside gen_s)
        load_s = round(ctx.load_s, 1) if self.engine_keep() else None
        gen_s = round(max(0.0, ctx.synth_s - (ctx.load_s if self.engine_keep() else 0.0)), 1)
        self._dead_jobs = 0
        self._failed_jobs = 0
        try:
            (self.work / BROKEN_FILE).unlink()
        except OSError:
            pass
        self.log.event("complete", ctx.job_id, ok=ok_n, held=held_n, seconds=round(time.time() - t_run, 1),
                       already=data.get("already"), words=job_words, audio=round(job_audio), load_s=load_s,
                       gen_s=gen_s)
        self.delete_job_dir(ctx.job_id)

    # ---- refs (8.3 step 5)
    def ensure_refs(self, ctx, ref_urls):
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        for r in ctx.payload["refs"]:
            sha, ext, size = r["sha256"], r["ext"], r["bytes"]
            dest = self.refs_dir / f"{sha}.{ext}"
            if dest.is_file() and dest.stat().st_size == size:
                continue
            entry = ref_urls.get(sha)
            url = entry.get("url") if isinstance(entry, dict) else None
            if not url:
                raise JobFail("ref_download", "no download URL for a reference", True, True)
            try:
                check_url(url)
            except NetError as e:
                raise JobFail("ref_download", str(e)[:200], True, True) from None
            part = dest.with_name(dest.name + ".part")
            last = ""
            for attempt in range(1, 4):
                ctx.check()
                t0 = time.time()
                try:
                    n, got = download_verified(url, part, size, sha)
                except NetError as e:
                    last = str(e)
                    self.log.event("ref_fetch", ctx.job_id, ref=sha[:12], attempt=attempt, error=last)
                    if attempt < 3:
                        self.sleep(2 * attempt, ctx)
                    continue
                if n != size or got != sha:
                    part.unlink(missing_ok=True)
                    self.log.event("ref_fetch", ctx.job_id, ref=sha[:12], result="mismatch")
                    raise JobFail("ref_mismatch", "reference clip does not match its sha256", True, True)
                os.replace(str(part), str(dest))
                self.log.event("ref_fetch", ctx.job_id, ref=sha[:12], bytes=n, seconds=round(time.time() - t0, 1))
                break
            else:
                part.unlink(missing_ok=True)
                raise JobFail("ref_download", f"reference download failed: {last}"[:200], True, True)

    # ---- the subprocess (8.3 step 7)
    def synth_command(self, ctx):
        script = self.cfg.synth_script or str(Path(__file__).resolve().with_name("indextts2_worker.py"))
        return [self.cfg.python, script, "--batch-file", str(ctx.dir / "chunk.json"), "--output-dir", str(ctx.out),
                "--references", str(self.refs_dir), "--bitrate", ctx.payload["settings"]["bitrate"]]

    def synth_env(self):
        env = dict(os.environ)
        env["INDEXTTS2_DIR"] = str(self.cfg.model_dir)
        env["PYTHONUNBUFFERED"] = "1"
        if gpu_index(env) is not None:              # the engine sees only this worker's card (as its device 0)
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_index(env))
        # some cloud images turn hf_transfer on without installing it, which crashes
        # IndexTTS-2's first-load model fetch on every run. Plain downloads work everywhere.
        env["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        for k in OFFLINE_ENV:
            env.pop(k, None)
        # IDX2_USE_ACCEL is removed as well. The accel engine skips beam search and repetition_penalty, so a site job
        # would be made differently from what its manifest reports.
        for k in ("IDX2_NUM_BEAMS", "IDX2_BEAMS_BY_EMOTION", "IDX2_USE_ACCEL", "SSW_WORKER_KEY", "WORKER_KEY",
                  "SSW_VERCEL_BYPASS"):
            env.pop(k, None)
        key = self.cfg.worker_key
        if key:                                  # no variable of any name may carry the key (or a config path that holds it)
            for k in [k for k, v in env.items() if v and key in v]:
                env.pop(k, None)
        return env

    def run_synth(self, ctx):
        """Make the job's lines. -> the engine's exit code for this run (0 = all made or held)."""
        keep = self.engine_keep()
        ctx.stall_begin(stall_seconds(), (lambda: self.engine_host().lines_seen) if keep else None)
        try:
            return self.run_synth_kept(ctx) if keep else self.run_synth_once(ctx)
        except Stalled as e:
            # both run paths already killed the engine's whole process tree on the way out
            self.log.event("stall", ctx.job_id, seconds=e.seconds)
            self.engine_shutdown("stalled")
            raise JobFail("stalled", f"the engine made no progress for {e.seconds} s and was stopped", True, False) from None
        finally:
            ctx.stall_end()

    def engine_request(self, ctx):
        """One serve-mode request. Absolute paths: the engine runs in the work folder, not in the job's folder."""
        return {"batch_file": os.path.abspath(str(ctx.dir / "chunk.json")), "output_dir": os.path.abspath(str(ctx.out)),
                "references": os.path.abspath(str(self.refs_dir)), "bitrate": ctx.payload["settings"]["bitrate"]}

    def run_synth_kept(self, ctx):
        ctx.check()
        host = self.engine_host()
        if host.alive() and host.jobs >= host.max_jobs:
            self.engine_shutdown("max_jobs", graceful=True)   # a fresh process now and then: no slow leak builds up
        started = not host.alive()
        rc, load_s = host.run_job(ctx, self.engine_request(ctx), ctx.dir / "worker.log")
        ctx.load_s += load_s
        if started:
            self.log.event("engine", ctx.job_id, state="started", ready=host.alive(), load_s=round(load_s, 1))
        return rc

    def run_synth_once(self, ctx):
        """One engine process for this run only (SSW_ENGINE_KEEP=0): the behaviour before 1.1.0."""
        ctx.check()
        with open(ctx.dir / "worker.log", "ab") as lf:
            proc = subprocess.Popen(self.synth_command(ctx), cwd=str(ctx.dir), env=self.synth_env(),
                                    stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                    **popen_tree_kwargs())
            ctx.proc = proc
            try:
                while True:
                    try:
                        return proc.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                    ctx.check()
            except BaseException:
                kill_tree(proc)
                raise
            finally:
                ctx.proc = None

    @staticmethod
    def log_has(path, offset, needle):
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                return needle in f.read().decode("utf-8", "replace")
        except OSError:
            return False

    # ---- what the subprocess made (8.3 step 9)
    def scan(self, ctx):
        prov = {}
        for rec in read_jsonl(ctx.out / PROV_LOG):
            if rec.get("sound_file"):
                prov[rec["sound_file"]] = rec
        qc, qc_last = {}, {}
        for rec in read_jsonl(ctx.out / QC_LOG):
            sf = rec.get("sound_file")
            if sf:
                qc[sf] = qc.get(sf, 0) + 1
                qc_last[sf] = rec
        res = {}
        for i in range(ctx.n):
            name = f"L{i:03d}.ogg"
            f, hf = ctx.out / name, ctx.out / HELD_DIR / name
            if f.is_file() and name in prov:
                res[i] = {"kind": "ok", "path": f, "rec": prov[name], "final": True,
                          "duration_sec": prov[name].get("duration_sec")}
            elif hf.is_file():
                last = qc_last.get(name, {})
                res[i] = {"kind": "held", "path": hf, "rec": last, "final": qc.get(name, 0) >= QC_TRIES,
                          "duration_sec": last.get("duration_sec")}
            else:
                res[i] = {"kind": "missing", "path": None, "rec": {}, "final": False, "duration_sec": None}
        return res

    # ---- upload (8.3 step 10)
    def upload_all(self, ctx, st):
        files = []
        for i, v in st.items():
            size = v["path"].stat().st_size
            if not 0 < size <= MAX_RESULT_BYTES:
                raise JobFail("other", f"line {i} result size {size} is outside 1..{MAX_RESULT_BYTES}", True, False)
            v["bytes"], v["sha256"] = size, sha256_file(v["path"])
            files.append({"index": i, "bytes": v["bytes"], "sha256": v["sha256"]})
        urls = self.request_upload_urls(ctx, files)
        t0 = time.time()
        url_lock = threading.Lock()       # a 403 refresh replaces one entry; the other threads read theirs
        cancel = threading.Event()

        def one(i, v):
            # the same three tries, 403 refresh and ctx.check() between files as before; now UPLOAD_THREADS at a time
            if cancel.is_set():
                return
            ctx.check()
            with url_lock:
                entry = urls.get(i)
            for attempt in range(1, 4):
                ok = False
                try:
                    s, _ = http_request("PUT", entry["url"], headers=safe_upload_headers(entry.get("headers")),
                                        data=v["path"].read_bytes(), timeout=120)
                    ok = 200 <= s < 300
                    err = f"HTTP {s}"
                    refresh = s == 403
                except NetError as e:
                    err, refresh = str(e), False
                if ok:
                    break
                self.log.event("upload", ctx.job_id, index=i, attempt=attempt, error=err)
                if attempt == 3:
                    raise JobFail("upload_failed", f"upload of line {i} failed: {err}"[:200], True, False)
                if cancel.is_set():
                    return
                self.sleep(2 * attempt, ctx)
                if refresh:
                    with url_lock:
                        urls.update(self.request_upload_urls(ctx, [f for f in files if f["index"] == i]))
                        entry = urls.get(i)
            v["key"] = entry["key"]

        pool = ThreadPoolExecutor(max_workers=UPLOAD_THREADS, thread_name_prefix="upload")
        futs = [pool.submit(one, i, v) for i, v in st.items()]
        try:
            pending = set(futs)
            while pending:
                done, pending = futures_wait(pending, timeout=0.25, return_when=FIRST_EXCEPTION)
                for f in done:
                    f.result()                         # the first failure (JobFail, Stop, LeaseLost ...) ends the upload
                if pending:
                    ctx.check()
        except BaseException:
            cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        self.log.event("upload", ctx.job_id, files=len(files), bytes=sum(v["bytes"] for v in st.values()),
                       seconds=round(time.time() - t0, 1), threads=UPLOAD_THREADS)

    def request_upload_urls(self, ctx, files):
        status, data = self.post_job("upload-urls", {"files": files}, ctx)
        if status != 200 or not isinstance(data.get("urls"), list):
            raise JobFail("upload_failed", f"upload-urls answered HTTP {status}", True, False)
        out = {}
        for e in data["urls"]:
            if isinstance(e, dict) and isinstance(e.get("index"), int) and e.get("url") and e.get("key"):
                try:
                    check_url(e["url"])
                except NetError as ex:
                    raise JobFail("upload_failed", str(ex)[:200], True, False) from None
                out[e["index"]] = e
        for f in files:
            if f["index"] not in out:
                raise JobFail("upload_failed", "upload-urls did not return every file", True, False)
        return out

    # ---- manifest (5.4)
    def build_manifest(self, ctx, st, t_run):
        torch_v, lines = None, []
        for i in range(ctx.n):
            v, rec = st[i], st[i]["rec"]
            line = ctx.payload["lines"][i]
            if v["kind"] == "ok":
                torch_v = torch_v or rec.get("torch_version")
                lines.append({"i": i, "status": "ok", "key": v["key"], "bytes": v["bytes"], "sha256": v["sha256"],
                              "duration_sec": rec.get("duration_sec"), "seed": rec.get("generation_seed"),
                              "num_beams": rec.get("num_beams"), "emo": rec.get("emo_vector"),
                              "emo_alpha": rec.get("emo_alpha"), "measured_lufs": rec.get("measured_lufs"),
                              "level": _s200(rec.get("level")), "level_recipe": _s200(rec.get("level_recipe")),
                              "gen_time_sec": rec.get("gen_time_sec"), "rtf": rec.get("rtf"), "qc_why": None})
            else:
                lines.append({"i": i, "status": "held", "key": v["key"], "bytes": v["bytes"], "sha256": v["sha256"],
                              "duration_sec": rec.get("duration_sec"), "seed": rec.get("generation_seed"),
                              "num_beams": None, "emo": line.get("emo"), "emo_alpha": None, "measured_lufs": None,
                              "level": None, "level_recipe": None, "gen_time_sec": None, "rtf": None,
                              "qc_why": _s200(rec.get("why")) or "held by worker QC"})
        return {"v": 1, "worker_version": WORKER_VERSION, "model_hash": self.model_hash,
                "model_revision": self.revision, "torch_version": _s200(torch_v) or "unknown",
                "gpu": (self.gpu_query() or {}).get("name") or "unknown",
                "bitrate": ctx.payload["settings"]["bitrate"], "started_at": ctx.started_at,
                "finished_at": utc_iso(), "lines": lines}


def _s200(v):
    return None if v is None else str(v)[:200]


def os_string():
    s = platform.system().lower() or "unknown"
    v = platform.version() if s == "windows" else platform.release()
    return re.sub(r"[^A-Za-z0-9._+-]", "", f"{s}-{v}")[:64]


# ----------------------------------------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(prog="net_worker.py", description="SpeakStone worker-network client")
    ap.add_argument("command", nargs="?", choices=["run", "init"], help="run: claim and make jobs; init: write config")
    ap.add_argument("--config", help="config file (default: per-user location)")
    ap.add_argument("--print-model-hash", action="store_true", help="print the model hash and exit")
    ap.add_argument("--model-dir", help="init: IndexTTS-2 folder")
    ap.add_argument("--api-base", help="init: site URL")
    ap.add_argument("--work-dir", help="init: work folder")
    ap.add_argument("--machine-name", help="init: name shown to the owner")
    ap.add_argument("--ask-bypass", action="store_true",
                    help="init: also ask for a Vercel protection-bypass secret (preview deploys)")
    return ap


def cmd_init(args):
    path = Path(args.config) if args.config else default_config_dir() / "config.json"
    key = os.environ.get("SSW_WORKER_KEY") or getpass.getpass("Worker key (input hidden): ").strip()
    if not KEY_RE.match(key):
        print("That is not a valid worker key (sswk_ + 43 characters).", file=sys.stderr)
        return EXIT_CONFIG
    data = {"worker_key": key}
    bypass = os.environ.get("SSW_VERCEL_BYPASS")
    if bypass is None and getattr(args, "ask_bypass", False):
        bypass = getpass.getpass("Vercel protection-bypass secret (input hidden, blank for none): ")
    if bypass and bypass.strip():
        data["vercel_bypass"] = bypass.strip()
    for k, v in (("api_base", args.api_base), ("model_dir", args.model_dir), ("work_dir", args.work_dir),
                 ("machine_name", args.machine_name)):
        if v:
            data[k] = v
    write_config(path, data)
    print(f"Wrote {path} (key {mask_key(key)}, user-only permissions).")
    return EXIT_OK


def no_error_boxes():
    """Windows only: a crashing program must not open a modal error box nobody is there to click (the job would sit
    behind it for ever). Children inherit the setting. Never raises."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def trust_bundled_certificates(ssl_module=None, where=None):
    """Windows hands Python only the certificates it already has in its store, and fetches missing ones lazily for
    browsers but not for us: on some PCs every other connection to the release page failed with "unable to get issuer
    certificate", so the program could not update (seen 2026-11-13). Every HTTPS connection this process makes now
    trusts the system store AND the certificate list shipped with the program. Nothing is trusted less than before.
    -> True when the list was added. Never raises."""
    try:
        import ssl as _ssl
        ssl_module = ssl_module or _ssl
        if where is None:
            import certifi
            where = certifi.where()
        make = ssl_module.create_default_context

        def context(*a, **kw):
            ctx = make(*a, **kw)
            try:
                ctx.load_verify_locations(cafile=where)
            except Exception:
                pass
            return ctx
        ssl_module._create_default_https_context = context
        return True
    except Exception:
        return False


def main(argv=None, env=None):
    no_error_boxes()
    trust_bundled_certificates()
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if not args.command and not args.print_model_hash:
        build_parser().print_usage(sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_config(args.config, env, require_key=not args.print_model_hash)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.print_model_hash:
        if not Path(cfg.model_dir).is_dir():
            print(f"model dir missing: {cfg.model_dir}", file=sys.stderr)
            return EXIT_MODEL
        print("hashing the model files (cached afterwards)...", file=sys.stderr)
        print(compute_model_hash(cfg.model_dir, cfg.work_dir / "model_hash.json"))
        return EXIT_OK
    return Worker(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
