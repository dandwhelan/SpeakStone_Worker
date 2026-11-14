"""Transfer links must not reach the volunteer's own machine, and an upload is always a PUT (2026-11-14 review).
Nothing here touches the network: the policy is plain functions and the upload is a fake."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_net_worker_core import JOB1, FakeApi, make_ctx, make_worker, nw  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_policy():
    before = dict(nw._transfer_api)
    yield
    nw._transfer_api.clear()
    nw._transfer_api.update(before)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://172.16.3.4/x", "https://192.168.1.10/x",
    "https://169.254.169.254/latest", "https://[::1]/x", "https://[fe80::1]/x", "https://[fd00::1]/x",
    "https://0.0.0.0/x", "https://localhost/x", "https://app.localhost/x"])
def test_remote_site_cannot_send_a_link_to_this_machine(url):
    nw.set_transfer_policy("https://site.example.com")
    with pytest.raises(nw.NetError):
        nw.check_url(url)


def test_remote_site_links_to_the_internet_are_allowed():
    nw.set_transfer_policy("https://site.example.com")
    assert nw.check_url("https://example.com/x") == "https://example.com/x"
    assert nw.check_url("https://93.184.216.34/x")
    with pytest.raises(nw.NetError):
        nw.check_url("http://example.com/x")
    with pytest.raises(nw.NetError):
        nw.check_url("file:///etc/passwd")


def test_loopback_site_may_use_loopback_http_for_tests():
    nw.set_transfer_policy("http://127.0.0.1:8000")
    assert nw.check_url("http://127.0.0.1:9000/x")
    assert nw.check_url("http://localhost/x")
    nw.set_transfer_policy("http://localhost:3000")
    assert nw.check_url("http://127.0.0.1/x")


def test_the_site_itself_is_always_reachable():
    nw.set_transfer_policy("https://192.168.1.50")
    assert nw.check_url("https://192.168.1.50/api/worker/claim")
    with pytest.raises(nw.NetError):
        nw.check_url("https://192.168.1.51/x")


def test_worker_sets_the_policy_from_its_site_address(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    assert nw._transfer_api["loopback"] is False
    with pytest.raises(nw.NetError):
        nw.check_url("https://127.0.0.1/x")


def test_upload_ignores_a_method_the_site_sends(tmp_path, monkeypatch):
    api = FakeApi()
    real_post = api.post

    def post(route, body):
        status, data = real_post(route, body)
        for u in data.get("urls", []):
            u["method"] = "DELETE"
        return status, data
    api.post = post
    w, _ = make_worker(tmp_path, monkeypatch, api)
    made = []
    monkeypatch.setattr(nw, "http_request", lambda method, url, **kw: made.append(method) or (200, b""))
    ctx = make_ctx(w, JOB1, n=2)
    for i in range(2):
        (ctx.out / f"L{i:03d}.ogg").write_bytes(b"OggS" + bytes([i]) * 50)
    st = {i: {"kind": "ok", "path": ctx.out / f"L{i:03d}.ogg"} for i in range(2)}
    w.upload_all(ctx, st)
    assert made == ["PUT", "PUT"]
