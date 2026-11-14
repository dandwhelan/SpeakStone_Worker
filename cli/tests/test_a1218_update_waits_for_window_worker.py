"""The background update waits for a worker that was started from the app window, as it does for its own."""
from speakstone_worker import cli

from test_update_tray import DrainRunner, step_setup


class WindowWorker(DrainRunner):
    """Not ours (running is False) but busy: the app window started it."""

    def __init__(self, events):
        super().__init__(running=False, events=events)
        self.busy = True

    def busy_elsewhere(self):
        return self.busy

    def drain_foreign(self):
        self.events.append("drain_foreign")


def test_update_waits_for_a_worker_started_from_the_app_window(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    marks = []
    f, c, st, ev = step_setup(tmp_path)
    f.before_swap = lambda: marks.append("resume")
    r = WindowWorker(ev)
    now = [0.0]
    assert cli.update_step(f, c, st, r, clock=lambda: now[0]) is False
    assert ev == ["verify", "drain_foreign"] and c.drain_reason == "update"
    now[0] += 600
    assert cli.update_step(f, c, st, r, clock=lambda: now[0]) is False and ev == ["verify", "drain_foreign"]
    r.busy = False                                             # its job finished and it left
    assert cli.update_step(f, c, st, r, clock=lambda: now[0]) is True
    assert ev[-1][0] == "swap" and c.drain_reason is None
    assert marks == []                                         # the window starts its own worker again, not the service


def test_update_gives_up_on_a_window_worker_after_the_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, ev = step_setup(tmp_path)
    r = WindowWorker(ev)
    now = [0.0]
    cli.update_step(f, c, st, r, clock=lambda: now[0], drain_limit=1800)
    now[0] = 1801
    assert cli.update_step(f, c, st, r, clock=lambda: now[0], drain_limit=1800) is False
    assert ev == ["verify", "drain_foreign", "undrain"] and c.drain_reason is None
    assert not any(isinstance(e, tuple) for e in ev)
