"""One worker per graphics card: SSW_GPU pins the memory check, the telemetry and the engine to one card."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("net_worker_gpu", ROOT / "net_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_gpu_index_reads_only_small_whole_numbers():
    nw = load_worker()
    assert nw.gpu_index({}) is None and nw.gpu_index({"SSW_GPU": ""}) is None
    assert nw.gpu_index({"SSW_GPU": "0"}) == 0 and nw.gpu_index({"SSW_GPU": " 3 "}) == 3
    for bad in ("-1", "1.5", "all", "0,1", "999"):
        assert nw.gpu_index({"SSW_GPU": bad}) is None


def test_the_card_query_names_this_workers_card():
    nw = load_worker()
    assert "-i" not in nw.smi("name", {})
    cmd = nw.smi("memory.free", {"SSW_GPU": "2"})
    assert cmd[0] == "nvidia-smi" and cmd[-2:] == ["-i", "2"] and "--query-gpu=memory.free" in cmd


def test_both_queries_and_the_engine_use_it(monkeypatch):
    nw = load_worker()
    monkeypatch.setenv("SSW_GPU", "1")
    seen = []

    class R:
        returncode, stdout = 0, "RTX 4070, 12282, 11000, 1282, 3\n"

    monkeypatch.setattr(nw.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
    g = nw.query_gpu()
    assert seen[0][-2:] == ["-i", "1"] and g["vram_free_mib"] == 11000
    nw._telemetry_cache["t"] = None
    nw.read_telemetry(runner=lambda cmd, **kw: (seen.append(cmd), R())[1])
    assert seen[1][-2:] == ["-i", "1"]
    text = (ROOT / "net_worker.py").read_text(encoding="utf-8")
    assert 'env["CUDA_VISIBLE_DEVICES"] = str(gpu_index(env))' in text
