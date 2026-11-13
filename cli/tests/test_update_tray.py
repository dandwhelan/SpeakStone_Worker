"""Self-update, own install folder, tray menu and the pause control. Every outside call is a fake."""
import hashlib
import json
import os
import shlex
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import (APP_NAME, __version__, board, cli, control as control_mod, service, tray,  # noqa: E402
                               update)
from speakstone_worker.settings import Settings  # noqa: E402

GOOD = "https://github.com/dandwhelan/SpeakStone_Worker/releases/download/v1.3.0/"


def release_json(tag="v1.3.0", names=("SpeakStoneWorker-windows.exe", "SpeakStoneWorker-windows.exe.sha256",
                                      "SpeakStoneWorker-linux", "SpeakStoneWorker-linux.sha256"), base=GOOD):
    return {"tag_name": tag, "assets": [{"name": n, "browser_download_url": base + n} for n in names]}


# ------------------------------------------------------------------ version and asset choice
def test_version_is_shown_and_bumped():
    assert __version__ == "1.2.7"
    assert f"{APP_NAME} {__version__}" in board.menu_screen().plain
    assert cli.window_title() == f"{APP_NAME} {__version__}"
    assert "Update now" in board.menu_screen(False, "1.3.0").plain
    assert "Update now" not in board.menu_screen(False, None).plain


def test_version_compare_is_numeric():
    assert update.is_newer("1.10.0", "1.9.0")
    assert update.is_newer("v1.2.1", "1.2.0")
    assert not update.is_newer("1.2.0", "1.2.0")
    assert not update.is_newer("1.1.9", "1.2.0")
    assert update.is_newer("2", "1.9.9") and not update.is_newer("1.2", "1.2.0")
    assert not update.is_newer("nightly", "1.2.0") and not update.is_newer("1.3.0", "x")


def test_asset_chosen_per_platform():
    w = update.pick_release(release_json(), "win32")
    li = update.pick_release(release_json(), "linux")
    assert w.asset_url.endswith("SpeakStoneWorker-windows.exe") and w.sha_url.endswith("-windows.exe.sha256")
    assert li.asset_url.endswith("SpeakStoneWorker-linux") and li.sha_url.endswith("-linux.sha256")
    assert w.version == "1.3.0"


def test_no_fingerprint_asset_means_no_update():
    data = release_json(names=("SpeakStoneWorker-windows.exe", "SpeakStoneWorker-linux"))
    with pytest.raises(update.UpdateError, match="fingerprint"):
        update.pick_release(data, "win32")


def test_no_asset_for_this_system():
    with pytest.raises(update.UpdateError, match="no file for this system"):
        update.pick_release(release_json(names=("SpeakStoneWorker-linux", "SpeakStoneWorker-linux.sha256")), "win32")


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/x/y/releases/download/v1/a", True),
    ("https://objects.githubusercontent.com/abc", True),
    ("http://github.com/x", False),
    ("https://evil.example.com/a", False),
    ("https://github.com.evil.example/a", False),
    ("https://githubusercontent.com.evil.io/a", False),
    ("ftp://github.com/a", False)])
def test_only_https_github_hosts(url, ok):
    assert update.url_allowed(url) is ok


def test_foreign_host_or_plain_http_in_release_is_refused():
    with pytest.raises(update.UpdateError, match="somewhere other than GitHub"):
        update.pick_release(release_json(base="https://evil.example.com/"), "win32")
    with pytest.raises(update.UpdateError):
        update.pick_release(release_json(base="http://github.com/x/"), "win32")


def test_redirect_to_a_foreign_host_is_refused():
    h = update._CheckedRedirects()
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(update._request(GOOD + "a"), None, 302, "Found", {}, "https://evil.example.com/a")


def test_http_get_refuses_plain_http():
    with pytest.raises(update.UpdateError):
        update.http_get("http://api.github.com/x")


def test_check_newer_uptodate_disabled_and_unreachable():
    def get(url):
        return json.dumps(release_json()).encode()
    assert update.check("1.2.0", env={}, platform="win32", get=get).version == "1.3.0"
    assert update.check("1.3.0", env={}, platform="win32", get=get) is None
    with pytest.raises(update.UpdateError, match="switched off"):
        update.check("1.2.0", env={"SSW_NO_UPDATE": "1"}, get=get)

    def down(url):
        raise urllib.error.URLError("no network")
    with pytest.raises(update.UpdateError, match="could not reach"):
        update.check("1.2.0", env={}, get=down)
    seen = []
    update.check("1.2.0", env={"SSW_UPDATE_URL": "https://example.test/r"}, platform="win32",
                 get=lambda u: seen.append(u) or json.dumps(release_json()).encode())
    assert seen == ["https://example.test/r"]


def test_user_agent_carries_the_version():
    assert update._request("https://github.com/x").get_header("User-agent") == f"SpeakStoneWorker/{__version__}"


# ------------------------------------------------------------------ download and verify
PAYLOAD = b"MZ" + b"x" * 100


def fetch_with(tmp_path, payload=PAYLOAD, fingerprint=None, version_out=None, download=None, min_bytes=50):
    exe = tmp_path / "app" / "SpeakStoneWorker.exe"
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(b"old")
    rel = update.Release("1.3.0", GOOD + "a.exe", GOOD + "a.exe.sha256")
    fp = fingerprint if fingerprint is not None else hashlib.sha256(payload).hexdigest()

    def dl(url, dest):
        Path(dest).write_bytes(payload)
    ran = []

    class R:
        returncode = 0
        stdout = version_out if version_out is not None else f"{APP_NAME} 1.3.0\n"

    def run(cmd):
        ran.append(cmd)
        return R()
    try:
        return update.fetch_verified(rel, exe, get=lambda u: (fp + "  a.exe\n").encode(), download=download or dl,
                                     run=run, min_bytes=min_bytes), exe, ran
    except update.UpdateError as e:
        return e, exe, ran


def test_good_download_is_kept_after_all_three_checks(tmp_path):
    new, exe, ran = fetch_with(tmp_path)
    assert new == tmp_path / "app" / "SpeakStoneWorker.new.exe" and new.exists()
    assert ran == [[str(new), "--version"]]


def test_bad_hash_deletes_the_file(tmp_path):
    err, exe, ran = fetch_with(tmp_path, fingerprint="0" * 64)
    assert isinstance(err, update.UpdateError) and "fingerprint" in str(err)
    assert not update.staging_name(exe).exists() and ran == [] and exe.read_bytes() == b"old"


def test_wrong_version_output_deletes_the_file(tmp_path):
    err, exe, ran = fetch_with(tmp_path, version_out=f"{APP_NAME} 1.2.0\n")
    assert isinstance(err, update.UpdateError) and "expected version" in str(err)
    assert not update.staging_name(exe).exists()


def test_too_small_and_unreadable_fingerprint_and_failed_download(tmp_path):
    err, exe, _ = fetch_with(tmp_path, min_bytes=10_000)
    assert "too small" in str(err) and not update.staging_name(exe).exists()
    err, exe, _ = fetch_with(tmp_path, fingerprint="not a hash")
    assert "could not be read" in str(err)

    def boom(url, dest):
        Path(dest).write_bytes(b"half")
        raise OSError("connection reset")
    err, exe, _ = fetch_with(tmp_path, download=boom)
    assert isinstance(err, update.UpdateError) and not update.staging_name(exe).exists()


def test_cleanup_old_removes_leftovers(tmp_path):
    exe = tmp_path / "SpeakStoneWorker.exe"
    for p in (update.old_name(exe), update.staging_name(exe)):
        p.write_bytes(b"x")
    update.cleanup_old(exe)
    assert not update.old_name(exe).exists() and not update.staging_name(exe).exists()
    update.cleanup_old(None)


# ------------------------------------------------------------------ the swap
def test_windows_swap_sequence(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    update.old_name(exe).write_bytes(b"older")           # an .old from the last swap is replaced
    spawned, exits = [], []
    update.swap_windows(exe, new, ["--background"], spawn=lambda c: spawned.append(c), exit_fn=lambda c: exits.append(c))
    assert exe.read_bytes() == b"new" and update.old_name(exe).read_bytes() == b"old" and not new.exists()
    assert exits == [0] and len(spawned) == 1
    assert spawned[0][1:] == ["--headless", str(exe), "--background"]       # still windowless


def test_windows_swap_puts_the_old_one_back_when_the_move_fails(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")

    def bad_replace(a, b):
        raise PermissionError("locked")
    with pytest.raises(OSError):
        update.swap_windows(exe, new, [], replace=bad_replace, spawn=lambda c: None, exit_fn=lambda c: None)
    assert exe.read_bytes() == b"old"


def test_windows_swap_keeps_running_when_the_new_one_cannot_start(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    exits = []

    def nospawn(c):
        raise OSError("no")
    with pytest.raises(update.UpdateError):
        update.swap_windows(exe, new, [], spawn=nospawn, exit_fn=exits.append)
    assert exits == [] and exe.read_bytes() == b"new"


def test_linux_swap_replaces_then_reexecs(tmp_path, monkeypatch):
    monkeypatch.delenv(update.RESET_ENV, raising=False)
    exe, new = tmp_path / "SpeakStoneWorker", tmp_path / "SpeakStoneWorker.new"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    calls = []
    update.swap_linux(exe, new, ["--background"], execv=lambda p, a: calls.append((p, a)))
    assert exe.read_bytes() == b"new" and calls == [(str(exe), [str(exe), "--background"])]
    assert os.environ[update.RESET_ENV] == "1"
    monkeypatch.delenv(update.RESET_ENV, raising=False)


def test_apply_picks_the_swap_for_the_platform_and_cleans_up_on_failure(tmp_path):
    rel = update.Release("1.3.0", "u", "s")
    exe = tmp_path / "SpeakStoneWorker.exe"
    new = tmp_path / "SpeakStoneWorker.new.exe"
    new.write_bytes(b"n")
    seen = []
    update.apply(rel, exe, ["--background"], platform="win32", fetch=lambda r, e: new,
                 rename=lambda a, b: seen.append("rename"), replace=lambda a, b: seen.append("replace"),
                 spawn=lambda c: seen.append("spawn"), remove=lambda p: None, exit_fn=lambda c: seen.append("exit"))
    assert seen == ["rename", "replace", "spawn", "exit"]
    seen.clear()
    update.apply(rel, exe, [], platform="linux", fetch=lambda r, e: new, replace=lambda a, b: seen.append("replace"),
                 execv=lambda p, a: seen.append("exec"))
    assert seen == ["replace", "exec"]

    def bad(a, b):
        raise OSError("x")
    new.write_bytes(b"n")
    with pytest.raises(update.UpdateError):
        update.apply(rel, exe, [], platform="linux", fetch=lambda r, e: new, replace=bad, execv=lambda p, a: None)
    assert not new.exists()
    os.environ.pop(update.RESET_ENV, None)


def test_relaunch_keeps_background_windowless_and_menu_plain():
    cmd = update.relaunch_command(r"C:\A\SpeakStoneWorker.exe", ["--background"], "win32", {"SystemRoot": r"D:\Win"})
    assert cmd == [r"D:\Win\System32\conhost.exe", "--headless", r"C:\A\SpeakStoneWorker.exe", "--background"]
    assert update.relaunch_command("/a/x", ["--start"], "linux") == ["/a/x", "--start"]


# ------------------------------------------------------------------ when it may swap
def test_only_frozen_in_the_install_folder_swaps(tmp_path):
    app = tmp_path / "app" / "SpeakStoneWorker.exe"
    app.parent.mkdir()
    app.write_bytes(b"x")
    other = tmp_path / "share" / "SpeakStoneWorker.exe"
    other.parent.mkdir()
    other.write_bytes(b"x")
    assert update.can_self_update(True, app, app) == (True, "")
    assert not update.can_self_update(False, app, app)[0]
    # from any folder it can write in (1.2.3): people run the file they downloaded, not the installed copy
    assert update.can_self_update(True, other, app, writable=lambda d: True) == (True, "")
    ok, why = update.can_self_update(True, other, app, writable=lambda d: False)
    assert not ok and "cannot write" in why


def make_flow(tmp_path, frozen=True, in_app=True, check_result=None, apply_fn=None, log=None):
    app = tmp_path / "app" / "SpeakStoneWorker.exe"
    app.parent.mkdir(exist_ok=True)
    app.write_bytes(b"x")
    exe = app if in_app else tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"x")
    t = [1000.0]
    f = update.UpdateFlow(log=(log or (lambda s: None)), check_fn=lambda env=None: check_result,
                          apply_fn=apply_fn or (lambda *a: None), frozen=frozen, exe=str(exe), app_exe=app,
                          args=["--background"], clock=lambda: t[0])
    f.t = t
    return f


REL = update.Release("1.3.0", GOOD + "a", GOOD + "a.sha256")


def test_flow_from_source_or_unwritable_folder_only_reports(tmp_path, monkeypatch):
    called = []
    f = make_flow(tmp_path, check_result=REL, apply_fn=lambda *a: called.append(a), frozen=False)
    assert f.check() == REL
    assert "1.3.0 is available" in f.apply() and called == []
    monkeypatch.setattr(update, "folder_writable", lambda d, mkstemp=None: False)
    f = make_flow(tmp_path, check_result=REL, apply_fn=lambda *a: called.append(a), in_app=False)
    assert f.check() == REL
    assert "1.3.0 is available" in f.apply() and called == []


def test_flow_updates_a_copy_run_from_another_folder(tmp_path):
    """1.2.0-1.2.2 only updated the installed copy: people running the downloaded file never updated."""
    called = []
    f = make_flow(tmp_path, check_result=REL, apply_fn=lambda *a, **k: called.append(a), in_app=False)
    assert f.check() == REL and f.can_apply() == (True, "")
    staged = tmp_path / "SpeakStoneWorker.new.exe"
    staged.write_bytes(b"y")
    f.fetch_fn = lambda rel, exe: staged
    f.apply()
    assert len(called) == 1


def test_flow_failed_check_is_one_log_line_and_silent(tmp_path):
    logs = []

    def down(env=None):
        raise update.UpdateError("could not reach the release page")
    f = make_flow(tmp_path, log=logs.append)
    f.check_fn = down
    assert f.check() is None and logs == ["update check: could not reach the release page"]
    assert not f.due() and f.release is None
    f.t[0] += update.CHECK_EVERY + 1
    assert f.due()


def test_flow_failed_apply_forgets_and_retries_next_check(tmp_path):
    logs = []

    def fail(rel, exe, args):
        raise update.UpdateError("the downloaded file does not match its fingerprint")
    f = make_flow(tmp_path, check_result=REL, apply_fn=fail, log=logs.append)
    f.check()
    assert "was not installed" in f.apply() and f.release is None and any("not installed" in x for x in logs)


# ------------------------------------------------------------------ background update step
class DrainRunner:
    """A worker that notes drain requests; the test decides when it 'exits'."""

    def __init__(self, running=False, events=None):
        self.running, self.drain_asks, self.drain_clears, self.events = running, 0, 0, events if events is not None else []

    def request_drain(self):
        self.drain_asks += 1
        self.events.append("drain")

    def clear_drain(self):
        self.drain_clears += 1
        self.events.append("undrain")


def step_setup(tmp_path, st=None, events=None, **kw):
    """A flow whose download and swap are recorded in `events`, in the order they happen."""
    events = events if events is not None else []
    staged = tmp_path / "app" / "SpeakStoneWorker.new.exe"

    def fetch(rel, exe):
        staged.write_bytes(b"new")
        events.append("verify")
        return staged

    def apply_fn(r, e, a, fetch=None):
        events.append(("swap", r.version, a))
    f = make_flow(tmp_path, check_result=REL, apply_fn=apply_fn, **kw)
    f.fetch_fn = fetch
    return f, control_mod.Control(), (st if st is not None else {"auto_update": True}), events


def test_update_order_is_verify_then_drain_then_wait_then_swap(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    r = DrainRunner(running=True, events=ev)
    now = [0.0]

    def step():
        return cli.update_step(f, c, st, r, clock=lambda: now[0])
    assert step() is False
    assert ev == ["verify", "drain"] and c.drain_reason == "update" and c.update_version == "1.3.0"
    assert control_mod.status_for(True, True, False, drain=c.drain_reason) == "Updating after this job"
    now[0] += 600
    assert step() is False and ev == ["verify", "drain"]            # still working: waits, asks only once, no kill
    r.running = False                                               # the job finished and the worker left
    assert step() is True
    assert ev == ["verify", "drain", ("swap", "1.3.0", ["--background"])] and c.drain_reason is None


def test_a_bad_download_stops_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)

    def bad(rel, exe):
        raise update.UpdateError("the downloaded file does not match its fingerprint")
    f.fetch_fn = bad
    r = DrainRunner(running=True, events=ev)
    assert cli.update_step(f, c, st, r) is False
    assert ev == [] and r.drain_asks == 0 and f.release is None


def test_update_gives_up_after_the_limit_and_leaves_the_worker_running(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    r = DrainRunner(running=True, events=ev)
    now = [0.0]
    cli.update_step(f, c, st, r, clock=lambda: now[0], drain_limit=1800)
    now[0] = 1801
    assert cli.update_step(f, c, st, r, clock=lambda: now[0], drain_limit=1800) is False
    assert ev == ["verify", "drain", "undrain"] and c.drain_reason is None and f.release is None
    assert not update.staging_name(f.exe).exists() and f.next_check > f.clock()      # tried again at the next check
    assert r.running and not any(isinstance(e, tuple) for e in ev)


def test_update_with_no_worker_running_swaps_straight_away(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    assert cli.update_step(f, c, st, DrainRunner(running=False, events=ev)) is True
    assert ev == ["verify", ("swap", "1.3.0", ["--background"])]


def test_update_step_auto_off_only_tells_until_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path, st={"auto_update": False})
    r = DrainRunner(running=False, events=ev)
    assert cli.update_step(f, c, st, r) is False and c.update_version == "1.3.0" and ev == []
    c.request_check()                                       # "Check for updates now" in the tray
    assert cli.update_step(f, c, st, r) is True and len(ev) == 2


def test_update_step_respects_the_off_switch_and_ineligible_copy(tmp_path, monkeypatch):
    f, c, st, ev = step_setup(tmp_path)
    assert cli.update_step(f, c, st, DrainRunner()) is False and ev == []        # SSW_NO_UPDATE=1 from conftest
    monkeypatch.delenv("SSW_NO_UPDATE")
    logs = []
    monkeypatch.setattr(update, "folder_writable", lambda d, mkstemp=None: False)
    f, c, st, ev = step_setup(tmp_path, in_app=False, log=logs.append)
    c.request_check()
    assert cli.update_step(f, c, st, DrainRunner()) is False and ev == []
    assert any("not installed here" in x for x in logs)


def test_checks_only_every_six_hours(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    n = []
    f, c, st, _ = step_setup(tmp_path, st={"auto_update": False})
    f.check_fn = lambda env=None: n.append(1) or None
    r = DrainRunner(running=True)
    cli.update_step(f, c, st, r)
    cli.update_step(f, c, st, r)
    assert len(n) == 1
    f.t[0] += 6 * 3600 + 1
    cli.update_step(f, c, st, r)
    assert len(n) == 2


def test_no_update_while_the_board_is_paused(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    assert cli.update_step(f, c, st, DrainRunner(), allow=False) is False and ev == []


def test_relaunch_arguments_per_mode():
    """The new process starts in the mode the old one was in, so it goes straight back to work."""
    exe = "C:\\A\\SpeakStoneWorker.exe"
    env = {"SystemRoot": "D:\\Win"}
    bg = update.relaunch_command(exe, ["--background"], "win32", env)
    assert bg[-1] == "--background" and "--headless" in bg                      # service: windowless, background
    assert update.relaunch_command(exe, ["--start"], "win32", env) == [exe, "--start"]   # working board: --start
    assert update.relaunch_command(exe, [], "win32", env) == [exe]                       # menu: just the menu
    assert update.relaunch_command("/a/x", ["--start"], "linux") == ["/a/x", "--start"]


def test_flow_arguments_follow_the_mode(tmp_path):
    f = make_flow(tmp_path, check_result=REL)
    f.args = ["--start"]
    seen = []
    f.apply_fn = lambda r, e, a, fetch=None: seen.append(a)
    f.fetch_fn = lambda r, e: tmp_path / "x"
    (tmp_path / "x").write_bytes(b"n")
    f.check()
    f.apply()
    assert seen == [["--start"]]


def test_background_sets_the_service_arguments(tmp_path, monkeypatch):
    from speakstone_worker import setup
    for n, v in (("hide_console", lambda: None), ("set_title", lambda t: None),
                 ("install_close_handlers", lambda f: None)):
        monkeypatch.setattr(cli, n, v)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: FakeRunner())

    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "background_tick", stop)
    f = make_flow(tmp_path, check_result=None)
    f.args = ["wrong"]
    cli.background(tmp_path, {"workers": 1}, flow=f, tray_start=lambda *a: None)
    assert f.args == ["--background"]


def test_the_first_tick_after_a_restart_claims_before_any_update_work():
    """background(): the tick runs before update_step, so a restarted service starts its worker at once."""
    import inspect
    src = inspect.getsource(cli._background)
    assert src.index("background_tick(") < src.index("update_step(")


# ------------------------------------------------------------------ service install copies the program
class R0:
    returncode = 0


def test_service_install_copies_the_exe_and_points_the_task_at_the_copy(tmp_path):
    share = tmp_path / "share" / "SpeakStoneWorker.exe"
    share.parent.mkdir()
    share.write_bytes(b"program")
    target = tmp_path / "data" / "app" / "SpeakStoneWorker.exe"
    calls = []
    ok = service.install(tmp_path / "data", runner=lambda c, **k: calls.append(c) or R0(), platform="win32", frozen=True,
                         executable=str(share), app_target=target)
    xml = (tmp_path / "data" / "task.xml").read_text(encoding="utf-16")
    assert ok and target.read_bytes() == b"program"
    assert str(target) in xml and "share" not in xml and "--headless" in xml and "--background" in xml


def test_service_install_from_the_installed_copy_does_not_copy_onto_itself(tmp_path):
    target = tmp_path / "app" / "SpeakStoneWorker.exe"
    target.parent.mkdir()
    target.write_bytes(b"p")
    seen = []
    ok = service.install(tmp_path, runner=lambda c, **k: R0(), platform="win32", frozen=True, executable=str(target),
                         app_target=target, copier=lambda a, b: seen.append(1))
    assert ok and seen == []


def test_service_install_fails_when_the_copy_fails_rather_than_pointing_at_the_share(tmp_path):
    share = tmp_path / "SpeakStoneWorker.exe"
    share.write_bytes(b"p")
    calls = []

    def bad(a, b):
        raise OSError("disk full")
    ok = service.install(tmp_path / "d", runner=lambda c, **k: calls.append(c) or R0(), platform="win32", frozen=True,
                         executable=str(share), app_target=tmp_path / "a" / "x.exe", copier=bad)
    assert ok is False and calls == []


def test_install_copy_moves_a_busy_copy_aside(tmp_path):
    src, dst = tmp_path / "src.exe", tmp_path / "app" / "SpeakStoneWorker.exe"
    src.write_bytes(b"new")
    dst.parent.mkdir()
    dst.write_bytes(b"old")
    first = [True]

    def replace(a, b):                                       # the first move onto a running file is refused
        if first[0] and Path(b) == dst:
            first[0] = False
            raise PermissionError("in use")
        os.replace(a, b)
    update.install_copy(src, dst, replace=replace)
    assert dst.read_bytes() == b"new" and update.old_name(dst).read_bytes() == b"old"


def test_linux_service_runs_the_copy(tmp_path):
    share = tmp_path / "SpeakStoneWorker"
    share.write_bytes(b"p")
    target = tmp_path / "app" / "SpeakStoneWorker"
    ok = service.install(tmp_path / "d", runner=lambda c, **k: R0(), platform="linux", unit_dir=tmp_path / "u",
                         frozen=True, executable=str(share), app_target=target)
    assert ok and f"ExecStart={shlex.quote(str(target))} --background" in (tmp_path / "u" / service.UNIT_NAME).read_text()


# ------------------------------------------------------------------ tray menu model
SETTINGS = {"block_fullscreen": True, "block_on_battery": False, "auto_update": True}


def by_key(items):
    return {i.key: i for i in items if i.key}


def test_menu_header_status_and_disabled_lines():
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS))
    assert m["title"].label == "SpeakStone Worker 1.2.0" and not m["title"].enabled
    assert m["status"].label == "Working" and not m["status"].enabled
    assert m["board"].default and m["pause"].label == "Pause" and "resume" not in m


@pytest.mark.parametrize("status", [control_mod.WORKING, control_mod.WAITING_WORK, control_mod.PAUSED,
                                    control_mod.WAITING_IDLE, control_mod.UPDATING])
def test_menu_shows_each_status(status):
    m = by_key(tray.build_menu("1.2.0", status, status == control_mod.PAUSED, SETTINGS))
    assert m["status"].label == status
    assert ("resume" in m) == (status == control_mod.PAUSED)


def test_menu_toggles_reflect_settings_and_update_item_appears_when_known():
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS))
    assert m["block_fullscreen"].checked is True and m["block_on_battery"].checked is False
    assert m["auto_update"].checked is True and "update_now" not in m
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS, "1.3.0"))
    assert m["update_now"].label == "Update to 1.3.0 now"
    keys = [i.key for i in tray.build_menu("1.2.0", "x", False, SETTINGS)]
    assert keys[-1] == "quit" and {"check", "logs", "board"} <= set(keys)


def test_tooltip_has_name_version_status():
    assert tray.tooltip("1.2.0", "Paused") == "SpeakStone Worker 1.2.0 - Paused"


class FakePystray:
    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class MenuItem:
        def __init__(self, text, action, **kw):
            self.text, self.action, self.kw = text, action, kw

    class Icon:
        def __init__(self, name, image, title, menu):
            self.name, self.image, self.title, self.menu, self.updates, self.stopped = name, image, title, menu, 0, False

        def run_detached(self):
            self.running = True

        def update_menu(self):
            self.updates += 1

        def stop(self):
            self.stopped = True


def fake_tray(actions, state=None, log=None):
    state = state or {"status": "Working", "paused": False, "settings": SETTINGS, "update_version": None}
    t = tray.Tray("1.2.0", lambda: state, actions, object(), FakePystray, log or (lambda s: None), poll_seconds=3600)
    return t, state


def test_each_menu_item_calls_its_action():
    calls = []
    keys = ["pause_15", "pause_60", "pause_120", "pause_240", "pause_0", "resume", "block_fullscreen", "block_on_battery", "auto_update", "board", "check", "update_now",
            "logs", "quit"]
    t, _ = fake_tray({k: (lambda k=k: calls.append(k)) for k in keys})
    for k in keys:
        t._run(k)()
    assert calls == keys


def test_menu_items_map_to_pystray_with_checks_and_default():
    t, st = fake_tray({"board": lambda: None})
    items = t._menu_items()
    named = {i.text: i for i in items if hasattr(i, "text")}
    assert named["Open the board"].kw["default"] is True
    assert named["Update automatically"].kw["checked"](None) is True
    assert named["SpeakStone Worker 1.2.0"].kw["enabled"] is False
    assert FakePystray.Menu.SEPARATOR in items


def test_a_failing_action_is_swallowed_and_logged():
    logs = []

    def boom():
        raise RuntimeError("x")
    t, _ = fake_tray({"quit": boom}, log=logs.append)
    t._run("quit")()
    assert "tray action quit failed" in logs[0]


def test_tray_refresh_updates_tooltip_and_menu_only_on_change():
    t, st = fake_tray({})
    t.refresh()
    t.refresh()
    assert t.icon.updates == 1 and t.icon.title.endswith("Working")
    st["status"] = "Paused"
    t.refresh()
    assert t.icon.updates == 2 and t.icon.title.endswith("Paused")


# ------------------------------------------------------------------ tray missing -> background unaffected
def test_tray_start_never_raises_and_says_why(tmp_path):
    logs = []

    def no_module(name):
        raise ImportError("no pystray")
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="win32", env={},
                      importer=no_module) is None
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="linux", env={}) is None
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="win32",
                      env={"SSW_NO_TRAY": "1"}) is None
    assert len(logs) == 3 and all(x.startswith("tray:") for x in logs)


def test_missing_icon_file_also_means_no_tray_not_a_crash(tmp_path):
    logs = []
    assert tray.start("1.2.0", dict, {}, tmp_path / "missing.png", logs.append, platform="win32", env={},
                      importer=lambda n: FakePystray) is None and logs


def test_display_needed_on_linux_only():
    assert tray.display_available("win32", {}) and not tray.display_available("linux", {})
    assert tray.display_available("linux", {"DISPLAY": ":0"})


# ------------------------------------------------------------------ pause, quit, actions
class FakeRunner:
    def __init__(self, running=False):
        self.running, self.exit_code, self.stop_asked_at, self.nice = running, None, 0.0, False
        self.starts, self.stops, self.drains, self.undrains = 0, 0, 0, 0

    def request_drain(self):
        self.drains += 1

    def clear_drain(self):
        self.undrains += 1

    def start(self):
        self.starts += 1
        self.running = True

    def request_stop(self):
        self.stops += 1
        self.stop_asked_at = 1.0

    def poll_stop(self, grace=120):
        return not self.running

    def stop_and_wait(self):
        self.running = False

    def shutdown_fast(self):
        self.running = False


RD = {"idle_seconds": lambda: 9999, "fullscreen": lambda: False, "battery": lambda: False}
ST = {"idle_minutes": 1, "pause_from": "", "pause_to": "", "block_fullscreen": False, "block_on_battery": False}


def test_pause_uses_the_drain_not_a_hard_stop():
    c, r = control_mod.Control(), FakeRunner(running=True)
    c.set_paused(True)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "drain"
    assert r.drains == 1 and r.stops == 0 and c.drain_reason == "pause"      # job in hand is kept, not handed back
    assert cli.background_tick(r, ST, readers=RD, control=c) == "drain" and r.drains == 1      # asked once
    assert control_mod.status_for(r.running, True, c.paused, drain=c.drain_reason) == "Pausing after this job"
    r.running = False                                                          # the job finished, the worker left
    assert cli.background_tick(r, ST, readers=RD, control=c) == "keep" and r.starts == 0
    assert control_mod.status_for(r.running, False, c.paused, drain=c.drain_reason) == "Paused"
    c.set_paused(False)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "start" and r.starts == 1
    assert r.undrains == 1


def test_resume_before_the_job_ends_cancels_the_drain():
    c, r = control_mod.Control(), FakeRunner(running=True)
    c.set_paused(True)
    cli.background_tick(r, ST, readers=RD, control=c)
    c.set_paused(False)
    cli.background_tick(r, ST, readers=RD, control=c)
    assert r.undrains == 1 and c.drain_reason is None and r.starts == 0


def test_no_new_worker_while_an_update_is_draining():
    c, r = control_mod.Control(), FakeRunner(running=False)
    c.drain_reason = "update"
    assert cli.background_tick(r, ST, readers=RD, control=c) == "wait" and r.starts == 0


def test_status_lines():
    f = control_mod.status_for
    assert f(True, True, False) == "Working" and f(True, False, False) == "Waiting for work"
    assert f(True, True, True) == "Pausing after this job" and f(False, False, True) == "Paused"
    assert f(False, False, False) == "Waiting for the PC to be idle"
    assert f(False, False, False, updating=True) == "Updating"
    assert f(True, True, False, drain="update") == "Updating after this job"


def test_tray_actions_change_control_and_settings(tmp_path):
    c = control_mod.Control()
    st = Settings(tmp_path / "cli.json")
    opened, folders = [], []
    acts = cli.make_tray_actions(tmp_path, c, st, None, open_board=lambda cmd: opened.append(cmd),
                                 open_folder=lambda p: folders.append(p))
    acts["pause_0"]()
    assert c.paused and c.paused_until is None and st["paused_until"] == 0
    acts["resume"]()
    assert not c.paused
    acts["block_fullscreen"]()
    assert st["block_fullscreen"] is False
    assert json.loads((tmp_path / "cli.json").read_text())["block_fullscreen"] is False      # saved: survives a restart
    acts["auto_update"]()
    assert st["auto_update"] is False
    acts["board"]()
    assert opened and opened[0][0] == sys.executable
    acts["logs"]()
    assert folders == [tmp_path / "work" / "logs"] and folders[0].exists()
    acts["check"]()
    assert c.take_check() and not c.take_check()
    acts["quit"]()
    assert c.quit


def test_pause_submenu_in_the_menu_model():
    items = tray.build_menu("1.2.1", "Waiting for work", False, SETTINGS)
    pause = by_key(items)["pause"]
    assert [c.label for c in pause.children] == ["15 minutes", "1 hour", "2 hours", "4 hours", "Until I resume"]
    assert [c.key for c in pause.children] == ["pause_15", "pause_60", "pause_120", "pause_240", "pause_0"]
    assert "resume" not in by_key(items)


def test_while_paused_the_top_level_shows_resume_and_no_submenu():
    m = by_key(tray.build_menu("1.2.1", "Paused until 21:30", True, SETTINGS))
    assert m["resume"].label == "Resume" and "pause" not in m and m["status"].label == "Paused until 21:30"


def test_pause_submenu_becomes_a_pystray_submenu():
    t, _ = fake_tray({k: (lambda: None) for k, _l, _m in tray.PAUSE_CHOICES})
    sub = [i for i in t._menu_items() if getattr(i, "text", "") == "Pause"][0]
    assert isinstance(sub.action, FakePystray.Menu)
    assert [i.text for i in sub.action.items] == ["15 minutes", "1 hour", "2 hours", "4 hours", "Until I resume"]


def test_timed_pause_sets_an_end_time_and_is_saved(tmp_path):
    c, st = control_mod.Control(), Settings(tmp_path / "cli.json")
    acts = cli.make_tray_actions(tmp_path, c, st, None)
    before = time.time()
    acts["pause_60"]()
    assert c.paused and before + 3590 < c.paused_until < before + 3700
    assert json.loads((tmp_path / "cli.json").read_text())["paused_until"] == c.paused_until
    acts["resume"]()
    assert not c.paused and c.paused_until is None and st["paused_until"] == 0


def test_timed_pause_ends_by_itself_and_the_worker_may_start_again():
    c, r = control_mod.Control(), FakeRunner(running=False)
    st = dict(ST, paused_until=0)
    c.pause(until=1000.0)
    assert not cli.pause_expiry_step(c, st, now=999.0) and c.paused
    assert cli.pause_expiry_step(c, st, now=1000.0) and not c.paused and st["paused_until"] == 0
    assert cli.background_tick(r, ST, readers=RD, control=c) == "start"


def test_timed_pause_still_drains_the_job_in_hand():
    c, r = control_mod.Control(), FakeRunner(running=True)
    c.pause(until=time.time() + 900)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "drain" and r.drains == 1 and r.stops == 0


def test_timed_pause_survives_a_restart_and_an_expired_one_is_cleared(tmp_path):
    st = Settings(tmp_path / "cli.json")
    st["paused_until"] = time.time() + 600
    c = control_mod.Control()
    cli.restore_pause(c, Settings(tmp_path / "cli.json"))               # the new process after a restart or an update
    assert c.paused and c.paused_until == st["paused_until"]
    st["paused_until"] = time.time() - 5
    c2 = control_mod.Control()
    s2 = Settings(tmp_path / "cli.json")
    cli.restore_pause(c2, s2)
    assert not c2.paused and s2["paused_until"] == 0


def test_until_i_resume_is_not_kept_across_a_restart(tmp_path):
    c, st = control_mod.Control(), Settings(tmp_path / "cli.json")
    cli.make_tray_actions(tmp_path, c, st, None)["pause_0"]()
    c2 = control_mod.Control()
    cli.restore_pause(c2, Settings(tmp_path / "cli.json"))
    assert not c2.paused


def test_status_says_until_when_for_a_timed_pause():
    until = time.mktime((2026, 11, 12, 21, 30, 0, 0, 0, -1))
    assert control_mod.status_for(False, False, True, until=until) == "Paused until 21:30"
    assert control_mod.status_for(False, False, True) == "Paused"
    assert control_mod.status_for(True, True, True, until=until) == "Pausing after this job"


def test_board_pause_keys_and_prompt():
    assert board.PAUSE_KEYS == {"1": 15, "2": 60, "3": 120, "4": 240, "5": 0}
    from rich.console import Console
    from speakstone_worker import stats
    s = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
         "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "paused", "making": False,
         "workers": 1, "pause_choice": True, "paused_until": "21:30"}
    con = Console(width=140, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(s))
    text = con.export_text()
    assert "[5] until I resume" in text and "until 21:30" in text


# ------------------------------------------------------------------ why the background mode ended
def bg_env(tmp_path, monkeypatch, tick):
    from speakstone_worker import setup
    for n, v in (("hide_console", lambda: None), ("set_title", lambda t: None)):
        monkeypatch.setattr(cli, n, v)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    handlers = []
    monkeypatch.setattr(cli, "install_close_handlers", handlers.append)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: FakeRunner())
    monkeypatch.setattr(cli, "background_tick", tick)
    return handlers


def bg_log(tmp_path):
    return (tmp_path / "work" / "logs" / "background.log").read_text()


def run_bg(tmp_path, tick_fn, st=None):
    f = make_flow(tmp_path, check_result=None)
    return cli.background(tmp_path, st or {"workers": 1, "paused_until": 0}, flow=f, tray_start=lambda *a: None,
                          making=lambda *a: False)


def test_exit_logged_for_quit_from_the_tray(tmp_path, monkeypatch):
    holder = {}

    def tick(runner, st, gate=None, control=None):
        control.request_quit()
    bg_env(tmp_path, monkeypatch, tick)
    assert run_bg(tmp_path, tick) == 0
    log = bg_log(tmp_path)
    assert "started" in log and "ended: quit from the tray" in log and log.count("ended:") == 1


def test_exit_logged_for_ctrl_c_and_for_errors(tmp_path, monkeypatch):
    def ctrl_c(*a, **k):
        raise KeyboardInterrupt
    bg_env(tmp_path, monkeypatch, ctrl_c)
    run_bg(tmp_path, ctrl_c)
    assert "ended: interrupted (Ctrl+C)" in bg_log(tmp_path)

    class Fatal(BaseException):                    # not an Exception, so the loop's per-tick guard lets it through
        pass

    def boom(*a, **k):
        raise Fatal("no memory")
    monkeypatch.setattr(cli, "background_tick", boom)
    monkeypatch.setattr(cli, "update_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad")))
    monkeypatch.setattr(cli, "job_running_now", lambda *a: False)
    with pytest.raises(Fatal):
        run_bg(tmp_path, boom)
    assert "ended: error: Fatal: no memory" in bg_log(tmp_path)


def test_exit_logged_for_a_signal_and_for_an_update_swap(tmp_path, monkeypatch):
    handlers = bg_env(tmp_path, monkeypatch, lambda *a, **k: None)

    def tick(runner, st, gate=None, control=None):
        handlers[0]()                                        # the console-close handler fires, then the process ends
        raise SystemExit(0)
    monkeypatch.setattr(cli, "background_tick", tick)
    with pytest.raises(SystemExit):
        run_bg(tmp_path, tick)
    log = bg_log(tmp_path)
    assert "ended: task ended / signal" in log and log.count("ended:") == 1

    (tmp_path / "work" / "logs" / "background.log").unlink()
    monkeypatch.setattr(cli, "background_tick", lambda *a, **k: None)

    def swap(*a, **k):
        raise SystemExit(0)
    monkeypatch.setattr(cli, "update_step", swap)
    with pytest.raises(SystemExit):
        run_bg(tmp_path, None)
    assert "ended: update (restarting as the new version)" in bg_log(tmp_path)


def test_a_failing_log_never_stops_the_exit(tmp_path, monkeypatch):
    note = cli.ExitNote(tmp_path)
    monkeypatch.setattr(cli, "log_background_error", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    note.set("x")
    note.write()
    note.write()
    assert note.done


def test_setup_not_finished_is_logged(tmp_path, monkeypatch):
    from speakstone_worker import setup
    monkeypatch.setattr(cli, "hide_console", lambda: None)
    monkeypatch.setattr(cli, "set_title", lambda t: None)
    monkeypatch.setattr(setup, "is_set_up", lambda i: False)
    assert cli.background(tmp_path, {"workers": 1}) == 2
    assert "ended: setup not finished" in bg_log(tmp_path)


def test_first_reason_wins():
    n = cli.ExitNote("x")
    n.set("a")
    n.set("b")
    assert n.reason == "a"


def test_updater_logs_before_it_restarts(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    logs = []
    f, c, st, ev = step_setup(tmp_path, log=logs.append)
    cli.update_step(f, c, st, DrainRunner(running=False, events=ev))
    assert any("restarting as the new version" in x for x in logs)


def test_tray_state_reader_reads_live_settings(tmp_path):
    c, st = control_mod.Control(), Settings(tmp_path / "cli.json")
    read = cli.tray_state_reader(c, st)
    assert read()["settings"] == {"block_fullscreen": True, "block_on_battery": True, "auto_update": True}
    st["block_on_battery"] = False
    c.update_version = "1.3.0"
    assert read()["settings"]["block_on_battery"] is False and read()["update_version"] == "1.3.0"


def test_background_quit_from_the_tray_ends_the_loop_cleanly(tmp_path, monkeypatch):
    from speakstone_worker import setup
    monkeypatch.setattr(cli, "hide_console", lambda: None)
    monkeypatch.setattr(cli, "set_title", lambda t: None)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli, "install_close_handlers", lambda f: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    runner = FakeRunner(running=True)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: runner)
    seen = {}

    def tray_start(version, get_state, actions, icon, log):
        seen["version"], seen["state"], seen["actions"] = version, get_state(), actions
        seen["stopped"] = stopped = []
        return type("T", (), {"stop": lambda self: stopped.append(1)})()

    def tick(runner, st, gate=None, control=None):
        seen["actions"]["quit"]()

    monkeypatch.setattr(cli, "background_tick", tick)
    flow = make_flow(tmp_path, check_result=None)
    st = {"workers": 1, "auto_update": True, "block_fullscreen": True, "block_on_battery": True}
    assert cli.background(tmp_path, st, flow=flow, tray_start=tray_start, making=lambda *a: False) == 0
    assert seen["version"] == __version__ and seen["state"]["status"] == control_mod.WAITING_IDLE
    assert seen["stopped"] == [1] and runner.running is False       # tray removed, the job handed back


def test_open_board_starts_a_new_console_on_windows():
    calls = []
    assert tray.open_board([r"C:\a\x.exe"], popen=lambda cmd, **kw: calls.append((cmd, kw)), platform="win32")
    assert calls[0][0] == [r"C:\a\x.exe"] and calls[0][1]["creationflags"] == 0x10
    assert calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_console_title_seam():
    got = []
    cli.set_title("T", platform="linux", write=got.append)
    assert got == ["\x1b]0;T\x07"]

    class K:
        def SetConsoleTitleW(self, t):
            got.append(t)
    cli.set_title("W", platform="win32", kernel32=K())
    assert got[-1] == "W"


def test_board_foot_shows_version_and_update():
    from rich.console import Console
    from speakstone_worker import stats
    s = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
         "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "idle", "making": False,
         "update": "1.3.0", "workers": 1}
    con = Console(width=110, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(s))
    text = con.export_text()
    assert f"v{__version__}" in text and "update 1.3.0 available" in text


def test_settings_default_and_reload(tmp_path):
    a = Settings(tmp_path / "cli.json")
    assert a["auto_update"] is True
    b = Settings(tmp_path / "cli.json")
    b["idle_minutes"] = 42
    a.reload()
    assert a["idle_minutes"] == 42


def test_paths_app_exe_is_inside_the_data_folder():
    from speakstone_worker import paths
    assert paths.app_exe({"LOCALAPPDATA": "C:/L", "XDG_DATA_HOME": "/x"}).parent.name == "app"
    assert (paths.assets_dir() / "icon.png").exists() and (paths.assets_dir() / "icon.ico").exists()



def test_folder_writable_and_error_text(tmp_path):
    import urllib.error
    assert update.folder_writable(tmp_path) and not list(tmp_path.iterdir())
    assert not update.folder_writable(tmp_path / "missing")
    assert update.describe_error(urllib.error.HTTPError("https://x", 403, "rate limit exceeded", None, None)).startswith("HTTPError 403")
    t = update.describe_error(urllib.error.URLError(TimeoutError("timed out")))
    assert t.startswith("URLError") and "TimeoutError" in t and "https://" not in t



def test_an_old_copy_still_running_does_not_block_the_next_swap_or_install(tmp_path):
    """A window left open from before the last update keeps `.old` locked: Windows will not delete it. The next update
    and the service install both failed on that PC until every old window was closed."""
    exe = tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"current")
    locked = update.old_name(exe)
    locked.write_bytes(b"still running")

    def remove(path):                                   # the locked file cannot be deleted
        if Path(path) != locked:
            update._remove(path)
    assert update.free_old_name(exe, remove).name == "SpeakStoneWorker.exe.old2"
    new = tmp_path / "SpeakStoneWorker.new.exe"
    new.write_bytes(b"newer")
    started = []
    update.swap_windows(exe, new, ["--background"], remove=remove, spawn=started.append, exit_fn=lambda c: None)
    assert exe.read_bytes() == b"newer" and (tmp_path / "SpeakStoneWorker.exe.old2").read_bytes() == b"current"
    assert locked.read_bytes() == b"still running" and len(started) == 1
    # the service install into a folder whose copy is in use and whose .old is locked
    src = tmp_path / "dl" / "SpeakStoneWorker.exe"
    src.parent.mkdir()
    src.write_bytes(b"downloaded")
    import os as _os

    def replace(a, b):                                  # the running copy cannot be overwritten, only renamed
        if Path(b) == exe and exe.exists():
            raise OSError("in use")
        _os.replace(a, b)
    update.install_copy(src, exe, replace=replace, remove=remove)
    assert exe.read_bytes() == b"downloaded" and (tmp_path / "SpeakStoneWorker.exe.old2").read_bytes() == b"newer"
    update.cleanup_old(exe)                             # nothing is running in the test: every old copy goes
    assert not list(tmp_path.glob("*.old*"))



def test_a_restarted_menu_window_keeps_its_output(monkeypatch):
    """After an update the menu window came back empty and black: its output had been sent nowhere (1.2.5)."""
    import subprocess
    monkeypatch.setattr(update.sys, "platform", "win32")
    calls = []
    update._spawn_detached(["C:/x/SpeakStoneWorker.exe", "--start"], popen=lambda cmd, **kw: calls.append(kw))
    assert "stdout" not in calls[0] and "stdin" not in calls[0] and calls[0]["creationflags"] & 0x10
    calls.clear()
    update._spawn_detached(["conhost.exe", "--headless", "C:/x/SpeakStoneWorker.exe", "--background"],
                           popen=lambda cmd, **kw: calls.append(kw))
    assert calls[0]["stdout"] == subprocess.DEVNULL and calls[0]["creationflags"] & 0x08


def test_attach_console_repairs_output_sent_nowhere():
    import io
    from speakstone_worker import cli
    opened, handles = [], []

    def opener(name, mode, **kw):
        opened.append(name)
        return io.StringIO()
    old = (cli.sys.stdout, cli.sys.stderr, cli.sys.stdin)
    try:
        assert cli.attach_console("linux", lambda: False) is False and opened == []
        assert cli.attach_console("win32", lambda: True) is False and opened == []     # a normal start: nothing to do
        assert cli.attach_console("win32", lambda: False, opener, lambda which, stream: handles.append(which)) is True
        assert opened == ["CONOUT$", "CONIN$"] and len(handles) == 3

        def no_console(name, mode, **kw):
            raise OSError("no console")
        assert cli.attach_console("win32", lambda: False, no_console) is False
    finally:
        cli.sys.stdout, cli.sys.stderr, cli.sys.stdin = old
