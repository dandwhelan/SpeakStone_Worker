"""Fix 3: a group meant to run restarts a dead member while the other one runs."""
from speakstone_worker import cli, control as control_mod
from test_a1216_restart import make_group, tick


def test_dead_second_worker_is_started_again_while_the_first_runs(tmp_path):
    clock = [0.0]
    g, sp = make_group(tmp_path, 2)
    gates = cli.RestartGates(lambda: clock[0])
    assert tick(g, gates) == "start" and len(sp.procs) == 2
    sp.procs[1].code = 1                         # the second one crashed, the first goes on
    assert tick(g, gates) == "keep" and len(sp.procs) == 2      # its backoff (30 s) is running
    clock[0] += 31
    assert tick(g, gates) == "start" and len(sp.procs) == 3
    assert g.runners[1].running and g.runners[0].running


def test_a_latched_member_is_not_revived_and_one_worker_setting_starts_no_second(tmp_path):
    g, sp = make_group(tmp_path, 2)
    gates = cli.RestartGates(lambda: 0.0)
    tick(g, gates)
    sp.procs[1].code = 7
    tick(g, gates)
    tick(g, gates)
    assert len(sp.procs) == 2
    one = tmp_path / "one"
    one.mkdir()
    g1, sp1 = make_group(one, 1)
    gates1 = cli.RestartGates(lambda: 0.0)
    tick(g1, gates1)
    assert len(sp1.procs) == 1                   # only the first worker: the second is never started
    assert tick(g1, gates1) == "keep" and len(sp1.procs) == 1


def test_no_revive_while_draining_for_an_update_or_a_stop_was_asked(tmp_path):
    clock = [0.0]
    g, sp = make_group(tmp_path, 2)
    gates = cli.RestartGates(lambda: clock[0])
    tick(g, gates)
    sp.procs[1].code = 0
    c = control_mod.Control()
    c.drain_reason = "update"
    assert tick(g, gates, c) == "keep" and len(sp.procs) == 2
    c.drain_reason = None
    assert tick(g, gates, c) == "start" and len(sp.procs) == 3
