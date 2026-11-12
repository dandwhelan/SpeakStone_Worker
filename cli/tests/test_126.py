"""1.2.6: the tray tells the truth about a worker started in the app window, generation resumes after an update, and
opening the app starts making lines at once."""
import os
import sys
import time
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import net_worker as nw  # noqa: E402
from speakstone_worker import cli, control as control_mod, locking, runner as rn, update  # noqa: E402
from speakstone_worker.settings import Settings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_update_tray import DrainRunner, FakeRunner, RD, ST, make_flow, step_setup  # noqa: E402


# ------------------------------------------------------------------ 1. tray status for a worker started elsewhere
def test_status_for_a_worker_started_in_the_app_window():
    f = control_mod.status_for
    assert f(True, True, False, foreign=True) == "Making lines (started in the app window)"
    assert f(True, True, False, foreign=True, lines=12) == "Making lines (started in the app window) - 12 lines in this job"
    assert f(True, True, True, foreign=True) == "Pausing after this job"          # paused: the drain message wins
    assert f(True, True, False, drain="update", foreign=True) == "Updating after this job"
    assert f(False, False, False) == "Waiting for the PC to be idle"


class ForeignRunner(FakeRunner):
    def __init__(self, busy=True):
        super().__init__(running=False)
        self.busy, self.foreign_drains = busy, 0

    def busy_elsewhere(self):
        return self.busy

    def drain_foreign(self):
        self.foreign_drains += 1


def test_pause_drains_a_worker_started_in_the_app_window_and_resume_clears_it():
    c, r = control_mod.Control(), ForeignRunner()
    c.set_paused(True)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "drain"
    assert r.foreign_drains == 1 and c.drain_reason == "pause" and r.starts == 0
    assert cli.background_tick(r, ST, readers=RD, control=c) == "drain" and r.foreign_drains == 1
    c.set_paused(False)
    r.busy = False
    cli.background_tick(r, ST, readers=RD, control=c)
    assert r.undrains == 1 and c.drain_reason is None


def test_real_runner_writes_the_drain_file_for_a_foreign_worker_and_resume_removes_it(tmp_path):
    g = rn.WorkerGroup(tmp_path, 1)
    holder = nw.InstanceLock(g.runners[0].lock_path)
    assert holder.acquire()
    try:
        g.drain_foreign()
        assert (tmp_path / "work" / "STOP_AFTER_JOB").exists()
        g.clear_drain()
        assert not (tmp_path / "work" / "STOP_AFTER_JOB").exists()
    finally:
        holder.release()


def test_job_lines_are_zero_without_a_log(tmp_path):
    assert cli.job_lines_now(tmp_path / "nothing") == 0


def test_background_status_says_making_in_the_app_window(tmp_path, monkeypatch):
    from speakstone_worker import setup
    for n, v in (("hide_console", lambda: None), ("set_title", lambda t: None), ("install_close_handlers", lambda f: None)):
        monkeypatch.setattr(cli, n, v)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    runner = ForeignRunner()
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: runner)
    monkeypatch.setattr(cli, "job_lines_now", lambda *a: 25)
    seen = {}

    def tray_start(version, get_state, actions, icon, log):
        seen["state"], seen["actions"] = get_state, actions
        return None

    def tick(r, st, gate=None, control=None):
        seen["actions"]["quit"]()
    monkeypatch.setattr(cli, "background_tick", tick)
    flow = make_flow(tmp_path, check_result=None)
    cli.background(tmp_path, {"workers": 1, "paused_until": 0, "auto_update": True, "block_fullscreen": True,
                              "block_on_battery": True}, flow=flow, tray_start=tray_start, making=lambda *a: True)
    assert seen["state"]()["status"] == "Making lines (started in the app window) - 25 lines in this job"


def test_no_update_swap_while_a_foreign_worker_is_running(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    assert cli.update_step(f, c, st, DrainRunner(running=False, events=ev), allow=False) is False and ev == []


# ------------------------------------------------------------------ 2. generation resumes after an update
def test_marker_is_written_before_the_swap_only_when_a_worker_was_draining(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    f.before_swap = lambda: ev.append("marker")
    f.swap_failed = lambda: ev.append("unmarker")
    r = DrainRunner(running=True, events=ev)
    cli.update_step(f, c, st, r)
    r.running = False
    assert cli.update_step(f, c, st, r) is True
    swap_at = [i for i, e in enumerate(ev) if isinstance(e, tuple)][0]
    assert ev.index("marker") < swap_at and "unmarker" in ev                # the fake swap returns, so it is undone
    f2, c2, st2, ev2 = step_setup(tmp_path)
    f2.before_swap = lambda: ev2.append("marker")
    cli.update_step(f2, c2, st2, DrainRunner(running=False, events=ev2))
    assert "marker" not in ev2                               # nothing was working: the idle rule decides as before


def test_marker_valid_for_five_minutes_and_removed_when_read(tmp_path):
    cli.write_resume_marker(tmp_path, now=1000.0)
    assert (tmp_path / cli.RESUME_MARKER).exists()
    assert cli.consume_resume_marker(tmp_path, now=1000.0 + 299) is True
    assert not (tmp_path / cli.RESUME_MARKER).exists()
    assert cli.consume_resume_marker(tmp_path, now=1000.0) is False       # gone
    cli.write_resume_marker(tmp_path, now=1000.0)
    assert cli.consume_resume_marker(tmp_path, now=1000.0 + 301) is False and not (tmp_path / cli.RESUME_MARKER).exists()
    (tmp_path / cli.RESUME_MARKER).write_text("garbage")
    assert cli.consume_resume_marker(tmp_path) is False


def test_forced_start_after_an_update_ignores_idle_but_not_the_blocks():
    st = dict(ST, idle_minutes=10)
    busy_pc = {"idle_seconds": lambda: 1, "fullscreen": lambda: False, "battery": lambda: False}
    c, r = control_mod.Control(), FakeRunner(running=False)
    assert cli.background_tick(r, st, readers=busy_pc, control=c) == "keep"            # normal rule: the user is at the PC
    c.force_start_until = time.time() + 300
    assert cli.background_tick(r, st, readers=busy_pc, control=c) == "start" and r.starts == 1
    assert c.force_start_until == 0.0
    c2, r2 = control_mod.Control(), FakeRunner()
    c2.force_start_until = time.time() + 300
    full = dict(busy_pc, fullscreen=lambda: True)
    assert cli.background_tick(r2, dict(st, block_fullscreen=True), readers=full, control=c2) == "keep"
    c3, r3 = control_mod.Control(), FakeRunner()
    c3.force_start_until = time.time() - 1                                              # expired
    assert cli.background_tick(r3, st, readers=busy_pc, control=c3) == "keep" and c3.force_start_until == 0.0
    c4, r4 = control_mod.Control(), FakeRunner()
    c4.force_start_until = time.time() + 300
    c4.set_paused(True)
    assert cli.background_tick(r4, st, readers=busy_pc, control=c4) == "drain" or r4.starts == 0
    assert r4.starts == 0                                                               # a pause still wins


def test_service_restart_reads_the_marker_and_forces_a_start(tmp_path, monkeypatch):
    from speakstone_worker import setup
    for n, v in (("hide_console", lambda: None), ("set_title", lambda t: None), ("install_close_handlers", lambda f: None)):
        monkeypatch.setattr(cli, n, v)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: FakeRunner())
    seen = {}

    def tick(r, st, gate=None, control=None):
        seen["force"] = control.force_start_until
        control.request_quit()
    monkeypatch.setattr(cli, "background_tick", tick)
    cli.write_resume_marker(tmp_path)
    flow = make_flow(tmp_path, check_result=None)
    cli.background(tmp_path, {"workers": 1, "paused_until": 0, "auto_update": True}, flow=flow,
                   tray_start=lambda *a: None, making=lambda *a: False)
    assert seen["force"] > time.time() and not (tmp_path / cli.RESUME_MARKER).exists()
    assert flow.args == ["--background"] and flow.before_swap is not None


def test_relaunch_arguments_for_the_three_modes_are_unchanged():
    exe = "C:\\A\\SpeakStoneWorker.exe"
    env = {"SystemRoot": "D:\\Win"}
    assert update.relaunch_command(exe, ["--background"], "win32", env)[-1] == "--background"
    assert update.relaunch_command(exe, ["--start"], "win32", env) == [exe, "--start"]
    assert update.relaunch_command(exe, [], "win32", env) == [exe]          # menu: a plain start, which now starts making lines


# ------------------------------------------------------------------ 3. opening the app starts making lines
def test_auto_start_rules(tmp_path):
    yes = dict(is_set_up=lambda i: True, read_key=lambda i: "sswk_x")
    assert cli.auto_start_wanted(False, False, {"start_on_open": True}, tmp_path, **yes) is True
    assert cli.auto_start_wanted(True, False, {"start_on_open": True}, tmp_path, **yes) is False          # --menu
    assert cli.auto_start_wanted(False, True, {"start_on_open": True}, tmp_path, **yes) is False          # --start is its own
    assert cli.auto_start_wanted(False, False, {"start_on_open": False}, tmp_path, **yes) is False        # setting off
    assert cli.auto_start_wanted(False, False, {}, tmp_path, **yes) is True                               # default on
    assert cli.auto_start_wanted(False, False, {}, tmp_path, is_set_up=lambda i: False, read_key=lambda i: "k") is False
    assert cli.auto_start_wanted(False, False, {}, tmp_path, is_set_up=lambda i: True, read_key=lambda i: "") is False


def test_setting_defaults_on_and_menu_flag_exists(tmp_path):
    assert Settings(tmp_path / "cli.json")["start_on_open"] is True
    assert "--menu" in cli.main.__code__.co_consts


def test_pause_active_timed_and_until_resume(tmp_path):
    now = 1000.0
    assert cli.pause_active(tmp_path, {"paused_until": now + 60}, now=now) == (True, now + 60)
    assert cli.pause_active(tmp_path, {"paused_until": now - 1}, now=now) == (False, None)
    cli.write_pause_flag(tmp_path, pid=4242)
    assert cli.pause_active(tmp_path, {"paused_until": 0}, now=now, alive=lambda p: p == 4242) == (True, None)
    assert cli.pause_active(tmp_path, {"paused_until": 0}, now=now, alive=lambda p: False) == (False, None)
    cli.clear_pause_flag(tmp_path)
    assert cli.pause_active(tmp_path, {}, now=now, alive=lambda p: True) == (False, None)


def test_tray_pause_until_resume_writes_and_clears_the_flag(tmp_path):
    c, st = control_mod.Control(), {"paused_until": 0}
    acts = cli.make_tray_actions(tmp_path, c, st, None)
    acts["pause_0"]()
    assert (tmp_path / cli.PAUSE_FLAG).exists()
    acts["resume"]()
    assert not (tmp_path / cli.PAUSE_FLAG).exists()


def test_pid_alive_for_this_process_and_a_dead_one():
    assert locking.pid_alive(os.getpid()) and not locking.pid_alive("nope")


class BoardRunner:
    """A fake worker group for start_now."""
    made = []

    def __init__(self, install, count=1, **kw):
        self.count, self.running, self.starts, self.stops, self.busy = 1, False, 0, 0, False
        BoardRunner.made.append(self)

    def find_existing(self):
        return {}

    def busy_elsewhere(self):
        return self.busy

    def start(self):
        self.starts += 1
        self.running = True

    def request_stop(self):
        self.stops += 1

    def stop_and_wait(self, *a, **k):
        self.stops += 1
        self.running = False

    def shutdown_fast(self, *a, **k):
        pass


class Keys:
    seq = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def poll(self, t):
        return Keys.seq.pop(0) if Keys.seq else "s"

    def wait(self):
        return "a"


class FakeLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def update(self, *a, **k):
        pass


class FakeCollector:
    queue = None

    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


class FakeGpu:
    def read(self):
        return None


def board_env(monkeypatch, keys):
    BoardRunner.made = []
    Keys.seq = list(keys)
    monkeypatch.setattr(cli, "WorkerGroup", BoardRunner)
    monkeypatch.setattr(cli, "KeyReader", Keys)
    monkeypatch.setattr(cli, "ensure_setup", lambda c, i: True)
    monkeypatch.setattr(cli.setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli, "install_close_handlers", lambda f: None)
    monkeypatch.setattr(cli, "fresh_screen", lambda c: None)
    monkeypatch.setattr(cli, "Collector", FakeCollector)
    monkeypatch.setattr(cli.stats, "GpuMonitor", FakeGpu)
    monkeypatch.setattr(cli, "Live", FakeLive)
    return Console(file=open(os.devnull, "w", encoding="utf-8"))


def test_opening_starts_making_lines_and_menu_key_stops_nothing(tmp_path, monkeypatch):
    con = board_env(monkeypatch, ["m"])
    keep = {}
    assert cli.start_now(con, tmp_path, {"workers": 1}, None, keep=keep) == "menu"
    r = BoardRunner.made[0]
    assert r.starts == 1 and r.running and r.stops == 0 and keep["runner"] is r        # [M]: the worker keeps running
    Keys.seq = ["s"]
    assert cli.start_now(con, tmp_path, {"workers": 1}, None, keep=keep) == "exit"      # back on the board: no second start
    assert len(BoardRunner.made) == 1 and r.starts == 1 and r.stops == 1 and not r.running   # [S] is what stops it


def test_active_pause_shows_the_board_paused_and_starts_nothing(tmp_path, monkeypatch):
    con = board_env(monkeypatch, ["m"])
    st = {"workers": 1, "paused_until": time.time() + 600}
    cli.start_now(con, tmp_path, st, None, keep={}, respect_pause=True)
    assert BoardRunner.made[0].starts == 0
    con = board_env(monkeypatch, ["m"])
    cli.start_now(con, tmp_path, st, None, keep={}, respect_pause=False)               # a deliberate Start now ignores it
    assert BoardRunner.made[0].starts == 1


def test_another_worker_already_running_shows_its_board_instead(tmp_path, monkeypatch):
    con = board_env(monkeypatch, ["m"])
    monkeypatch.setattr(BoardRunner, "busy_elsewhere", lambda self: True)
    cli.start_now(con, tmp_path, {"workers": 1}, None, keep={})
    assert BoardRunner.made[0].starts == 0
