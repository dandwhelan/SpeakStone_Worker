"""net_worker.py core: what leaves the machine, the engine kept loaded between jobs (against a FAKE engine script,
never the real model or a GPU), uploads, the finish grace, idle waits, stale job folders and the public-tier name.
No network: the site API and the upload PUTs are fakes."""
import base64
import ast
import hashlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import net_worker as nw  # noqa: E402

JOB1 = "11111111-1111-1111-1111-111111111111"
JOB2 = "22222222-2222-2222-2222-222222222222"

FAKE_ENGINE = r'''
import json, os, sys, time
args = sys.argv[1:]
state = os.environ["FAKE_STATE"]
mode = os.environ.get("FAKE_MODE", "")

def make(batch, out):
    rows = json.load(open(batch, encoding="utf-8"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "generation_provenance.jsonl"), "a", encoding="utf-8") as pf:
        for r in rows:
            with open(os.path.join(out, r["sound_file"]), "wb") as f:
                f.write(b"OggS" + b"x" * 100)
            pf.write(json.dumps({"sound_file": r["sound_file"], "duration_sec": 1.5,
                                 "generation_seed": r["generation_seed"]}) + "\n")
    print("made", len(rows), flush=True)
    print("a warning on stderr", file=sys.stderr, flush=True)

with open(os.path.join(state, "pids.txt"), "a") as f:
    f.write(f"{os.getpid()} {'serve' if '--serve' in args else 'once'} {os.getcwd()}\n")
if "--serve" not in args:
    if "--refs-stdin" in args:
        with open(os.path.join(state, "once_refs.txt"), "a") as f:
            f.write(sys.stdin.readline())
    make(args[args.index("--batch-file") + 1], args[args.index("--output-dir") + 1])
    sys.exit(0)
print("loading model", flush=True)
print("[ENGINE-READY]", flush=True)
while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    req = json.loads(raw)
    with open(os.path.join(state, "reqs.txt"), "a") as f:
        f.write(json.dumps(req) + "\n")
    flag = os.path.join(state, "crashed")
    if mode == "crash_once" and not os.path.exists(flag):
        open(flag, "w").close()
        print("RuntimeError: boom", file=sys.stderr, flush=True)
        os._exit(7)
    if mode == "hang":
        time.sleep(600)
    make(req["batch_file"], req["output_dir"])
    print("[JOB-DONE] rc=0", flush=True)
'''

REF = b"fake reference clip bytes"
REF_SHA = hashlib.sha256(REF).hexdigest()


# ------------------------------------------------------------------ fakes
class FakeApi:
    def __init__(self, jobs=(), tier="own", empty=None):
        self.jobs = list(jobs)
        self.calls = []
        self.tier = tier
        self.empty = empty or {"job": None, "reason": "none"}
        self.upload_url_calls = 0

    def get_me(self):
        return 200, {"worker": {"label": "x", "tier": self.tier, "status": "active"}}

    def post(self, route, body):
        self.calls.append((route, json.loads(json.dumps(body))))
        if route == "claim":
            return 200, ({"job": self.jobs.pop(0)} if self.jobs else dict(self.empty))
        if route == "upload-urls":
            self.upload_url_calls += 1
            return 200, {"urls": [{"index": f["index"], "url": f"https://up.test/{f['index']}/{self.upload_url_calls}",
                                   "key": f"k/{f['index']}", "headers": {"Content-Type": "audio/ogg"}}
                                  for f in body["files"]]}
        return 200, {}

    def routes(self):
        return [r for r, _ in self.calls]


def payload(n=3):
    return {"v": 1, "recipe": nw.RECIPE, "engine": nw.ENGINE, "settings": {"bitrate": "32k"},
            "refs": [{"sha256": REF_SHA, "ext": "ogg", "bytes": len(REF), "text": "hello"}],
            "lines": [{"i": i, "text": f"line number {i}", "ref": REF_SHA, "seed": 100 + i, "emo": [0] * 8,
                       "post_af": ""} for i in range(n)]}


REF_URL = "https://1.1.1.1/refs/ref.ogg?sig=1"     # never fetched: download_verified is replaced


def job(job_id, n=3):
    return {"id": job_id, "lease_id": "lease", "attempt": 1, "payload": payload(n), "heartbeat_every_seconds": 3600,
            "ref_urls": {REF_SHA: {"url": REF_URL, "expires_at": "2099-01-01T00:00:00Z"}}}


@pytest.fixture
def puts(monkeypatch):
    """Upload PUTs never leave the machine."""
    rec = {"n": 0, "max_live": 0, "live": 0, "fail_once": set(), "lock": threading.Lock()}

    def fake_http(method, url, headers=None, data=None, timeout=30, **kw):
        with rec["lock"]:
            rec["n"] += 1
            rec["live"] += 1
            rec["max_live"] = max(rec["max_live"], rec["live"])
        try:
            time.sleep(0.05)
            idx = int(url.split("/")[3])
            with rec["lock"]:
                if idx in rec["fail_once"]:
                    rec["fail_once"].discard(idx)
                    return 403, b""
            return 200, b""
        finally:
            with rec["lock"]:
                rec["live"] -= 1
    monkeypatch.setattr(nw, "http_request", fake_http)
    return rec


def make_worker(tmp_path, monkeypatch, api=None, mode="", keep=True, gpu=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    script = tmp_path / "fake_engine.py"
    script.write_text(FAKE_ENGINE, encoding="utf-8")
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_MODE", mode)
    cfg = nw.Config()
    cfg.work_dir = (tmp_path / "work").resolve()
    cfg.worker_key = ""
    cfg.model_dir = str(tmp_path / "model") if (tmp_path / "model" / "config.yaml").exists() else str(tmp_path)
    cfg.python = sys.executable
    cfg.synth_script = str(script)
    cfg.need_free_mib = 0
    cfg.engine_keep = keep
    w = nw.Worker(cfg, log=nw.Log(tmp_path / "logs", echo=False), gpu_query=gpu or (lambda: dict(nw.NULL_GPU)),
                  skip_vram_wait=False, install_signals=False)
    w.api = api or FakeApi()
    w.sleep_impl = lambda s, ctx=None: None
    w.jobs_dir.mkdir(parents=True, exist_ok=True)
    fetched = []

    def fake_download(url, size, sha, timeout=60):
        fetched.append(url)
        return len(REF), REF_SHA, REF
    monkeypatch.setattr(nw, "download_verified", fake_download)
    w.fetched = fetched
    return w, state


def pids(state):
    p = state / "pids.txt"
    return [ln.split()[:2] for ln in p.read_text().splitlines()] if p.exists() else []


def events(tmp_path, name):
    out = []
    for f in sorted((tmp_path / "logs").glob("worker_*.log")):
        for ln in f.read_text(encoding="utf-8").splitlines():
            parts = ln.split(" ")
            if len(parts) > 1 and parts[1] == name:
                out.append(ln)
    return out


def make_ctx(w, job_id, n=2):
    jobdir = w.safe_job_dir(job_id)
    jobdir.mkdir(parents=True, exist_ok=True)
    ctx = nw.JobCtx(job_id, "lease", 1, payload(n), jobdir)
    ctx.worker = w
    nw.atomic_write_json(jobdir / "chunk.json", nw.build_chunk(ctx.payload))
    ctx.out.mkdir(parents=True, exist_ok=True)
    return ctx


# ------------------------------------------------------------------ 1. what goes to the site
def test_safe_msg_removes_urls_paths_and_names():
    msg = ("reference download failed: https://bucket.example/x.ogg?X-Amz-Signature=abc and "
           "https://huggingface.co/a/b at C:\\Users\\bob\\w and C:/Users/bob/w, /mnt/c/Users/bob/w, /home/bob/w, "
           "/Users/bob/w, \\\\nas01\\share\\f.txt, //nas01/share/f on DESKTOP-ABC as alice (al stays)")
    s = nw.safe_msg(msg, names=["DESKTOP-ABC", "alice"])
    for bad in ("bucket.example", "Signature", "huggingface", "bob", "nas01", "DESKTOP-ABC", "alice"):
        assert bad not in s, bad
    assert s.count("<url>") == 2 and "(al stays)" in s


def test_safe_msg_uses_this_machines_names(monkeypatch):
    monkeypatch.setattr(nw.socket, "gethostname", lambda: "volunteer-pc.lan")
    monkeypatch.setattr(nw.getpass, "getuser", lambda: "jo")      # under 3 characters: left alone
    monkeypatch.setenv("USERNAME", "voluser")
    s = nw.safe_msg("OSError on VOLUNTEER-PC for voluser, job ok")
    assert "VOLUNTEER-PC" not in s and "voluser" not in s and "job ok" in s


def test_send_fail_scrubs_every_message(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    ctx = make_ctx(w, JOB1)
    f = nw.JobFail("upload_failed", "upload of line 2 failed: PUT https://r2.example/k?sig=SECRET C:/Users/bob/x",
                   True, False)
    w.send_fail(ctx, f)
    body = [b for r, b in w.api.calls if r == "fail"][-1]
    assert "SECRET" not in body["message"] and "bob" not in body["message"] and "<url>" in body["message"]


# ------------------------------------------------------------------ 2. loudness recipe
def test_loudness_plan_levels_shouted_lines_too():
    src = (ROOT / "runpod_worker.py").read_text(encoding="utf-8")
    ns = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in (
                "TARGET_LUFS", "LOUDNESS_WINDOW_LU", "LEVEL_RECIPE") for t in node.targets):
            exec(compile(ast.Module([node], []), "runpod_worker.py", "exec"), ns)
        if isinstance(node, ast.FunctionDef) and node.name in ("loudness_plan", "_loudnorm_measure"):
            exec(compile(ast.Module([node], []), "runpod_worker.py", "exec"), ns)
            if node.name == "_loudnorm_measure":
                assert "_run_with_retry(" in ast.get_source_segment(src, node)
    plan = ns["loudness_plan"]
    assert ns["LEVEL_RECIPE"] == "lufs-16-w2-v3"
    assert plan(-8.0, True) == "down" and plan(-8.0, False) == "down"
    assert plan(-25.0, True) == "up" and plan(-15.0, True) is None and plan(-80.0, False) is None and plan(None, 1) is None


# ------------------------------------------------------------------ 3. engine kept loaded (fake engine)
def test_two_jobs_reuse_one_engine_and_log_load_and_gen(tmp_path, monkeypatch, puts):
    api = FakeApi([job(JOB1), job(JOB2)])
    w, state = make_worker(tmp_path, monkeypatch, api)
    try:
        w.cycle()
        w.cycle()
        assert [p[1] for p in pids(state)] == ["serve"]                 # one engine process for both jobs
        assert len((state / "reqs.txt").read_text().splitlines()) == 2
        reqs = [json.loads(x) for x in (state / "reqs.txt").read_text().splitlines()]
        assert all(Path(r[k]).is_absolute() for r in reqs for k in ("batch_file", "output_dir"))
        assert all("references" not in r and r["refs"] == {f"{REF_SHA}.ogg": base64.b64encode(REF).decode()}
                   for r in reqs)                                     # the clip rides in the request, not on disk
        assert w.fetched == [REF_URL, REF_URL]                          # once per job, nothing kept between jobs
        assert pids(state) and (state / "pids.txt").read_text().split()[2] == str(w.work)   # stable cwd
        done = events(tmp_path, "complete")
        assert len(done) == 2 and "load_s=" in done[0] and "gen_s=" in done[0]
        assert "load_s=0.0" in done[1]                                  # reused
        assert done[0].index("audio=") < done[0].index("load_s=")       # older fields keep their order
        assert api.routes().count("complete") == 2
        assert w.engine_alive()
    finally:
        w.engine_shutdown("test")


def test_job_output_lands_in_the_jobs_own_log(tmp_path, monkeypatch):
    w, state = make_worker(tmp_path, monkeypatch)
    try:
        ctx = make_ctx(w, JOB1)
        assert w.run_synth(ctx) == 0
        text = (ctx.dir / "worker.log").read_text(encoding="utf-8")
        assert "loading model" in text and "made 2" in text and "a warning on stderr" in text
        assert "[JOB-DONE]" not in text and "[ENGINE-READY]" not in text
        ctx2 = make_ctx(w, JOB2)
        assert w.run_synth(ctx2) == 0
        assert "made 2" in (ctx2.dir / "worker.log").read_text() and "loading model" not in (ctx2.dir / "worker.log").read_text()
        assert ctx2.load_s == 0.0 and ctx.load_s > 0
    finally:
        w.engine_shutdown("test")


def test_engine_crash_mid_job_gives_nonzero_and_respawns(tmp_path, monkeypatch):
    w, state = make_worker(tmp_path, monkeypatch, mode="crash_once")
    try:
        ctx = make_ctx(w, JOB1)
        log = ctx.dir / "worker.log"
        rc = w.run_synth(ctx)
        assert rc != 0 and not w.engine_alive()
        assert nw.last_error_line(log) == "RuntimeError: boom"
        assert w.run_synth(ctx) == 0                                   # a fresh engine for the re-run
        assert [p[1] for p in pids(state)] == ["serve", "serve"] and pids(state)[0][0] != pids(state)[1][0]
    finally:
        w.engine_shutdown("test")


@pytest.mark.parametrize("abort", ["lease", "stop", "yield"])
def test_abort_kills_the_engine(tmp_path, monkeypatch, abort):
    w, state = make_worker(tmp_path, monkeypatch, mode="hang")
    ctx = make_ctx(w, JOB1)
    seen = {}

    def trigger():
        for _ in range(200):
            if (state / "reqs.txt").exists():
                break
            time.sleep(0.05)
        seen["proc"] = w.engine.proc
        if abort == "lease":
            ctx.lost_status = 410
            ctx.lost.set()
        elif abort == "stop":
            w.request_stop()
        else:
            w._yield_last, w._yield_at = True, time.time() + 3600
            w.cfg.yield_lock = "set"
    t = threading.Thread(target=trigger)
    t.start()
    exc = {"lease": nw.LeaseLost, "stop": nw.Stop, "yield": nw.Yield}[abort]
    with pytest.raises(exc):
        w.run_synth(ctx)
    t.join()
    assert seen["proc"].poll() is not None and not w.engine_alive() and ctx.proc is None


def test_handle_job_stop_kills_engine_and_releases(tmp_path, monkeypatch):
    api = FakeApi()
    w, state = make_worker(tmp_path, monkeypatch, api, mode="hang")
    timer = threading.Timer(1.5, w.request_stop)
    timer.start()
    with pytest.raises(nw.Stop):
        w.handle_job(job(JOB1))
    timer.cancel()
    assert not w.engine_alive()
    assert [b["reason"] for r, b in api.calls if r == "fail"] == ["released"]


def test_idle_and_max_jobs_recycle(tmp_path, monkeypatch):
    w, state = make_worker(tmp_path, monkeypatch, FakeApi())
    try:
        assert w.run_synth(make_ctx(w, JOB1)) == 0 and w.engine_alive()
        w.cycle()                                                      # empty queue, engine still fresh
        assert w.engine_alive()
        w.engine.last_used -= w.engine.idle_seconds + 1
        w.cycle()
        assert not w.engine_alive()
        assert any("reason=idle" in e for e in events(tmp_path, "engine"))
        w.engine.max_jobs = 1
        assert w.run_synth(make_ctx(w, JOB1)) == 0
        assert w.run_synth(make_ctx(w, JOB2)) == 0                      # second run: a fresh process
        assert [p[1] for p in pids(state)] == ["serve"] * 3
    finally:
        w.engine_shutdown("test")


def test_engine_keep_off_uses_one_process_per_run(tmp_path, monkeypatch, puts):
    cfile = tmp_path / "config.json"
    cfile.write_text("{}")
    cfg = nw.load_config(str(cfile), {"SSW_ENGINE_KEEP": "0", "INDEXTTS2_DIR": str(tmp_path)}, require_key=False)
    assert cfg.engine_keep is False
    assert nw.load_config(str(cfile), {"INDEXTTS2_DIR": str(tmp_path)}, require_key=False).engine_keep is True
    cfile.write_text(json.dumps({"engine_keep": False}))
    assert nw.load_config(str(cfile), {"INDEXTTS2_DIR": str(tmp_path)}, require_key=False).engine_keep is False
    api = FakeApi([job(JOB1)])
    w, state = make_worker(tmp_path, monkeypatch, api, keep=False)
    w.cycle()
    assert [p[1] for p in pids(state)] == ["once"] and w.engine is None
    done = events(tmp_path, "complete")
    assert len(done) == 1 and "gen_s=" in done[0] and "load_s=" not in done[0]


def test_wait_for_vram_skips_when_our_engine_holds_the_memory(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, gpu=lambda: {"vram_free_mib": 300})
    w.cfg.need_free_mib = 5200
    slept = []

    def sleep(s, ctx=None):
        slept.append(s)
        raise nw.Stop()                                       # leave the wait loop after one round
    w.sleep_impl = sleep
    with pytest.raises(nw.Stop):
        w.wait_for_vram()                                     # no engine: waits for free memory
    assert slept == [nw.VRAM_POLL_SECONDS]
    w.stop_event.clear()
    w.sleep_impl = lambda s, ctx=None: None
    try:
        assert w.run_synth(make_ctx(w, JOB1)) == 0
        slept.clear()
        w.wait_for_vram()                                     # engine alive: returns at once
        assert slept == []
        w.cfg.need_free_mib = 0
        calls = iter([True, False])
        monkeypatch.setattr(w, "yield_busy", lambda force=False: next(calls))
        w.wait_for_vram()                                     # a yield gives the memory back
        assert not w.engine_alive()
    finally:
        w.engine_shutdown("test")


# ------------------------------------------------------------------ 4. uploads
def test_uploads_run_in_parallel_and_refresh_on_403(tmp_path, monkeypatch, puts):
    api = FakeApi()
    w, _ = make_worker(tmp_path, monkeypatch, api)
    ctx = make_ctx(w, JOB1, n=8)
    for i in range(8):
        (ctx.out / f"L{i:03d}.ogg").write_bytes(b"OggS" + bytes([i]) * 50)
    st = {i: {"kind": "ok", "path": ctx.out / f"L{i:03d}.ogg"} for i in range(8)}
    puts["fail_once"].add(3)
    w.upload_all(ctx, st)
    assert all(v["key"] == f"k/{i}" for i, v in st.items())
    assert puts["n"] == 9 and api.upload_url_calls == 2 and 1 < puts["max_live"] <= nw.UPLOAD_THREADS


# ------------------------------------------------------------------ 5. a finished job is delivered despite a stop
def test_stop_during_upload_still_completes(tmp_path, monkeypatch, puts):
    api = FakeApi([job(JOB1)])
    w, state = make_worker(tmp_path, monkeypatch, api)
    real = w.upload_all

    def upload_then_stop(ctx, st):
        (w.work / "STOP").write_text("")                     # the user asks to stop while the clips go up
        assert w.stop_requested()
        return real(ctx, st)
    w.upload_all = upload_then_stop
    try:
        w.handle_job(job(JOB1))
        assert "complete" in api.routes() and "fail" not in api.routes()
        assert w.stop_requested()                            # the stop still happens, after the job
    finally:
        w.engine_shutdown("test")


def test_grace_is_bounded_and_lease_loss_still_aborts(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    ctx = make_ctx(w, JOB1)
    w.request_stop()
    ctx.start_grace()
    ctx.check()                                              # stop asked, but in grace: no raise
    w.call_retry(lambda: "ok", ctx)
    ctx.lost_status = 410
    ctx.lost.set()
    with pytest.raises(nw.LeaseLost):
        ctx.check()
    ctx.lost.clear()
    ctx.start_grace(0)                                       # grace over
    with pytest.raises(nw.Stop):
        ctx.check()


# ------------------------------------------------------------------ 6. idle waits and 429
def test_idle_wait_is_capped_and_jittered():
    assert nw.idle_wait(300, rnd=lambda: 1.0) == 60.0
    assert nw.idle_wait(30, rnd=lambda: 0.0) == 24.0 and nw.idle_wait(30, rnd=lambda: 1.0) == 36.0
    assert nw.idle_wait(0, rnd=lambda: 0.5) == 1.0


def test_empty_queue_uses_server_hint(tmp_path, monkeypatch):
    api = FakeApi(empty={"job": None, "reason": "none", "retry_after_seconds": 20})
    w, _ = make_worker(tmp_path, monkeypatch, api)
    slept = []
    w.sleep_impl = lambda s, ctx=None: slept.append(s)
    monkeypatch.setattr(nw.random, "random", lambda: 0.5)
    w.cycle()
    api.empty = {"job": None, "reason": "none"}
    w.cycle()
    w.cycle()
    api.empty = {"job": None, "reason": "none", "retry_after_seconds": 900}
    w.cycle()
    assert slept == [20.0, 15.0, 30.0, 60.0]


def test_429_retry_after(monkeypatch):
    assert nw.retry_after_seconds({"retry-after": "42"}, {}) == 42.0
    assert nw.retry_after_seconds({}, {"retry_after_seconds": 9000}) == nw.RETRY_AFTER_CAP
    assert nw.retry_after_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, {}, now=0) == nw.RETRY_AFTER_CAP
    assert nw.retry_after_seconds({}, {}) is None
    api = nw.Api("https://x.test", "")
    with pytest.raises(nw.NetError) as e:
        api._finish(429, b'{"retry_after_seconds": 75}', {})
    assert e.value.retry_after == 75.0


def test_call_retry_waits_at_least_retry_after(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    slept = []
    w.sleep_impl = lambda s, ctx=None: slept.append(s)
    seq = iter([nw.NetError("HTTP 429", retry_after=120.0), "ok"])

    def fn():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    assert w.call_retry(fn) == "ok" and slept == [120.0]


# ------------------------------------------------------------------ 7. stale job folders
def test_scan_resume_deletes_every_folder_it_will_not_offer(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(12)]
    for n, jid in enumerate(ids):
        d = w.jobs_dir / jid
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"job_id": jid}))
        os.utime(d, (1000 + n, 1000 + n))
    got = w.scan_resume()
    assert got == list(reversed(ids))[:nw.MAX_RESUME]
    assert sorted(p.name for p in w.jobs_dir.iterdir()) == sorted(got)


# ------------------------------------------------------------------ 8. public tier sends no machine name
def test_public_tier_sends_generic_machine_name(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    w.cfg.machine_name = "volunteer-pc"
    w.tier = "public"
    assert w.claim_body([])["machine_name"] == "worker"
    w.tier = "own"
    assert w.claim_body([])["machine_name"] == "volunteer-pc"
    w.tier = None
    assert w.claim_body([])["machine_name"] == "volunteer-pc"


def test_startup_keeps_tier(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi(tier="public"))
    (tmp_path / "config.yaml").write_text("x")
    monkeypatch.setattr(nw, "compute_model_hash", lambda *a, **k: "sha256:" + "0" * 64)
    w.startup()
    assert w.tier == "public" and w.claim_body([])["machine_name"] == "worker"


# ------------------------------------------------------------------ the engine's serve loop (heavy modules stubbed)
def _load_engine_module(monkeypatch):
    for k in ("HF_HOME", "HF_HUB_CACHE"):
        monkeypatch.delenv(k, raising=False)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.__version__ = "stub"
    rw = types.ModuleType("runpod_worker")
    rw.DEFAULT_REFS = Path(".")
    for name, mod in (("torch", torch), ("numpy", types.ModuleType("numpy")),
                      ("soundfile", types.ModuleType("soundfile")), ("runpod_worker", rw)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("idx2_under_test", ROOT / "indextts2_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_serve_protocol(tmp_path, monkeypatch, capsys):
    m = _load_engine_module(monkeypatch)
    seen = []

    def runner(tts, b, o, r, br, device, ref_cache):
        seen.append((tts, b, br, Path(ref_cache)))
        return 0 if len(seen) == 1 else 1
    reqs = [{"batch_file": "a.json", "output_dir": "o", "references": "r", "bitrate": "48k"}, "not json",
            {"batch_file": "b.json", "output_dir": "o", "references": "r"}]
    stdin = io.BytesIO(b"".join((json.dumps(x) if isinstance(x, dict) else x).encode() + b"\n" for x in reqs))
    rc = m.serve("cpu", tmp_path / "cache", stdin=stdin, load=lambda: "TTS", runner=runner)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == ["[ENGINE-READY]", "[JOB-DONE] rc=0", "[JOB-DONE] rc=2", "[JOB-DONE] rc=1"]
    assert seen[0][0] == "TTS" and seen[0][2] == "48k" and seen[1][2] == "32k"
    assert seen[0][3] == (tmp_path / "cache").resolve()


def test_serve_ends_after_an_escaped_error(tmp_path, monkeypatch, capsys):
    m = _load_engine_module(monkeypatch)

    def runner(*a, **k):
        raise RuntimeError("CUDA error: an illegal memory access was encountered")
    stdin = io.BytesIO(b'{"batch_file":"a","output_dir":"o","references":"r"}\n{"batch_file":"b","output_dir":"o","references":"r"}\n')
    assert m.serve("cpu", tmp_path / "c", stdin=stdin, load=lambda: "TTS", runner=runner) == 3
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["[ENGINE-READY]", "[JOB-DONE] rc=1"] and "RuntimeError: CUDA error" in cap.err


def test_one_shot_cli_still_runs_one_batch(tmp_path, monkeypatch):
    m = _load_engine_module(monkeypatch)
    got = []
    monkeypatch.setattr(m, "run_batch", lambda *a, **k: got.append((a, k)) or 0)
    monkeypatch.setattr(sys, "argv", ["x", "--batch-file", "b.json", "--output-dir", "o", "--bitrate", "48k"])
    assert m.main() == 0
    assert got[0][0][0] is None and got[0][0][1:3] == ("b.json", "o") and got[0][0][4] == "48k"


def test_run_batch_does_not_depend_on_cwd(tmp_path, monkeypatch):
    m = _load_engine_module(monkeypatch)
    (tmp_path / "job").mkdir()
    (tmp_path / "job" / "chunk.json").write_text(json.dumps([{"model_engine": "other"}]))
    monkeypatch.chdir(tmp_path / "job")
    assert m.run_batch(None, "chunk.json", "out", "refs") == 0
    assert (tmp_path / "job" / "out").is_dir()


# ------------------------------------------------------------------ 9. drain: finish the job in hand, claim nothing new
def _ready(tmp_path):
    (tmp_path / "model").mkdir(exist_ok=True)               # startup() wants a model folder that looks complete
    (tmp_path / "model" / "config.yaml").write_text("x")


def test_drain_finishes_the_job_in_hand_claims_nothing_more_and_exits_ok(tmp_path, monkeypatch, puts):
    _ready(tmp_path)
    api = FakeApi([job(JOB1), job(JOB2)])
    w, state = make_worker(tmp_path, monkeypatch, api)
    real = w.upload_all

    def upload_then_drain(ctx, st):
        (w.work / nw.DRAIN_FILE).write_text("")             # the app asks while the job is still going up
        return real(ctx, st)
    w.upload_all = upload_then_drain
    assert w.run() == nw.EXIT_OK
    assert api.routes().count("claim") == 1 and "complete" in api.routes() and "fail" not in api.routes()
    assert len(api.jobs) == 1                               # the second job was never claimed
    assert not (w.work / nw.DRAIN_FILE).exists()            # removed because the worker left on account of it
    assert events(tmp_path, "stop") and "drained" in events(tmp_path, "stop")[-1]
    assert not w.engine_alive()                             # the kept engine was shut down properly
    assert not w.stop_requested()                           # and it was not the STOP path


def test_drain_with_nothing_in_hand_exits_at_once_without_claiming(tmp_path, monkeypatch):
    _ready(tmp_path)
    api = FakeApi([job(JOB1)])
    w, _ = make_worker(tmp_path, monkeypatch, api)
    (w.work).mkdir(parents=True, exist_ok=True)
    orig = w.startup

    def startup_then_ask():
        orig()                                              # startup clears stale files, then the app asks
        (w.work / nw.DRAIN_FILE).write_text("")
    w.startup = startup_then_ask
    assert w.run() == nw.EXIT_OK and api.routes() == [] and not (w.work / nw.DRAIN_FILE).exists()


def test_drain_asked_during_an_idle_wait_ends_the_wait(tmp_path, monkeypatch):
    _ready(tmp_path)
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    t0 = time.time()
    threading.Timer(0.3, lambda: (w.work / nw.DRAIN_FILE).write_text("")).start()
    w.sleep_impl = w._real_sleep
    assert w.run() == nw.EXIT_OK and time.time() - t0 < 20     # the idle wait is 30 s or more


def test_stale_drain_file_is_cleared_at_start(tmp_path, monkeypatch):
    _ready(tmp_path)
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    w.work.mkdir(parents=True, exist_ok=True)
    (w.work / nw.DRAIN_FILE).write_text("")
    w.startup()
    assert not (w.work / nw.DRAIN_FILE).exists()


def test_stop_file_still_means_stop_now(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    (w.work / "STOP").write_text("")
    assert w.stop_requested() and not w.drain_requested()


def test_a_text_only_key_stops_the_worker_instead_of_waiting():
    api = nw.Api("https://x", "k")
    body = json.dumps({"code": "text_only", "error": "This worker key can only upload text."}).encode()
    with pytest.raises(nw.ExitRun) as e:
        api._finish(403, body)
    assert e.value.code == nw.EXIT_KEY
    assert api._finish(403, b'{"reason": "paused"}')[0] == 403        # an ordinary 403 (paused) is unchanged


# ------------------------------------------------------------------ references live in memory only (1.2.27)
def _files_holding(root, needle):
    return [p for p in Path(root).rglob("*") if p.is_file() and needle in p.read_bytes()]


def test_a_job_leaves_no_reference_on_disk(tmp_path, monkeypatch, puts):
    for keep in (True, False):
        sub = tmp_path / ("kept" if keep else "once")
        sub.mkdir()
        w, state = make_worker(sub, monkeypatch, FakeApi([job(JOB1)]), keep=keep)
        try:
            w.cycle()
            assert "complete" in w.api.routes()
            assert not (w.work / "refs").exists() and not (w.work / "ref_wav_cache").exists()
            assert _files_holding(w.work, REF) == [] and _files_holding(w.work, base64.b64encode(REF)) == []
            if not keep:                                                  # the one-shot engine got it on stdin
                got = json.loads((state / "once_refs.txt").read_text().splitlines()[0])
                assert got == {f"{REF_SHA}.ogg": base64.b64encode(REF).decode()}
        finally:
            w.engine_shutdown("test")


def test_a_reference_no_line_uses_is_refused():
    p = payload()
    p["refs"].append({"sha256": "e" * 64, "ext": "ogg", "bytes": 5, "text": ""})
    assert nw.check_payload(p) == ("bad_job", "a reference is used by no line")
    assert nw.check_payload(payload()) is None


def test_references_too_large_for_memory_are_refused():
    p = payload()
    p["refs"] = [{"sha256": f"{i:064x}", "ext": "ogg", "bytes": nw.MAX_REF_BYTES, "text": ""} for i in range(14)]
    for n, ln in enumerate(p["lines"]):
        ln["ref"] = p["refs"][n]["sha256"]
    p["lines"] += [dict(p["lines"][0], i=i, ref=p["refs"][i]["sha256"]) for i in range(3, 14)]
    assert nw.check_payload(p) == ("bad_job", "the references are too large to hold in memory")


def test_serve_takes_references_as_bytes(tmp_path, monkeypatch, capsys):
    m = _load_engine_module(monkeypatch)
    seen = []

    def runner(tts, b, o, r, br, device, ref_cache):
        seen.append(r)
        return 0
    name = "a" * 64 + ".ogg"
    good = {"batch_file": "a", "output_dir": "o", "refs": {name: base64.b64encode(b"voice").decode()}}
    bad_name = {"batch_file": "a", "output_dir": "o", "refs": {"../x.ogg": "dm9pY2U="}}
    bad_b64 = {"batch_file": "a", "output_dir": "o", "refs": {name: "not base64!"}}
    stdin = io.BytesIO(b"".join(json.dumps(x).encode() + b"\n" for x in (good, bad_name, bad_b64)))
    assert m.serve("cpu", tmp_path / "cache", stdin=stdin, load=lambda: "TTS", runner=runner) == 0
    assert capsys.readouterr().out.splitlines() == ["[ENGINE-READY]", "[JOB-DONE] rc=0", "[JOB-DONE] rc=2",
                                                    "[JOB-DONE] rc=2"]
    assert seen == [{name: b"voice"}]
    assert not (tmp_path / "cache").exists()                            # no folder is made for in-memory clips


def test_run_batch_in_memory_reports_a_missing_clip(tmp_path, monkeypatch, capsys):
    m = _load_engine_module(monkeypatch)
    (tmp_path / "chunk.json").write_text(json.dumps([{"model_engine": m.ENGINE, "canonical_reference_clip": "x.ogg",
                                                      "sound_file": "L000.ogg"}]))
    assert m.run_batch(None, tmp_path / "chunk.json", tmp_path / "out", {"y.ogg": b"v"}) == 1
    assert "the job's memory" in capsys.readouterr().err
