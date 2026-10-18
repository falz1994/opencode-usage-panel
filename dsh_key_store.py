#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Key store for the OpenCode Zen credentials DSH uses.

DSH does not read ``~/.local/share/opencode/auth.json`` (that file belongs to
the opencode CLI). The keys DSH hands to requests live in

    $DSH_HOME/.credentials.yaml      (default ~/.dsh/.credentials.yaml)

as two references this module manages:

* ``OPENCODE_API_KEY`` — bound by settings.yaml to the ``opencode-go*`` routes
  the **main agent** uses.
* ``OPENCODE_SUBAGENT_API_KEY`` — bound by settings.yaml to the dedicated
  ``opencode-go-sub*`` routes the **subagents** use. Keeping it separate is what
  lets the subagent key be rotated on demand without touching the main agent's
  credential.

Both are addressed the same way, by reference name, so ``load_key``/``set_key``
take a ``name``. An OpenCode Zen usage limit is enforced per *workspace*, not
per model, so a key whose workspace is out of quota fails for every model —
including the cheapest ones. ``validate_key`` only proves authentication; a
key can authenticate and still be rate-limited (``GoUsageLimitError``), which
is why the panel reports the usage windows rather than trusting a 200 alone.

This module reads and writes those references. It deliberately mirrors the
conventions of DSH's own ``@deepseek-ai/dsh-credentials-local`` provider, which
is what keeps a write here safe while DSH is running:

* **Owner-only permissions.** That provider calls ``assertOwnerOnly`` on every
  read and refuses a document readable beyond its owner. Writes here land at
  ``0600``, and a pre-existing looser mode is tightened rather than trusted.
* **Cross-process writer lock.** Writers serialise on the ``wx``-created
  sibling ``<file>.lock``, the same protocol ``withFileLock`` uses, so a write
  here cannot interleave with one from the Web UI's Models page.
* **Atomic commit.** Content is rendered into a temp file in the same directory
  and ``os.replace``d over the target, so a reader sees either the old document
  or the new one, never a half-written one.
* **Surgical edit.** Only the ``OPENCODE_API_KEY`` line is touched; every other
  byte (other refs, ``records``, comments, blank lines, ordering) is preserved.

DSH watches the file with chokidar and reloads it in place, and the credential
seam resolves a reference once per operation — so a key written here is used by
the *next* subagent request with no restart.

Precedence caveat: an ``OPENCODE_API_KEY`` inherited in the DSH process
environment outranks this file. ``describe_environment_shadow()`` reports it.

CLI:
    python3 dsh_key_store.py show                 masked fingerprints of both refs
    python3 dsh_key_store.py path                 resolved file path
    python3 dsh_key_store.py check                verify the main key works
    python3 dsh_key_store.py --ref sub check      verify the subagent key works
    python3 dsh_key_store.py set                  read a new main key from stdin
    python3 dsh_key_store.py --ref sub set        read a new subagent key from stdin

The key value is never printed, logged or echoed back: only a masked
fingerprint (prefix + SHA-256 digest prefix) ever leaves this module.
"""

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_DSH_HOME = os.path.expanduser("~/.dsh")
CREDENTIALS_FILENAME = ".credentials.yaml"

#: The reference name that settings.yaml binds to every opencode-go route.
REF_NAME = "OPENCODE_API_KEY"

#: The reference the dedicated subagent routes use (see settings.yaml
#: `opencode-go-sub*`). Keeping it separate is what lets the subagent key be
#: rotated without touching the credential the main agent uses.
SUBAGENT_REF_NAME = "OPENCODE_SUBAGENT_API_KEY"

#: Both managed references, in display order.
REF_NAMES = (REF_NAME, SUBAGENT_REF_NAME)

#: Same endpoint the panel polls; used to prove a candidate key is usable.
ZEN_ENDPOINT = "https://opencode.ai/zen/go/v1/usage"
USER_AGENT = "opencode-usage-widget/1.0"
REQUEST_TIMEOUT = 20

#: Matches DSH's own doc lock wait so both sides give up on the same schedule.
LOCK_WAIT_MS = 5000
#: A lock older than this is treated as abandoned (DSH holds it only for the
#: duration of one read-render-commit cycle).
LOCK_STALE_MS = 15000

_REF_LINE = re.compile(r"^(?P<indent> +)(?P<name>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<value>.*)$")


class KeyStoreError(Exception):
    """A failure a caller can show the user verbatim."""


# --------------------------------------------------------------------------
# Paths and permissions
# --------------------------------------------------------------------------

def dsh_home():
    """Resolve the harness home directory the same way DSH does."""
    return os.path.expanduser(os.environ.get("DSH_HOME") or DEFAULT_DSH_HOME)


def credentials_path():
    """Path of the credentials document.

    ``DSH_KEY_STORE_PATH`` overrides it; that exists so tests and dry runs can
    exercise the write path without touching the live credential store.
    """
    override = os.environ.get("DSH_KEY_STORE_PATH")
    if override:
        return os.path.expanduser(override)
    return os.path.join(dsh_home(), CREDENTIALS_FILENAME)


# --------------------------------------------------------------------------
# Document handling
# --------------------------------------------------------------------------

def _reject_beyond_owner(path, text):
    """Refuse a document DSH itself would refuse, naming the fix."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & 0o077:
        raise KeyStoreError(
            "%s is readable beyond its owner (mode %s); DSH will not load it. "
            "Fix it with: chmod 600 %s" % (path, format(mode, "04o"), path)
        )


def read_document(path=None):
    """Return the raw credentials document, or None when it does not exist."""
    path = path or credentials_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KeyStoreError("cannot read %s: %s" % (path, exc))
    _reject_beyond_owner(path, text)
    return text


def _refs_block(lines):
    """Locate the top-level ``refs:`` mapping.

    Returns ``(header_index, first_index, end_index)`` where ``end`` is the
    index of the first line after the block. Raises when the document is not in
    the version-1 layout this module is willing to edit.
    """
    header = None
    for i, line in enumerate(lines):
        if re.match(r"^refs:[ \t]*$", line):
            header = i
            break
    if header is None:
        raise KeyStoreError(
            "the credentials document has no top-level 'refs:' mapping; "
            "refusing to rewrite a layout this module does not understand"
        )
    end = len(lines)
    for j in range(header + 1, len(lines)):
        line = lines[j]
        if line.strip() == "":
            continue
        if not line.startswith(" "):
            end = j
            break
    return header, header + 1, end


def _render_ref_value(name, value):
    """Render one ``refs`` entry the way DSH's own writer does.

    The value must stay a bare YAML scalar: the provider rejects a non-string
    (a quoted-looking key is still a string, but a ``#`` or ``:`` inside would
    change how YAML reads it). The grammar is checked first.
    """
    if not re.fullmatch(r"[A-Za-z0-9+/=_.\-]+", value or ""):
        raise KeyStoreError(
            "refusing to store a value with characters outside the API-key "
            "grammar (expected letters, digits, + / = _ - .)"
        )
    return "  %s: %s" % (name, value)


def render_document_with_ref(text, name, value):
    """Return the document with ``refs.<name>`` set to ``value``.

    Every other line is carried over untouched. A missing document is created
    in the version-1 layout.
    """
    new_line = _render_ref_value(name, value)

    if text is None or text.strip() == "":
        return "version: 1\nrefs:\n%s\n" % new_line

    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]

    header, first, end = _refs_block(lines)

    target = None
    for i in range(first, end):
        m = _REF_LINE.match(lines[i])
        if m and m.group("name") == name:
            target = i
            break

    if target is not None:
        lines[target] = new_line
    else:
        # Insert as the first ref, keeping any comment block above it readable.
        lines.insert(first, new_line)

    out = "\n".join(lines)
    return out + "\n" if trailing_newline or True else out


# --------------------------------------------------------------------------
# Locking and atomic write
# --------------------------------------------------------------------------

def _lock_path(path):
    return path + ".lock"


def _try_lock(path):
    """Create the ``wx`` lock sibling. Returns True when this process owns it."""
    try:
        fd = os.open(_lock_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise KeyStoreError("cannot create lock %s: %s" % (_lock_path(path), exc))
    os.close(fd)
    return True


def _lock_is_stale(path):
    try:
        age_ms = (time.time() - os.stat(_lock_path(path)).st_mtime) * 1000.0
    except OSError:
        return False
    return age_ms > LOCK_STALE_MS


class _Lock:
    """Context manager around the credentials document's writer lock.

    Mirrors DSH's ``withFileLock``: the lock is a ``wx``-created sibling, a
    contended lock is retried until ``LOCK_WAIT_MS``, and an abandoned lock
    (older than ``LOCK_STALE_MS``) is broken rather than blocking forever.
    """

    def __init__(self, path, wait_ms=LOCK_WAIT_MS):
        self.path = path
        self.wait_ms = wait_ms
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + self.wait_ms / 1000.0
        delay = 0.02
        while True:
            if _try_lock(self.path):
                self.held = True
                return self
            if _lock_is_stale(self.path):
                try:
                    os.unlink(_lock_path(self.path))
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise KeyStoreError(
                    "timed out waiting for the credentials writer lock (%s); "
                    "another writer (DSH's Models page?) is holding it"
                    % _lock_path(self.path)
                )
            time.sleep(delay)
            delay = min(delay * 2, 0.25)

    def __exit__(self, *exc_info):
        if self.held:
            try:
                os.unlink(_lock_path(self.path))
            except OSError:
                pass
            self.held = False
        return False


def write_atomic(path, text, mode=0o600):
    """Commit ``text`` to ``path`` atomically with exactly ``mode``.

    The temp file is a same-directory sibling so the ``os.replace`` stays on one
    filesystem, and it is created with the final mode so the rename never
    widens permissions (no chmod race).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def load_key(path=None, name=REF_NAME):
    """Return the stored value of ``name``, or None when unset.

    Raises KeyStoreError when the document exists but is unreadable or violates
    the owner-only rule DSH enforces.
    """
    text = read_document(path)
    if text is None:
        return None
    lines = text.split("\n")
    _, first, end = _refs_block(lines)
    for i in range(first, end):
        m = _REF_LINE.match(lines[i])
        if m and m.group("name") == name:
            value = m.group("value").strip()
            if not value:
                return None
            if value[0] in "\"'":
                value = value.strip("\"'")
            return value or None
    return None


def load_all(path=None):
    """Both managed references, keyed by reference name."""
    return {name: load_key(path, name) for name in REF_NAMES}


def fingerprint(key):
    """Describe a key without revealing it."""
    if not key:
        return {"present": False}
    return {
        "present": True,
        "length": len(key),
        "prefix": key[:5] + "…",
        "sha256_8": hashlib.sha256(key.encode("utf-8")).hexdigest()[:8],
    }


def masked(key):
    """A single-line label for a key, safe to render on screen."""
    if not key:
        return "not set"
    return "%s…%s" % (key[:5], hashlib.sha256(key.encode("utf-8")).hexdigest()[:6])


def describe_environment_shadow(env=None):
    """Whether an inherited environment variable would outrank the file.

    DSH layers the inherited process environment *above* the managed store, so
    a set variable makes a write here invisible to that process.
    """
    env = os.environ if env is None else env
    value = env.get(REF_NAME)
    if not value:
        return {"shadowed": False}
    return {"shadowed": True, "ref": REF_NAME, "length": len(value)}


def validate_key(key, endpoint=ZEN_ENDPOINT, timeout=REQUEST_TIMEOUT):
    """Prove a key authenticates against Zen. Returns ``(ok, detail)``.

    This proves *authentication only*. Zen enforces its usage limits per
    workspace, so a key can authenticate and still be unable to serve a single
    request; use ``probe_key`` to learn that.
    Never includes the key in the returned detail.
    """
    if not key:
        return False, "no key to validate"
    req = Request(
        endpoint,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        usage = body.get("usage") or {}
        if not usage:
            return False, "endpoint accepted the key but returned no usage windows"
        return True, "HTTP %s, %d usage windows" % (resp.status, len(usage))
    except HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = " (key rejected: wrong, expired, or revoked)"
        return False, "HTTP %s%s" % (exc.code, hint)
    except URLError as exc:
        return False, "network error: %s" % (exc.reason,)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def probe_key(key, timeout=REQUEST_TIMEOUT):
    """Report whether a key can actually serve requests right now.

    Returns ``(usable, detail, windows)`` where ``windows`` is the per-window
    usage map. A key whose workspace is out of quota still authenticates, so
    this is the check that matters before routing traffic to it.
    """
    if not key:
        return False, "no key", {}
    req = Request(
        ZEN_ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return False, "HTTP %s" % exc.code, {}
    except URLError as exc:
        return False, "network error: %s" % (exc.reason,), {}
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc), {}

    windows = body.get("usage") or {}
    if not windows:
        return False, "no usage windows returned", {}
    exhausted = [
        name for name, info in windows.items()
        if str((info or {}).get("status", "ok")) != "ok"
        or float((info or {}).get("percent", 0) or 0) >= 100
    ]
    if exhausted:
        return False, "out of quota in: %s" % ", ".join(sorted(exhausted)), windows
    return True, "usable", windows


def set_key(key, path=None, validate=True, name=REF_NAME):
    """Store ``key`` as ``refs.<name>``, safely and atomically.

    Returns a report dict. When ``validate`` is true the key must satisfy the
    live endpoint first, so a typo cannot overwrite a working credential.
    Raises KeyStoreError on refusal.
    """
    path = path or credentials_path()

    key = (key or "").strip()
    if not key:
        raise KeyStoreError("refusing to store an empty key")
    if "\n" in key or "\r" in key:
        raise KeyStoreError("refusing to store a key containing a newline")

    if validate:
        ok, detail = validate_key(key)
        if not ok:
            raise KeyStoreError("the new key did not validate against Zen: %s" % detail)

    with _Lock(path):
        text = read_document(path)          # re-read under the lock
        previous = None
        if text is not None:
            try:
                lines = text.split("\n")
                _, first, end = _refs_block(lines)
                for i in range(first, end):
                    m = _REF_LINE.match(lines[i])
                    if m and m.group("name") == name:
                        previous = m.group("value").strip().strip("\"'") or None
                        break
            except KeyStoreError:
                previous = None
        next_text = render_document_with_ref(text, name, key)
        write_atomic(path, next_text, mode=0o600)

    stored = load_key(path, name)
    if stored != key:
        raise KeyStoreError(
            "wrote %s but read back a different value; the file may be "
            "managed by another process" % path
        )

    return {
        "path": path,
        "ref": name,
        "changed": previous != key,
        "previous": fingerprint(previous),
        "current": fingerprint(key),
        "environment": describe_environment_shadow(),
    }


def status(path=None):
    """Everything a UI needs to render the key rows."""
    path = path or credentials_path()
    keys = load_all(path)
    return {
        "path": path,
        "exists": os.path.exists(path),
        "refs": {
            name: {
                "masked": masked(keys.get(name)),
                "fingerprint": fingerprint(keys.get(name)),
            }
            for name in REF_NAMES
        },
        "environment": describe_environment_shadow(),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _resolve_ref(value):
    """Map a CLI reference selector onto a real reference name."""
    if value is None:
        return REF_NAME
    if value in REF_NAMES:
        return value
    aliases = {"main": REF_NAME, "primary": REF_NAME,
               "sub": SUBAGENT_REF_NAME, "subagent": SUBAGENT_REF_NAME,
               "subagents": SUBAGENT_REF_NAME}
    if value.lower() in aliases:
        return aliases[value.lower()]
    raise KeyStoreError(
        "unknown reference %r; use one of %s, or the aliases main/sub"
        % (value, ", ".join(REF_NAMES))
    )


def _cmd_show(args):
    st = status(args.path)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    if st["environment"]["shadowed"]:
        print(
            "\nwarning: %s is set in this environment and OUTRANKS the file; "
            "DSH would ignore a write here." % REF_NAME,
            file=sys.stderr,
        )
    return 0


def _cmd_path(args):
    print(credentials_path() if not args.path else args.path)
    return 0


def _cmd_check(args):
    name = _resolve_ref(args.ref)
    key = load_key(args.path, name)
    if not key:
        print("no %s stored in %s" % (name, args.path or credentials_path()))
        return 1
    usable, detail, windows = probe_key(key)
    print("%s %s — %s (fingerprint %s)" % (
        name, "USABLE" if usable else "NOT USABLE", detail, masked(key)))
    for win, info in sorted(windows.items()):
        info = info or {}
        note = "" if str(info.get("status", "ok")) == "ok" else "  <- %s" % info.get("status")
        print("   %-8s %s%%  resets %s%s" % (
            win, info.get("percent"), info.get("resetsAt"), note))
    if not usable and "quota" in detail:
        print("   note: authentication succeeded; the workspace is rate-limited, "
              "so every model (including the cheapest) returns 429 until the "
              "window resets.")
    return 0 if usable else 1


def _cmd_set(args):
    name = _resolve_ref(args.ref)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            key = fh.read()
    else:
        if sys.stdin.isatty():
            sys.stderr.write("Paste the new %s and press Enter: " % name)
            sys.stderr.flush()
        key = sys.stdin.readline()
    if not key.strip():
        print("no key supplied on stdin", file=sys.stderr)
        return 2
    try:
        report = set_key(key, args.path, validate=not args.no_validate, name=name)
    except KeyStoreError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 1
    print("stored %s in %s (ref %s)" % (
        report["current"]["prefix"], report["path"], report["ref"]))
    print("previous: %s" % (
        report["previous"].get("prefix", "not set") if report["previous"]["present"] else "not set"))
    print("sha256_8: %s" % report["current"]["sha256_8"])
    if report["environment"]["shadowed"]:
        print(
            "warning: %s is set in the environment and outranks the file"
            % REF_NAME,
            file=sys.stderr,
        )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dsh_key_store",
        description="Manage the OpenCode Zen keys DSH uses.",
    )
    ap.add_argument("--path", default=None,
                    help="credentials document (default $DSH_HOME/.credentials.yaml)")
    ap.add_argument("--ref", default=None,
                    help="which reference: main (default) or sub; "
                         "defaults to %s" % REF_NAME)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="masked fingerprints of every managed ref")
    sub.add_parser("path", help="print the resolved document path")
    sub.add_parser("check", help="validate the selected key against Zen")

    p_set = sub.add_parser("set", help="store a new key (read from stdin)")
    p_set.add_argument("--file", default=None, help="read the key from this file")
    p_set.add_argument("--no-validate", action="store_true",
                       help="skip the live endpoint check before writing")

    args = ap.parse_args(argv)
    return {
        "show": _cmd_show, "path": _cmd_path,
        "check": _cmd_check, "set": _cmd_set,
    }[args.command](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyStoreError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(1)
