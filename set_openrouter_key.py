#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Store/rotate the OpenRouter key used by the usage panel.

The key is written to ``refs.OPENROUTER_API_KEY`` in the same owner-only DSH
credentials document the panel already reads (``$DSH_HOME/.credentials.yaml``).
It is validated against OpenRouter's ``/api/v1/auth/key`` endpoint before any
write, and the value is never printed, logged or echoed back.

Usage:
    python3 set_openrouter_key.py            # hidden prompt
    python3 set_openrouter_key.py --file F   # read from F
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dsh_key_store  # noqa: E402

OPENROUTER_REF = "OPENROUTER_API_KEY"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
USER_AGENT = "opencode-usage-widget/1.0"
REQUEST_TIMEOUT = 20


def validate_openrouter_key(key):
    """Return ``(ok, detail)`` without ever including the key in detail."""
    req = Request(
        OPENROUTER_ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        hint = " (key rejected)" if exc.code in (401, 403) else ""
        return False, "HTTP %s%s" % (exc.code, hint)
    except URLError as exc:
        return False, "network error: %s" % (exc.reason,)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    data = body.get("data") or {}
    if not data:
        return False, "endpoint accepted the key but returned no data"
    free = data.get("free_model_daily_requests") or {}
    return True, "HTTP 200, free daily limit %s" % free.get("limit", "?")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=None,
                    help="read the key from this file instead of stdin")
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            key = fh.read()
    else:
        if sys.stdin.isatty():
            sys.stderr.write("Paste the OpenRouter API key and press Enter: ")
            sys.stderr.flush()
        key = sys.stdin.readline()
    key = key.strip()
    if not key:
        sys.stderr.write("error: no key supplied\n")
        return 2

    ok, detail = validate_openrouter_key(key)
    if not ok:
        sys.stderr.write("refused: OpenRouter validation failed: %s\n" % detail)
        return 1
    try:
        report = dsh_key_store.set_key(key, validate=False, name=OPENROUTER_REF)
    except dsh_key_store.KeyStoreError as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 1

    print("stored %s in %s (ref %s, %s)" % (
        report["current"]["prefix"], report["path"], report["ref"], detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
