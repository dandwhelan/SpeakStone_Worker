"""1.2.9: jobs may carry up to 40 reference clips; a worker the site calls too old asks the app to update at once."""
import importlib.util
import sys
from pathlib import Path

from speakstone_worker import cli

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("net_worker_129", ROOT / "net_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_reference_limit_is_forty():
    nw = load_worker()
    assert nw.MAX_REFS_PER_JOB == 40
    text = (ROOT / "net_worker.py").read_text(encoding="utf-8")
    assert "len(refs) <= MAX_REFS_PER_JOB" in text and "len(refs) <= 8" not in text


class R:
    def __init__(self, work):
        self.work_dir = work


class Group:
    def __init__(self, *runners):
        self.runners = list(runners)


def test_update_note_is_taken_once(tmp_path):
    (tmp_path / "work").mkdir()
    r = R(tmp_path / "work")
    assert cli.take_update_request(r) is False
    (tmp_path / "work" / cli.UPDATE_FILE).write_text("1.2.9", encoding="utf-8")
    assert cli.take_update_request(Group(R(tmp_path / "none"), r)) is True
    assert cli.take_update_request(r) is False and not (tmp_path / "work" / cli.UPDATE_FILE).exists()


def test_update_note_never_raises():
    assert cli.take_update_request(object()) is False
    assert cli.take_update_request(None) is False


def test_same_note_name_in_worker_and_app():
    assert load_worker().UPDATE_FILE == cli.UPDATE_FILE


class FakeCtx:
    def __init__(self):
        self.loaded = []

    def load_verify_locations(self, cafile=None):
        self.loaded.append(cafile)


class FakeSsl:
    def __init__(self):
        self.made = []
        self._create_default_https_context = None

    def create_default_context(self, *a, **kw):
        c = FakeCtx()
        self.made.append(c)
        return c


def test_every_https_context_also_trusts_the_shipped_list():
    for mod in (cli, load_worker()):
        fake = FakeSsl()
        assert mod.trust_bundled_certificates(fake, "C:/x/cacert.pem") is True
        ctx = fake._create_default_https_context()
        assert ctx.loaded == ["C:/x/cacert.pem"]            # system store (the default context) plus the shipped list


def test_a_bad_list_does_not_break_connections():
    class Bad(FakeCtx):
        def load_verify_locations(self, cafile=None):
            raise OSError("unreadable")

    class S(FakeSsl):
        def create_default_context(self, *a, **kw):
            return Bad()
    fake = S()
    assert cli.trust_bundled_certificates(fake, "nope.pem") is True
    assert isinstance(fake._create_default_https_context(), Bad)


def test_the_real_list_is_there():
    import certifi
    import ssl
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=certifi.where())       # raises if the shipped list is unreadable


from speakstone_worker import __version__, update  # noqa: E402


class UFlow:
    def __init__(self, release=None, error=None, can=(True, ""), note="Updating..."):
        self._release, self._error, self._can, self._note = release, error, can, note
        self.logged, self.applied, self.forced, self.next_check = [], 0, False, 99.0
        self.log = self.logged.append

    def check(self):
        assert self.next_check == 0.0                       # asked to look NOW, not at the next six-hourly check
        if self._error:
            self.log("update check: " + self._error)
        return self._release

    def can_apply(self):
        return self._can

    def apply(self):
        self.applied += 1
        return self._note


REL = update.Release("9.9.9", "u", "s")


def test_update_now_says_latest_or_why_it_could_not_look(monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    assert cli.update_now(UFlow()) == f"You have the latest version ({__version__})."
    f = UFlow(error="could not reach the release page (timed out)")
    assert cli.update_now(f) == "Could not look for updates: could not reach the release page (timed out)."
    assert f.logged and f.log == f.logged.append            # the log line is still written; the log is put back


def test_update_now_installs_or_waits_for_the_job(monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    f = UFlow(REL)
    assert cli.update_now(f) == "Updating..." and f.applied == 1
    f = UFlow(REL)
    assert "after the job in hand" in cli.update_now(f, working=True) and f.applied == 0 and f.forced is True
    f = UFlow(REL, can=(False, "running from source"))
    assert "9.9.9 is available (running from source)" in cli.update_now(f) and f.applied == 0


def test_update_now_when_switched_off(monkeypatch):
    monkeypatch.setenv("SSW_NO_UPDATE", "1")
    assert cli.update_now(UFlow(REL)) == "Updates are switched off on this PC."
    monkeypatch.delenv("SSW_NO_UPDATE", raising=False)
    assert cli.update_now(None) == "Updates are switched off on this PC."
