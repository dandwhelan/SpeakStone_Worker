"""Fixes from the 2026-11-12 review of the command-line app: the live board's restarts and GPU thread, setup paths
and disk space, the log cache, two-worker download figures, settings saves and the tray thread."""
import json
import os
import shutil
import sys
import threading
import time
import types
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speakstone_worker import cli, setup, stats, tray  # noqa: E402
from speakstone_worker.settings import Settings  # noqa: E402


# ---------------------------------------------------------------- the live board
class FakeGroup:
    """A worker group whose process takes `leave_polls` looks to exit after a stop, as a real one hands its job back."""

    def __init__(self, leave_polls=3):
        self.alive, self.leave_polls, self.countdown = False, leave_polls, None
        self.starts, self.count, self.exit_code = 0, 1, None

    @property
    def running(self):
        if self.alive and self.countdown is not None:
            self.countdown -= 1
            if self.countdown <= 0:
                self.alive, self.countdown, self.exit_code = False, None, 0
        return self.alive

    def start(self):
        if self.running:
            return False
        self.alive, self.starts = True, self.starts + 1
        return True

    def request_stop(self):
        if self.alive and self.countdown is None:
            self.countdown = self.leave_polls

    def exit_now(self):
        self.alive, self.exit_code = False, 1

    def find_existing(self):
        return {}

    def busy_elsewhere(self):
        return False

    def stop_and_wait(self, *a, **k):
        self.alive = False

    def shutdown_fast(self, *a, **k):
        self.alive = False


class Keys:
    def __init__(self, script):
        self.script = list(script)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def poll(self, timeout=0.25):
        k = self.script.pop(0)
        return k() if callable(k) else k


class FakeLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def update(self, *a, **k):
        pass


class FakeMonitor:
    instances = []

    def __init__(self, *a, **k):
        self.closed = False
        FakeMonitor.instances.append(self)

    def read(self):
        return None

    def close(self):
        self.closed = True


def board(monkeypatch, tmp_path, group, keys, update_step=None):
    for name, value in (("ensure_setup", lambda c, i: True), ("fresh_screen", lambda c: None),
                        ("install_close_handlers", lambda f: None), ("Live", FakeLive),
                        ("WorkerGroup", lambda install, n: group), ("KeyReader", lambda: keys),
                        ("Collector", lambda *a: types.SimpleNamespace(queue=None, close=lambda: None))):
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setattr(cli.setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli.stats, "GpuMonitor", FakeMonitor)
    monkeypatch.setattr(cli.board, "render_board", lambda s: None)
    flow = None
    if update_step:
        monkeypatch.setattr(cli, "update_step", update_step)
        flow = types.SimpleNamespace(args=[], version=None)
    console = types.SimpleNamespace(size=(80, 24), print=lambda *a, **k: None)
    return cli.start_now(console, tmp_path, {"workers": 1}, flow)


def test_resume_while_the_worker_is_still_leaving_starts_it_again(monkeypatch, tmp_path):
    """[P], a pause length, then [P] again before the old worker has handed its job back. The start was refused while it
    ran and never tried again, so the board sat on 'Stopped'."""
    g = FakeGroup(leave_polls=4)
    FakeMonitor.instances.clear()
    keys = Keys([None, "p", "1", "p", None, None, None, None, None, None, "m"])
    assert board(monkeypatch, tmp_path, g, keys) == "menu"
    assert g.starts == 2
    assert FakeMonitor.instances and all(m.closed for m in FakeMonitor.instances)


def test_menu_key_while_the_old_worker_is_leaving_still_starts_it_again(monkeypatch, tmp_path):
    """[M] stops nothing: pressed while the resumed worker is still waiting for the old one to go, the board waits for it
    to leave, starts the new one and only then hands over to the menu."""
    g = FakeGroup(leave_polls=6)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    keep = {}
    keys = Keys([None, "p", "1", "p", "m"])
    FakeMonitor.instances.clear()
    for name, value in (("ensure_setup", lambda c, i: True), ("fresh_screen", lambda c: None),
                        ("install_close_handlers", lambda f: None), ("Live", FakeLive),
                        ("WorkerGroup", lambda install, n: g), ("KeyReader", lambda: keys),
                        ("Collector", lambda *a: types.SimpleNamespace(queue=None, close=lambda: None))):
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setattr(cli.setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli.stats, "GpuMonitor", FakeMonitor)
    monkeypatch.setattr(cli.board, "render_board", lambda s: None)
    console = types.SimpleNamespace(size=(80, 24), print=lambda *a, **k: None)
    assert cli.start_now(console, tmp_path, {"workers": 1}, None, keep) == "menu"
    assert g.starts == 2
    assert keep["runner"].running is True


def test_timed_pause_end_waits_for_the_old_worker(monkeypatch, tmp_path):
    g = FakeGroup(leave_polls=50)
    clock = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: clock[0])

    def tick(advance=0.0):
        def k():
            clock[0] += advance
            return None
        return k
    # pause for 15 minutes, jump past the end while the old worker is still leaving, then let it go
    keys = Keys([None, "p", "1", tick(16 * 60), None, lambda: g.exit_now(), None, None, "m"])
    board(monkeypatch, tmp_path, g, keys)
    assert g.starts == 2


def test_a_failed_update_swap_starts_the_worker_again(monkeypatch, tmp_path):
    """The worker left so the update could go in; the swap failed. The board used to stay stopped."""
    g = FakeGroup()
    tried = []

    def update_step(flow, ctl, st, runner):
        if not runner.running and not tried:
            tried.append(1)
            return True                 # apply was tried and came back: it failed
        return False
    keys = Keys([None, lambda: g.exit_now(), None, None, None, "m"])
    board(monkeypatch, tmp_path, g, keys, update_step=update_step)
    assert tried and g.starts == 2


def test_gpu_monitor_close_stops_its_thread():
    calls = []
    mon = stats.GpuMonitor(reader=lambda: None, d3_reader=lambda: calls.append(1) or 40.0, windows=True)
    time.sleep(0.1)
    mon.close()
    mon._thread.join(3)
    assert not mon._thread.is_alive()
    n = len(calls)
    time.sleep(0.3)
    assert len(calls) == n


# ---------------------------------------------------------------- setup
def test_model_download_script_survives_an_apostrophe_in_the_path(tmp_path):
    seen = []
    install = tmp_path / "O'Brien" / "SpeakStoneWorker"
    env = setup.Env(install, tmp_path, run=lambda cmd, e, cwd, on_line: seen.append(cmd) or 0, is_win=True,
                    base_env={"USERPROFILE": str(tmp_path / "nohome"), "HOME": str(tmp_path / "nohome")})
    setup.step_model(env)
    code = str(seen[-1][-1])
    compile(code, "<model>", "exec")
    assert repr(str(install / "IndexTTS-2")) in code


U = namedtuple("U", "total used free")


def test_a_repair_run_needs_less_free_space_once_the_model_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(1, 1, 10 * 1024 ** 3))
    env = setup.Env(tmp_path / "inst", tmp_path)
    with pytest.raises(setup.SetupError, match="about 20 GB"):
        setup.step_folders(env)                         # first install: the model is still to come
    env.model_dir.mkdir(parents=True)
    for f in setup.MODEL_FILES:
        (env.model_dir / f).write_bytes(b"x")
    setup.step_folders(env)                             # 10 GB free is enough to fetch the missing pieces
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(1, 1, 2 * 1024 ** 3))
    with pytest.raises(setup.SetupError, match="about 8 GB"):
        setup.step_folders(env)


# ---------------------------------------------------------------- board data
def test_logs_are_parsed_again_only_when_they_change(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    for day in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"):
        (d / f"worker_{day}.log").write_text(f"{day}T10:00:00Z claim job=a lines=3\n", encoding="utf-8")
    parsed = []
    real = stats.parse_line
    monkeypatch.setattr(stats, "parse_line", lambda ln: parsed.append(ln) or real(ln))
    stats._PARSED.clear()
    first = stats.read_events(d)
    assert len(first) == 3 and len(parsed) == 3                      # the newest three days
    assert stats.read_events(d) == first and len(parsed) == 3        # nothing changed: nothing parsed
    with open(d / "worker_2026-11-12.log", "a", encoding="utf-8") as f:
        f.write("2026-11-12T10:05:00Z complete job=a ok=3 held=0 words=30 audio=12 seconds=60\n")
    assert len(stats.read_events(d)) == 4 and len(parsed) == 5      # only the changed file again
    assert not any("2026-11-09" in k for k in stats._PARSED)


def test_two_workers_downloads_are_added_up():
    a = stats.summarize([], time.time())
    b = stats.summarize([], time.time())
    a.update(dl_today=5_000_000, dl_job=1_000_000, making=True, job="j1", job_started=10.0)
    b.update(dl_today=7_000_000, dl_job=2_000_000, making=True, job="j2", job_started=20.0)
    m = stats.merge_summaries([a, b])
    assert m["dl_today"] == 12_000_000 and m["dl_job"] == 2_000_000 and m["job"] == "j2"


# ---------------------------------------------------------------- settings
def test_settings_saves_from_several_threads_do_not_collide(tmp_path):
    a, b = Settings(tmp_path / "cli.json"), Settings(tmp_path / "cli.json")
    errors = []

    def save_many(st, key):
        for i in range(40):
            try:
                st[key] = not st[key]
            except Exception as e:                      # before the fix: FileNotFoundError from the shared cli.tmp
                errors.append(e)
    ts = [threading.Thread(target=save_many, args=(a, "block_fullscreen")),
          threading.Thread(target=save_many, args=(b, "block_on_battery"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert isinstance(json.loads((tmp_path / "cli.json").read_text(encoding="utf-8")), dict)
    assert not list(tmp_path.glob("*.tmp"))


def test_settings_save_waits_out_a_reader_on_windows(tmp_path, monkeypatch):
    st = Settings(tmp_path / "cli.json")
    real, fails = os.replace, [2]

    def flaky(src, dst):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("in use")
        real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    st["idle_minutes"] = 25
    assert Settings(tmp_path / "cli.json")["idle_minutes"] == 25


# ---------------------------------------------------------------- tray
def test_tray_runs_the_icon_loop_in_its_own_thread():
    """run_detached() only prepares the icon on Linux's GTK and AppIndicator backends; nothing ran its loop there."""
    ran = threading.Event()

    class P:
        class Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        class MenuItem:
            def __init__(self, *a, **k):
                pass

        class Icon:
            def __init__(self, *a):
                self.title = ""

            def run(self, setup=None):
                ran.set()

            def run_detached(self, setup=None):
                raise AssertionError("run_detached is not used")

            def update_menu(self):
                pass

            def stop(self):
                pass
    state = {"status": "Working", "paused": False, "settings": {}, "update_version": None}
    t = tray.Tray("1.2.5", lambda: state, {}, object(), P, poll_seconds=3600)
    t.start()
    assert ran.wait(2)
    t.stop()
