"""1.2.26: the first-start feature menu, NVIDIA-only voice lines, and the second worker under the same name."""
import json
import subprocess

from speakstone_worker import cli, firstrun, identity, setup, stats
from speakstone_worker.settings import Settings

NV16 = {"ok": True, "why": "NVIDIA RTX 4080, 16 GB", "mib": 16376, "name": "NVIDIA RTX 4080", "suggest_two": True}
NV8 = {"ok": True, "why": "NVIDIA RTX 3070, 8 GB", "mib": 8192, "name": "NVIDIA RTX 3070", "suggest_two": False}
AMD = {"ok": False, "why": "Found AMD Radeon RX 7900 XTX. Making voice lines needs an NVIDIA graphics card",
       "mib": 0, "name": "", "suggest_two": False}


# ---- settings
def test_new_install_sees_the_menu_and_an_old_one_does_not(tmp_path):
    fresh = Settings(tmp_path / "a.json")
    assert fresh["first_run_done"] is False and fresh["make_lines"] is True
    old = tmp_path / "b.json"
    old.write_text(json.dumps({"idle_minutes": 10, "workers": 1}))           # written by 1.2.25
    assert Settings(old)["first_run_done"] is True
    assert firstrun.wanted(fresh, is_set_up=False)
    assert not firstrun.wanted(fresh, is_set_up=True)                       # a set-up PC never sees it


# ---- graphics card
def reading(mib, name="NVIDIA GeForce RTX 4080"):
    return {"mem_total_mib": mib, "mem_used_mib": 500, "name": name, "temp_c": 40, "util_pct": 0}


def test_only_nvidia_with_8_gb_can_make_lines():
    assert stats.lines_support(reading(16376))[0]
    ok, why = stats.lines_support(reading(6144, "NVIDIA GeForce RTX 2060"))
    assert not ok and "8 GB" in why
    ok, why = stats.lines_support(None, ["AMD Radeon RX 7900 XTX"])
    assert not ok and "AMD Radeon RX 7900 XTX" in why and "NVIDIA" in why
    ok, why = stats.lines_support(None, ["Intel(R) UHD Graphics 770"])
    assert not ok and "Intel" in why
    ok, why = stats.lines_support(None, ["NVIDIA GeForce RTX 3080"])          # card there, driver missing
    assert not ok and "driver" in why
    assert not stats.lines_support(None, [])[0]


def test_display_adapters_reads_lspci_and_windows():
    out = ("00:02.0 VGA compatible controller: Intel Corporation Device a780\n"
           "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 31\n"
           "00:1f.3 Audio device: Intel Corporation\n")
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out)
    names = stats.display_adapters(run, platform_name="linux")
    assert len(names) == 2 and "Navi 31" in names[1]
    win = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="AMD Radeon RX 7900 XTX\r\n\r\n")
    assert stats.display_adapters(win, platform_name="win32") == ["AMD Radeon RX 7900 XTX"]

    def boom(cmd, **kw):
        raise OSError("no lspci")
    assert stats.display_adapters(boom, platform_name="linux") == []


def test_gpu_facts_suggests_two_workers_from_16_gb():
    assert firstrun.gpu_facts(lambda: reading(16376))["suggest_two"]
    assert not firstrun.gpu_facts(lambda: reading(12288))["suggest_two"]
    f = firstrun.gpu_facts(lambda: None, lambda: ["AMD Radeon RX 6800"])
    assert not f["ok"] and not f["suggest_two"] and "AMD" in f["why"]


# ---- the menu
def test_menu_keys_and_save(tmp_path):
    st = Settings(tmp_path / "cli.json")
    c = firstrun.initial_choices(st, NV16)
    assert c["make_lines"] and c["update_addons"] and c["send_wow_text"] and not c["two_workers"]
    assert "Two workers" in firstrun.render(c, NV16)
    firstrun.handle_key(c, "t", NV16)
    firstrun.handle_key(c, "i", NV16)
    firstrun.handle_key(c, "1", NV16)
    assert c["two_workers"] and c["idle_minutes"] == 20 and not c["update_addons"]
    assert firstrun.handle_key(c, "\r", NV16)[0] == "done"
    firstrun.save(st, c)
    again = Settings(tmp_path / "cli.json")
    assert again["first_run_done"] and again["two_workers_wanted"] and again["idle_minutes"] == 20
    assert again["update_addons"] is False


def test_no_two_worker_offer_on_8_gb_and_turning_lines_off_hides_the_sub_settings():
    c = firstrun.initial_choices(_st(), NV8)
    assert "Two workers" not in firstrun.render(c, NV8)
    firstrun.handle_key(c, "t", NV8)
    assert not c["two_workers"]
    firstrun.handle_key(c, "3", NV8)
    assert not c["make_lines"] and "idle minutes" not in firstrun.render(c, NV8)


def test_a_non_nvidia_pc_cannot_turn_lines_on():
    c = firstrun.initial_choices(_st(), AMD)
    assert c["make_lines"] is False
    result, note = firstrun.handle_key(c, "3", AMD)
    assert result == "again" and "NVIDIA" in note and c["make_lines"] is False
    assert "not available" in firstrun.render(c, AMD)


def test_folder_key_checks_the_folder():
    c = firstrun.initial_choices(_st(), NV8)
    assert firstrun.handle_key(c, "f", NV8, ask=lambda _: "Z:\\nope", isdir=lambda p: False)[1]
    firstrun.handle_key(c, "f", NV8, ask=lambda _: "D:\\WoW", isdir=lambda p: True)
    assert c["wow_folder"] == "D:\\WoW"


def test_what_comes_after_the_menu():
    on = {"make_lines": True, "update_addons": False, "send_wow_text": False}
    assert firstrun.next_choice(on, False) == "1"
    light = {"make_lines": False, "update_addons": True, "send_wow_text": False}
    assert firstrun.next_choice(light, False) == "2" and firstrun.next_choice(light, True) is None
    assert firstrun.next_choice({"make_lines": False, "update_addons": False, "send_wow_text": False}, False) is None


class FakeKeys:
    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def wait(self):
        return self.seq.pop(0)


def test_run_saves_on_enter(tmp_path):
    from rich.console import Console
    st = Settings(tmp_path / "cli.json")
    con = Console(file=open(tmp_path / "out.txt", "w"), width=120)
    c = firstrun.run(con, st, FakeKeys(["2", "\n"]), gpu=NV8)
    assert c["send_wow_text"] is False and st["first_run_done"]


def _st():
    return {"update_addons": True, "send_wow_text": True, "make_lines": True, "idle_minutes": 10,
            "two_workers_wanted": False, "wow_folder": ""}


# ---- making lines off
def test_lines_off_never_auto_starts(tmp_path):
    st = {"start_on_open": True, "make_lines": False}
    assert not cli.auto_start_wanted(False, False, st, tmp_path, is_set_up=lambda i: True, read_key=lambda i: "k")
    st["make_lines"] = True
    assert cli.auto_start_wanted(False, False, st, tmp_path, is_set_up=lambda i: True, read_key=lambda i: "k")


def test_settings_toggle_refuses_a_non_nvidia_card():
    st = {"make_lines": False}
    assert "NVIDIA" in cli.toggle_make_lines(st, gpu=AMD) and st["make_lines"] is False
    cli.toggle_make_lines(st, gpu=NV8)
    assert st["make_lines"] is True
    cli.toggle_make_lines(st)
    assert st["make_lines"] is False


def test_start_now_with_lines_off_goes_back_to_the_menu(tmp_path, monkeypatch):
    from rich.console import Console
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    called = []
    monkeypatch.setattr(cli, "ensure_setup", lambda *a: called.append(a) or True)
    con = Console(file=open(tmp_path / "o.txt", "w"))
    assert cli.start_now(con, tmp_path, {"make_lines": False}) == "menu" and not called


class FakeRunner:
    def __init__(self):
        self.running, self.drained, self.cleared = True, 0, 0

    def request_drain(self):
        self.drained += 1

    def clear_drain(self):
        self.cleared += 1

    def poll_stop(self):
        pass


def test_lines_off_finishes_the_job_then_rests():
    from speakstone_worker import control as control_mod
    r, ctl = FakeRunner(), control_mod.Control()
    assert cli.lines_off_tick(r, ctl) == "drain" and r.drained == 1 and ctl.drain_reason == "lines_off"
    cli.lines_off_tick(r, ctl)
    assert r.drained == 1                                            # asked once
    r.running = False
    assert cli.lines_off_tick(r, ctl) == "off" and ctl.drain_reason is None and ctl.status == cli.LINES_OFF


def test_light_setup_when_lines_off_and_text_on(tmp_path, monkeypatch):
    from rich.console import Console
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(cli, "run_setup_ui", lambda con, inst, base, only=None: seen.append(only))
    con = Console(file=open(tmp_path / "o.txt", "w"))
    assert cli.ensure_setup(con, tmp_path, {"make_lines": False, "send_wow_text": True})
    assert seen == [setup.LIGHT_STEPS]
    seen.clear()
    assert cli.ensure_setup(con, tmp_path, {"make_lines": False, "send_wow_text": False}) and seen == []


def test_light_steps_need_little_disk(tmp_path, monkeypatch):
    env = setup.Env(tmp_path / "inst", tmp_path, light=True)
    monkeypatch.setattr(setup.shutil, "disk_usage", lambda p: type("U", (), {"free": 3 * 1024 ** 3})())
    setup.step_folders(env)                                          # 3 GB free is plenty without the voice engine
    env.light = False
    try:
        setup.step_folders(env)
        raise AssertionError("the full setup should need about 20 GB")
    except setup.SetupError:
        pass


def test_wanted_second_worker_is_added_once_after_setup(tmp_path, monkeypatch):
    from rich.console import Console
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    st = Settings(tmp_path / "cli.json")
    st["two_workers_wanted"] = True
    con = Console(file=open(tmp_path / "o.txt", "w"))
    ran = []
    gpu = lambda: {"mem_total_mib": 16376, "mem_used_mib": 600}
    cli.add_wanted_second_worker(con, tmp_path, st, enroll_runner=lambda i: ran.append(i), gpu_reader=gpu)
    assert ran and st["workers"] == 2 and st["two_workers_wanted"] is False
    assert cli.add_wanted_second_worker(con, tmp_path, st, enroll_runner=lambda i: ran.append(i)) is None
    assert len(ran) == 1


# ---- the second worker's name
def test_second_worker_is_renamed_to_the_first_ones_label(tmp_path):
    (tmp_path / "enrolled.json").write_text(json.dumps({"label": "Dusk Hollow"}))
    (tmp_path / "worker_config_2.json").write_text(json.dumps({"worker_key": "sswk_" + "b" * 43}))
    (tmp_path / "enrolled_2.json").write_text(json.dumps({"label": "Calm Heron"}))
    calls = []

    def renamer(install, base, name, config, enrolled):
        calls.append((name, config, enrolled))
        identity.save(install, {"label": name}, 0, enrolled)
        return True, "ok"
    assert identity.match_second_name(tmp_path, "https://x", renamer=renamer)
    assert calls == [("Dusk Hollow", "worker_config_2.json", "enrolled_2.json")]
    assert identity.label_of(tmp_path) == "Dusk Hollow"                             # the first is untouched
    assert identity.match_second_name(tmp_path, "https://x", renamer=renamer) and len(calls) == 1   # already equal


def test_no_second_worker_no_rename(tmp_path):
    (tmp_path / "enrolled.json").write_text(json.dumps({"label": "Dusk Hollow"}))
    assert not identity.match_second_name(tmp_path, "https://x", renamer=lambda *a, **k: (True, ""))


# ---- a text-upload-only key (light setup)
class _Resp:
    status = 201

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, n):
        return json.dumps(self.body).encode()


def _http_error(code, body):
    import io
    import urllib.error
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_text_only_enrol_sends_the_purpose(tmp_path):
    from speakstone_worker import enroll
    sent = []

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return _Resp({"key": "sswk_" + "a" * 43, "label": "Calm Heron"})
    enroll.enroll("https://x", "PC", "AMD Radeon", 0, "windows", "1.2.26", opener=opener, purpose=enroll.PURPOSE_TEXT)
    assert sent == [{"machine_name": "PC", "gpu": "AMD Radeon", "vram_mib": 0, "os": "windows",
                     "worker_version": "1.2.26", "purpose": "text"}]


def test_a_site_without_text_keys_falls_back_to_the_ordinary_enrol():
    from speakstone_worker import enroll
    sent = []

    def opener(req, timeout):
        body = json.loads(req.data)
        sent.append(body)
        if "purpose" in body:
            raise _http_error(422, {"code": "bad_request"})
        return _Resp({"key": "sswk_" + "a" * 43, "label": "Calm Heron"})
    got = enroll.enroll("https://x", "PC", "RTX 3070", 8192, "linux", "1.2.26", opener=opener, purpose="text")
    assert got["label"] == "Calm Heron" and len(sent) == 2 and "purpose" not in sent[1]


def test_a_refused_ordinary_enrol_still_says_why():
    from speakstone_worker import enroll
    import pytest

    def opener(req, timeout):
        raise _http_error(422, {"code": "gpu_too_small"})
    with pytest.raises(enroll.EnrollError, match="8 GB"):
        enroll.enroll("https://x", "PC", "AMD", 0, "linux", "1.2.26", opener=opener, purpose="text")
    with pytest.raises(enroll.EnrollError, match="8 GB"):
        enroll.enroll("https://x", "PC", "AMD", 0, "linux", "1.2.26", opener=opener)


# ---- what kind of key this PC holds (site contract 2026-11-25)
def test_enrol_reports_which_kind_of_key_it_got():
    from speakstone_worker import enroll
    ok = lambda req, timeout: _Resp({"key": "sswk_" + "a" * 43, "label": "Worker 9"})
    assert enroll.enroll("https://x", "PC", "AMD", 0, "linux", "1.2.26", opener=ok, purpose="text")["purpose"] == "text"
    assert enroll.enroll("https://x", "PC", "RTX", 8192, "linux", "1.2.26", opener=ok)["purpose"] == "gpu"

    def old_site(req, timeout):
        if "purpose" in json.loads(req.data):
            raise _http_error(422, {"code": "gpu_too_small"})
        return _Resp({"key": "sswk_" + "a" * 43, "label": "Worker 9"})
    assert enroll.enroll("https://x", "PC", "RTX", 8192, "linux", "1.2.26", opener=old_site,
                         purpose="text")["purpose"] == "gpu"          # fell back: an ordinary key


def _keyed(inst, purpose):
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "worker_config.json").write_text(json.dumps({"worker_key": "sswk_" + "t" * 43}))
    (inst / "enrolled.json").write_text(json.dumps({"label": "Worker 9", "purpose": purpose}))


def test_a_text_key_does_not_count_as_a_finished_full_setup(tmp_path):
    _keyed(tmp_path, "text")
    assert setup.text_key(tmp_path) and setup.key_ready(tmp_path)
    assert not setup.artifact_ok(tmp_path, "enroll")
    _keyed(tmp_path, "gpu")
    assert not setup.text_key(tmp_path) and setup.artifact_ok(tmp_path, "enroll")


def test_turning_lines_on_replaces_a_text_key_with_a_gpu_key(tmp_path):
    inst = tmp_path / "inst"
    _keyed(inst, "text")
    asked = []

    def enr(machine_name=None):
        asked.append(1)
        return {"key": "sswk_" + "g" * 43, "label": "Worker 9", "purpose": "gpu", "tier": "public"}

    def run(cmd, e, cwd, on_line):
        (inst / "worker_config.json").write_text(json.dumps({"worker_key": e["SSW_WORKER_KEY"]}))
        return 0
    light = setup.Env(inst, tmp_path, run=run, enroll=enr, light=True, base_env={})
    setup.step_enroll(light)
    assert asked == []                                               # the light setup keeps its text key
    full = setup.Env(inst, tmp_path, run=run, enroll=enr, base_env={})
    setup.step_enroll(full)
    assert asked == [1] and not setup.text_key(inst)
    assert json.loads((inst / "worker_config.json").read_text())["worker_key"].startswith("sswk_g")


def test_me_answer_records_the_kind_of_key(tmp_path):
    ME = {"worker": {"label": "Worker 9", "tier": "public", "purpose": "text"}}
    got = identity.fetch("https://x", "k", opener=lambda req, timeout: _MeResp(ME))
    assert got["purpose"] == "text"
    identity.save(tmp_path, got, 0)
    assert setup.text_key(tmp_path)


class _MeResp(_Resp):
    status = 200
