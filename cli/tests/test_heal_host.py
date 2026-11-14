"""A service started by an old version's restart repairs its own spinning console host."""
from speakstone_worker import update


def heal(cpu_values, env=None, host=lambda: 77, **kw):
    seq = iter(cpu_values)
    started = []
    out = update.heal_spinning_host(["--background"], platform="win32", frozen=True, env={} if env is None else env,
                                    host=host, cpu=lambda pid: next(seq), sleep=lambda s: None,
                                    spawn=started.append, exe="C:/x/SpeakStoneWorker.exe", **kw)
    return out, started


def test_a_spinning_host_is_replaced_once():
    env = {}
    out, started = heal([10.0, 11.4], env)                # 1.4 s of processor time in 1.5 s
    assert out is True and len(started) == 1 and env[update.HEALED_ENV] == "1"
    assert started[0][0].lower().endswith("conhost.exe") and "--background" in started[0]
    out, started = heal([10.0, 11.4], env)                # the replacement never does it again
    assert out is False and started == []


def test_a_quiet_host_is_left_alone():
    assert heal([10.0, 10.02]) == (False, [])
    assert heal([None, None]) == (False, [])
    assert heal([10.0, 11.4], host=lambda: None) == (False, [])


def test_only_the_packaged_windows_program_does_this():
    started = []
    kw = dict(env={}, host=lambda: 77, cpu=lambda pid: 0.0, sleep=lambda s: None, spawn=started.append)
    assert update.heal_spinning_host(["--background"], platform="linux", frozen=True, **kw) is False
    assert update.heal_spinning_host(["--background"], platform="win32", frozen=False, **kw) is False
    assert started == []


def test_the_console_host_is_found_up_the_chain():
    table = {100: (90, "speakstoneworker.exe"), 90: (80, "speakstoneworker.exe"), 80: (5, "conhost.exe"), 5: (1, "svchost.exe")}
    assert update.console_host_above(100, table) == 80
    assert update.console_host_above(100, {100: (90, "a.exe"), 90: (80, "b.exe"), 80: (5, "explorer.exe"), 5: (1, "x")}) is None
    assert update.console_host_above(100, {}) is None


def test_it_never_raises():
    def boom():
        raise OSError("no")
    assert update.heal_spinning_host(["--background"], platform="win32", frozen=True, env={}, host=boom) is False
