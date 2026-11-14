"""The worker label shown in the menu, the board and the tray: asked of the site once, cached, never the key."""
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import board, cli, identity, tray  # noqa: E402

KEY = "sswk_" + "B" * 43


class Resp:
    def __init__(self, body, status=200):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Opener:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.requests = body, error, []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.error:
            raise self.error
        return Resp(self.body)


def with_key(tmp_path):
    (tmp_path / "worker_config.json").write_text(json.dumps({"worker_key": KEY, "api_base": "https://x.test"}))


ME = {"worker": {"label": "Dusk Hollow", "tier": "own", "status": "active", "machine_name": "pc", "jobs_done": 3}}


def test_label_is_fetched_with_the_key_as_bearer_and_saved_without_the_key(tmp_path):
    with_key(tmp_path)
    (tmp_path / "enrolled.json").write_text(json.dumps({"worker_id": "w1", "label": None, "tier": None}))
    op = Opener(ME)
    info = identity.refresh(tmp_path, "https://site.test/", opener=op, now=1000.0)
    req, timeout = op.requests[0]
    assert req.full_url == "https://site.test/api/worker/me" and req.get_method() == "GET"
    assert req.get_header("Authorization") == f"Bearer {KEY}" and timeout <= 2.0
    saved = json.loads((tmp_path / "enrolled.json").read_text())
    assert saved["label"] == "Dusk Hollow" and saved["tier"] == "own" and saved["worker_id"] == "w1"
    assert KEY not in (tmp_path / "enrolled.json").read_text() and info["label"] == "Dusk Hollow"


def test_flat_answer_shape_is_accepted_too(tmp_path):
    with_key(tmp_path)
    assert identity.refresh(tmp_path, "https://s.test", opener=Opener({"label": "Ash", "tier": "public"}))["tier"] == "public"


def test_cache_is_reused_for_a_day_then_refreshed(tmp_path):
    with_key(tmp_path)
    op = Opener(ME)
    identity.refresh(tmp_path, "https://s.test", opener=op, now=1000.0)
    identity.refresh(tmp_path, "https://s.test", opener=op, now=1000.0 + 3600)
    assert len(op.requests) == 1
    op2 = Opener({"worker": {"label": "Renamed", "tier": "own"}})
    identity.refresh(tmp_path, "https://s.test", opener=op2, now=1000.0 + identity.REFRESH_SECONDS + 1)
    assert len(op2.requests) == 1 and identity.label_of(tmp_path) == "Renamed"


def test_failure_leaves_the_old_text(tmp_path):
    with_key(tmp_path)
    identity.refresh(tmp_path, "https://s.test", opener=Opener(ME), now=1.0)
    bad = Opener(error=urllib.error.URLError("down"))
    info = identity.refresh(tmp_path, "https://s.test", opener=bad, now=1.0 + identity.REFRESH_SECONDS + 5)
    assert info["label"] == "Dusk Hollow" and identity.describe(tmp_path) == "Dusk Hollow (own)"
    junk = Opener({"nonsense": True})
    assert identity.refresh(tmp_path, "https://s.test", opener=junk, force=True)["label"] == "Dusk Hollow"


def test_no_key_means_no_request(tmp_path):
    op = Opener(ME)
    assert identity.refresh(tmp_path, "https://s.test", opener=op) == {} and op.requests == []
    assert identity.describe(tmp_path) == "(not set up yet - choose Start now)"


def test_unknown_label_with_a_key_says_could_not_ask(tmp_path):
    with_key(tmp_path)
    identity.refresh(tmp_path, "https://s.test", opener=Opener(error=OSError("x")))
    assert identity.describe(tmp_path) == "(could not ask the site yet)"


def test_settings_line_and_other_places_show_the_label(tmp_path):
    with_key(tmp_path)
    cli.ask_site_for_label(tmp_path, opener=Opener(ME))
    assert cli.worker_label(tmp_path) == "Dusk Hollow" and identity.describe(tmp_path) == "Dusk Hollow (own)"
    assert "SpeakStone Worker 1.2.17 - Dusk Hollow" in board.menu_screen(False, None, "Dusk Hollow").plain
    m = tray.build_menu("1.2.17", "Working", False, {}, None, "Dusk Hollow")
    assert m[0].label == "SpeakStone Worker 1.2.17 - Dusk Hollow"
    assert tray.build_menu("1.2.17", "Working", False, {})[0].label == "SpeakStone Worker 1.2.17"
    state = cli.tray_state_reader(cli.control_mod.Control(), {"block_fullscreen": 1}, tmp_path)()
    assert state["label"] == "Dusk Hollow"


def test_board_shows_the_label(tmp_path):
    from rich.console import Console
    from speakstone_worker import stats
    s = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
         "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "idle", "making": False, "workers": 1,
         "label": "Dusk Hollow"}
    con = Console(width=120, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(s))
    assert "v1.2.17 - Dusk Hollow" in con.export_text()


def test_the_key_is_never_in_what_is_shown(tmp_path):
    with_key(tmp_path)
    identity.refresh(tmp_path, "https://s.test", opener=Opener(ME))
    shown = identity.describe(tmp_path) + board.menu_screen(False, None, identity.label_of(tmp_path)).plain
    assert KEY not in shown and KEY not in (tmp_path / "enrolled.json").read_text()
