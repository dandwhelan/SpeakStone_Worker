import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

from speakstone_worker import enroll, identity, net, paths, stats


def test_api_base_rules():
    d = paths.DEFAULT_API_BASE if hasattr(paths, "DEFAULT_API_BASE") else None
    from speakstone_worker import DEFAULT_API_BASE as D
    assert paths.api_base({}) == D
    assert paths.api_base({"SPEAKSTONE_API_BASE": "https://example.org/"}) == "https://example.org"
    assert paths.api_base({"SPEAKSTONE_API_BASE": "http://localhost:8080"}) == "http://localhost:8080"
    assert paths.api_base({"SPEAKSTONE_API_BASE": "http://127.0.0.1:3000/"}) == "http://127.0.0.1:3000"
    assert paths.api_base({"SPEAKSTONE_API_BASE": "http://[::1]:3000"}) == "http://[::1]:3000"
    for bad in ("http://example.org", "ftp://example.org", "file:///etc", "javascript:1", "https://", "https://u:p@x.org",
                "http://[bad", "not a url", "http://localhost.evil.org"):
        assert paths.api_base({"SPEAKSTONE_API_BASE": bad}) == D, bad


class _Redirect(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Redirect.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_redirect_is_refused_and_header_not_forwarded():
    srv = HTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/start", headers={"Authorization": "Bearer k"})
        with pytest.raises(urllib.error.URLError):
            net.open_url(req, timeout=5)
        assert [p for p, _ in _Redirect.seen] == ["/start"]
    finally:
        srv.shutdown()


def test_defaults_use_the_no_redirect_opener():
    import inspect
    for fn in (identity.fetch, identity.refresh, identity.rename, enroll.enroll, stats.fetch_queue):
        assert inspect.signature(fn).parameters["opener"].default is net.open_url


def test_redirect_reads_as_cannot_reach(tmp_path):
    def boom(req, timeout=None):
        raise urllib.error.URLError("redirect refused (HTTP 302)")
    assert identity.fetch("https://x", "k", boom) is None
    with pytest.raises(enroll.EnrollError):
        enroll.enroll("https://x", "pc", "gpu", 8192, "win", "1", opener=boom)
    assert stats.fetch_queue("https://x", opener=boom) is None
