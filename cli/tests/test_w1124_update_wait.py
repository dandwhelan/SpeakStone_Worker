"""Menu update on Windows: the old window says the new one is coming and stays until it is drawing (owner 2026-11-24:
an empty new window with no word in the old one)."""
from pathlib import Path

from speakstone_worker import update


class Proc:
    def __init__(self, pid=50, done_after=None):
        self.pid, self.calls, self.done_after = pid, 0, done_after

    def poll(self):
        self.calls += 1
        return 0 if self.done_after is not None and self.calls > self.done_after else None


def ticking():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def test_wait_returns_when_the_child_appears():
    clock, sleep = ticking()
    seen = iter([{}, {}, {60: (50, "speakstoneworker.exe")}])
    assert update.wait_started(Proc(), table=lambda: next(seen), clock=clock, sleep=sleep) is True


def test_wait_gives_up_after_the_limit_and_when_it_exits():
    clock, sleep = ticking()
    assert update.wait_started(Proc(), table=dict, clock=clock, sleep=sleep, limit=5) is False
    assert update.wait_started(Proc(done_after=1), table=dict, clock=clock, sleep=sleep) is False
    assert update.wait_started(None) is False


def swap(tmp_path, args, say):
    exe, new = tmp_path / "w.exe", tmp_path / "w.exe.new"
    exe.write_text("old"); new.write_text("new")
    waited, exits = [], []
    update.swap_windows(exe, new, args, spawn=lambda c: "proc", exit_fn=exits.append, say=say, wait=waited.append)
    return waited, exits


def test_menu_swap_says_and_waits(tmp_path):
    said = []
    waited, exits = swap(tmp_path, [], said.append)
    assert waited == ["proc"] and exits == [0] and "new window" in said[0]


def test_service_swap_never_waits(tmp_path):
    said = []
    waited, exits = swap(tmp_path, ["--background"], said.append)
    assert waited == [] and said == [] and exits == [0]
    waited, _ = swap(tmp_path, [], None)
    assert waited == []
