"""Linux: the downloaded update is made runnable before it is proved and before it is put in place."""
import os

from speakstone_worker import APP_NAME, update

GOOD = "https://github.com/dandwhelan/SpeakStone_Worker/releases/download/v9.9.9/"
REL = update.Release("9.9.9", GOOD + "SpeakStoneWorker-linux", GOOD + "SpeakStoneWorker-linux.sha256")


def test_make_runnable_marks_the_file_on_linux_only(tmp_path):
    f = tmp_path / "prog"
    f.write_bytes(b"x")
    calls = []
    update.make_runnable(f, chmod=lambda p, m: calls.append((p, m)), platform="linux")
    assert calls == [(str(f), 0o755)]
    update.make_runnable(f, chmod=lambda p, m: calls.append((p, m)), platform="win32")
    assert len(calls) == 1


def test_the_download_is_made_runnable_before_the_start_proof(tmp_path, monkeypatch):
    import hashlib
    exe = tmp_path / "SpeakStoneWorker"
    exe.write_bytes(b"old")
    body = b"n" * 100
    order = []
    monkeypatch.setattr(update, "make_runnable", lambda p, **k: order.append("runnable"))

    def download(url, dest):
        open(dest, "wb").write(body)

    class R:
        returncode, stdout = 0, f"{APP_NAME} 9.9.9\n"

    def run(cmd, **k):
        order.append("proof")
        return R()
    new = update.fetch_verified(REL, exe, get=lambda u: (hashlib.sha256(body).hexdigest() + "  x").encode(),
                                download=download, run=run, min_bytes=10)
    assert order == ["runnable", "proof"] and new.exists()


def test_swap_on_linux_marks_it_runnable_and_drops_our_private_library_path(tmp_path, monkeypatch):
    exe, new = tmp_path / "SpeakStoneWorker", tmp_path / "SpeakStoneWorker.new"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    seen = {}
    monkeypatch.setattr(update, "_child_env", lambda: {"LD_LIBRARY_PATH": "/usr/local/cuda/lib64", update.RESET_ENV: "1"})
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIabc:/usr/local/cuda/lib64")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/cuda/lib64")
    marked = []
    monkeypatch.setattr(update, "make_runnable", lambda p, **k: marked.append(str(p)))

    def execv(path, argv):
        seen.update(path=path, argv=argv, ld=os.environ.get("LD_LIBRARY_PATH"), orig=os.environ.get("LD_LIBRARY_PATH_ORIG"),
                    reset=os.environ.get(update.RESET_ENV))
    update.swap_linux(exe, new, ["--background"], execv=execv)
    assert exe.read_bytes() == b"new" and marked == [str(new)]
    assert seen["ld"] == "/usr/local/cuda/lib64" and seen["orig"] is None and seen["reset"] == "1"
    assert seen["argv"] == [str(exe), "--background"]


def test_the_start_proof_allows_three_minutes():
    import inspect
    assert inspect.signature(update.self_run).parameters["timeout"].default == 180
