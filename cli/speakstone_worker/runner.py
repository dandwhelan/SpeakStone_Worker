"""Starts and stops `net_worker.py run` as a child process. Stopping is polite: the STOP file makes the worker hand the
current job back and exit; only if it ignores that for `grace` seconds is the process ended."""
import os
import subprocess
import sys
import time
from pathlib import Path

from . import locking
from . import paths


# ---------------------------------------------------------------- tie the worker's life to ours
_KEEP_JOB = []                              # the one job object, kept open for the life of the app
_KEEP = []                                  # callbacks that must live as long as the app


def create_kill_on_close_job(kernel32=None):
    """Windows: a Job Object that ends every process in it when its last handle closes (the app exiting however it
    exits, including the console's X). Returns the job handle, or None when it cannot be made."""
    import ctypes
    from ctypes import wintypes
    k = kernel32 or ctypes.windll.kernel32

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k.CreateJobObjectW.restype = wintypes.HANDLE
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))   # ExtendedLimitInformation
    return job if ok else None


def assign_to_job(job, pid, kernel32=None):
    import ctypes
    from ctypes import wintypes
    k = kernel32 or ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x0100 | 0x0001, False, pid)          # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not h:
        return False
    ok = bool(k.AssignProcessToJobObject(job, h))
    k.CloseHandle(h)
    return ok


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def pid_is_ours(pid, python_path):
    """True when `pid` is alive and is the install's own python (not another program that reused the number)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wintypes.HANDLE
            h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                k.GetExitCodeProcess(h, ctypes.byref(code))
                if code.value != 259:                       # STILL_ACTIVE
                    return False
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    return False
                exe = buf.value
            finally:
                k.CloseHandle(h)
        else:
            exe = os.readlink(f"/proc/{int(pid)}/exe")
        return _same_path(exe, python_path)
    except (OSError, ValueError):
        return False


def install_close_handlers(on_close):
    """Run `on_close()` when the console window is closed (X), the user logs off, the PC shuts down, or the app is
    told to end (SIGBREAK / SIGTERM / SIGHUP). `on_close` must return within a few seconds: Windows allows little."""
    import signal

    def sig(_n, _f):
        on_close()
        raise SystemExit(0)
    for name in ("SIGBREAK", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), sig)
            except (ValueError, OSError):
                pass
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(ev):
            if ev in CLOSE_EVENTS:
                try:
                    on_close()
                except Exception:
                    pass
                return True
            return False
        cb = proto(handler)
        _KEEP.append(cb)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)


CLOSE_EVENTS = (2, 5, 6)                    # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT


class _Adopted:
    """Stands in for a Popen of a worker started by an earlier copy of this app."""

    def __init__(self, pid, python_path):
        self.pid, self.python_path = pid, python_path

    def poll(self):
        return None if pid_is_ours(self.pid, self.python_path) else 0

    def terminate(self):
        kill_pid_tree(self.pid)


def kill_pid_tree(pid):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
        else:
            os.killpg(os.getpgid(pid), 9)
    except (OSError, ValueError):
        pass


DRAIN_FILE = "STOP_AFTER_JOB"      # the worker finishes the job in hand, claims nothing new, then exits (net_worker.py)


class WorkerRunner:
    def __init__(self, install, popen=subprocess.Popen, clock=time.time, nice=False, index=1):
        self.install = Path(install)
        self.index = index                  # 1 = the first worker; 2 = the second on the same card
        self.popen = popen
        self.clock = clock
        self.nice = nice                    # Linux: run at low priority (used when idle time cannot be read)
        self.proc = None
        self.stop_asked_at = 0.0

    @property
    def pid_file(self):
        return self.work_dir / "worker.pid"

    def find_existing(self):
        """pid of a worker still running from this install folder (an earlier copy of the app), else None."""
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if pid_is_ours(pid, self.python):
            return pid
        self._drop_pid_file()
        return None

    def _drop_pid_file(self):
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    def adopt(self, pid):
        self.proc = _Adopted(pid, self.python)
        self.stop_asked_at = 0.0

    def kill_tree(self):
        """Last resort on the way out: end the worker and everything it started."""
        if self.running and getattr(self.proc, "pid", None):
            kill_pid_tree(self.proc.pid)

    def shutdown_fast(self, wait=4.0, sleep=time.sleep):
        """For a closing console: hand the job back (STOP file), wait a few seconds, then end whatever is left."""
        self.request_stop()
        end = self.clock() + wait
        while self.running and self.clock() < end:
            sleep(0.2)
        self.kill_tree()

    @property
    def python(self):
        return self.install / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")

    @property
    def work_dir(self):
        return self.install / ("work" if self.index == 1 else f"work_{self.index}")

    @property
    def config_path(self):
        return self.install / ("worker_config.json" if self.index == 1 else f"worker_config_{self.index}.json")

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def exit_code(self):
        return None if self.proc is None else self.proc.poll()

    def command(self):
        return [str(self.python), str(self.install / "net_worker.py"), "run",
                "--config", str(self.config_path)]

    def environment(self, base=None):
        e = paths.clean_child_env(base)     # Linux: never hand the app's private libraries to the worker and ffmpeg
        e["PATH"] = str(self.install / "ffmpeg") + os.pathsep + e.get("PATH", "")
        e["INDEXTTS2_PYTHON"] = str(self.python)
        e["INDEXTTS2_DIR"] = str(self.install / "IndexTTS-2")
        e["HF_HOME"] = str(self.install / "hf")      # ONE cache for every worker, where setup pre-fetched the files
        e["HF_HUB_CACHE"] = str(self.install / "hf" / "hub")   # absolute: the engine's own default is relative to the job folder
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        e.pop("HF_HUB_OFFLINE", None)                  # never offline: a missing file is fetched, a cached one only checked
        e.pop("TRANSFORMERS_OFFLINE", None)
        e["PYTHONUNBUFFERED"] = "1"
        e.pop("SSW_WORKER_KEY", None)       # the key comes from the worker's own config file
        return e

    def start(self):
        if self.running:
            return False
        self.clear_stop()
        self.clear_drain()
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True          # own process group, so the whole tree can end with killpg
            if self.nice:
                kw["preexec_fn"] = lambda: os.nice(10)
        self.proc = self.popen(self.command(), env=self.environment(), cwd=str(self.install),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.stop_asked_at = 0.0
        pid = getattr(self.proc, "pid", None)
        if sys.platform == "win32" and pid:
            try:
                if not _KEEP_JOB:
                    _KEEP_JOB.append(create_kill_on_close_job())
                if _KEEP_JOB[0]:
                    assign_to_job(_KEEP_JOB[0], pid)
            except Exception:
                pass
        if pid:
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                self.pid_file.write_text(str(pid), encoding="utf-8")
            except OSError:
                pass
        return True

    @property
    def lock_path(self):
        return self.config_path.with_name(self.config_path.name + ".lock")

    def busy_elsewhere(self):
        """True when a worker with this config's key runs that this app did not start (another window, the service)."""
        return not self.running and locking.is_locked(self.lock_path)

    def drain_foreign(self):
        """Ask a worker we do not own to finish its job and leave (its STOP_AFTER_JOB file)."""
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / DRAIN_FILE).write_text("drain", encoding="utf-8")
        except OSError:
            pass

    def stop_foreign(self):
        """Ask a worker we do not own to stop: its STOP file (it hands its job back)."""
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "STOP").write_text("stop", encoding="utf-8")
        except OSError:
            pass

    def request_drain(self):
        """Ask the worker to finish its job and leave. Nothing is handed back and nothing is killed."""
        if not self.running:
            return
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / DRAIN_FILE).write_text("drain", encoding="utf-8")
        except OSError:
            pass

    def clear_drain(self):
        try:
            (self.work_dir / DRAIN_FILE).unlink()
        except OSError:
            pass

    def clear_stop(self):
        try:
            (self.work_dir / "STOP").unlink()
        except OSError:
            pass

    def request_stop(self):
        if not self.running or self.stop_asked_at:
            return
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "STOP").write_text("stop", encoding="utf-8")
        except OSError:
            pass
        self.stop_asked_at = self.clock()

    def poll_stop(self, grace=120):
        """True once the worker is gone. After `grace` seconds without leaving, ends it."""
        if not self.running:
            return True
        if self.stop_asked_at and self.clock() - self.stop_asked_at > grace:
            # terminate() on Windows ends only the worker and can leave its engine child holding graphics memory,
            # so end the whole tree. Fall back to terminate() when there is no pid to work with.
            pid = getattr(self.proc, "pid", None)
            if pid:
                kill_pid_tree(pid)
            else:
                try:
                    self.proc.terminate()
                except OSError:
                    pass
        return False

    def stop_and_wait(self, grace=120, sleep=time.sleep, on_wait=None):
        self.request_stop()
        while not self.poll_stop(grace):
            if on_wait:
                on_wait()
            sleep(0.5)
        self.clear_stop()


class WorkerGroup:
    """One or two workers on the same card, driven as one. Same surface as WorkerRunner for the code that uses it.
    Both are always known (so a leftover second worker is found and stopped after switching back to one);
    only the first `count` are started."""

    def __init__(self, install, count=1, **kw):
        self.runners = [WorkerRunner(install, index=i, **kw) for i in (1, 2)]
        self.count = 2 if count == 2 and self.runners[1].config_path.exists() else 1

    @property
    def active(self):
        return self.runners[:self.count]

    @property
    def running(self):
        return any(r.running for r in self.runners)

    @property
    def nice(self):
        return self.runners[0].nice

    @nice.setter
    def nice(self, v):
        for r in self.runners:
            r.nice = v

    @property
    def stop_asked_at(self):
        return self.runners[0].stop_asked_at

    @stop_asked_at.setter
    def stop_asked_at(self, v):
        for r in self.runners:
            r.stop_asked_at = v

    def start(self):
        return any([r.start() for r in self.active])

    def request_stop(self):
        for r in self.runners:
            r.request_stop()

    def busy_elsewhere(self):
        return any(r.busy_elsewhere() for r in self.active)

    def drain_foreign(self):
        for r in self.active:
            if r.busy_elsewhere():
                r.drain_foreign()

    def stop_foreign(self):
        for r in self.active:
            if r.busy_elsewhere():
                r.stop_foreign()

    def request_drain(self):
        for r in self.runners:
            r.request_drain()

    def clear_drain(self):
        for r in self.runners:
            r.clear_drain()

    def poll_stop(self, grace=120):
        return all([r.poll_stop(grace) for r in self.runners])

    def stop_and_wait(self, grace=120, sleep=time.sleep, on_wait=None):
        for r in self.runners:
            r.request_stop()
        while not self.poll_stop(grace):
            if on_wait:
                on_wait()
            sleep(0.5)
        for r in self.runners:
            r.clear_stop()
            r._drop_pid_file()
        time.sleep(2)

    def shutdown_fast(self, wait=4.0, sleep=time.sleep):
        for r in self.runners:
            r.request_stop()
        end = self.runners[0].clock() + wait
        while self.running and self.runners[0].clock() < end:
            sleep(0.2)
        for r in self.runners:
            r.kill_tree()

    def find_existing(self):
        """{index: pid} of workers left running by an earlier copy of the app."""
        found = {}
        for r in self.runners:
            pid = r.find_existing()
            if pid:
                found[r.index] = pid
        return found

    def adopt(self, found):
        for r in self.runners:
            if r.index in found:
                r.adopt(found[r.index])
