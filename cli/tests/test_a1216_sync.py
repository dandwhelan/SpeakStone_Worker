"""Fix 5: the worker scripts are swapped in, never truncated in place."""
import os

from speakstone_worker import setup


def test_files_are_replaced_through_a_temp_name_and_no_temp_is_left(tmp_path, monkeypatch):
    res, inst = tmp_path / "res", tmp_path / "inst"
    res.mkdir()
    inst.mkdir()
    for n in setup.WORKER_FILES:
        (res / n).write_text("new " + n)
        (inst / n).write_text("old " + n)
    calls = []
    real = os.replace
    monkeypatch.setattr(setup.os, "replace", lambda a, b: (calls.append((os.path.basename(a), os.path.basename(b))), real(a, b))[1])
    assert sorted(setup.sync_worker_files(inst, res)) == sorted(setup.WORKER_FILES)
    assert all(a == b + ".new" for a, b in calls) and len(calls) == len(setup.WORKER_FILES)
    assert (inst / "net_worker.py").read_text() == "new net_worker.py"
    assert not list(inst.glob("*.new"))
    assert setup.sync_worker_files(inst, res) == []              # unchanged: nothing copied


def test_a_failed_swap_leaves_the_old_file_whole(tmp_path, monkeypatch):
    res, inst = tmp_path / "res", tmp_path / "inst"
    res.mkdir()
    inst.mkdir()
    (res / "net_worker.py").write_text("new")
    (inst / "net_worker.py").write_text("old")

    def boom(a, b):
        raise PermissionError("in use")
    monkeypatch.setattr(setup.os, "replace", boom)
    assert setup.sync_worker_files(inst, res) == []
    assert (inst / "net_worker.py").read_text() == "old" and not list(inst.glob("*.new"))
