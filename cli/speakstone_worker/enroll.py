"""Auto-enrol: one POST to the site, which answers with this PC's worker key."""
import json
import urllib.error
import urllib.request

from .net import open_url


class EnrollError(Exception):
    """Message is written for the person at the keyboard. Never contains a key."""


def friendly_error(status, code=None):
    if status == 422 and code == "gpu_too_small":
        return "This PC needs an NVIDIA graphics card with at least 8 GB of memory."
    if status == 503 and code == "network_full":
        return "The network is full right now. Please try again tomorrow."
    if status == 429:
        return "Too many sign-ups from this network just now. Please try again in a little while."
    if status == 503:
        return "The queue is not taking new workers right now. Please try again later."
    if status == 422:
        return "The site did not accept this PC's details (graphics card or system name). Nothing was changed."
    return f"The site answered with an unexpected error (HTTP {status}). Please try again later."


def _error_code(err):
    """The `code` field of the server's JSON error body, or None. Only this short word is used, never the body."""
    try:
        data = json.loads(err.read(10_000).decode("utf-8"))
        code = data.get("code") if isinstance(data, dict) else None
        return code if isinstance(code, str) and len(code) < 40 else None
    except (OSError, ValueError, AttributeError):
        return None


PURPOSE_TEXT = "text"      # a key only for sending captured WoW text: this PC makes no voice lines (any graphics card)


def enroll(api_base, machine_name, gpu, vram_mib, os_name, worker_version, opener=open_url, timeout=30, purpose=None):
    """`purpose=PURPOSE_TEXT` asks for a text-upload-only key (the light setup, settings make_lines off). A site that
    does not know the field yet answers 422: then this asks once more the ordinary way, which works on an 8 GB+
    NVIDIA PC, so nothing is worse than before."""
    if purpose:
        try:
            return _enroll(api_base, machine_name, gpu, vram_mib, os_name, worker_version, opener, timeout, purpose)
        except _Refused422:
            pass
    try:
        return _enroll(api_base, machine_name, gpu, vram_mib, os_name, worker_version, opener, timeout, None)
    except _Refused422 as e:
        raise EnrollError(e.args[0]) from None


class _Refused422(Exception):
    pass


def _enroll(api_base, machine_name, gpu, vram_mib, os_name, worker_version, opener, timeout, purpose):
    fields = {"machine_name": machine_name, "gpu": gpu, "vram_mib": int(vram_mib), "os": os_name,
              "worker_version": worker_version}
    if purpose:
        fields["purpose"] = purpose
    body = json.dumps(fields).encode("utf-8")
    req = urllib.request.Request(api_base.rstrip("/") + "/api/worker/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{worker_version}"})
    try:
        with opener(req, timeout=timeout) as r:
            status = getattr(r, "status", 201)
            data = json.loads(r.read(100_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = _error_code(e)
        if e.code == 422:
            raise _Refused422(friendly_error(e.code, code)) from None
        raise EnrollError(friendly_error(e.code, code)) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise EnrollError("Could not reach the SpeakStone site. Check your internet connection and try again.") from None
    except ValueError:
        raise EnrollError("The site's answer was not understood. Please try again later.") from None
    if status != 201 or not isinstance(data, dict) or not isinstance(data.get("key"), str) or not data["key"]:
        raise EnrollError(friendly_error(status) if status != 201 else "The site's answer was incomplete.")
    return {"worker_id": data.get("worker_id"), "key": data["key"], "label": str(data.get("label") or ""),
            "tier": data.get("tier")}
