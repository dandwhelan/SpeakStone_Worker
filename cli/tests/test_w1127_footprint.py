"""1.2.27: a smaller install (gpt.pth stored as float16, triton dropped), cleanup after an update, anonymous public keys,
and a serve engine whose memory does not grow from job to job."""
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import net_worker as nw  # noqa: E402
from test_net_worker_core import FakeApi, _load_engine_module, make_worker, puts  # noqa: E402,F401


# ------------------------------------------------------------------ model hash survives the float16 conversion
def _model(tmp_path, gpt=b"G" * 100):
    d = tmp_path / "model"
    d.mkdir()
    (d / "config.yaml").write_text("x")
    (d / "gpt.pth").write_bytes(gpt)
    return d


def test_a_float16_checkpoint_hashes_as_its_float32_original(tmp_path):
    d = _model(tmp_path, b"F" * 200)
    original = nw.compute_model_hash(d)
    orig_sha = nw.sha256_file(str(d / "gpt.pth"))
    (d / "gpt.pth").write_bytes(b"h" * 100)                              # the converted file
    note = {"gpt.pth": {"sha256": orig_sha, "size": 200, "fp16_sha256": nw.sha256_file(str(d / "gpt.pth")),
                        "fp16_size": 100}}
    (d / nw.FP16_NOTE).write_text(json.dumps(note))
    assert nw.compute_model_hash(d) == original
    (d / "gpt.pth").write_bytes(b"x" * 100)                              # same size, other bytes: no longer that file
    assert nw.compute_model_hash(d) != original
    (d / nw.FP16_NOTE).write_text("not json")
    assert nw.compute_model_hash(d) != original                          # a broken note is ignored, never trusted


def test_the_note_is_outside_the_hashed_files(tmp_path):
    d = _model(tmp_path)
    (d / nw.FP16_NOTE).write_text("{}")
    assert [f[0] for f in nw.list_model_files(d)] == ["config.yaml", "gpt.pth"]


def test_start_converts_once_and_skips_a_converted_model(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    d = _model(tmp_path)
    ran = []
    monkeypatch.setattr(nw.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    w.shrink_model(d)
    assert len(ran) == 1 and ran[0][-2:] == ["--shrink-model", str(d)]
    (d / nw.FP16_NOTE).write_text(json.dumps({"gpt.pth": {"fp16_size": 100}}))
    w.shrink_model(d)
    assert len(ran) == 1                                                 # already float16: nothing run
    monkeypatch.setenv("SSW_SHRINK_MODEL", "0")
    (d / nw.FP16_NOTE).unlink()
    w.shrink_model(d)
    assert len(ran) == 1


# ------------------------------------------------------------------ cleanup after an update
def _old(path, days=2):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_cleanup_removes_what_older_versions_left(tmp_path):
    work, install, model = tmp_path / "work", tmp_path / "install", tmp_path / "install" / "IndexTTS-2"
    for d in (work / "refs", work / "ref_wav_cache", install / "uv_cache" / "wheels", model):
        d.mkdir(parents=True)
    (work / "refs" / "a.ogg").write_bytes(b"v" * 10)
    (work / "ref_wav_cache" / "a.wav").write_bytes(b"w" * 20)
    (install / "uv_cache" / "wheels" / "x.whl").write_bytes(b"u" * 30)
    _old(install / "uv_cache")
    (model / "gpt.pth.123.part").write_bytes(b"p" * 40)
    _old(model / "gpt.pth.123.part")
    (model / "gpt.pth.456.part").write_bytes(b"p")                       # a conversion running now: kept
    (model / "gpt.pth").write_bytes(b"g")
    assert nw.cleanup_after_update(work, install, model) == 100
    assert not (work / "refs").exists() and not (work / "ref_wav_cache").exists()
    assert not (install / "uv_cache").exists()
    assert sorted(p.name for p in model.iterdir()) == ["gpt.pth", "gpt.pth.456.part"]
    assert nw.cleanup_after_update(work, install, model) == 0            # nothing left: cheap, never raises
    assert nw.cleanup_after_update(tmp_path / "no", tmp_path / "no", tmp_path / "no") == 0


def test_a_fresh_uv_cache_is_left_for_a_setup_that_may_be_running(tmp_path):
    (tmp_path / "uv_cache").mkdir()
    nw.cleanup_after_update(tmp_path / "w", tmp_path, tmp_path)
    assert (tmp_path / "uv_cache").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux installs only")
def test_triton_is_uninstalled_with_the_installs_own_uv(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "triton").mkdir(parents=True)
    (site / "triton" / "big.so").write_bytes(b"t" * 500)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "uv").write_text("")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        import shutil
        shutil.rmtree(site / "triton")
    assert nw.drop_unused_packages(tmp_path, run=run) == 500
    assert seen[0][1:4] == ["pip", "uninstall", "--python"] and seen[0][-1] == "triton"
    assert nw.drop_unused_packages(tmp_path, run=run) == 0               # gone: nothing run
    assert len(seen) == 1


def test_logs_are_kept_three_days():
    assert nw.LOG_KEEP_DAYS == 3


# ------------------------------------------------------------------ anonymous public keys
def test_a_public_key_sends_no_card_model_or_os_build(tmp_path, monkeypatch):
    gpu = lambda: {"name": "NVIDIA GeForce RTX 4070", "vram_total_mib": 12282, "vram_free_mib": 11000, "gpu_util": 3}
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi(), gpu=gpu)
    w.tier = "public"
    b = w.claim_body([])
    assert "gpu" not in b and b["machine_name"] == "worker" and b["os"] in ("windows", "linux")
    assert b["vram_total_mib"] == 12282 and b["vram_free_mib"] == 11000 and b["gpu_util"] == 3
    w.tier = "own"
    b = w.claim_body([])
    assert b["gpu"] == "NVIDIA GeForce RTX 4070" and b["os"] == nw.os_string()


def test_enrolment_sends_no_card_model():
    src = (ROOT / "cli" / "speakstone_worker" / "enroll.py").read_text(encoding="utf-8")
    assert '"gpu": "",' in src


# ------------------------------------------------------------------ the serve engine's memory stays flat
def test_the_memory_loader_is_installed_once_per_model(monkeypatch):
    m = _load_engine_module(monkeypatch)
    calls = []

    class TTS:
        cache_spk_cond = "voice"

        def _load_and_cut_audio(self, path, secs, verbose=False, sr=None):
            calls.append(path)
            return "loaded"
    tts = TTS()
    first = m.install_memory_loader(tts)
    wrapper = tts._load_and_cut_audio
    for _ in range(50):                                                  # fifty jobs on one loaded engine
        assert m.install_memory_loader(tts) is first
    assert tts._load_and_cut_audio is wrapper                            # never wrapped twice
    assert tts._load_and_cut_audio("plain.wav", 15) == "loaded" and calls == ["plain.wav"]


# ------------------------------------------------------------------ clips pinned in RAM and wiped after the job
def test_a_pinned_clip_is_wiped_and_unpinned():
    buf = bytearray(b"a voice" * 1000)
    pinned = nw.pin(buf)
    assert pinned in (True, False)                                       # best effort: a container may refuse mlock
    nw.release(buf)
    assert buf == bytearray(len(buf)) and id(buf) not in nw._PINNED
    nw.release(bytearray())                                              # never raises, pinned or not
    nw.release(b"immutable")


def test_the_jobs_clips_are_wiped_when_it_ends(tmp_path, monkeypatch, puts):
    from test_net_worker_core import JOB1, job
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi([job(JOB1)]))
    held = []
    real_pin = nw.pin
    monkeypatch.setattr(nw, "pin", lambda b: held.append(b) or real_pin(b))
    try:
        w.cycle()
    finally:
        w.engine_shutdown("test")
    assert held and all(b == bytearray(len(b)) for b in held)


def test_no_core_dumps_sets_the_limit():
    resource = pytest.importorskip("resource")
    old = resource.getrlimit(resource.RLIMIT_CORE)
    try:
        nw.no_core_dumps()
        assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (old[0] if old[1] == resource.RLIM_INFINITY or old[0] <= 0 else 0,
                                                      old[1]))
        except (ValueError, OSError):
            pass


# ------------------------------------------------------------------ clips encrypted at rest in R2 (site: lib/refcrypt.ts)
VECTOR = "a1c08c0a93c03747d6147066483f0f6d7921420a2e09f3b017cbd5d91921e3178efdee4371"   # the site's test has the same


def test_decrypt_matches_the_sites_cipher():
    b = bytearray(b"SpeakStone reference clip test vector")
    nw.decrypt_ref(b, bytes([7]) * 32)
    assert b.hex() == VECTOR
    nw.decrypt_ref(b, bytes([7]) * 32)                                   # the same XOR both ways
    assert b == bytearray(b"SpeakStone reference clip test vector")
    with pytest.raises(ValueError):
        nw.decrypt_ref(bytearray(b"x"), b"short")


def test_an_encrypted_clip_is_decrypted_in_memory_and_checked(tmp_path, monkeypatch, puts):
    import base64
    import hashlib
    from test_net_worker_core import JOB1, REF, REF_SHA, job
    key = bytes(range(32))
    cipher = bytearray(REF)
    nw.decrypt_ref(cipher, key)                                          # what the uploader stored
    j = job(JOB1)
    j["ref_urls"][REF_SHA]["key"] = base64.b64encode(key).decode()
    w, state = make_worker(tmp_path, monkeypatch, FakeApi([j]))
    monkeypatch.setattr(nw, "download_verified",
                        lambda url, size, sha, timeout=60: (len(cipher), hashlib.sha256(cipher).hexdigest(),
                                                            bytearray(cipher)))
    try:
        w.cycle()
    finally:
        w.engine_shutdown("test")
    assert "complete" in w.api.routes()
    reqs = [json.loads(x) for x in (state / "reqs.txt").read_text().splitlines()]
    assert reqs[0]["refs"] == {f"{REF_SHA}.ogg": base64.b64encode(REF).decode()}   # the engine got the plain clip


def test_a_wrong_key_fails_the_checksum_not_the_engine(tmp_path, monkeypatch, puts):
    import base64
    import hashlib
    from test_net_worker_core import JOB1, REF, REF_SHA, job
    j = job(JOB1)
    j["ref_urls"][REF_SHA]["key"] = base64.b64encode(b"\x01" * 32).decode()
    w, state = make_worker(tmp_path, monkeypatch, FakeApi([j]))
    monkeypatch.setattr(nw, "download_verified",
                        lambda url, size, sha, timeout=60: (len(REF), hashlib.sha256(REF).hexdigest(), bytearray(REF)))
    try:
        w.cycle()
    finally:
        w.engine_shutdown("test")
    assert "complete" not in w.api.routes() and not (state / "reqs.txt").exists()
    fails = [b for r, b in w.api.calls if r == "fail"]
    assert fails and fails[0]["reason"] == "ref_mismatch"


def test_a_malformed_key_refuses_the_job(tmp_path, monkeypatch, puts):
    from test_net_worker_core import JOB1, REF_SHA, job
    j = job(JOB1)
    j["ref_urls"][REF_SHA]["key"] = "not base64!"
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi([j]))
    try:
        w.cycle()
    finally:
        w.engine_shutdown("test")
    fails = [b for r, b in w.api.calls if r == "fail"]
    assert fails and fails[0]["reason"] == "bad_job"
