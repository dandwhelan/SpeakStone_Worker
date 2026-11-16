"""Menu [4] / Settings [W]: the add-on's captured text read from the WoW folder and sent to the site."""
from speakstone_worker import wowtext

SV = r'''
SpeakStone_MainDB = {
["settings"] = {
["autoPlay"] = true,
},
["harvest"] = {
["quests"] = {
[5650] = {
["completion"] = {
["text"] = "You have passed my test.\n\nSoon, \"champion\".",
["npcID"] = 3044,
},
},
},
["gossip"] = {
["npc2104"] = {
["texts"] = {
"Our town's under the ocean.", -- [1]
},
},
},
["chars"] = {
{
["playerClass"] = "PRIEST",
}, -- [1]
},
["sent"] = {
["abc"] = true,
},
},
}
SpeakStone_OtherDB = nil
'''


def make_wow(tmp_path, text=SV, game="_retail_", fname="SpeakStone_Main.lua"):
    root = tmp_path / "World of Warcraft"
    sv = root / game / "WTF" / "Account" / "123#1" / "SavedVariables"
    sv.mkdir(parents=True)
    (sv / fname).write_text(text, encoding="utf-8")
    (root / game / "WTF" / "Config.wtf").write_text('SET textLocale "enUS"\n', encoding="utf-8")
    toc = root / game / "Interface" / "AddOns" / "SpeakStone_Main"
    toc.mkdir(parents=True)
    (toc / "SpeakStone_Main.toc").write_text("## Interface: 110207\n## Version: 5.0.8\n", encoding="utf-8")
    return root


def test_parse_reads_what_wow_writes():
    d = wowtext.parse_saved_variables(SV)
    h = d["SpeakStone_MainDB"]["harvest"]
    assert h["quests"][5650]["completion"]["text"] == 'You have passed my test.\n\nSoon, "champion".'
    assert h["gossip"]["npc2104"]["texts"][1] == "Our town's under the ocean."
    assert h["chars"][1]["playerClass"] == "PRIEST"
    assert d["SpeakStone_OtherDB"] is None


def test_export_round_trips_and_skips_bookkeeping():
    h = wowtext.parse_saved_variables(SV)["SpeakStone_MainDB"]["harvest"]
    batches, prints, skipped = wowtext.build_batches(h, {"locale": "enUS", "flavour": "retail"}, set(), "retail")
    assert len(batches) == 1 and len(prints[0]) == 2 and skipped == 0
    back = wowtext.parse_saved_variables(batches[0])["SpeakStone_MainExport"]
    assert back["quests"][5650] == h["quests"][5650]
    assert back["gossip"] == h["gossip"]
    assert back["locale"] == "enUS" and back["chars"][1]["playerClass"] == "PRIEST"
    assert "sent" not in back                     # the add-on's own bookkeeping never leaves the PC


def test_big_store_is_cut_into_valid_pieces():
    h = {"quests": {i: {"description": {"text": "x" * 500}} for i in range(1, 201)}}
    batches, prints, _ = wowtext.build_batches(h, {"locale": "enUS"}, set(), "retail", max_bytes=20_000)
    assert len(batches) > 1
    assert sum(len(p) for p in prints) == 200
    seen = set()
    for b in batches:
        assert len(b.encode()) <= 21_000
        seen |= set(wowtext.parse_saved_variables(b)["SpeakStone_MainExport"]["quests"])
    assert seen == set(range(1, 201))


def test_finds_files_sends_once_then_only_what_changed(tmp_path):
    root = make_wow(tmp_path)
    install = tmp_path / "app"
    install.mkdir()
    sent = []
    poster = lambda base, key, src: (sent.append(src), (True, {"new": 2, "known": 0}))[1]
    s = wowtext.collect_and_send(install, "https://site", "k", poster=poster, roots=[root], say=lambda *_: None)
    assert s["files"] == 1 and s["entries"] == 2 and s["uploads"] == 1
    assert '["flavour"] = "retail"' in sent[0] and '["addonVersion"] = "5.0.8"' in sent[0]

    s = wowtext.collect_and_send(install, "https://site", "k", poster=poster, roots=[root], say=lambda *_: None)
    assert s["uploads"] == 0 and s["skipped"] == 2

    sv = next(root.rglob("SpeakStone_Main.lua"))
    sv.write_text(SV.replace('"Our town', '"Brand new line.", -- [2]\n"Our town'), encoding="utf-8")
    s = wowtext.collect_and_send(install, "https://site", "k", poster=poster, roots=[root], say=lambda *_: None)
    assert s["entries"] == 1 and s["skipped"] == 1    # only the gossip entry that grew goes again


def test_failed_upload_is_tried_again(tmp_path):
    root = make_wow(tmp_path)
    install = tmp_path / "app"
    install.mkdir()
    s = wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (False, "Could not reach the site."),
                                 roots=[root], say=lambda *_: None)
    assert s["uploads"] == 0 and s["errors"]
    s = wowtext.collect_and_send(install, "https://site", "k", poster=lambda *a: (True, {}), roots=[root],
                                 say=lambda *_: None)
    assert s["entries"] == 2


def test_forever_file_is_marked_forever(tmp_path):
    root = make_wow(tmp_path, SV.replace("SpeakStone_MainDB", "SpeakStone_ForeverMainDB"), "_forever_",
                    "SpeakStone_Forever_Main.lua")
    install = tmp_path / "app"
    install.mkdir()
    got = []
    wowtext.collect_and_send(install, "https://site", "k", poster=lambda b, k, src: (got.append(src), (True, {}))[1],
                             roots=[root], say=lambda *_: None)
    assert got and '["flavour"] = "forever"' in got[0]


def test_nothing_installed_says_so(tmp_path):
    said = []
    s = wowtext.collect_and_send(tmp_path, "https://site", "k", poster=None, roots=[], say=said.append)
    assert s["files"] == 0 and "No SpeakStone saved text found" in said[0]
