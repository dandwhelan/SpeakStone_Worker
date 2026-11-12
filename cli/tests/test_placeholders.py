"""A placeholder file is not an installed file, and the app always puts its own worker files in place."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import setup  # noqa: E402

GOOD_WORKER = 'WORKER_VERSION = "9"\n' + "# real worker\n" * 200


def fill(i, tool_bytes=200_000, worker=GOOD_WORKER):
    for rel in ("ffmpeg/ffmpeg.exe", "tools/uv.exe"):
        p = i / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"M" * tool_bytes)
    for n in setup.WORKER_FILES:
        (i / n).write_text(worker, encoding="utf-8")


def test_real_looking_files_pass(tmp_path):
    fill(tmp_path)
    for step in ("worker_files", "uv", "ffmpeg"):
        assert setup.artifact_ok(tmp_path, step), step


def test_one_byte_programs_are_rejected(tmp_path):
    fill(tmp_path, tool_bytes=1)
    assert not setup.artifact_ok(tmp_path, "uv") and not setup.artifact_ok(tmp_path, "ffmpeg")
    fill(tmp_path, tool_bytes=setup.MIN_TOOL_BYTES - 1)
    assert not setup.artifact_ok(tmp_path, "uv")
    fill(tmp_path, tool_bytes=setup.MIN_TOOL_BYTES)
    assert setup.artifact_ok(tmp_path, "uv") and setup.artifact_ok(tmp_path, "ffmpeg")


def test_placeholder_worker_files_are_rejected(tmp_path):
    fill(tmp_path, worker="x")
    assert not setup.artifact_ok(tmp_path, "worker_files")
    fill(tmp_path, worker="# a long file with no version in it\n" * 100)          # big enough, but not the worker
    assert not setup.artifact_ok(tmp_path, "worker_files")
    fill(tmp_path)
    (tmp_path / "runpod_worker.py").write_text("x")                                  # any one of the three
    assert not setup.artifact_ok(tmp_path, "worker_files")
    (tmp_path / "runpod_worker.py").unlink()
    assert not setup.artifact_ok(tmp_path, "worker_files")                           # missing


def test_a_finished_looking_install_of_placeholders_is_not_set_up(tmp_path):
    """The folder found on a real PC: setup_state says done, every file is 1 byte, the config is {}."""
    fill(tmp_path, tool_bytes=1, worker="x")
    (tmp_path / "worker_config.json").write_text("{}")
    (tmp_path / "setup_state.json").write_text('{"done": ' + str(list(setup.STEPS)).replace("'", '"') + "}")
    assert setup.is_set_up(tmp_path) is False
    assert not setup.artifact_ok(tmp_path, "enroll")
    kept = setup.verified_done(tmp_path, set(setup.STEPS))
    assert {"worker_files", "uv", "ffmpeg", "enroll"}.isdisjoint(kept)        # these steps run again


def test_pyproject_still_needs_more_than_a_stub(tmp_path):
    (tmp_path / "index-tts").mkdir()
    (tmp_path / "index-tts" / "pyproject.toml").write_text("x")
    assert not setup._engine_ok(tmp_path)


def test_sync_puts_the_bundled_worker_files_over_placeholders_and_older_ones(tmp_path):
    res, inst = tmp_path / "res", tmp_path / "inst"
    res.mkdir()
    inst.mkdir()
    for n in setup.WORKER_FILES:
        (res / n).write_text(GOOD_WORKER.replace("9", "10"), encoding="utf-8")
        (inst / n).write_text("x")                                                   # placeholder
    assert sorted(setup.sync_worker_files(inst, res)) == sorted(setup.WORKER_FILES)
    assert setup.artifact_ok(inst, "worker_files")
    (res / "net_worker.py").write_text(GOOD_WORKER.replace("9", "11"), encoding="utf-8")     # an updated app
    assert setup.sync_worker_files(inst, res) == ["net_worker.py"]
    assert "11" in (inst / "net_worker.py").read_text()
    assert setup.sync_worker_files(inst, res) == []                                  # identical: nothing copied


def test_start_and_background_both_sync_before_working():
    import inspect
    from speakstone_worker import cli
    assert "sync_worker_files" in inspect.getsource(cli.start_now)
    assert "sync_worker_files" in inspect.getsource(cli._background)


def test_a_test_can_never_resolve_to_a_real_install(_sandbox_home):
    """The guard in conftest.py: every per-user folder points into this test's temporary home."""
    from speakstone_worker import paths

    def inside(path):
        try:
            Path(os.path.realpath(path)).relative_to(os.path.realpath(_sandbox_home))
            return True
        except ValueError:
            return False
    assert inside(paths.data_dir()) and inside(paths.app_dir()) and inside(Path.home())
    assert inside(os.environ["LOCALAPPDATA"]) and inside(os.environ["XDG_DATA_HOME"])
