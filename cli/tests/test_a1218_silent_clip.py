"""A clip with no sound in it is held, and no message to the site can carry an infinite figure.

2026-11-14: one line ("Ugh...") came out as 0.4 s of silence. Its loudness measured as minus infinity, the worker
wrote -Infinity into its final message, the site answered HTTP 400 and the whole 29-line job failed on three machines."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import net_worker as nw  # noqa: E402


def test_infinite_figures_never_reach_the_site():
    body = {"manifest": {"lines": [{"i": 0, "measured_lufs": float("-inf"), "rtf": float("nan"), "duration_sec": 0.4,
                                    "emo": [0, 0.6, float("inf")]}]}}
    clean = nw._finite(body)
    text = json.dumps(clean, allow_nan=False)                      # raises if anything infinite is left
    line = json.loads(text)["manifest"]["lines"][0]
    assert line["measured_lufs"] is None and line["rtf"] is None and line["emo"] == [0, 0.6, None]
    assert line["duration_sec"] == 0.4
    assert body["manifest"]["lines"][0]["measured_lufs"] == float("-inf")      # the caller's own data is untouched


def test_post_sends_valid_json(monkeypatch):
    sent = {}

    def fake(method, url, headers, data=None, timeout=None, resp_headers=None):
        sent["data"] = data
        return 200, b"{}"
    monkeypatch.setattr(nw, "http_request", fake)
    api = nw.Api.__new__(nw.Api)
    api.base, api.timeout = "https://example.invalid", 5
    api._headers = lambda extra=None: dict(extra or {})
    api._finish = lambda s, b, h: (s, {})
    api.post("complete", {"manifest": {"x": float("-inf")}})
    assert b"Infinity" not in sent["data"] and json.loads(sent["data"])["manifest"]["x"] is None


def test_the_engine_script_holds_a_silent_clip():
    src = (ROOT / "indextts2_worker.py").read_text(encoding="utf-8")
    at = src.index("why = rw.qc_clip(dur, text)")
    assert '(level or {}).get("silent")' in src[at:at + 300]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_silent_file_is_marked_silent_with_no_figure(tmp_path):
    import runpod_worker as rw
    wav = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", "1.5", str(wav)], capture_output=True, check=True)
    info = rw.apply_post_fx_and_encode(wav, tmp_path / "s.ogg")
    assert info["measured_lufs"] is None and info.get("silent") is True
    json.dumps(info, allow_nan=False)
