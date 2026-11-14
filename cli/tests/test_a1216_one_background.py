"""Only one background program per data folder."""
from speakstone_worker import cli, service


def put_pid(tmp_path, pid):
    (tmp_path / service.BACKGROUND_PID).write_text(str(pid), encoding="utf-8")


NAMES = {10: (1, "SpeakStoneWorker.exe"), 20: (1, "SpeakStoneWorker.exe"), 30: (1, "notepad.exe")}


def test_a_live_other_copy_is_found(tmp_path):
    put_pid(tmp_path, 20)
    assert cli.other_background(tmp_path, me=10, alive=lambda p: True, table=NAMES, sleep=lambda s: None, wait=0) == 20


def test_no_file_own_number_or_a_dead_one_is_no_other_copy(tmp_path):
    assert cli.other_background(tmp_path, me=10, alive=lambda p: True, table=NAMES, wait=0) is None
    put_pid(tmp_path, 10)
    assert cli.other_background(tmp_path, me=10, alive=lambda p: True, table=NAMES, wait=0) is None
    put_pid(tmp_path, 20)
    assert cli.other_background(tmp_path, me=10, alive=lambda p: False, table=NAMES, wait=0) is None


def test_a_reused_number_that_is_another_program_does_not_count(tmp_path):
    put_pid(tmp_path, 30)
    assert cli.other_background(tmp_path, me=10, alive=lambda p: True, table=NAMES, wait=0) is None


def test_a_copy_that_is_leaving_is_waited_for(tmp_path):
    put_pid(tmp_path, 20)
    looks = {"n": 0}

    def alive(pid):
        looks["n"] += 1
        return looks["n"] < 3                       # gone on the third look: an update handing over
    assert cli.other_background(tmp_path, me=10, alive=alive, table=NAMES, sleep=lambda s: None, wait=60) is None


def test_the_second_copy_leaves_without_starting_anything(tmp_path):
    started = []
    code = cli.background(tmp_path, {}, tray_start=lambda *a, **k: started.append(1), other=lambda install: 4242)
    assert code == 0 and started == []
    log = (tmp_path / "work" / "logs" / "background.log").read_text(encoding="utf-8")
    assert "already running" in log and "started" not in log
    assert not (tmp_path / service.BACKGROUND_PID).exists()         # the first copy's number is left alone


def test_install_does_not_start_a_second_copy(tmp_path):
    exe = tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"x")
    import os
    put_pid(tmp_path / "d" if (tmp_path / "d").mkdir() is None else tmp_path, os.getpid())     # a live process
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    def runner(cmd, **k):
        seen.append(cmd[1])
        return R()
    ok = service.install(tmp_path / "d", runner=runner, platform="win32", frozen=True, executable=str(exe),
                         app_target=exe, copier=lambda a, b: None)
    assert ok and "/Create" in seen and "/Run" not in seen
    assert "up to a minute" in ok.sentence(10)
