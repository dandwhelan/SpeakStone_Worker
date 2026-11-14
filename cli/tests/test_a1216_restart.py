"""Fix 2: one restart gate per worker; exit 7 latches; exit 6 waits a minute."""
from pathlib import Path

from speakstone_worker import cli, control as control_mod
from speakstone_worker.runner import WorkerGroup

ST = {"idle_minutes": 1, "pause_from": "00:00", "pause_to": "00:00", "block_fullscreen": False,
      "block_on_battery": False}
RD = {"idle_seconds": lambda: 9999, "fullscreen": lambda: False, "battery": lambda: False}


class FakeProc:
    def __init__(self):
        self.code, self.pid = None, 0

    def poll(self):
        return self.code


class Spawner:
    def __init__(self):
        self.procs = []

    def __call__(self, cmd, **kw):
        p = FakeProc()
        self.procs.append(p)
        return p


def make_group(tmp_path, count=1):
    sp = Spawner()
    (tmp_path / "worker_config_2.json").write_text("{}")
    g = WorkerGroup(tmp_path, count, popen=sp)
    return g, sp


def tick(g, gates, c=None):
    return cli.background_tick(g, ST, readers=RD, gate=gates, control=c)


def test_exit_7_latches_until_the_person_acts(tmp_path):
    clock = [1000.0]
    g, sp = make_group(tmp_path)
    gates = cli.RestartGates(lambda: clock[0])
    assert tick(g, gates) == "start"
    sp.procs[0].code = 7
    assert tick(g, gates) == "wait"
    assert (tmp_path / "work" / cli.LATCH_FILE).exists()
    clock[0] += 24 * 3600
    assert tick(g, gates) == "wait" and len(sp.procs) == 1
    # a service restart forgets the gates, the file still holds
    assert tick(g, cli.RestartGates(lambda: clock[0])) == "wait" and len(sp.procs) == 1
    assert cli.latched_now(g)
    cli.clear_latches(install=tmp_path)
    assert not cli.latched_now(g)
    assert tick(g, cli.RestartGates(lambda: clock[0])) == "start" and len(sp.procs) == 2


def test_exit_6_waits_60_seconds_and_3_4_5_wait_half_an_hour(tmp_path):
    clock = [0.0]
    g, sp = make_group(tmp_path)
    gates = cli.RestartGates(lambda: clock[0])
    tick(g, gates)
    sp.procs[-1].code = 6
    assert tick(g, gates) == "wait"
    clock[0] += 59
    assert tick(g, gates) == "wait"
    clock[0] += 2
    assert tick(g, gates) == "start"
    for code in (3, 4, 5):
        sp.procs[-1].code = code
        assert tick(g, gates) == "wait"
        clock[0] += 29 * 60
        assert tick(g, gates) == "wait"
        clock[0] += 2 * 60
        assert tick(g, gates) == "start"


def test_each_worker_has_its_own_gate(tmp_path):
    clock = [0.0]
    g, sp = make_group(tmp_path, 2)
    gates = cli.RestartGates(lambda: clock[0])
    assert tick(g, gates) == "start" and len(sp.procs) == 2
    sp.procs[0].code = 7                      # the first worker is broken, the second keeps going
    tick(g, gates)
    assert gates.of(g.runners[0]).blocked() and not gates.of(g.runners[1]).blocked()
    sp.procs[1].code = 1                      # now the second one crashes: only its own backoff applies
    tick(g, gates)
    assert gates.of(g.runners[0]).blocked()
    assert gates.of(g.runners[1]).blocked()
    clock[0] += 31
    assert tick(g, gates) == "start"
    assert len(sp.procs) == 3                 # only the second worker came back


def test_status_says_why_a_worker_is_latched():
    assert control_mod.status_for(False, False, False, broken=cli.BROKEN_SENTENCE) == cli.BROKEN_SENTENCE
    assert control_mod.status_for(True, True, False, broken=cli.BROKEN_SENTENCE) == control_mod.WORKING


def test_tray_resume_clears_the_latch(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / cli.LATCH_FILE).write_text("x")
    c = control_mod.Control()
    c.pause()
    acts = cli.make_tray_actions(tmp_path, c, {"paused_until": 0}, None)
    acts["resume"]()
    assert not (tmp_path / "work" / cli.LATCH_FILE).exists()
