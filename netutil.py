"""Small HTTPS helper: urllib with a working certificate bundle.

python.org builds of CPython on macOS ship without system root certificates
in their OpenSSL, so plain `urllib` fails TLS verification. `certifi` (pulled
in by our dependencies) provides the Mozilla bundle; use it when available.
"""
from __future__ import annotations

import json
import ssl
import urllib.request


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi is in requirements
        return ssl.create_default_context()


def open_url(request: urllib.request.Request | str, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def get_json(url: str, timeout: float) -> dict:
    with open_url(url, timeout) as resp:
        return json.load(resp)
