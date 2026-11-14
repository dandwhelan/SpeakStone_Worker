"""Fix 6: removing the service really ends the background program, and says so honestly. Nothing here runs schtasks."""
from pathlib import Path

from speakstone_worker import cli, control as control_mod, service


class R:
    returncode = 0


def run_ok(cmd, **kw):
    run_ok.calls.append(cmd)
    return R()


run_ok.calls = []


def test_uninstall_asks_a_running_background_to_quit_and_waits(tmp_path):
    run_ok.calls.clear()
    (tmp_path / service.BACKGROUND_PID).write_text("4242")
    state = {"alive": True, "t": 0.0}

    def alive(pid):
        return state["alive"]

    def sleep(s):
        state["t"] += s
        if (tmp_path / service.QUIT_FILE).exists() and state["t"] >= 3:
            state["alive"] = False

    r = service.uninstall(runner=run_ok, platform="win32", data_dir=tmp_path, alive=alive, sleep=sleep,
                          clock=lambda: state["t"])
    assert r and r.sentence() == "Service removed."
    assert ["schtasks", "/Delete", "/TN", service.TASK_NAME, "/F"] in run_ok.calls
    assert (tmp_path / service.QUIT_FILE).exists()            # the background program deletes it, not us


def test_uninstall_does_not_claim_removed_while_it_is_still_closing(tmp_path):
    (tmp_path / service.BACKGROUND_PID).write_text("4242")
    t = [0.0]
    r = service.uninstall(runner=run_ok, platform="win32", data_dir=tmp_path, alive=lambda p: True,
                          sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], wait=20)
    assert not r and r.removed and r.closing and t[0] >= 20
    assert "still closing" in r.sentence() and r.sentence() != "Service removed."


def test_nothing_running_means_no_wait_and_no_quit_file(tmp_path):
    r = service.uninstall(runner=run_ok, platform="win32", data_dir=tmp_path, alive=lambda p: False,
                          sleep=lambda s: 1 / 0)
    assert r and not (tmp_path / service.QUIT_FILE).exists()
    (tmp_path / service.BACKGROUND_PID).write_text("9")        # a stale file of a dead process
    assert service.uninstall(runner=run_ok, platform="win32", data_dir=tmp_path, alive=lambda p: False)


def test_no_task_found_is_said_plainly(tmp_path):
    class Bad:
        returncode = 1
    r = service.uninstall(runner=lambda c, **k: Bad(), platform="win32", data_dir=tmp_path, alive=lambda p: False)
    assert not r and r.sentence() == "No service was installed."


def test_linux_is_left_alone(tmp_path):
    calls = []
    r = service.uninstall(runner=lambda c, **k: calls.append(c) or R(), platform="linux", unit_dir=tmp_path / "u",
                          data_dir=tmp_path, alive=lambda p: 1 / 0)
    assert r and ["systemctl", "--user", "disable", "--now", service.UNIT_NAME] in calls


def test_background_checks_the_quit_file_like_a_tray_quit(tmp_path):
    c = control_mod.Control()
    assert cli.take_quit_request(tmp_path, c) is False and not c.quit
    (tmp_path / service.QUIT_FILE).write_text("quit")
    assert cli.take_quit_request(tmp_path, c) is True and c.quit
    assert not (tmp_path / service.QUIT_FILE).exists()


def test_pid_file_is_written_and_only_our_own_is_removed(tmp_path):
    import os
    cli.write_background_pid(tmp_path)
    assert (tmp_path / service.BACKGROUND_PID).read_text() == str(os.getpid())
    (tmp_path / service.BACKGROUND_PID).write_text("1")          # a restarted copy already wrote its number
    cli.drop_background_pid(tmp_path)
    assert (tmp_path / service.BACKGROUND_PID).exists()
    (tmp_path / service.BACKGROUND_PID).write_text(str(os.getpid()))
    cli.drop_background_pid(tmp_path)
    assert not (tmp_path / service.BACKGROUND_PID).exists()
