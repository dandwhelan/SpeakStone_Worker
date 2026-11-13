"""1.2.10: the background service follows a version the app window installed."""
from speakstone_worker import cli


class Runner:
    def __init__(self, running):
        self.running, self.drains = running, 0

    def request_drain(self):
        self.drains += 1


class Flow:
    def __init__(self, version, restart=True):
        self.version, self.restart, self.restarts, self.logs = version, restart, 0, []

    def replaced(self):
        return self.version

    def restart_as_installed(self):
        self.restarts += 1
        if self.restart == "exit":
            raise SystemExit(0)
        return self.restart

    def log(self, text):
        self.logs.append(text)


class Note:
    updating = False


def markers(tmp_path):
    return cli.resume_marker(tmp_path).exists() if hasattr(cli, "resume_marker") else None


def test_idle_service_restarts_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f = Flow("9.9.9")
    assert cli.service_follow_step(f, Runner(False), {}, tmp_path) is True and f.restarts == 1


def test_service_with_its_own_worker_finishes_the_job_then_resumes(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    wrote, cleared = [], []
    monkeypatch.setattr(cli, "write_resume_marker", wrote.append)
    monkeypatch.setattr(cli, "clear_resume_marker", cleared.append)
    f, r, state = Flow("9.9.9", restart="exit"), Runner(True), {}
    assert cli.service_follow_step(f, r, state, tmp_path) is False and r.drains == 1 and wrote == []
    r.running = False
    state["looked"] = 0
    note = Note()
    try:
        cli.service_follow_step(f, r, state, tmp_path, note)
        raise AssertionError("should have left")
    except SystemExit:
        pass
    assert wrote == [tmp_path] and cleared == [] and note.updating is True and f.restarts == 1


def test_a_restart_that_fails_leaves_no_resume_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    wrote, cleared = [], []
    monkeypatch.setattr(cli, "write_resume_marker", wrote.append)
    monkeypatch.setattr(cli, "clear_resume_marker", cleared.append)
    f = Flow("9.9.9", restart=False)
    assert cli.service_follow_step(f, Runner(False), {"draining": True}, tmp_path) is False
    assert wrote == [tmp_path] and cleared == [tmp_path]


def test_nothing_happens_without_a_newer_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f = Flow(None)
    assert cli.service_follow_step(f, Runner(True), {}, tmp_path) is False and f.restarts == 0
