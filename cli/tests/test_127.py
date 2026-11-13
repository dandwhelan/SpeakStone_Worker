"""1.2.7: a window left open while the service updated itself follows the installed version."""
import os

from speakstone_worker import APP_NAME, __version__, cli, update


def flow_for(tmp_path, logs=None):
    exe = tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"old program")
    return update.UpdateFlow(log=(logs.append if logs is not None else (lambda s: None)), frozen=True, exe=str(exe),
                             args=["--start"]), exe


def newer():
    major, minor, patch = (int(x) for x in __version__.split("."))
    return f"{major}.{minor}.{patch + 1}"


def test_unchanged_file_is_never_started(tmp_path):
    f, exe = flow_for(tmp_path)
    ran = []
    assert f.replaced(version_of=lambda p: ran.append(p) or "") is None and ran == []


def test_a_newer_file_put_in_place_by_another_copy_is_seen_once(tmp_path):
    f, exe = flow_for(tmp_path)
    exe.write_bytes(b"a newer and longer program")
    ran = []

    def version_of(p):
        ran.append(p)
        return f"{APP_NAME} {newer()}"
    assert f.replaced(version_of=version_of) == newer()
    assert f.replaced(version_of=version_of) == newer() and len(ran) == 1      # the slow look happens once


def test_a_changed_file_that_is_not_newer_is_ignored(tmp_path):
    f, exe = flow_for(tmp_path)
    exe.write_bytes(b"something else entirely")
    assert f.replaced(version_of=lambda p: f"{APP_NAME} {__version__}") is None
    assert f.replaced(version_of=lambda p: "") is None


def test_not_from_source(tmp_path):
    f = update.UpdateFlow(frozen=False, exe=None)
    assert f.replaced() is None


def test_restart_starts_the_installed_file_and_leaves(tmp_path):
    logs = []
    f, exe = flow_for(tmp_path, logs)
    exe.write_bytes(b"a newer and longer program")
    f.replaced(version_of=lambda p: f"{APP_NAME} {newer()}")
    started, left = [], []
    f.restart_as_installed(spawn=started.append, exit_fn=left.append)
    assert started == [[str(exe), "--start"]] and left == [0] and newer() in logs[-1]


def test_restart_that_cannot_start_does_not_loop(tmp_path):
    f, exe = flow_for(tmp_path)
    exe.write_bytes(b"a newer and longer program")
    f.replaced(version_of=lambda p: f"{APP_NAME} {newer()}")

    def spawn(cmd):
        raise OSError("no")
    assert f.restart_as_installed(spawn=spawn, exit_fn=lambda c: None) is False
    assert f.replaced(version_of=lambda p: f"{APP_NAME} {newer()}") is None


class Runner:
    def __init__(self, running):
        self.running, self.drains = running, 0

    def request_drain(self):
        self.drains += 1


class Flow:
    def __init__(self, version):
        self.version, self.restarts, self.logs = version, 0, []

    def replaced(self):
        return self.version

    def restart_as_installed(self):
        self.restarts += 1
        return True

    def log(self, text):
        self.logs.append(text)


def test_board_follows_at_once_when_it_has_no_worker_of_its_own(monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f = Flow("9.9.9")
    assert cli.follow_installed_update(f, Runner(False), False, {}, clock=lambda: 100) is True and f.restarts == 1
    f = Flow("9.9.9")                                   # the service's worker is running: this window only watches
    r = Runner(True)
    assert cli.follow_installed_update(f, r, True, {}, clock=lambda: 100) is True and r.drains == 0


def test_board_lets_its_own_worker_finish_first(monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f, r, state, now = Flow("9.9.9"), Runner(True), {}, [100]
    assert cli.follow_installed_update(f, r, False, state, clock=lambda: now[0]) is False
    now[0] += 20
    assert cli.follow_installed_update(f, r, False, state, clock=lambda: now[0]) is False
    assert r.drains == 1 and f.restarts == 0            # asked once, not every look
    r.running = False
    now[0] += 20
    assert cli.follow_installed_update(f, r, False, state, clock=lambda: now[0]) is True and f.restarts == 1


def test_board_does_nothing_without_a_newer_file_or_too_soon(monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f = Flow(None)
    assert cli.follow_installed_update(f, Runner(False), False, {}, clock=lambda: 100) is False
    f = Flow("9.9.9")
    assert cli.follow_installed_update(f, Runner(False), False, {"looked": 95}, clock=lambda: 100) is False
    monkeypatch.setenv("SSW_NO_UPDATE", "1")
    assert cli.follow_installed_update(f, Runner(False), False, {}, clock=lambda: 100) is False and f.restarts == 0
