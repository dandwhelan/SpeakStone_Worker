"""Fix 4: a single-key change keeps the other changes; the wanted worker count is applied to a running group."""
import json

from speakstone_worker import cli
from speakstone_worker.settings import Settings
from test_a1216_restart import make_group


def test_two_copies_do_not_overwrite_each_others_changes(tmp_path):
    path = tmp_path / "cli.json"
    window, tray = Settings(path), Settings(path)
    tray["block_on_battery"] = not tray["block_on_battery"]
    window["workers"] = 2                       # the window's copy is older than the file
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["workers"] == 2 and saved["block_on_battery"] == tray["block_on_battery"]
    assert window["block_on_battery"] == tray["block_on_battery"]


def test_count_change_keeps_the_first_worker_and_drains_the_second(tmp_path):
    g, sp = make_group(tmp_path, 2)
    g.start()
    assert len(sp.procs) == 2
    assert cli.apply_worker_count(g, {"workers": 1}) is True and g.count == 1
    assert (g.runners[1].work_dir / "STOP_AFTER_JOB").exists()
    assert not (g.runners[0].work_dir / "STOP_AFTER_JOB").exists()
    assert g.runners[0].running and len(sp.procs) == 2          # the first one was not restarted
    assert cli.apply_worker_count(g, {"workers": 1}) is False   # nothing changed
    assert cli.apply_worker_count(g, {"workers": 2}) is True and g.count == 2
    assert not (g.runners[1].work_dir / "STOP_AFTER_JOB").exists()


def test_count_two_needs_the_second_config_and_bad_values_are_ignored(tmp_path):
    from speakstone_worker.runner import WorkerGroup
    g = WorkerGroup(tmp_path, 1)
    assert cli.apply_worker_count(g, {"workers": 2}) is False and g.count == 1     # no worker_config_2.json
    assert cli.apply_worker_count(g, {"workers": "x"}) is False
    assert cli.apply_worker_count(g, {}) is False
    assert cli.apply_worker_count(object(), {"workers": 2}) is False
