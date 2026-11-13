"""Menu, live board, settings and the hidden --background mode."""
import argparse
import ctypes
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live

from . import (APP_NAME, __version__, board, identity, locking, control as control_mod, enroll as enroll_mod, idle, paths, service, setup,
               stats, tray as tray_mod, uninstall, update)
from .keys import KeyReader, drain
from .runner import WorkerGroup, install_close_handlers
from .settings import Settings, in_pause_window, parse_hhmm


VT = {"on": True}                           # False when the console cannot do ANSI: no alternate screen then


def enable_vt(kernel32=None):
    """Windows: switch on ANSI escape handling for the console's output (and input). Classic conhost starts with it
    off, so clears and the alternate screen are silently ignored. True when output VT is on (always True elsewhere)."""
    if sys.platform != "win32" and kernel32 is None:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k = kernel32 or ctypes.windll.kernel32
        ok_out = False
        for std, flag in ((-11, 0x0004), (-10, 0x0200)):        # stdout: VT processing; stdin: VT input
            h = k.GetStdHandle(std)
            mode = wintypes.DWORD()
            if not h or not k.GetConsoleMode(h, ctypes.byref(mode)):
                continue
            ok = bool(k.SetConsoleMode(h, mode.value | flag))
            if std == -11:
                ok_out = ok
        return ok_out
    except Exception:
        return False


def fresh_screen(console):
    """Start a screen from nothing: stale keys dropped, a hard clear (cls on Windows, works with or without ANSI),
    then rich's own clear and the cursor back."""
    drain()
    if sys.platform == "win32":
        try:
            subprocess.run(["cmd", "/c", "cls"], check=False)  # fixed argument list, no user input
        except Exception:
            pass
    try:
        console.show_cursor(True)
    except Exception:
        pass
    console.clear()


def window_title(status=""):
    return f"{APP_NAME} {__version__}" + (f" - {status}" if status else "")


def set_title(text, platform=None, kernel32=None, write=None):
    """Console window title. Windows: the API (works with or without ANSI); elsewhere the terminal escape."""
    platform = platform or sys.platform
    try:
        if platform == "win32":
            import ctypes
            (kernel32 or ctypes.windll.kernel32).SetConsoleTitleW(text)
        else:
            (write or sys.stdout.write)(f"\x1b]0;{text}\x07")
    except Exception:
        pass


def output_is_console():
    """Windows: does this program's standard output really end at a console window? `isatty()` cannot tell: it is also
    True for the NUL device, which is exactly where a restart from 1.2.4 or older sends the output."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        mode = ctypes.c_ulong(0)
        return bool(k32.GetConsoleMode(k32.GetStdHandle(0xFFFFFFF5), ctypes.byref(mode)))
    except Exception:
        return True                                    # cannot tell: change nothing


def attach_console(platform=None, is_console=None, opener=open, set_handle=None):
    """Windows: when this program has a console window but its output goes nowhere, point output and input at the
    window. Versions up to 1.2.4 restart the program after an update with its output thrown away, so the first start
    of ANY newer version from one of those is an empty black window unless the new version repairs it itself.
    -> True when it re-attached. Never raises."""
    platform = platform or sys.platform
    if platform != "win32":
        return False
    if (is_console or output_is_console)():
        return False
    try:
        con_out = opener("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        con_in = opener("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        return False                                  # no console at all (a pipe, a service): leave things as they are
    sys.stdout = sys.stderr = con_out
    sys.stdin = con_in
    try:
        if set_handle is None:
            import msvcrt
            k32 = ctypes.windll.kernel32
            k32.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]

            def set_handle(which, stream):
                k32.SetStdHandle(which, msvcrt.get_osfhandle(stream.fileno()))
        set_handle(0xFFFFFFF5, con_out)               # STD_OUTPUT_HANDLE (-11)
        set_handle(0xFFFFFFF4, con_out)               # STD_ERROR_HANDLE (-12)
        set_handle(0xFFFFFFF6, con_in)                # STD_INPUT_HANDLE (-10)
    except Exception:
        pass
    return True


def make_console():
    attach_console()
    VT["on"] = enable_vt()                           # before the Console is made, so rich sees ANSI is available
    for stream in (sys.stdout, sys.stderr):          # piped/cp1252 consoles would choke on the block letters
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return Console(highlight=False)


def worker_version(install):
    try:
        m = re.search(r'WORKER_VERSION\s*=\s*"([^"]+)"', (install / "net_worker.py").read_text(encoding="utf-8"))
        return m.group(1) if m else "1.1.0"
    except OSError:
        return "1.1.0"


def worker_label(install):
    return identity.label_of(install)


def ask_site_for_label(install, wait=identity.TIMEOUT, opener=None, force=False):
    """Fill in the label from the site when it is unknown or a day old; never longer than `wait` seconds, silent."""
    kw = {"opener": opener} if opener else {}
    kw["force"] = force
    t = threading.Thread(target=lambda: identity.refresh(install, paths.api_base(), **kw), daemon=True)
    t.start()
    t.join(wait)


# ---------------------------------------------------------------- setup with a progress board
def run_setup_ui(console, install, base):
    steps, done, cur = list(setup.STEPS), set(setup.load_state(setup.Env(install, paths.resource_dir()))), [None]
    # Two steps can now run at once (setup.LANE_A / LANE_B), so each active step keeps its own bar, line and speed,
    # and the board shows the most recently updated one with the other's short status on the same line.
    ui_lock = threading.RLock()
    active = {}                                  # step -> {"frac", "line", "dl", "t0"}
    dl_by_thread = {}                            # a download's speed text, picked up by the progress call in the same thread
    live = Live(console=console, refresh_per_second=4, screen=VT["on"])     # alternate screen: nothing is left behind the menu
    lane_steps = set(setup.LANE_A) | set(setup.LANE_B)

    def redraw(error=None):
        with ui_lock:
            cur_view = active.get(cur[0]) or {"frac": None, "line": "", "dl": ""}
            labels = dict(setup.LABELS)
            others = []
            for s, v in active.items():
                if s == cur[0]:
                    continue
                labels[s] = labels.get(s, s) + "  (running)"
                pct = f" {round(v['frac'] * 100)}%" if v["frac"] is not None else ""
                others.append(setup.LABELS.get(s, s).split(" (")[0] + pct)
            line = cur_view["line"]
            if others:
                line = " / ".join(others) + "  |  " + line
            live.update(board.render_setup(steps, labels, done, cur[0], cur_view["frac"], line, cur_view["dl"], error))

    def progress(step, frac, msg):
        if step == "done":
            return
        with ui_lock:
            if step not in active:
                if step not in lane_steps:               # a step that runs alone ends everything before it
                    done.update(active)
                    active.clear()
                else:                                    # a lane step ends when its done-marker is saved
                    done.update(setup.load_state(setup.Env(install, paths.resource_dir())))
                    active.pop("hf_hub", None)
                    for s in [s for s in active if s in done]:
                        del active[s]
                active[step] = {"frac": None, "line": "", "dl": "", "t0": time.time()}
            v = active[step]
            cur[0] = step
            if frac is not None and step not in ("gpu",):
                v["frac"] = frac if step in ("uv", "index_tts", "ffmpeg", "model", "extras") else v["frac"]
            if step in lane_steps or step == "index_tts":
                v["dl"] = dl_by_thread.get(threading.get_ident(), v["dl"]) if frac is not None else v["dl"]
            if msg:
                v["line"] = msg
            redraw()

    def download(url, dest, cb):
        t0 = time.time()

        def inner(frac, got, total):
            if total:
                rate = got / max(0.5, time.time() - t0)
                dl_by_thread[threading.get_ident()] = f"{got / 1e6:.0f} of {total / 1e6:.0f} MB  {rate / 1e6:.1f} MB/s"
            cb(frac, got, total)
        setup.real_download(url, dest, inner)

    api = paths.api_base()
    host = socket.gethostname()

    def do_enroll(machine_name=None):
        gpu = stats.gpu_reading() or {}
        return enroll_mod.enroll(api, machine_name or host, gpu.get("name", "unknown"), gpu.get("mem_total_mib", 0),
                                 ("windows" if platform.system() == "Windows" else "linux"), worker_version(install))

    env = setup.Env(install, paths.resource_dir(), download=download, run=setup.real_run, enroll=do_enroll,
                    gpu_total_mib=stats.gpu_total_mib, progress=progress, api_base=(api if os.environ.get("SPEAKSTONE_API_BASE") else ""),
                    machine_name=host)
    with live:
        redraw()
        try:
            setup.run_setup(env)
        except (setup.SetupError, enroll_mod.EnrollError) as e:
            redraw(error=str(e))
            return str(e)
        done.update(steps)
        redraw()
        time.sleep(2)
    fresh_screen(console)
    return None


def ensure_setup(console, install):
    if setup.is_set_up(install) and not setup.extras_cached(install):
        setup.forget_extras(install)             # setup says done but the cache lost the files: fetch them once, with a bar
    if setup.is_set_up(install):
        return True
    err = run_setup_ui(console, install, os.environ)
    if err:
        console.print(f"\n[#e0962c]Setup stopped: {err}[/]\nRun this again to carry on from where it stopped.")
        return False
    return True


# ---------------------------------------------------------------- live board
class Collector:
    """Gathers board data; the slow queue call runs on its own thread."""

    def __init__(self, install, api, started):
        self.install, self.api, self.started = install, api, started
        self.queue = None
        self.gpu_cache = None
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            try:
                self.queue = stats.fetch_queue(self.api)
            except Exception:                  # keep the last answer and ask again in 30 s
                pass
            for _ in range(30):
                if self._stop:
                    return
                time.sleep(1)

    def close(self):
        self._stop = True


def board_state(install, queue, gpu, state, started, now=None, events=None, workers=1, update_version=None,
                label=""):
    now = now or time.time()
    if events is not None:
        s = stats.summarize(events, now)
    else:
        dirs = [install / "work" / "logs"] + ([install / "work_2" / "logs"] if workers == 2 else [])
        s = stats.merge_summaries([stats.summarize(stats.read_events(d), now) for d in dirs])
    s["workers"] = workers
    s["net"] = stats.download_figures(s)
    s["queue"], s["gpu"], s["state"], s["update"], s["label"] = queue, gpu, state, update_version, label
    s["time_left"] = stats.time_left(queue, s["words_hour"], s["lines_hour"])
    s["since_label"] = time.strftime("%H:%M", time.localtime(s["first_today"] or started))
    if s["making"]:
        s["job_pct"] = min(0.95, (now - s["job_started"]) / max(1.0, s["avg_job_seconds"]))
    if state == "paused":
        s["making"] = False
    return s


def start_now(console, install, settings, flow=None, keep=None, respect_pause=False):
    """The working board. `keep` holds the worker between the board and the menu, so [M] stops nothing and coming back
    does not start a second one. `respect_pause`: an active pause set from the tray shows the board paused."""
    keep = keep if keep is not None else {}
    if not ensure_setup(console, install):
        return "menu"
    fresh_screen(console)
    setup.sync_worker_files(install, paths.resource_dir())
    runner = keep.get("runner")
    reuse = runner is not None and runner.running
    if not reuse:
        runner = WorkerGroup(install, settings["workers"])
    old = None if reuse else runner.find_existing()
    if old:
        console.print(f"\nA worker from an earlier window is still running (process {', '.join(map(str, old.values()))}).")
        console.print(f"  [{board.GOLD}][S][/] Stop it and start fresh    [{board.GOLD}][A][/] Attach to it (show the board)")
        with KeyReader() as keys:
            k = keys.wait()
        runner.adopt(old)
        if k != "a":
            console.print("Giving its job back...")
            runner.stop_and_wait()
            runner = WorkerGroup(install, settings["workers"])
            old = None
    started, paused, paused_until, choosing = time.time(), False, None, False
    if respect_pause and not reuse and not old:
        paused, paused_until = pause_active(install, settings)
    ctl = control_mod.Control()
    if flow:
        flow.args = ["--start"]                      # an update while working restarts straight into making lines
    col = Collector(install, paths.api_base(), started)
    install_close_handlers(runner.shutdown_fast)
    watching = False
    if not old and not reuse and not paused and runner.busy_elsewhere():          # a worker with this key already runs (another window or the service)
        watching = True
    if not old and not watching and not reuse and not paused:
        runner.start()
    result = "menu"
    follow = {"looked": time.time()}
    try:
        with KeyReader() as keys, Live(console=console, screen=VT["on"], auto_refresh=False) as live:
            last_gpu, gpu, last_draw, last_view, last_size = 0, None, 0, None, console.size
            monitor = stats.GpuMonitor()
            while True:
                if time.time() - last_gpu > 2:
                    gpu, last_gpu = monitor.read(), time.time()
                state = "paused" if paused else ("stopped" if not runner.running and not watching else "idle")
                view = board_state(install, col.queue, gpu, state, started, workers=runner.count,
                                   update_version=flow.version if flow else None, label=worker_label(install))
                view["pause_choice"] = choosing
                view["watching"] = watching
                if state == "stopped" and _group_exit_code(runner) == 6:
                    view["stop_note"] = BUSY_SENTENCE
                view["paused_until"] = control_mod.until_label(paused_until) if paused and paused_until else None
                # Redraw only every 5 s or when something changed: a 4/s full-screen redraw flickered (owner 2026-11-11).
                resized = console.size != last_size
                if resized or (view != last_view and time.time() - last_draw >= 2) or time.time() - last_draw >= 5:
                    live.update(board.render_board(view), refresh=True)
                    last_draw, last_view, last_size = time.time(), view, console.size
                k = keys.poll(1.0)
                if not paused or not runner.running:
                    follow_installed_update(flow, runner, watching, follow)    # the service updated: follow it
                if flow and not paused and not watching and not follow.get("draining"):
                    update_step(flow, ctl, settings, runner)       # a swap restarts with --start and does not return
                if paused and paused_until and time.time() >= paused_until:      # a timed pause ends by itself
                    paused, paused_until = False, None
                    runner.start()
                if watching:
                    if k == "s":                                              # stop the OTHER worker through its STOP file
                        runner.stop_foreign()
                        console.print("Asked the other worker to stop; it hands its job back.")
                        result = "menu"
                        break
                    if k == "m":
                        break
                    k = None
                if choosing and k:
                    choosing = False
                    minutes = board.PAUSE_KEYS.get(k)
                    if minutes is not None:
                        paused, paused_until = True, (time.time() + minutes * 60 if minutes else None)
                        runner.request_stop()
                    k = None
                elif k == "p":
                    if paused:
                        paused, paused_until = False, None
                        runner.start()
                    else:
                        choosing = True                                       # next key picks how long
                elif k in ("s", "m"):
                    result = "exit" if k == "s" else "menu"
                    break
    except KeyboardInterrupt:
        result = "exit"
    finally:
        if flow:
            flow.args = []                         # back at the menu: an update restarts to the menu
        col.close()
        fresh_screen(console)                      # the board's screen is gone: land on a clean one
        if result == "menu":
            keep["runner"] = runner                # [M] stops nothing: the worker keeps making lines behind the menu
        elif runner.running:
            console.print("Giving the current job back before stopping...")
            runner.stop_and_wait()
    return result


# ---------------------------------------------------------------- settings
def settings_menu(console, install, st):
    ask_site_for_label(install, force=True)          # a rename on the site shows at once
    while True:
        fresh_screen(console)
        console.print(f"\n[{board.GOLD}]SETTINGS[/]")
        console.print(f"  [{board.GOLD}][1][/] Idle minutes before starting: [bold]{st['idle_minutes']}[/]")
        sched = f"{st['pause_from']} - {st['pause_to']}" if st["pause_from"] and st["pause_to"] else "none"
        console.print(f"  [{board.GOLD}][2][/] Daily pause (no work in this window): [bold]{sched}[/]")
        console.print(f"  [{board.GOLD}][3][/] Uninstall the service")
        console.print(f"  [{board.GOLD}][4][/] Worker label: [bold]{identity.describe(install)}[/]")
        console.print(f"  [{board.GOLD}][5][/] Workers on this card: [bold]{st['workers']}[/]")
        console.print(f"  [{board.GOLD}][6][/] Remove SpeakStone Worker from this PC (deletes every downloaded file)")
        console.print(f"  [{board.GOLD}][7][/] Update automatically: [bold]{'on' if st['auto_update'] else 'off'}[/]")
        console.print(f"  [{board.GOLD}][8][/] Start making lines when the app opens: "
                      f"[bold]{'on' if st['start_on_open'] else 'off'}[/]")
        console.print(f"  [{board.GOLD}][B][/] Back")
        with KeyReader() as keys:
            k = keys.wait()
        if k == "8":
            st["start_on_open"] = not st["start_on_open"]
        elif k == "7":
            st["auto_update"] = not st["auto_update"]
        elif k == "6":
            if remove_program(console, install):
                return "exit"
        elif k == "1":
            console.show_cursor(True)
            v = input("Minutes of no keyboard or mouse use before starting (1-240): ").strip()
            if v.isdigit() and 1 <= int(v) <= 240:
                st["idle_minutes"] = int(v)
        elif k == "2":
            console.show_cursor(True)
            v = input("Pause window as HH:MM-HH:MM (for example 18:00-23:00), blank for none: ").strip()
            m = re.match(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$", v)
            if m and parse_hhmm(m.group(1)) is not None and parse_hhmm(m.group(2)) is not None:
                st["pause_from"], st["pause_to"] = m.group(1), m.group(2)
            elif not v:
                st["pause_from"] = st["pause_to"] = ""
        elif k == "3":
            console.print("Service removed." if service.uninstall() else "No service was installed.")
            time.sleep(1.5)
        elif k == "4":
            console.print(rename_flow(console, install))
            time.sleep(2.5)
        elif k == "5":
            console.print(set_workers(console, install, st))
            time.sleep(2)
        elif k in ("b", "q", "\x1b"):
            return


def rename_flow(console, install, ask=input, renamer=identity.rename):
    """Settings > 4: show the label, ask for a new one, send it to the site. Returns the sentence to show."""
    console.print(f"Worker label: {identity.describe(install)}")
    console.show_cursor(True)
    text = ask("New name (Enter to keep): ").strip()
    if not text:
        return "Kept the current name."
    name, why = identity.check_name(text)
    if not name:
        return why
    ok, msg = renamer(install, paths.api_base(), name)
    return msg


def stop_all_workers(install, wait=8.0):
    group = WorkerGroup(install, 2)
    found = group.find_existing()
    if found:
        group.adopt(found)
        group.shutdown_fast(wait=wait)


def remove_program(console, install, ask=input, remover=uninstall.remove_everything, sleep=time.sleep):
    """Settings > 6. True when the user went ahead (the app then exits)."""
    gb = uninstall.folder_bytes(install) / 1e9
    console.print(f"\nThis stops the workers, removes the service and deletes [bold]{install}[/] ({gb:.1f} GB):")
    console.print("Python, the voice engine, the model files, download caches, logs, settings and this PC's worker key.")
    console.print("Running the app again later downloads everything again.")
    console.show_cursor(True)
    if ask("Type DELETE to go ahead, anything else to cancel: ").strip() != "DELETE":
        console.print("Nothing was deleted.")
        sleep(1.5)
        return False
    console.print("Deleting...")
    r = remover(install, stop_workers=lambda: stop_all_workers(install), remove_service=service.uninstall)
    if r["ok"]:
        console.print(f"Done. {r['freed'] / 1e9:.1f} GB freed. You can now delete the SpeakStoneWorker program file itself.")
    else:
        console.print(f"Some files could not be deleted ({r['freed'] / 1e9:.1f} GB freed). Close other programs "
                      f"using them, then delete this folder by hand: {r['left']}")
    ask("Press Enter to close.")
    return True


def set_workers(console, install, st, gpu_reader=None, enroll_runner=None):
    """Flip between 1 and 2 workers on this card. Returns the sentence to show. 2 needs room on the card at this
    moment and a second key (kept for next time, even after switching back to 1)."""
    if st["workers"] == 2:
        st["workers"] = 1
        return "One worker on this card. The second one stops the next time you start."
    ok, why = setup.second_worker_allowed((gpu_reader or stats.gpu_reading)())
    if not ok:
        return why
    if not setup.is_set_up(install):
        return "Finish the first-time setup first (choose Start now)."
    try:
        (enroll_runner or enroll_second_worker)(install)
    except (setup.SetupError, enroll_mod.EnrollError) as e:
        return str(e)
    st["workers"] = 2
    return "Two workers on this card. It takes effect the next time you start."


def enroll_second_worker(install):
    api, host = paths.api_base(), socket.gethostname()

    def do_enroll(machine_name=None):
        gpu = stats.gpu_reading() or {}
        return enroll_mod.enroll(api, machine_name or host, gpu.get("name", "unknown"), gpu.get("mem_total_mib", 0),
                                 ("windows" if platform.system() == "Windows" else "linux"), worker_version(install))
    env = setup.Env(install, paths.resource_dir(), run=setup.real_run, enroll=do_enroll,
                    api_base=(api if os.environ.get("SPEAKSTONE_API_BASE") else ""), machine_name=host)
    setup.enroll_second(env)


# ---------------------------------------------------------------- background mode
def hide_console():
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.GetConsoleWindow()
            if h:
                ctypes.windll.user32.ShowWindow(h, 0)
        except Exception:
            pass


# Exit codes the worker uses when restarting cannot help (key refused and the like): wait long, then try once more.
NO_RESTART_CODES = (3, 4, 5, 6)
NO_RESTART_WAIT = 30 * 60
CRASH_BACKOFF_FIRST = 30
CRASH_BACKOFF_MAX = 10 * 60


BUSY_SENTENCE = "Another SpeakStone Worker is already running with this key on this PC."


class RestartGate:
    """Decides when a worker that exited may be started again, so a refused key is not retried every few seconds.
    The worker's own exit code is the only input; `clock` is injected for tests."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.until = 0.0
        self.crashes = 0
        self.seen_running = False

    def blocked(self):
        return self.clock() < self.until

    def observe(self, running, exit_code):
        """Call once per tick with whether the worker runs and, when it does not, how its process ended."""
        if running:
            self.seen_running = True
            return
        if not self.seen_running:
            return
        self.seen_running = False
        if exit_code in (None, 0):
            self.crashes = 0
            return
        if exit_code in NO_RESTART_CODES:
            self.until = self.clock() + NO_RESTART_WAIT
            return
        self.until = self.clock() + min(CRASH_BACKOFF_MAX, CRASH_BACKOFF_FIRST * 2 ** self.crashes)
        self.crashes += 1


def _group_exit_code(runner):
    """First non-zero exit code among the workers that have ended, else 0 (None if there is nothing to read)."""
    rs = getattr(runner, "runners", None) or [runner]
    codes = [getattr(r, "exit_code", None) for r in rs]
    bad = [c for c in codes if c not in (None, 0)]
    return bad[0] if bad else (0 if any(c == 0 for c in codes) else None)


def log_background_error(install, text):
    """One line in work/logs/background.log. Best effort: logging must never be the thing that stops the service."""
    try:
        d = Path(install) / "work" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "background.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


BACKGROUND_LOG_MAX = 1_000_000      # above this the service log is cut down once, when the service starts
BACKGROUND_LOG_KEEP = 200_000


def trim_background_log(path, max_bytes=BACKGROUND_LOG_MAX, keep=BACKGROUND_LOG_KEEP):
    """background.log only ever grew. When it is over max_bytes keep just its last `keep` bytes (from a line start).
    Rewritten in place once per service start, never while it is being appended to. Never raises."""
    try:
        path = Path(path)
        if path.stat().st_size <= max_bytes:
            return False
        with open(path, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        tail = tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(tail)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def safe_check(flow, install):
    """flow.check() for a thread: whatever goes wrong becomes one line in background.log, never a dead thread."""
    try:
        flow.check()
    except Exception as e:
        log_background_error(install, f"update check failed: {type(e).__name__}: {e}")


def background_tick(runner, st, now=None, readers=None, gate=None, control=None):
    """One decision. Returns the action taken. Pure apart from starting/stopping the worker."""
    r = readers or {}
    idle_s = (r.get("idle_seconds") or idle.idle_seconds)()
    t = time.localtime(now or time.time())
    paused = in_pause_window(t.tm_hour * 60 + t.tm_min, st["pause_from"], st["pause_to"])
    if control is not None and control.paused:
        # the tray's Pause: finish the job in hand, take no new one (nothing is handed back)
        foreign = not runner.running and _busy(runner)       # a worker started in the app window: pause it too
        if (runner.running or foreign) and control.drain_reason is None:
            if runner.running:
                runner.request_drain()
            else:
                runner.drain_foreign()
            control.drain_reason = "pause"
        runner.poll_stop()
        return "drain" if (runner.running or foreign) else "keep"
    if control is not None and control.drain_reason == "pause":      # Resume: cancel a drain that has not finished yet
        runner.clear_drain()
        control.drain_reason = None
    mode = "idle"
    if idle_s is None:                         # no idle time available: always on, at low priority
        mode, idle_s, runner.nice = "now", 0, True
    fullscreen, battery = (r.get("fullscreen") or idle.fullscreen_busy)(), (r.get("battery") or idle.on_battery)()
    action = idle.decide(mode, runner.running, idle_s, st["idle_minutes"], fullscreen, battery, paused,
                         st["block_fullscreen"], st["block_on_battery"])
    if control is not None and control.force_start_until:
        # it was working when an update restarted the program: carry on at once, even if the PC is not idle by the
        # rule. The pause window, full-screen and battery blocks still win.
        if runner.running or time.time() > control.force_start_until:
            control.force_start_until = 0.0
        elif action == "keep" and not paused and not (st["block_fullscreen"] and fullscreen) \
                and not (st["block_on_battery"] and battery):
            action = "start"
    if gate is not None:
        # a stop we asked for (idle ended, pause) is not a crash, whatever code a killed process leaves
        gate.observe(runner.running, 0 if runner.stop_asked_at or runner.running else _group_exit_code(runner))
        if action == "start" and gate.blocked():
            action = "wait"
    if action == "start":
        busy = getattr(runner, "busy_elsewhere", None)
        if busy is not None and busy():
            action = "wait"                    # a worker with this key already runs (another window): never start a second
    if action == "start" and control is not None and control.drain_reason == "update":
        action = "wait"                        # the worker left so the update can go in: do not start another
    if action == "start":
        runner.start()
        if control is not None:
            control.force_start_until = 0.0
        if gate is not None:
            gate.seen_running = True           # a refused key exits within seconds, before the next tick sees it run
    elif action == "stop":
        runner.request_stop()
    runner.poll_stop()
    if not runner.running:
        runner.stop_asked_at = 0.0
    return action


def _busy(runner):
    f = getattr(runner, "busy_elsewhere", None)
    return bool(f and f())


RESUME_MARKER = "update_resume.json"
RESUME_VALID_SECONDS = 5 * 60
PAUSE_FLAG = "paused_by_tray.json"


def write_resume_marker(install, now=None):
    """Just before an update swap that followed a drain: 'it was working', so the new process starts a worker at once."""
    try:
        Path(install).mkdir(parents=True, exist_ok=True)
        (Path(install) / RESUME_MARKER).write_text(json.dumps({"at": now or time.time()}), encoding="utf-8")
    except OSError:
        pass


def consume_resume_marker(install, now=None):
    """True when a marker written at most 5 minutes ago exists. The file is removed either way."""
    p = Path(install) / RESUME_MARKER
    try:
        at = float(json.loads(p.read_text(encoding="utf-8")).get("at", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        at = 0.0
    try:
        p.unlink()
    except OSError:
        pass
    return bool(at) and 0 <= (now or time.time()) - at <= RESUME_VALID_SECONDS


def clear_resume_marker(install):
    try:
        (Path(install) / RESUME_MARKER).unlink()
    except OSError:
        pass


def write_pause_flag(install, pid=None):
    try:
        (Path(install) / PAUSE_FLAG).write_text(json.dumps({"pid": pid or os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def clear_pause_flag(install):
    try:
        (Path(install) / PAUSE_FLAG).unlink()
    except OSError:
        pass


def pause_active(install, st, now=None, alive=locking.pid_alive):
    """(active, until) for a pause set from the tray: a timed one still running (saved in the settings) or an 'until I
    resume' one whose service process is still alive. Read by an app window that is opening."""
    now = now or time.time()
    until = _setting(st, "paused_until", 0) or 0
    if until > now:
        return True, until
    try:
        pid = json.loads((Path(install) / PAUSE_FLAG).read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return False, None
    return (True, None) if pid and alive(pid) else (False, None)


def _setting(st, key, default=None):
    try:
        return st[key]
    except KeyError:
        return default


def interactive_command():
    """[program, args] that opens the normal menu: the exe itself, or `python -m speakstone_worker` from source."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "speakstone_worker"]


def follow_installed_update(flow, runner, watching, state, clock=time.time, every=15):
    """Board and menu: when another copy of the program (the service) has already installed a newer version, this
    window restarts as it. Our own worker first finishes the job in hand; a worker that belongs to the service is
    left alone. `state` is a dict kept by the caller. Does not return when it restarted."""
    if not flow or update.disabled() or clock() - state.get("looked", 0) < every:
        return False
    state["looked"] = clock()
    if not flow.replaced():
        return False
    if runner is not None and runner.running and not watching:
        if not state.get("draining"):
            runner.request_drain()
            state["draining"] = True
            flow.log("a newer version is installed; restarting after the job in hand")
        return False
    return flow.restart_as_installed()


DRAIN_LIMIT = 30 * 60           # how long an update waits for the job in hand before giving up for this round


def update_step(flow, control, st, runner, clock=time.time, drain_limit=DRAIN_LIMIT, allow=True):
    """Update work for the background loop and the live board. Look every 6 hours (or when asked). When one may be
    installed: (1) download and prove the new file first, (2) ask a running worker to finish its job and leave, no
    job is cut short, (3) when it has gone, swap and restart. After `drain_limit` of waiting the round is given up,
    the worker keeps going and it is tried again at the next check. A successful swap does not return.
    Returns True when a swap was tried."""
    if update.disabled():
        return False
    if control.take_check():
        flow.forced = True
        flow.next_check = 0.0
    if flow.due():
        flow.check()
        control.update_version = flow.version
    if not flow.release or not allow or not (_setting(st, "auto_update", True) or flow.forced):
        return False
    ok, why = flow.can_apply()
    if not ok:
        if flow.forced:
            flow.log(f"update {flow.version} not installed here: {why}")
            flow.forced = False
        return False
    if flow.staged is None or not Path(flow.staged).exists():
        control.status = control_mod.UPDATING
        if flow.prepare():                                  # failed: nothing was stopped
            control.update_version = flow.version
            return False
    if runner.running:
        if control.drain_reason != "update":
            runner.request_drain()
            control.drain_reason, control.drain_since = "update", clock()
            flow.log(f"update {flow.version} is ready; waiting for the job in hand to finish")
        elif clock() - control.drain_since > drain_limit:
            runner.clear_drain()
            control.drain_reason = None
            flow.log(f"update {flow.version}: the job did not finish in time, trying again later")
            flow.give_up()
            control.update_version = None
        return False
    was_working = control.drain_reason == "update"
    control.drain_reason = None
    control.status = control_mod.UPDATING
    flow.log(f"update {flow.version}: restarting as the new version")
    if was_working and flow.before_swap:
        flow.before_swap()                      # the restarted service starts a worker at once
    note = flow.apply()
    if was_working and flow.swap_failed:
        flow.swap_failed()                      # still here: the swap did not happen, so no resume marker
    flow.forced = False
    flow.log(note)
    control.update_version = flow.version
    return True


def make_tray_actions(install, control, st, flow, open_board=tray_mod.open_board, open_folder=tray_mod.open_folder):
    def toggle(key):
        def run():
            reload = getattr(st, "reload", None)
            if reload:
                reload()
            st[key] = not st[key]
        return run

    def logs():
        d = Path(install) / "work" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        open_folder(d)
    def pause_for(minutes):
        def run():
            until = time.time() + minutes * 60 if minutes else None
            control.pause(until)
            st["paused_until"] = until or 0          # a timed pause is kept, so it survives a restart or an update
            if until is None and install:
                write_pause_flag(install)            # so an app window that opens knows (only while this service runs)
        return run

    def resume():
        control.set_paused(False)
        st["paused_until"] = 0
        if install:
            clear_pause_flag(install)
    acts = {k: pause_for(m) for k, _label, m in tray_mod.PAUSE_CHOICES}
    return {**acts, "resume": resume,
            "block_fullscreen": toggle("block_fullscreen"), "block_on_battery": toggle("block_on_battery"),
            "auto_update": toggle("auto_update"), "board": lambda: open_board(interactive_command()),
            "check": control.request_check, "update_now": control.request_check, "logs": logs,
            "quit": control.request_quit}


def tray_state_reader(control, st, install=""):
    return lambda: {"status": control.status, "paused": control.paused, "paused_until": control.paused_until,
                    "label": identity.label_of(install),
                    "settings": {k: _setting(st, k) for k, _ in tray_mod.TOGGLES},
                    "update_version": control.update_version}


def restore_pause(control, st, now=None):
    """At start: a timed pause still running is picked up again; an expired one is cleared."""
    now = now or time.time()
    until = _setting(st, "paused_until", 0) or 0
    if until > now:
        control.pause(until)
    elif until:
        st["paused_until"] = 0


def pause_expiry_step(control, st, now=None):
    """A timed pause ends by itself; the next tick then lets the worker start again."""
    if control.pause_expired(now or time.time()):
        control.set_paused(False)
        st["paused_until"] = 0
        return True
    return False


class ExitNote:
    """Why the background mode ended, written once to background.log. Set the first reason that applies; `write` is
    safe to call from a signal handler or the console-close handler and never raises."""

    def __init__(self, install, version=__version__):
        self.install, self.version, self.reason, self.done, self.updating = install, version, None, False, False

    def set(self, reason):
        if self.reason is None:
            self.reason = reason

    def write(self):
        if self.done:
            return
        self.done = True
        try:
            log_background_error(self.install, f"background {self.version} ended: {self.reason or 'unknown'}")
        except Exception:
            pass


def job_lines_now(install, workers=1, now=None):
    """Lines in the job the log shows in hand (0 when none): the tray's line for a worker started in the app window."""
    try:
        dirs = [Path(install) / "work" / "logs"] + ([Path(install) / "work_2" / "logs"] if workers == 2 else [])
        s = stats.merge_summaries([stats.summarize(stats.read_events(d), now or time.time()) for d in dirs])
        return int(s.get("job_lines", 0)) if s.get("making") else 0
    except Exception:
        return 0


def job_running_now(install, workers=1, now=None):
    """True when the log shows a claimed job that has not finished (drives the tray's 'Working' line)."""
    try:
        dirs = [Path(install) / "work" / "logs"] + ([Path(install) / "work_2" / "logs"] if workers == 2 else [])
        return any(stats.summarize(stats.read_events(d), now or time.time())["making"] for d in dirs)
    except Exception:
        return False


def background(install, st, flow=None, tray_start=tray_mod.start, making=job_running_now):
    """The service. Whatever ends it, one line in background.log says why (a hard kill cannot be logged)."""
    note = ExitNote(install)
    trim_background_log(Path(install) / "work" / "logs" / "background.log")
    log_background_error(install, f"background {__version__} started")
    try:
        code = _background(install, st, note, flow, tray_start, making)
        note.set("setup not finished" if code == 2 else "stopped")
        return code
    except SystemExit as e:
        note.set("update (restarting as the new version)" if note.updating else f"exit {e.code} (task ended / signal)")
        raise
    except BaseException as e:
        note.set(f"error: {type(e).__name__}: {e}")
        raise
    finally:
        note.write()


def _background(install, st, note, flow, tray_start, making, job_lines=None):
    job_lines = job_lines or job_lines_now
    hide_console()
    set_title(window_title("background"))
    if setup.is_set_up(install) and not setup.extras_cached(install):
        setup.forget_extras(install)
        try:
            setup.run_setup(setup.Env(install, paths.resource_dir(), run=setup.real_run), only=("extras",))
        except setup.SetupError:
            return 2
    if not setup.is_set_up(install):
        return 2
    setup.sync_worker_files(install, paths.resource_dir())
    log = lambda text: log_background_error(install, text)
    flow = flow or update.UpdateFlow(log=log, app_exe=paths.app_exe())
    flow.args = ["--background"]               # after an update the new process carries on as the service
    update.cleanup_old(flow.exe)
    control = control_mod.Control()
    clear_pause_flag(install)                    # an 'until I resume' pause does not outlive the service process
    restore_pause(control, st)
    if consume_resume_marker(install):           # an update restarted us while a worker was making lines
        control.force_start_until = time.time() + RESUME_VALID_SECONDS
        log("restarted after an update; starting the worker at once")
    flow.before_swap = lambda: write_resume_marker(install)
    flow.swap_failed = lambda: clear_resume_marker(install)
    runner = WorkerGroup(install, st["workers"])

    def on_close():                              # console closed, log-off, shutdown, SIGTERM/SIGBREAK/SIGHUP
        note.set("task ended / signal")
        note.write()                             # now: the process may be ended within seconds
        runner.shutdown_fast()
    install_close_handlers(on_close)
    gate = RestartGate()
    tray = tray_start(__version__, tray_state_reader(control, st, install), make_tray_actions(install, control, st, flow),
                      paths.assets_dir() / "icon.png", log)
    last_making, making_now, next_label, lines_now = 0.0, False, 0.0, 0
    try:
        while not control.quit:
            try:
                reload = getattr(st, "reload", None)
                if reload:
                    reload()
                if time.time() >= next_label:         # the label for the tray: once a day, every 30 minutes while unknown
                    next_label = time.time() + (identity.REFRESH_SECONDS if worker_label(install) else identity.RETRY_SECONDS)
                    ask_site_for_label(install)
                pause_expiry_step(control, st)
                background_tick(runner, st, gate=gate, control=control)    # first: claim at once when the rules allow
                foreign = not runner.running and _busy(runner)      # making lines, but started in the app window
                try:
                    update_step(flow, control, st, runner, allow=not foreign)
                except SystemExit:                  # a swap on Windows ends the process this way
                    note.updating = True
                    raise
                if (runner.running or foreign) and time.time() - last_making > 10:
                    making_now, last_making = making(install, st["workers"]), time.time()
                    lines_now = job_lines(install, st["workers"]) if foreign else 0
                control.status = control_mod.status_for(runner.running or foreign, making_now and (runner.running or foreign),
                                                        control.paused, drain=control.drain_reason,
                                                        until=control.paused_until, foreign=foreign, lines=lines_now)
            except Exception as e:                  # one odd tick must not end the service
                log(f"tick failed: {type(e).__name__}: {e}")
            time.sleep(5)
    except KeyboardInterrupt:
        note.set("interrupted (Ctrl+C)")
    finally:
        if control.quit:
            note.set("quit from the tray")
        clear_pause_flag(install)
        if tray:
            tray.stop()
        if runner.running:
            runner.stop_and_wait()
    return 0


# ---------------------------------------------------------------- main
def auto_start_wanted(menu, start, st, install, is_set_up=None, read_key=None):
    """A plain start (no arguments) on a finished setup that has a key goes straight to making lines. --menu or the
    Settings toggle turns it off; --start is its own explicit request."""
    if menu or start or not _setting(st, "start_on_open", True):
        return False
    return bool((is_set_up or setup.is_set_up)(install) and (read_key or identity.read_key)(install))


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


def main(argv=None):
    no_error_boxes()
    ap = argparse.ArgumentParser(prog="speakstone_worker", description=APP_NAME)
    ap.add_argument("--background", action="store_true", help="run when the PC is idle (used by the service)")
    ap.add_argument("--start", action="store_true", help="skip the menu and start now")
    ap.add_argument("--menu", action="store_true", help="open the menu without starting")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    install, st = paths.data_dir(), Settings()
    if a.background:
        return background(install, st)
    console = make_console()
    set_title(window_title())
    flow = update.UpdateFlow(log=lambda text: log_background_error(install, text), app_exe=paths.app_exe(), args=[])
    update.cleanup_old(flow.exe)
    looker = None
    labeller = threading.Thread(target=lambda: identity.refresh(install, paths.api_base()), daemon=True)
    labeller.start()                                   # the worker's label, when it is not known yet
    if not update.disabled():                          # a short, silent look in the background; never blocks the menu
        looker = threading.Thread(target=lambda: safe_check(flow, install), daemon=True)
        looker.start()
    auto_tried = False
    keep = {"runner": None}                            # the worker a window keeps while the menu is showing
    # a plain start on a finished setup with a key goes straight to making lines; --menu or the setting turns it off
    auto = auto_start_wanted(a.menu, a.start, st, install)
    first = a.start or auto                            # the first start respects a pause set from the tray
    try:
        choice = "1" if (a.start or auto) else None
        while True:
            if choice is None:
                if looker:
                    looker.join(1.5)                   # the first menu waits a moment for the answer, no longer
                    labeller.join(0.5)
                    looker = None
                working = keep["runner"] is not None and keep["runner"].running
                if flow.release and not auto_tried and st["auto_update"] and flow.can_apply()[0] and not working:
                    auto_tried = True
                    console.print(f"Updating to {flow.version}...")
                    console.print(flow.apply())        # does not return when it worked
                    time.sleep(2)
                fresh_screen(console)
                console.print(board.menu_panel(service.is_installed(), flow.version, worker_label(install)))
                with KeyReader() as keys:
                    choice = keys.wait()
            if choice == "u" and flow.release:
                if keep["runner"] is not None and keep["runner"].running:
                    console.print("Open the board: it updates after the job in hand.")
                else:
                    console.print(flow.apply())
                time.sleep(2.5)
            elif choice == "1":
                if start_now(console, install, st, flow, keep=keep, respect_pause=first) == "exit":
                    return 0
                first = False
            elif choice == "2":
                if service.is_installed():
                    console.print("Service removed." if service.uninstall() else "Could not remove the service.")
                elif ensure_setup(console, install):
                    ok = service.install(install)
                    console.print("Installed. It starts when your PC has been idle for "
                                  f"{st['idle_minutes']} minutes." if ok else
                                  "Could not install the service (the program could not be copied into its own folder, "
                                  "or the task was refused).")
                time.sleep(2)
            elif choice == "3":
                if settings_menu(console, install, st) == "exit":
                    return 0
            elif choice in ("q", "\x1b"):
                return 0
            choice = None
    except KeyboardInterrupt:
        return 0
    finally:
        kept = keep["runner"]
        if kept is not None and kept.running:          # leaving the app stops the worker this window started
            console.print("Giving the current job back before stopping...")
            kept.stop_and_wait()
