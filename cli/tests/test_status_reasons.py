"""1.2.14: the board and the tray say WHY the worker is idle, and a yielding worker still tells the site it is alive.
Real parsing of small fake worker logs in tmp_path; the site and the clock are fakes."""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from speakstone_worker import board, cli, stats
from speakstone_worker import control as control_mod

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import net_worker as nw  # noqa: E402

NOW = 1_800_000_000.0


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_log(logs, rows):
    """rows: (seconds before NOW, 'event job=- ...') -> worker_2027-01-15.log"""
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "worker_2027-01-15.log").write_text(
        "".join(f"{stamp(NOW - ago)} {text}\n" for ago, text in rows), encoding="utf-8")


def summary(tmp_path, rows):
    write_log(tmp_path / "logs", rows)
    return stats.summarize(stats.read_events(tmp_path / "logs"), NOW)


def kinds(s):
    return [e[1] for e in s["events"]]


# ------------------------------------------------------------------ 1. the board
def test_idle_reason_is_the_last_idle_and_a_claim_clears_it(tmp_path):
    s = summary(tmp_path, [(100, "idle job=- reason=none wait=20"), (80, "idle job=- reason=vram free_mib=10 need_mib=99")])
    assert s["idle_reason"] == "vram" and not s["making"]
    s = summary(tmp_path, [(100, "idle job=- reason=yield wait=20"), (50, "claim job=abc attempt=1 lines=5")])
    assert s["idle_reason"] is None and s["making"]
    s = summary(tmp_path, [(100, "idle job=- reason=yield wait=20"), (50, "resume job=abc lines=5")])
    assert s["idle_reason"] is None
    s = summary(tmp_path, [(100, "idle job=- reason=yield wait=20"), (50, "run_start job=abc")])
    assert s["idle_reason"] is None
    assert summary(tmp_path, [(100, "start job=-")])["idle_reason"] is None


def test_yield_logs_one_paused_line_then_one_resumed_line(tmp_path):
    rows = [(1000 - 20 * i, "idle job=- reason=yield wait=20") for i in range(30)] + [(300, "claim job=abc lines=4")]
    s = summary(tmp_path, rows)
    assert kinds(s).count("paused") == 1 and kinds(s).count("resumed") == 1
    assert kinds(s)[0] == "resumed" and kinds(s)[1] == "paused"            # newest first
    still = summary(tmp_path, rows[:-1])
    assert kinds(still) == ["paused"] and still["idle_reason"] == "yield"
    # the yield ended by a normal empty-queue idle (not a claim) is a resume as well
    s = summary(tmp_path, [(100, "idle job=- reason=yield wait=20"), (50, "idle job=- reason=none wait=20")])
    assert kinds(s) == ["resumed", "paused"] and s["idle_reason"] == "none"


def test_update_required_still_adds_its_update_event_and_stops_making(tmp_path):
    s = summary(tmp_path, [(100, "claim job=abc lines=2"), (50, "idle job=- reason=update_required wait=60"),
                           (30, "idle job=- reason=update_required wait=60")])
    assert kinds(s).count("update") == 1 and s["idle_reason"] == "update_required" and not s["making"]


@pytest.mark.parametrize("reason,text", [
    ("yield", "Paused: another program on this PC is using the graphics card. It carries on by itself."),
    ("vram", "Waiting: the graphics card's memory is in use by another program."),
    ("update_required", "This version is too old for new work. Updating..."),
    ("none", "Waiting for work..."), (None, "Waiting for work..."), ("odd", "Waiting for work..."),
])
def test_board_text_per_reason(reason, text):
    assert board.WAIT_TEXT.get(reason, "Waiting for work...") == text


def test_board_renders_the_reason(tmp_path):
    from rich.console import Console
    write_log(tmp_path / "logs", [(100, "idle job=- reason=yield wait=20")])
    s = cli.board_state(tmp_path, None, None, "working", NOW, now=NOW, events=stats.read_events(tmp_path / "logs"))
    con = Console(width=140, height=50, record=True, force_terminal=False)
    con.print(board.render_board(s))
    out = " ".join(con.export_text().split())
    assert "Paused: another program on this PC is using the graphics card" in out and "Waiting for work..." not in out


# ------------------------------------------------------------------ 2. the tray
def test_tray_never_says_making_when_the_foreign_worker_only_idles():
    f = control_mod.status_for
    # the bug: a worker from the app window that was idle on yield showed "Making lines (started in the app window)"
    assert f(True, False, False, foreign=True, idle_reason="yield") == "Paused for another program using the graphics card"
    assert f(True, False, False, foreign=True, idle_reason="vram") == "Waiting for the graphics card"
    assert f(True, False, False, foreign=True, idle_reason="update_required") == "Updating to a newer version"
    assert f(True, False, False, foreign=True, idle_reason="none") == "Waiting for work"
    assert f(True, False, False, foreign=True) == "Waiting for work"
    assert f(True, False, False, idle_reason="yield") == "Paused for another program using the graphics card"
    assert f(True, True, False, foreign=True, lines=3) == "Making lines (started in the app window) - 3 lines in this job"
    assert f(True, True, False) == "Working"


def test_tray_status_from_a_real_log(tmp_path):
    logs = tmp_path / "work" / "logs"
    write_log(logs, [(900, "claim job=abc lines=5"), (800, "run_start job=abc"), (700, "idle job=abc reason=yield"),
                     (20, "idle job=- reason=yield wait=20")])
    assert cli.job_running_now(tmp_path, now=NOW) is False
    assert cli.idle_reason_now(tmp_path, now=NOW) == "yield"
    write_log(logs, [(900, "claim job=abc lines=5"), (800, "run_start job=abc")])
    assert cli.job_running_now(tmp_path, now=NOW) is True and cli.idle_reason_now(tmp_path, now=NOW) is None


# ------------------------------------------------------------------ 3. presence ping
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ping_worker(tmp_path, monkeypatch, fail=False):
    cfg = nw.Config()
    cfg.work_dir = (tmp_path / "work").resolve()
    cfg.worker_key = "k"
    cfg.api_base = "https://site.invalid"
    w = nw.Worker(cfg, log=nw.Log(tmp_path / "logs", echo=False), skip_vram_wait=True, install_signals=False)
    w.clock = Clock()
    calls = []

    def fake_http(method, url, headers=None, data=None, timeout=30, max_bytes=0, resp_headers=None):
        calls.append((method, url, headers.get("Authorization")))
        if fail:
            raise OSError("down")
        return 200, b"{}"
    monkeypatch.setattr(nw, "http_request", fake_http)
    return w, calls


def test_presence_pings_once_after_240_seconds(tmp_path, monkeypatch):
    w, calls = ping_worker(tmp_path, monkeypatch)
    assert nw.PRESENCE_SECONDS == 240
    w.presence()                                    # first idle moment: starts the clock, no call
    w.clock.t += 239
    w.presence()
    assert calls == []
    w.clock.t += 1
    w.presence()
    assert calls == [("GET", "https://site.invalid/api/worker/me?present=1", "Bearer k")]
    w.clock.t += 100
    w.presence()
    assert len(calls) == 1                          # not again until another 240 s
    w.clock.t += 140
    w.presence()
    assert len(calls) == 2


def test_presence_swallows_errors_and_logs_one_ping_failed_per_30_minutes(tmp_path, monkeypatch):
    w, calls = ping_worker(tmp_path, monkeypatch, fail=True)
    w.presence()
    for _ in range(12):                             # 12 failing pings over 48 minutes
        w.clock.t += 240
        w.presence()
    assert len(calls) == 12
    log = (tmp_path / "logs").glob("worker_*.log")
    text = "".join(f.read_text(encoding="utf-8") for f in log)
    assert text.count(" ping_failed ") == 2         # at 4 min and again 30+ minutes later


def test_presence_is_silent_when_the_user_stopped_or_paused(tmp_path, monkeypatch):
    w, calls = ping_worker(tmp_path, monkeypatch)
    w.presence()
    w.work.mkdir(parents=True, exist_ok=True)
    (w.work / nw.DRAIN_FILE).write_text("1", encoding="utf-8")
    w.clock.t += 600
    w.presence()
    assert calls == []
    (w.work / nw.DRAIN_FILE).unlink()
    monkeypatch.setattr(w, "stop_requested", lambda: True)
    w.presence()
    assert calls == []


def test_yield_loop_pings_through_the_wait(tmp_path, monkeypatch):
    w, calls = ping_worker(tmp_path, monkeypatch)
    n = {"i": 0}

    def sleep(s, ctx=None):
        n["i"] += 1
        w.clock.t += s
    w.sleep_impl = sleep
    monkeypatch.setattr(w, "yield_busy", lambda force=False: n["i"] < 30)    # 30 x 20 s = 10 minutes
    w.wait_for_vram()
    assert len(calls) == 2 and all(c[1].endswith("/api/worker/me?present=1") for c in calls)
    assert w._ping_at is None                       # the next spell starts its own clock
