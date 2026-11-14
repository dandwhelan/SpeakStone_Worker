"""Opening the window starts an installed service that is not running; the tray says what Quit does."""
from speakstone_worker import cli, service, tray


def test_window_starts_an_installed_service_that_is_not_running(tmp_path):
    logs, started = [], []
    assert cli.wake_service(tmp_path, installed=lambda: True, running=lambda d: False,
                            start=lambda: started.append(1) or True, log=logs.append) is True
    assert started == [1] and "started it" in logs[0]


def test_nothing_is_started_when_not_installed_or_already_running(tmp_path):
    started = []
    go = lambda: started.append(1) or True
    assert cli.wake_service(tmp_path, installed=lambda: False, running=lambda d: False, start=go, log=lambda t: None) is False
    assert cli.wake_service(tmp_path, installed=lambda: True, running=lambda d: True, start=go, log=lambda t: None) is False
    assert started == []


def test_a_failed_start_is_logged_and_never_raises(tmp_path):
    logs = []
    assert cli.wake_service(tmp_path, installed=lambda: True, running=lambda d: False, start=lambda: False, log=logs.append) is False
    assert "could not be started" in logs[0]

    def boom():
        raise OSError("no")
    assert cli.wake_service(tmp_path, installed=boom) is False


def test_background_running_reads_the_pid_file(tmp_path):
    assert service.background_running(tmp_path, alive=lambda p: True) is False          # no file
    (tmp_path / service.BACKGROUND_PID).write_text("4242", encoding="utf-8")
    assert service.background_running(tmp_path, alive=lambda p: p == 4242) is True
    assert service.background_running(tmp_path, alive=lambda p: False) is False


def test_start_asks_the_right_program():
    seen = []

    class R:
        returncode = 0

    def runner(cmd, **kw):
        seen.append(cmd)
        return R()
    assert service.start(runner=runner, platform="win32") is True
    assert seen[0][:2] == ["schtasks", "/Run"] and service.TASK_NAME in seen[0]
    assert service.start(runner=runner, platform="linux") is True
    assert "start" in seen[1] and service.UNIT_NAME in seen[1]


def test_tray_entry_says_what_quitting_does():
    items = tray.build_menu("1.2.16", "Working", False, {})
    quit_item = [i for i in items if getattr(i, "key", None) == "quit"][0]
    assert "background service" in quit_item.label and "sign in" in quit_item.label


def test_tests_can_never_start_the_real_service(tmp_path, monkeypatch):
    """conftest sets SSW_NO_SERVICE_WAKE; with no start function passed in, wake_service must do nothing at all."""
    import os
    assert os.environ.get("SSW_NO_SERVICE_WAKE") == "1"
    called = []
    monkeypatch.setattr(cli.service, "is_installed", lambda *a, **k: called.append("query") or True)
    monkeypatch.setattr(cli.service, "start", lambda *a, **k: called.append("start") or True)
    assert cli.wake_service(tmp_path) is False and called == []
