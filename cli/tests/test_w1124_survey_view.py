"""Settings "What it found" is a table, one row per game (owner 2026-11-24: easier to read and nicer to view)."""
import io

from rich.console import Console

from speakstone_worker import cli, wowtext

ROWS = [
    {"folder": r"C:\WoW", "game": "_retail_", "name": "Retail", "test_realm": False, "addons": {"retail": "6.1.3"},
     "packs": 16, "files": [{"account": "ACC[1]", "flavour": "retail", "entries": 22, "waiting": 3, "error": ""}]},
    {"folder": r"C:\WoW", "game": "_classic_era_", "name": "Classic Era", "test_realm": False, "addons": {},
     "packs": 0, "files": []},
]


def text(view):
    c = Console(width=120, file=io.StringIO(), record=True, color_system=None)
    c.print(view)
    return c.export_text()


def test_table_shows_each_game_version_and_counts(tmp_path):
    out = text(cli.wow_survey_view(tmp_path, {"wow_folder": "", "update_addons": True}, surveyor=lambda *a: ROWS))
    assert "What it found" in out and r"in C:\WoW" in out
    retail = next(l for l in out.splitlines() if "Retail" in l)
    for bit in ("6.1.3", "16", "22", "3"):
        assert bit in retail
    assert "not supported" in next(l for l in out.splitlines() if "Classic Era" in l) and "ACC[1]" not in out
    assert "Last upload" in out and "none yet" in out


def test_upload_line_uses_the_game_name_and_splits_the_link(tmp_path):
    wowtext.log_upload(tmp_path, "_forever_ / ACC#1: sent 2 entries, the site took 0 new lines (9 already known)  "
                                 "https://speakstone.beanw.co.uk/s/abc")
    out = text(cli.wow_survey_view(tmp_path, {"wow_folder": "", "update_addons": False}, surveyor=lambda *a: ROWS))
    line = next(l for l in out.splitlines() if "Last upload" in l)
    assert "WoW Forever: sent 2 entries" in line and "ACC#1" not in line and "https://" not in line
    assert "https://speakstone.beanw.co.uk/s/abc" in out and "Add-on updates (off)" in out


def test_no_folder_and_a_failing_look(tmp_path):
    st = {"wow_folder": "", "update_addons": True}
    assert "No WoW folder found" in text(cli.wow_survey_view(tmp_path, st, surveyor=lambda *a: []))

    def boom(*a):
        raise OSError("x")
    assert "Could not look" in text(cli.wow_survey_view(tmp_path, st, surveyor=boom))


def test_addon_update_cell():
    assert "up to date" in cli._addon_update_cell({"checked": True})
    assert "updated 2 add-ons to 6.1.3" in cli._addon_update_cell(
        {"updated": ["SpeakStone_Main to 6.1.3", "SpeakStone_Pack_X to 6.1.3"]})
    assert "waiting for WoW" in cli._addon_update_cell({"waiting": ["a"]})


def test_only_retail_and_forever_are_supported(tmp_path):
    rows = [dict(ROWS[0], game="_ptr_", name="Retail PTR"), dict(ROWS[0], game="_forever_", name="WoW Forever",
                                                                 addons={}, files=[])]
    out = text(cli.wow_survey_view(tmp_path, {"wow_folder": "", "update_addons": True}, surveyor=lambda *a: rows))
    ptr = next(l for l in out.splitlines() if "Retail PTR" in l)
    assert "not supported" in ptr and "6.1.3" not in ptr
    assert "not installed" in next(l for l in out.splitlines() if "WoW Forever" in l)
