"""1.2.23 (owner 2026-11-24): the Worker keeps the player's installed SpeakStone add-ons up to date from GitHub releases.
Never reaches GitHub: the release lookup and the download are fakes."""
import json
import os
import shutil
import zipfile

import pytest

from speakstone_worker import addons, settings, wowtext


def make_game(tmp_path, installed, game="_retail_"):
    """A WoW install with these add-ons {name: version} in one game folder."""
    g = tmp_path / "World of Warcraft" / game
    (g / "WTF").mkdir(parents=True)
    for name, ver in installed.items():
        d = g / "Interface" / "AddOns" / name
        d.mkdir(parents=True)
        (d / f"{name}.toc").write_text(f"## Title: {name}\n## Version: {ver}\n", encoding="utf-8")
        (d / "old_only.lua").write_text("-- gone after the update\n", encoding="utf-8")
    (g / "Interface" / "AddOns").mkdir(parents=True, exist_ok=True)
    return g


def make_zip(path, name, version, extra=None, toc_version=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{name}/{name}.toc", f"## Title: {name}\n## Version: {toc_version or version}\n")
        z.writestr(f"{name}/Core.lua", f"-- {name} {version}\n")
        z.writestr(f"{name}/Sounds/a.ogg", b"OggS" + b"\0" * 100)
        for n, data in (extra or {}).items():
            z.writestr(n, data)
    return path


class FakeGitHub:
    """Releases {name: version}; zips are built on download from what is stored for that name."""

    def __init__(self, tmp_path, releases, zips=None):
        self.dir = tmp_path / "gh"
        self.dir.mkdir()
        self.releases, self.asked, self.downloads = releases, [], []
        self.zips = {}
        for name, ver in releases.items():
            self.zips[name] = (zips or {}).get(name) or make_zip(self.dir / f"{name}-v{ver}.zip", name, ver)

    def latest(self, name):
        self.asked.append(name)
        if name not in self.releases:
            return None
        ver = self.releases[name]
        return addons.pick_release(name, {"tag_name": f"v{ver}", "assets": [
            {"name": "release.json", "size": 300, "browser_download_url": "https://github.com/x/release.json"},
            {"name": f"{name}-v{ver}.zip", "size": self.zips[name].stat().st_size,
             "browser_download_url": f"https://github.com/dandwhelan/{name}/releases/download/v{ver}/{name}-v{ver}.zip"},
        ]})

    def download(self, url, dest, expected):
        self.downloads.append(url)
        name = url.rsplit("/", 1)[-1].split("-v")[0]
        shutil.copyfile(self.zips[name], dest)
        return os.path.getsize(dest)

    def update(self, install, game, name, folder, rel, running, log):
        return addons.update_one(install, game, name, folder, rel, running=running, download=self.download, log=log)


def not_running(game):
    return False


def run(tmp_path, game, gh, running=not_running):
    install = tmp_path / "app"
    install.mkdir(exist_ok=True)
    return install, addons.run(install, [game], latest=gh.latest, update=gh.update, running=running)


def version_of(game, name):
    return addons.toc_version((game / "Interface" / "AddOns" / name / f"{name}.toc").read_text(encoding="utf-8"))


def addons_listing(game):
    return sorted(p.name for p in (game / "Interface" / "AddOns").iterdir())


# ---- the happy path
def test_updates_installed_addons_and_nothing_else(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2", "SpeakStone_Pack_Midnight": "6.1.2",
                             "SpeakStone_Pack_Legion": "6.1.3", "OtherAddon": "1.0"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3", "SpeakStone_Pack_Midnight": "6.1.3",
                               "SpeakStone_Pack_Legion": "6.1.3", "SpeakStone_Pack_Classic_Part1": "6.1.3"})
    install, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.3" and version_of(g, "SpeakStone_Pack_Midnight") == "6.1.3"
    assert not (g / "Interface" / "AddOns" / "SpeakStone_Main" / "old_only.lua").exists()   # swapped in whole
    # never installs a pack the player does not have, never asks about other add-ons
    assert addons_listing(g) == ["OtherAddon", "SpeakStone_Main", "SpeakStone_Pack_Legion", "SpeakStone_Pack_Midnight"]
    assert "SpeakStone_Pack_Classic_Part1" not in gh.asked and "OtherAddon" not in gh.asked
    assert gh.asked[0] == "SpeakStone_Main"                                  # base add-on first
    assert len(gh.downloads) == 2                                            # Legion is already current
    r = res["games"]["_retail_"]
    assert r["updated"] == ["SpeakStone_Main to 6.1.3", "SpeakStone_Pack_Midnight to 6.1.3"] and res["updated_base"]
    # no staging or backup left anywhere near AddOns, and none in the data folder either
    assert not (g / addons.GAME_STAGE_NAME).exists()
    assert not any((install / addons.STAGE_NAME).iterdir())


def test_never_downgrades_or_touches_an_equal_version(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Pack_BattleforAzeroth": "6.1.2", "SpeakStone_Main": "6.1.3"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Pack_BattleforAzeroth": "4.1.0", "SpeakStone_Main": "6.1.3"})
    _, res = run(tmp_path, g, gh)
    assert gh.downloads == [] and version_of(g, "SpeakStone_Pack_BattleforAzeroth") == "6.1.2"
    assert res["games"]["_retail_"]["updated"] == []


# ---- WoW running
def test_nothing_is_touched_while_wow_runs_from_that_folder(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2", "SpeakStone_Pack_Midnight": "6.1.2"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3", "SpeakStone_Pack_Midnight": "6.1.3"})
    _, res = run(tmp_path, g, gh, running=lambda game: True)
    assert gh.downloads == [] and version_of(g, "SpeakStone_Main") == "6.1.2"
    assert res["games"]["_retail_"]["waiting"] == ["SpeakStone_Main 6.1.3",
                                                    "SpeakStone_Pack_Midnight 6.1.3 (after the base add-on)"]


def test_wow_started_during_the_download_leaves_the_old_folder(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    calls = []

    def running(game):            # closed when the run starts, open by the time of the swap
        calls.append(game)
        return len(calls) > 1

    _, res = run(tmp_path, g, gh, running=running)
    assert version_of(g, "SpeakStone_Main") == "6.1.2"
    assert (g / "Interface" / "AddOns" / "SpeakStone_Main" / "old_only.lua").exists()
    assert "WoW was started" in res["games"]["_retail_"]["problems"][0]


def test_wow_running_in_matches_the_game_folder_only(tmp_path):
    g = tmp_path / "World of Warcraft" / "_retail_"
    other = tmp_path / "World of Warcraft" / "_forever_"
    assert addons.wow_running_in(g, [("wow.exe", str(g / "Wow.exe"))])
    assert not addons.wow_running_in(other, [("wow.exe", str(g / "Wow.exe"))])
    assert addons.wow_running_in(other, [("wowclassic.exe", None)])       # path unreadable: assume it is this one
    assert not addons.wow_running_in(g, [])


# ---- dev copies are left alone
def test_git_folder_and_marker_are_left_alone(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2", "SpeakStone_Pack_Midnight": "6.1.2"})
    (g / "Interface" / "AddOns" / "SpeakStone_Main" / ".git").mkdir()
    (g / "Interface" / "AddOns" / "SpeakStone_Pack_Midnight" / addons.DEV_MARKER).write_text("", encoding="utf-8")
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3", "SpeakStone_Pack_Midnight": "6.1.3"})
    _, res = run(tmp_path, g, gh)
    assert gh.asked == [] and gh.downloads == []
    assert res["games"]["_retail_"]["skipped"] == ["SpeakStone_Main (a git folder)",
                                                    f"SpeakStone_Pack_Midnight (marked {addons.DEV_MARKER})"]


def test_linked_folder_is_left_alone(tmp_path):
    g = make_game(tmp_path, {})
    real = tmp_path / "clone" / "SpeakStone_Main"
    real.mkdir(parents=True)
    (real / "SpeakStone_Main.toc").write_text("## Version: 6.1.2\n", encoding="utf-8")
    link = g / "Interface" / "AddOns" / "SpeakStone_Main"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("cannot make a link here")
        import subprocess
        if subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True).returncode:
            pytest.skip("cannot make a junction here")
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    _, res = run(tmp_path, g, gh)
    assert gh.downloads == [] and (real / "SpeakStone_Main.toc").read_text(encoding="utf-8") == "## Version: 6.1.2\n"
    assert res["games"]["_retail_"]["skipped"] == ["SpeakStone_Main (a linked folder)"]


def test_unreadable_version_is_left_alone(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "@project-version@"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    _, res = run(tmp_path, g, gh)
    assert gh.downloads == [] and res["games"]["_retail_"]["skipped"] == ["SpeakStone_Main (version not readable)"]


# ---- bad zips are refused and the old add-on stays
@pytest.mark.parametrize("bad", [
    {"SpeakStone_Main/../../evil.lua": "x"},
    {"/abs/evil.lua": "x"},
    {"C:/evil.lua": "x"},
    {"OtherAddon/evil.lua": "x"},
    {"SpeakStone_Main\\..\\evil.lua": "x"},
])
def test_path_traversal_zip_is_refused(tmp_path, bad):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    z = make_zip(tmp_path / "bad.zip", "SpeakStone_Main", "6.1.3", extra=bad)
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"}, zips={"SpeakStone_Main": z})
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.2"
    assert "outside SpeakStone_Main/" in res["games"]["_retail_"]["problems"][0]
    assert not (tmp_path / "evil.lua").exists() and not (g / "evil.lua").exists()
    assert not (g / "Interface" / "evil.lua").exists() and not (g / "Interface" / "AddOns" / "evil.lua").exists()


def test_symlink_entry_is_refused(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    z = make_zip(tmp_path / "link.zip", "SpeakStone_Main", "6.1.3")
    with zipfile.ZipFile(z, "a") as zf:
        info = zipfile.ZipInfo("SpeakStone_Main/link")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"}, zips={"SpeakStone_Main": z})
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.2" and "link" in res["games"]["_retail_"]["problems"][0]


def test_toc_version_must_match_the_release(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    z = make_zip(tmp_path / "w.zip", "SpeakStone_Main", "6.1.3", toc_version="6.1.1")
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"}, zips={"SpeakStone_Main": z})
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.2" and "says 6.1.1" in res["games"]["_retail_"]["problems"][0]


def test_damaged_or_wrong_size_download_is_refused(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    good = gh.zips["SpeakStone_Main"]
    data = bytearray(good.read_bytes())
    i = data.find(b"OggS")
    data[i + 10] ^= 0xFF                                            # same size, bad checksum
    bad = tmp_path / "damaged.zip"
    bad.write_bytes(bytes(data))
    gh.zips["SpeakStone_Main"] = bad
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.2" and "damaged" in res["games"]["_retail_"]["problems"][0]



def test_wrong_size_download_is_refused(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    real = gh.download

    def longer(url, dest, expected):
        n = real(url, dest, expected)
        with open(dest, "ab") as f:
            f.write(b"x")
        return n + 1

    gh.download = longer
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Main") == "6.1.2" and "size" in res["games"]["_retail_"]["problems"][0]


def test_base_failure_holds_back_the_packs(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.0", "SpeakStone_Pack_Midnight": "6.1.0"})
    z = make_zip(tmp_path / "w.zip", "SpeakStone_Main", "6.1.3", toc_version="6.0.0")
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3", "SpeakStone_Pack_Midnight": "6.1.3"},
                    zips={"SpeakStone_Main": z})
    _, res = run(tmp_path, g, gh)
    assert version_of(g, "SpeakStone_Pack_Midnight") == "6.1.0"
    assert res["games"]["_retail_"]["waiting"] == ["SpeakStone_Pack_Midnight 6.1.3 (after the base add-on)"]


# ---- release answers
def test_pick_release_refuses_foreign_links_and_huge_files():
    asset = {"name": "SpeakStone_Main-v6.1.3.zip", "size": 100,
             "browser_download_url": "https://evil.example/SpeakStone_Main-v6.1.3.zip"}
    with pytest.raises(addons.UpdateError):
        addons.pick_release("SpeakStone_Main", {"tag_name": "v6.1.3", "assets": [asset]})
    asset["browser_download_url"] = "http://github.com/x.zip"
    with pytest.raises(addons.UpdateError):
        addons.pick_release("SpeakStone_Main", {"tag_name": "v6.1.3", "assets": [asset]})
    asset["browser_download_url"] = "https://github.com/x.zip"
    asset["size"] = addons.MAX_ZIP_BYTES + 1
    with pytest.raises(addons.UpdateError):
        addons.pick_release("SpeakStone_Main", {"tag_name": "v6.1.3", "assets": [asset]})
    asset["size"] = 100
    rel = addons.pick_release("SpeakStone_Main", {"tag_name": "v6.1.3", "assets": [asset]})
    assert rel == {"version": "6.1.3", "url": "https://github.com/x.zip", "size": 100}
    assert addons.url_allowed("https://release-assets.githubusercontent.com/a")
    assert not addons.url_allowed("https://user:pw@github.com/a")


def test_download_refuses_more_than_expected(tmp_path, monkeypatch):
    class Resp:
        def __init__(self):
            self.left = [b"a" * 10, b"b" * 10]

        def read(self, n):
            return self.left.pop(0) if self.left else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_open(req, timeout=None):
        seen["headers"] = dict(req.header_items())
        return Resp()

    monkeypatch.setattr(addons._OPENER, "open", fake_open)
    with pytest.raises(addons.UpdateError):
        addons.http_download("https://github.com/x.zip", tmp_path / "x.zip", 15)
    assert not any(k.lower() == "authorization" for k in seen["headers"])        # no key is ever sent to GitHub


# ---- a swap cut short is put back
def test_recover_puts_a_moved_out_folder_back(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    install = tmp_path / "app"
    job = install / addons.STAGE_NAME / "SpeakStone_Main-1-2"
    (job / "old").mkdir(parents=True)
    addon = g / "Interface" / "AddOns" / "SpeakStone_Main"
    os.rename(addon, job / "old" / "SpeakStone_Main")                 # power cut between the two renames
    (job / addons.JOB_NAME).write_text(json.dumps({"addons": str(addon.parent), "name": "SpeakStone_Main"}),
                                       encoding="utf-8")
    addons.recover(install, [g])
    assert version_of(g, "SpeakStone_Main") == "6.1.2" and not job.exists()


# ---- setting, Settings lines, and the send runs after an update
def test_setting_on_by_default(tmp_path):
    assert settings.Settings(tmp_path / "cli.json")["update_addons"] is True


def test_status_lines_say_what_happened(tmp_path):
    g = make_game(tmp_path, {"SpeakStone_Main": "6.1.2"})
    gh = FakeGitHub(tmp_path, {"SpeakStone_Main": "6.1.3"})
    install = tmp_path / "app"
    install.mkdir()
    assert "not checked yet" in addons.status_lines(install)[0]
    addons.check_and_update(install, roots=[g], latest=gh.latest, update=gh.update, running=not_running)
    lines = addons.status_lines(install)
    assert lines[0] == "Add-ons (_retail_): updated SpeakStone_Main to 6.1.3"
    addons.check_and_update(install, roots=[g], latest=gh.latest, update=gh.update, running=not_running)
    assert addons.status_lines(install)[0] == "Add-ons (_retail_): up to date"
    assert addons.status_lines(install, enabled=False) == ["Add-on updates: off"]


def test_maybe_update_respects_setting_and_schedule(tmp_path):
    st = settings.Settings(tmp_path / "cli.json")
    install = tmp_path / "app"
    install.mkdir()
    calls = []

    def runner(inst, folder, log):
        calls.append(1)
        addons.save_state(inst, {"last_check": 1e6, "errors": 0, "waiting": 0, "games": {}})
        return {"games": {"_retail_": {"updated": ["SpeakStone_Main to 6.1.3"]}}, "updated_base": True, "errors": 0}

    assert addons.maybe_update(install, st, now=1e6, runner=runner) is True
    assert addons.maybe_update(install, st, now=1e6 + 60, runner=runner) is False        # not due again yet
    assert addons.maybe_update(install, st, now=1e6 + addons.CHECK_EVERY + 1, runner=runner) is True
    st["update_addons"] = False
    assert addons.maybe_update(install, st, now=1e12, runner=runner) is False
    assert len(calls) == 2


def test_send_runs_straight_after_a_base_update(tmp_path):
    st = settings.Settings(tmp_path / "cli.json")
    install = tmp_path / "app"
    install.mkdir()
    wowtext.save_sent(install, set())                                   # the send ran a moment ago
    order = []

    class Stop:
        n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

    import speakstone_worker.wowtext as wt
    orig = wt.collect_and_send
    wt.collect_and_send = lambda *a, **k: (order.append("send"),
                                           {"files": 0, "entries": 0, "uploads": 0, "errors": []})[1]
    try:
        wowtext.auto_loop(install, st, lambda: "https://site", lambda: "key", stop=Stop(), sleep=lambda s: None,
                          before=lambda: (order.append("update"), True)[1])
    finally:
        wt.collect_and_send = orig
    assert order == ["update", "send"]                                  # forced: not waiting the 3 hours
