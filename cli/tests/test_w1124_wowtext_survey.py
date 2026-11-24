"""1.2.23 (owner 2026-11-24): send captured WoW text by default, show what was found, and the scan/parse fixes."""
import json
import shutil
import time

from speakstone_worker import settings, wowtext
from test_wowtext import SV, make_wow


# ---- default on, and switched on once for PCs that saved the old default
def test_new_install_sends_by_default(tmp_path):
    st = settings.Settings(tmp_path / "cli.json")
    assert st["send_wow_text"] is True
    assert not (tmp_path / "cli.json").exists()          # nothing written just for reading the settings


def test_old_saved_off_is_switched_on_once(tmp_path):
    p = tmp_path / "cli.json"
    p.write_text(json.dumps({"send_wow_text": False, "idle_minutes": 7}), encoding="utf-8")
    st = settings.Settings(p)
    assert st["send_wow_text"] is True and st["idle_minutes"] == 7
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["send_wow_text"] is True and saved["wow_text_on_v2"] is True
    st["send_wow_text"] = False                           # the player turns it off again afterwards
    assert settings.Settings(p)["send_wow_text"] is False  # and it stays off


# ---- what the Settings screen shows
def test_survey_lists_each_game_with_version_and_waiting(tmp_path):
    root = make_wow(tmp_path)
    fv = make_wow(tmp_path / "f", SV.replace("SpeakStone_MainDB", "SpeakStone_ForeverMainDB"), "_forever_",
                  "SpeakStone_Forever_Main.lua")
    shutil.rmtree(fv / "_forever_" / "Interface" / "AddOns" / "SpeakStone_Main")   # make_wow adds a retail toc
    toc = fv / "_forever_" / "Interface" / "AddOns" / "SpeakStone_Forever_Main"
    toc.mkdir(parents=True)
    (toc / "SpeakStone_Forever_Main.toc").write_text("## Version: 6.1.2\n", encoding="utf-8")
    (fv / "_forever_" / "Interface" / "AddOns" / "SpeakStone_Forever_Chatter").mkdir()
    (fv / "_forever_" / "Interface" / "AddOns" / "SpeakStone_Forever_Main.old").mkdir()
    (root / "_classic_era_" / "WTF").mkdir(parents=True)
    install = tmp_path / "app"
    install.mkdir()

    rows = wowtext.survey(install, roots=[root, fv / "_forever_"])
    by = {r["game"]: r for r in rows}
    assert by["_retail_"]["addons"] == {"retail": "5.0.8"}
    assert by["_retail_"]["files"][0]["entries"] == 2 and by["_retail_"]["files"][0]["waiting"] == 2
    assert by["_forever_"]["addons"] == {"forever": "6.1.2"} and by["_forever_"]["packs"] == 1
    assert by["_classic_era_"]["addons"] == {}
    text = "\n".join(wowtext.survey_lines(rows))
    assert "Retail (_retail_): SpeakStone 5.0.8" in text and "WoW Forever (_forever_): SpeakStone Forever 6.1.2" in text
    assert "2 captured, 2 waiting to be sent" in text and "Classic Era (_classic_era_): no SpeakStone add-on" in text

    wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (True, {}), roots=[root], say=lambda *_: None)
    by = {r["game"]: r for r in wowtext.survey(install, roots=[root])}
    assert by["_retail_"]["files"][0]["waiting"] == 0


def test_test_realm_text_is_never_sent(tmp_path):
    root = make_wow(tmp_path, game="_ptr_")
    install = tmp_path / "app"
    install.mkdir()
    s = wowtext.collect_and_send(install, "https://site", "k", poster=None, roots=[root], say=lambda *_: None)
    assert s["files"] == 0
    assert "test realm" in "\n".join(wowtext.survey_lines(wowtext.survey(install, roots=[root])))


# ---- performance
def test_no_wow_is_remembered_so_the_disks_are_not_searched_every_5_minutes(tmp_path):
    wowtext.collect_and_send(tmp_path, "https://site", "k", poster=None, roots=[], say=lambda *_: None)
    assert time.time() - wowtext.last_run(tmp_path) < 60


def test_parser_reads_a_big_file_quickly():
    body = "".join(f'[{i}] = {{ ["description"] = {{ ["text"] = "{"word " * 200}\\226\\128\\148 \\"q\\"\\n" }} }},\n'
                   for i in range(1, 3001))
    text = 'SpeakStone_MainDB = { ["harvest"] = { ["quests"] = {\n' + body + "} } }\n"
    t = time.perf_counter()
    d = wowtext.parse_saved_variables(text)
    assert time.perf_counter() - t < 3.0                 # was ~10x slower one character at a time
    q = d["SpeakStone_MainDB"]["harvest"]["quests"]
    assert len(q) == 3000 and q[7]["description"]["text"].endswith('\xe2\x80\x94 "q"\n')


def test_harvest_is_parsed_once_per_file_version(tmp_path, monkeypatch):
    root = make_wow(tmp_path)
    path = next(root.rglob("SpeakStone_Main.lua"))
    calls = []
    real = wowtext.parse_saved_variables
    monkeypatch.setattr(wowtext, "parse_saved_variables", lambda t: (calls.append(1), real(t))[1])
    wowtext.read_harvest(path, "SpeakStone_MainDB")
    wowtext.read_harvest(path, "SpeakStone_MainDB")
    assert len(calls) == 1


# ---- clearing what was sent: the receipt the add-on reads at login (Harvester.lua ApplyWorkerReceipt)
def test_hash_matches_the_addon():
    # the same figures tests/lua/worker_receipt_test.lua checks against the add-on's own HashText
    assert wowtext.sent_key("q", 5650, "completion", "You have passed my test.") == 2030421999
    assert wowtext.sent_key("g", "npc2104", "Our town’s under the ocean.") == 1032111830
    assert wowtext.sent_key("s", "63_description", 3846) == wowtext.sent_key("s", "63_description", 3846.0)


def _with_receipt(root, game="_retail_", folder="SpeakStone_Main"):
    p = root / game / "Interface" / "AddOns" / folder / "WorkerReceipt.lua"
    p.write_text("-- ships empty\n", encoding="utf-8")
    return p


def _receipt_keys(p):
    d = wowtext.parse_saved_variables(p.read_text(encoding="utf-8"))["SpeakStone_WorkerReceipt"]
    return set(d["sent"].values())


def test_confirmed_lines_go_in_the_receipt(tmp_path):
    root = make_wow(tmp_path)
    rec = _with_receipt(root)
    install = tmp_path / "app"
    install.mkdir()
    s = wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (True, {}), roots=[root],
                                 say=lambda *_: None)
    assert s["receipts"] == 1
    assert _receipt_keys(rec) == {
        wowtext.sent_key("q", 5650, "completion", 'You have passed my test.\n\nSoon, "champion".'),
        wowtext.sent_key("g", "npc2104", "Our town's under the ocean."),
    }
    text = rec.read_text(encoding="utf-8")
    assert all(ch in "0123456789,{}=_ \n" or ch.isalpha() for ch in text.split("SpeakStone_WorkerReceipt", 1)[1])


def test_failed_upload_clears_nothing(tmp_path):
    root = make_wow(tmp_path)
    rec = _with_receipt(root)
    install = tmp_path / "app"
    install.mkdir()
    wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (False, "Could not reach the site."),
                             roots=[root], say=lambda *_: None)
    assert _receipt_keys(rec) == set()


def test_older_addon_without_placeholder_gets_no_file(tmp_path):
    root = make_wow(tmp_path)
    install = tmp_path / "app"
    install.mkdir()
    said = []
    s = wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (True, {}), roots=[root],
                                 say=said.append)
    assert s["receipts"] == 0 and not list(root.rglob("WorkerReceipt.lua"))
    assert any("update the SpeakStone add-on" in x for x in said)


def test_speakers_are_sent_too():
    h = {"speakers": {"63_description": {"npcID": 3846, "npcName": "Talen"}}}
    batches, prints, _ = wowtext.build_batches(h, {"locale": "enUS"}, set(), "retail")
    assert len(prints[0]) == 1
    assert wowtext.parse_saved_variables(batches[0])["SpeakStone_MainExport"]["speakers"]["63_description"]["npcID"] == 3846


def test_only_a_real_site_reply_counts_as_sent():
    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return self.body
    ok, _ = wowtext.post_export("https://site", "k", "x", opener=lambda req, timeout: R(b'{"hello": 1}'))
    assert not ok
    ok, counts = wowtext.post_export("https://site", "k", "x",
                                     opener=lambda req, timeout: R(b'{"submission_id": "s", "counts": {"new": 3}}'))
    assert ok and counts == {"new": 3, "submission_id": "s"}


# ---- the upload log (Settings [L]): what was sent, with a link to the site's page for it
def test_each_upload_and_failure_is_logged_with_its_link(tmp_path):
    root = make_wow(tmp_path)
    install = tmp_path / "app"
    install.mkdir()
    wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (False, "Could not reach the site."),
                             roots=[root], say=lambda *_: None)
    wowtext.collect_and_send(install, "https://site/", "k",
                             poster=lambda *a: (True, {"new": 1, "known": 1, "submission_id": "abc-123"}),
                             roots=[root], say=lambda *_: None)
    log = wowtext.recent_uploads(install)
    assert len(log) == 2
    assert "NOT SENT, 2 entries kept for next time (Could not reach the site.)" in log[0]
    assert "_retail_ / 123#1: sent 2 entries, the site took 1 new lines (1 already known)" in log[1]
    assert log[1].endswith("https://site/s/abc-123")


def test_a_strange_submission_id_never_becomes_a_link():
    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return self.body
    ok, res = wowtext.post_export("https://site", "k", "x",
                                  opener=lambda req, timeout: R(b'{"submission_id": "../evil?x=1", "counts": {}}'))
    assert ok and "submission_id" not in res


def test_windows_line_breaks_read_as_before(tmp_path):
    root = make_wow(tmp_path)
    path = next(root.rglob("SpeakStone_Main.lua"))
    path.write_bytes(SV.replace("\\n\\n", "\\\n\\\n").replace("\n", "\r\n").encode("utf-8"))
    h = wowtext.read_harvest(path, "SpeakStone_MainDB")
    assert h["quests"][5650]["completion"]["text"].startswith("You have passed my test.\n\nSoon")
