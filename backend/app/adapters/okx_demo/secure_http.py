from __future__ import annotations

from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, build_opener


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def build_direct_no_redirect_opener() -> OpenerDirector:
    """Ignore inherited proxy variables and never forward auth across redirects."""

    return build_opener(ProxyHandler({}), RejectRedirects())
