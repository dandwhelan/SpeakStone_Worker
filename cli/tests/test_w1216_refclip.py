"""A reference clip is converted for at most REF_MAX_SECONDS (2026-11-14 review): the length limit is an output option."""
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_engine_module():
    spec = importlib.util.spec_from_file_location("idx2_w1216", ROOT / "indextts2_worker.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        return None
    return mod


def test_convert_is_limited_and_the_limit_is_an_output_option(tmp_path, monkeypatch):
    src = (ROOT / "indextts2_worker.py").read_text(encoding="utf-8")
    assert "REF_MAX_SECONDS = 30" in src
    mod = _load_engine_module()
    if mod is None:                     # the engine's own libraries are not installed here: the source check stands
        assert '"-t", str(REF_MAX_SECONDS)' in src
        return
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"wav")
        return types.SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "a.ogg").write_bytes(b"OggS")
    out = mod.prepare_reference(tmp_path / "refs", "a.ogg", tmp_path / "cache")
    cmd = seen["cmd"]
    assert out.exists()
    t = cmd.index("-t")
    assert cmd[t + 1] == "30" and t > cmd.index("-i") and t < len(cmd) - 2     # after the input, before the output path
    assert cmd[-1].endswith(".part.wav")
