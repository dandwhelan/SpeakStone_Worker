"""The app's own calls to the site. A redirect is never followed.

urllib follows a redirect by default and sends the Authorization header (the worker key) on to wherever it points.
The site never redirects these calls, so any redirect means something is wrong; refuse it and let the caller treat it as
"could not reach the site".
"""
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"redirect refused (HTTP {code})")


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(req, timeout=None):
    """Like urllib.request.urlopen(req, timeout=...) but a redirect raises URLError."""
    return _OPENER.open(req, timeout=timeout)
