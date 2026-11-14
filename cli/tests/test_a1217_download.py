"""A setup download that drops is tried again and resumed; it never ends the program with a traceback."""
import http.client
import urllib.error

import pytest

from speakstone_worker import setup


class Resp:
    def __init__(self, chunks, status=200, length=None):
        self.chunks, self.status = list(chunks), status
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if isinstance(c, Exception):
            raise c
        return c


def opener_from(responses, seen):
    def opener(req, timeout=None):
        seen.append(dict(req.header_items()))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return opener


def test_a_drop_is_resumed_from_the_saved_bytes(tmp_path):
    seen = []
    responses = [Resp([b"aaaa", http.client.IncompleteRead(b"")], length=10),
                 Resp([b"bbbbbb"], status=206, length=6)]
    setup.real_download("https://x/f.zip", tmp_path / "f.zip", opener=opener_from(responses, seen), sleep=lambda s: None)
    assert (tmp_path / "f.zip").read_bytes() == b"aaaabbbbbb"
    assert "Range" not in seen[0] and seen[1]["Range"] == "bytes=4-"
    assert not (tmp_path / "f.zip.part").exists()


def test_a_server_that_cannot_resume_starts_again(tmp_path):
    seen = []
    responses = [Resp([b"aaaa", ConnectionResetError("gone")]),          # no length, as an archive link answers
                 Resp([b"aaaa", b"bbbb"])]
    setup.real_download("https://x/f.zip", tmp_path / "f.zip", opener=opener_from(responses, seen), sleep=lambda s: None)
    assert (tmp_path / "f.zip").read_bytes() == b"aaaabbbb"


def test_a_short_file_counts_as_a_drop(tmp_path):
    seen = []
    responses = [Resp([b"aaaa"], length=8), Resp([b"bbbb"], status=206, length=4)]
    setup.real_download("https://x/f", tmp_path / "f", opener=opener_from(responses, seen), sleep=lambda s: None)
    assert (tmp_path / "f").read_bytes() == b"aaaabbbb"


def test_when_every_try_fails_it_is_an_ordinary_setup_message(tmp_path):
    waits = []
    responses = [urllib.error.URLError("no route")] * 3
    with pytest.raises(setup.SetupError) as e:
        setup.real_download("https://x/f", tmp_path / "f", opener=opener_from(responses, []), sleep=waits.append, tries=3)
    assert "kept dropping" in str(e.value) and len(waits) == 2
    assert not (tmp_path / "f").exists()


def test_a_refusal_is_not_retried(tmp_path):
    waits = []
    err = urllib.error.HTTPError("https://x/f", 404, "nope", {}, None)
    with pytest.raises(setup.SetupError) as e:
        setup.real_download("https://x/f", tmp_path / "f", opener=opener_from([err], []), sleep=waits.append)
    assert "404" in str(e.value) and waits == []


def test_an_old_part_file_is_not_trusted(tmp_path):
    (tmp_path / "f.part").write_bytes(b"OLD")
    seen = []
    setup.real_download("https://x/f", tmp_path / "f", opener=opener_from([Resp([b"new"], length=3)], seen), sleep=lambda s: None)
    assert (tmp_path / "f").read_bytes() == b"new" and "Range" not in seen[0]
