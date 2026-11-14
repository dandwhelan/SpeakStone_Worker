"""Windows refusing to remove or install the service is explained; the job bar counts finished lines."""
from speakstone_worker import cli, service


class R:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_uninstall_refused_says_why_and_what_to_do(tmp_path):
    def runner(cmd, **k):
        return R(1, err="ERROR: Access is denied.") if "/Delete" in cmd else R(0)
    gone = service.uninstall(runner=runner, platform="win32", data_dir=tmp_path, alive=lambda p: False)
    assert not gone and gone.denied and not gone.removed
    text = gone.sentence("Could not remove the service.")
    assert "administrator" in text and service.ADMIN_REMOVE in text and "next sign in" in text
    assert "Could not remove the service." not in text


def test_uninstall_that_works_is_unchanged(tmp_path):
    gone = service.uninstall(runner=lambda cmd, **k: R(0), platform="win32", data_dir=tmp_path, alive=lambda p: False)
    assert gone and gone.sentence() == "Service removed."


def test_uninstall_failing_for_another_reason_keeps_the_plain_message(tmp_path):
    def runner(cmd, **k):
        return R(1, err="ERROR: The system cannot find the file specified.") if "/Delete" in cmd else R(0)
    gone = service.uninstall(runner=runner, platform="win32", data_dir=tmp_path, alive=lambda p: False)
    assert not gone and not gone.denied
    assert gone.sentence("Could not remove the service.") == "Could not remove the service."


def test_install_refused_says_why(tmp_path):
    exe = tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"x")

    def runner(cmd, **k):
        return R(1, err="ERROR: Access is denied.") if "/Create" in cmd else R(0)
    ok = service.install(tmp_path / "d", runner=runner, platform="win32", frozen=True, executable=str(exe),
                         app_target=exe, copier=lambda a, b: None)
    assert not ok and ok.denied
    assert "administrator" in ok.sentence(10) and service.ADMIN_REMOVE in ok.sentence(10)


def test_install_result_still_reads_as_true_or_false(tmp_path):
    assert service.Installed(True) and not service.Installed(False)
    assert service.Installed(True) == True and service.Installed(False) == False       # noqa: E712
    assert "10 minutes" in service.Installed(True).sentence(10)
    assert "Could not install" in service.Installed(False).sentence(10)


def test_access_denied_is_recognised_in_a_few_languages():
    assert service.access_denied("ERROR: Access is denied.") and service.access_denied("FEHLER: Zugriff verweigert.")
    assert not service.access_denied("ERROR: The system cannot find the file specified.") and not service.access_denied("")


def test_lines_made_counts_finished_and_held_clips(tmp_path):
    out = tmp_path / "work" / "jobs" / "abc" / "out"
    (out / "_held_qc").mkdir(parents=True)
    for n in ("L000.ogg", "L001.ogg", "L002.ogg"):
        (out / n).write_bytes(b"x")
    (out / "_held_qc" / "L003.ogg").write_bytes(b"x")
    (out / "generation_provenance.jsonl").write_text("{}", encoding="utf-8")
    assert cli.lines_made(tmp_path, "abc") == 4
    assert cli.lines_made(tmp_path, "nope") == 0 and cli.lines_made(tmp_path, None) == 0


def test_the_bar_follows_finished_lines_not_the_clock(tmp_path):
    logs = tmp_path / "work" / "logs"
    logs.mkdir(parents=True)
    import time
    t0 = time.time() - 3000                                       # the job started long ago: the clock says "done"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))
    (logs / time.strftime("worker_%Y-%m-%d.log", time.gmtime(t0))).write_text(
        f"{stamp} claim job=11111111-2222-3333-4444-555555555555 attempt=1 lines=40\n"
        f"{stamp} run_start job=11111111-2222-3333-4444-555555555555 run=1 lines=40\n", encoding="utf-8")
    out = tmp_path / "work" / "jobs" / "11111111-2222-3333-4444-555555555555" / "out"
    out.mkdir(parents=True)
    s = cli.board_state(tmp_path, None, None, "idle", t0)
    assert s["making"] and s["job_done"] == 0 and s["job_pct"] <= 0.05          # nothing made yet: no pretend progress
    for n in range(10):
        (out / f"L{n:03d}.ogg").write_bytes(b"x")
    s = cli.board_state(tmp_path, None, None, "idle", t0)
    assert s["job_done"] == 10 and abs(s["job_pct"] - 0.25) < 1e-9
