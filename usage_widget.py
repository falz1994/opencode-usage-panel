#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenCode Usage widget.

Frameless, always-on-top GTK3 panel that shows the OpenCode Zen usage of the key
that DSH hands to its subagents (rolling / weekly / monthly windows). It polls

    GET https://opencode.ai/zen/go/v1/usage
    Authorization: Bearer <key>

every N seconds (default 60), rendering one progress bar per window with a
countdown to each window's reset time.

The header carries the two controls that do not need a menu: a grip (`⠿`) that
moves the window when dragged — the drop position is written to the XDG config
directory and restored on the next start, so the panel stays where it was put —
and a `↻` button that forces a refresh instead of waiting for the next poll.

Which keys: the panel tracks the **main agent** key — the reference
`OPENCODE_API_KEY` in $DSH_HOME/.credentials.yaml that the `opencode-go*` routes
in settings.yaml resolve (see dsh_key_store.py) — and it can also rotate the
separate `OPENCODE_SUBAGENT_API_KEY` that the dedicated `opencode-go-sub*`
routes use for subagents. That is deliberately *not* the key in
~/.local/share/opencode/auth.json, which belongs to the opencode CLI; the panel
never touches that file. Both rows open a dialog that validates a pasted key
against the live endpoint and only then writes it, with the same lock +
atomic-commit + 0600 rules DSH itself uses. A written key is picked up by the
next request — no DSH restart.

Note that Zen enforces its usage limit per *workspace*, not per model: a key
whose workspace is out of quota authenticates fine and then fails every request
(including the cheapest models) with 429 until the window resets. `--check-key`
reports that, because a successful authentication is not proof of a usable key.

The key is only ever sent in the Authorization header; it is never printed,
logged, or displayed (the panel shows a masked fingerprint).

The panel also owns the DeepSeek Harness server itself. `dsh web` is what serves
the Web UI, and both its port and its launch token are decided when the process
starts: the token is random, printed once on stdout and stored nowhere. So the
panel reports whether the server is up, starts it — falling back to the next free
port when the preferred one is taken, and to an OS-chosen port when the whole
range is — stops it, restarts it, and keeps the authenticated URL for its `open`
button. A server started outside the panel is still detected (its port is
listening), but its URL is reported as unknown rather than guessed: only a token
seen in a log the panel can read, and re-verified against the live server, is
ever shown.

CLI:
    usage_widget.py                     run the GUI
    usage_widget.py --once              fetch, print parsed JSON to stdout, exit
    usage_widget.py --interval N        poll every N seconds
    usage_widget.py --position x,y      place the window at x,y
    usage_widget.py --forget-position   drop the remembered position, exit
    usage_widget.py --show-key          masked fingerprints of both keys
    usage_widget.py --check-key [--ref sub]   is that key usable right now?
    usage_widget.py --set-key [--ref sub]     store a new key read from stdin
    usage_widget.py --dsh-status        is the DSH server up? (JSON, exit)
    usage_widget.py --dsh-start         start it, first free port wins
    usage_widget.py --dsh-stop          stop it
    usage_widget.py --dsh-restart       restart it
    usage_widget.py --dsh-open          open the Web UI with its token
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject  # noqa: E402

try:
    import cairo
except ImportError:
    cairo = None

# Sits next to this file; owns every read/write of the subagent credential.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsh_key_store  # noqa: E402


def _env(name, default=""):
    """Environment variable as a trimmed string.

    The optional rows (the two router-backed Google counters, the BigQuery
    credit row and the subscription date) ship with **no** personal defaults:
    each one is pointed at your own account through these variables and stays
    hidden until it is. No credential ever lives in this file — see the
    README section *Configuring your API keys*.
    """
    return (os.environ.get(name) or default).strip()


APP = "OpenCode Usage"
# PyGObject still routes several GTK3 calls we depend on through deprecated
# shims (set_wmclass, get_monitor_geometry). They work; keep the log clean.
for _pat in ("set_wmclass", "get_monitor_geometry"):
    warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*%s.*" % _pat)
# Must match the launcher's basename (opencode-usage-widget.desktop) so GNOME
# Shell associates the running window with its dock icon instead of showing a
# second, unnamed entry.
APP_ID = "opencode-usage-widget"
# Gap between the panel and the right edge of the workarea.
MARGIN_RIGHT = 28
#: Where the panel remembers the spot the user dragged it to. XDG config rather
#: than the install directory: a window position is per-user state, and the
#: autostart entry passes no --position, so this is what makes the panel come
#: back where it was left.
STATE_DIR = os.path.join(
    os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config"),
    APP_ID,
)
STATE_FILE = os.path.join(STATE_DIR, "window.json")
ENDPOINT = "https://opencode.ai/zen/go/v1/usage"
#: OpenRouter key endpoint; used for the free-model daily request counter.
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
#: Reference in $DSH_HOME/.credentials.yaml for the OpenRouter key.
OPENROUTER_REF = "OPENROUTER_API_KEY"
#: Synthetic row key for the OpenRouter daily-free counter.
OPENROUTER_ROW = "openrouter_daily"
# Where the panel's key comes from and how it is edited. The opencode CLI's
# own credential (~/.local/share/opencode/auth.json) is a different key and is
# not read here.
KEY_LABEL = "DSH subagents"
REQUEST_TIMEOUT = 15

#: The panel shows the usage of the main-agent key and lets the subagent key be
#: rotated on demand; both live in the DSH credential store.
USAGE_REF = dsh_key_store.REF_NAME
SUBAGENT_REF = dsh_key_store.SUBAGENT_REF_NAME

WINDOWS = ["rolling", "weekly", "monthly"]
WINDOW_LABELS = {
    "rolling": "Rolling (5h)",
    "weekly": "Weekly",
    "monthly": "Monthly",
}
WINDOW_LABELS[OPENROUTER_ROW] = "OpenRouter daily"

#: Google AI Studio publishes per-model free-tier limits but exposes NO
#: consumption endpoint: the Zen /usage endpoint reports workspace windows only,
#: and Google returns the limits without usage. The only honest per-account
#: counter is the router's own usage table: OmniRoute records one row per
#: upstream call with its connection id, its status and its token counts, and it
#: is the only source that can tell the two Google accounts apart.
#:
#: An earlier version of this row counted `providerID=google` lines in the
#: opencode CLI log. That log never sees router traffic — the `code` combo calls
#: Google directly and the ACP bridge is only used for Zen models — so the row
#: sat at 0/500 while the account was in fact at 488/500. Limits below are the
#: ones the upstream reports in its own 429 bodies (read 2026-09-30).
GEMINI_ROW = "gemini_daily"
GEMINI_LABEL = "Gemini 1 (router)"
GEMINI_MODEL = _env("PANEL_GEMINI_MODEL", "gemini-flash-lite-latest")
#: The router connection id of *your* first Google account. Empty by default:
#: with no id there is nothing to count, so the row is not shown at all.
#: Set `PANEL_GEMINI_CONNECTION_ID` to the id your router assigned to it.
GEMINI_CONNECTION_ID = _env("PANEL_GEMINI_CONNECTION_ID")
GEMINI_RPD = int(_env("PANEL_GEMINI_RPD", "500") or 0)
GEMINI_RPM = int(_env("PANEL_GEMINI_RPM", "15") or 0)
GEMINI_TPM = int(_env("PANEL_GEMINI_TPM", "250000") or 0)
WINDOW_LABELS[GEMINI_ROW] = GEMINI_LABEL

#: Second Google account, same source and limits, different connection id
#: (`PANEL_GEMINI2_CONNECTION_ID`). Two keys only add capacity when they live
#: in different projects, which is the whole reason there are two rows.
GEMINI2_ROW = "gemini2_daily"
GEMINI2_LABEL = "Gemini 2 (router)"
GEMINI2_CONNECTION_ID = _env("PANEL_GEMINI2_CONNECTION_ID")
GEMINI2_RPD = int(_env("PANEL_GEMINI2_RPD", "500") or 0)
GEMINI2_RPM = int(_env("PANEL_GEMINI2_RPM", "15") or 0)
GEMINI2_TPM = int(_env("PANEL_GEMINI2_TPM", "250000") or 0)
WINDOW_LABELS[GEMINI2_ROW] = GEMINI2_LABEL

#: Router storage, opened read-only by the two rows above.
GEMINI_DB = os.path.expanduser(
    _env("PANEL_ROUTER_DB", "~/.omniroute/storage.sqlite"))

# Google Cloud's billing API does not expose a trial-credit balance to a service
# account. The panel therefore estimates it from BigQuery's applied promotional
# credits, and always labels the result with the export's latest covered day.
# The console-confirmed balance remains a fallback if BigQuery is unavailable.
#
# Entirely optional, and off by default: the row appears only once you point it
# at *your* project and billing export, since none of these can be guessed.
GCP_CREDIT_ROW = "gcp_credit"
GCP_CREDIT_LABEL = "Google Cloud credit"
#: Your console-confirmed credit line, used when BigQuery cannot be reached.
GCP_CREDIT_ORIGINAL = float(_env("PANEL_GCP_CREDIT_ORIGINAL", "0") or 0)
GCP_CREDIT_REMAINING = float(_env("PANEL_GCP_CREDIT_REMAINING", "0") or 0)
GCP_CREDIT_EXPIRES = _env("PANEL_GCP_CREDIT_EXPIRES")
GCP_CLOUD_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "google-cloud.svg")
# The service-account key is deliberately kept outside this widget directory
# (so a `git clone` can never pick it up) and can be moved without editing
# source. It is read only to obtain a short-lived OAuth token; neither the key
# nor the token is logged or displayed.
GCP_BILLING_KEY = os.path.expanduser(os.environ.get(
    "GCP_BILLING_SERVICE_ACCOUNT", "~/GCP-Connection/key.json"))
GCP_BILLING_PROJECT = _env("PANEL_GCP_BILLING_PROJECT")
GCP_BILLING_TABLE = _env("PANEL_GCP_BILLING_TABLE")
# A full-table aggregate is small today, but this prevents the panel's normal
# five-minute refresh loop from creating needless BigQuery queries.
GCP_BILLING_REFRESH_SECONDS = 12 * 60 * 60
WINDOW_LABELS[GCP_CREDIT_ROW] = GCP_CREDIT_LABEL

# Manual subscription end date. The Zen usage endpoint exposes window resets,
# but not subscription or billing information, so this one is typed in by hand.
# Empty by default: the row only appears when it is set.
OPENCODE_SUBSCRIPTION_ENDS = _env("PANEL_SUBSCRIPTION_ENDS")


def subscription_row_enabled():
    """Is the manually-declared subscription date worth a row?"""
    return bool(OPENCODE_SUBSCRIPTION_ENDS)


def gemini_row_enabled():
    """Does this install have a first router-backed Google account?"""
    return bool(GEMINI_CONNECTION_ID)


def gemini2_row_enabled():
    """Does this install have a second one?"""
    return bool(GEMINI2_CONNECTION_ID)


def gcp_row_enabled():
    """Is the BigQuery credit row fully pointed at a project and export?"""
    return bool(GCP_BILLING_PROJECT and GCP_BILLING_TABLE)

# ──────────────────────── DeepSeek Harness server (`dsh web`) ───────────────
#: The panel's second job. `dsh web` is the process that serves the Web UI, and
#: nothing else on this desktop knows whether it is up: its port and its launch
#: token are both decided when the process starts, and the token — random,
#: printed once on stdout as
#:
#:     dsh web: http://127.0.0.1:<port>/?token=<token>
#:
#: — is persisted nowhere. That is why the row can honestly say "running, URL
#: unknown" for a server the panel did not start: only a token captured in a log
#: it can read, and re-verified against the live server, is ever offered.
DSH_LABEL = "DSH server"
DSH_DEFAULT_PORT = 3080
#: Ports tried above the preferred one before handing the choice to the OS.
DSH_PORT_SPAN = 20
#: Panel-owned log of the server it starts (0600: the banner carries the token).
DSH_PANEL_LOG = os.path.expanduser("~/.dsh-web-panel.log")
#: The desktop launcher's log; a server it started left its banner here.
DSH_LAUNCHER_LOG = os.path.expanduser("~/.dsh-web-launcher.log")
#: Remembers the port this panel last started the server on.
DSH_STATE_FILE = os.path.join(STATE_DIR, "dsh.json")
DSH_START_TIMEOUT = 45.0
DSH_STOP_TIMEOUT = 10.0
DSH_PROBE_TIMEOUT = 3.0
#: The server row's own clock: it can fall over in seconds, while the usage
#: windows it shares the panel with move in hours.
DSH_POLL_SECONDS = 5
DSH_BANNER_RE = re.compile(
    r"dsh web:\s+http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]+)")


def humanize_age(seconds):
    """Compact age for the server row: 45s, 12m, 3h07m, 2d04h."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours = minutes // 60
    if hours < 24:
        return "%dh%02dm" % (hours, minutes % 60)
    return "%dd%02dh" % (hours // 24, hours % 24)


def _read_proc_cmdline(pid):
    """`/proc/<pid>/cmdline` as one space-joined string ("" when unreadable)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0")
                    if part)


def _proc_uptime(pid):
    """Seconds since `pid` started, or None when /proc will not say."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat = fh.read().decode("utf-8", "replace")
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            up = float(fh.read().split()[0])
        # Field 22 is starttime in clock ticks. `comm` may itself contain
        # spaces and parentheses, so the split happens after the LAST ')'.
        rest = stat[stat.rindex(")") + 2:].split()
        started = float(rest[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return max(0.0, up - started)


def _looks_like_dsh_web(cmdline):
    """Is this command line a live `dsh web` server?"""
    if not cmdline:
        return False
    argv0 = cmdline.split(" ", 1)[0]
    if not (argv0.endswith("node") or argv0.endswith("dsh")):
        return False
    # Either shape: `node …/dsh/lib/bin.js web …` (the shebang path) or
    # `node …/bin.js --profile web …` (the alias's own expansion).
    return bool(re.search(r"dsh(?:/lib/bin\.js)?\s+(?:web|--profile\s+web)(?:\s|$)",
                          cmdline))


def dsh_web_processes():
    """Every live `dsh web` process: [{pid, port, uptime, cmdline}].

    Read from /proc rather than from `pgrep -f`: the panel runs with the user's
    own privileges, so it can tell a real server from a shell, an editor or an
    agent that merely mentions the command on its own command line.
    """
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        cmdline = _read_proc_cmdline(pid)
        if not _looks_like_dsh_web(cmdline):
            continue
        match = re.search(r"--port[= ](\d+)", cmdline)
        found.append({
            "pid": pid,
            "port": int(match.group(1)) if match else None,
            "uptime": _proc_uptime(pid),
            "cmdline": cmdline,
        })
    found.sort(key=lambda item: item["pid"])
    return found


def port_in_use(port, host="127.0.0.1"):
    """Is something already listening on 127.0.0.1:`port`?"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    try:
        if probe.connect_ex((host, int(port))) == 0:
            return True
    finally:
        probe.close()
    # A wildcard-bound socket can refuse the loopback connect on some setups;
    # a failed bind is the other half of the answer.
    bind = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bind.bind((host, int(port)))
    except OSError:
        return True
    finally:
        bind.close()
    return False


def port_listener_pids(port):
    """PIDs owning a listening socket on `port` (best effort, via `ss`)."""
    try:
        proc = subprocess.run(["ss", "-ltnpH", "sport = :%d" % int(port)],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(pid) for pid in re.findall(r"pid=(\d+)", proc.stdout)]


class _NoRedirect(HTTPRedirectHandler):
    """Stop urllib from following the 303 a valid launch token earns.

    The redirect *is* the answer: it is how the Harness says "this token is
    live, here is your cookie". Following it would turn that into a 200 and
    throw the evidence away.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DSH_OPENER = build_opener(_NoRedirect)


def dsh_http_code(port, token=None):
    """HTTP status from 127.0.0.1:`port`, or None when nothing answers."""
    url = "http://127.0.0.1:%d/" % int(port)
    if token:
        url += "?token=" + token
    request = Request(url, headers={"cache-control": "no-store"})
    try:
        with _DSH_OPENER.open(request, timeout=DSH_PROBE_TIMEOUT) as response:
            return response.status
    except HTTPError as exc:
        # A 401/303 *is* the answer here, but it is still an open response:
        # close it, or a poll every 5 s leaks a socket per probe until the GC
        # gets around to it.
        code = exc.code
        exc.close()
        return code
    except Exception:
        return None


def dsh_is_server(port):
    """Does the listener on `port` behave like a Harness server?

    An unauthenticated root request earns the same minimal 401 the Harness
    sends; that is what tells a DSH port from an unrelated service that happens
    to sit on it, which is exactly the case the port fallback exists for.
    """
    return dsh_http_code(port) == 401


def dsh_token_live(port, token):
    """Does `token` authenticate against the server on `port` right now?

    A valid launch token on `/` earns a 303 to the clean root (minting the
    browser cookie on the way). Anything else is a token from a previous
    process — the normal case for a server the panel did not start.
    """
    return dsh_http_code(port, token) == 303


def _read_tail(path, limit=65536):
    """The last `limit` bytes of `path` as text ("" when unreadable)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def dsh_last_banner(path):
    """Newest `dsh web:` banner in `path` as (port, token), or None."""
    matches = DSH_BANNER_RE.findall(_read_tail(path))
    if not matches:
        return None
    port, token = matches[-1]
    return (int(port), token)


def dsh_known_url(port):
    """A *verified* URL for the server on `port`, or None.

    Only two sources can hold the banner, and both are checked against the live
    server before the URL is offered: a stale token would otherwise look like a
    working link and land the browser on a 401.
    """
    for path in (DSH_PANEL_LOG, DSH_LAUNCHER_LOG):
        banner = dsh_last_banner(path)
        if banner is None or banner[0] != int(port):
            continue
        if dsh_token_live(port, banner[1]):
            return "http://127.0.0.1:%d/?token=%s" % (int(port), banner[1])
    return None


def dsh_saved_port():
    """The port this panel last started the server on, or None."""
    try:
        with open(DSH_STATE_FILE, "r", encoding="utf-8") as fh:
            port = int(json.load(fh).get("port"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return port if 1 <= port <= 65535 else None


def dsh_save_port(port):
    """Remember the port (atomic replace, so a crash cannot leave half a file)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = DSH_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"port": int(port), "at": int(time.time())}, fh)
        os.replace(tmp, DSH_STATE_FILE)
    except OSError:
        pass


def dsh_pick_port(preferred):
    """Which port to start on: `preferred`, the next free one, or 0.

    Returns (port, note). A `preferred` that is already a Harness server gives
    None — there is nothing to start — while a port taken by some other service
    falls through to the next free one, which is the "open it on another port"
    case. 0 is the last resort: `dsh web --port 0` lets the OS pick, and the
    banner reports what it picked.
    """
    preferred = int(preferred)
    if not port_in_use(preferred):
        return preferred, ""
    if dsh_is_server(preferred):
        return None, "already running on %d" % preferred
    for candidate in range(preferred + 1, min(preferred + DSH_PORT_SPAN, 65535) + 1):
        if not port_in_use(candidate):
            return candidate, "%d busy → %d" % (preferred, candidate)
    return 0, "%d busy → OS-chosen port" % preferred


def dsh_status(preferred=None):
    """Everything the server row renders: running, port, pid, uptime, URL."""
    processes = dsh_web_processes()
    hint = int(preferred or dsh_saved_port() or DSH_DEFAULT_PORT)
    main = None
    for proc in processes:
        if proc["port"] == hint or (proc["port"] is None and hint == DSH_DEFAULT_PORT):
            main = proc
            break
    if main is None and processes:
        main = processes[0]
    if main is None and port_in_use(hint):
        # A listener we cannot map to a process (another PID namespace, a
        # container) still means "up"; only the pid is unknown.
        pids = port_listener_pids(hint)
        main = {"pid": pids[0] if pids else None, "port": hint,
                "uptime": None, "cmdline": ""}
    if main is None:
        return {"running": False, "port": None, "pid": None, "uptime": None,
                "url": None, "others": [], "preferred": hint,
                "preferred_free": not port_in_use(hint), "note": ""}
    port = main["port"] or hint
    return {
        "running": True,
        "port": port,
        "pid": main["pid"],
        "uptime": main["uptime"],
        "url": dsh_known_url(port),
        "others": [p for p in processes if p["pid"] != main["pid"]],
        "preferred": hint,
        "preferred_free": not port_in_use(hint),
        "note": "",
    }


def dsh_env():
    """Environment for the child: the panel's, with the usual bin dirs on PATH.

    The panel is started by autostart, whose PATH does not necessarily carry
    ~/.npm-global/bin (the `dsh` shim) or the node that runs it, so both
    directories are prepended rather than assumed.
    """
    env = dict(os.environ)
    extra = [os.path.expanduser("~/.npm-global/bin"),
             os.path.expanduser("~/.local/node/bin")]
    path = env.get("PATH", "")
    for entry in reversed([e for e in extra if os.path.isdir(e)]):
        if entry not in path.split(os.pathsep):
            path = entry + os.pathsep + path
    env["PATH"] = path
    # The Web runtime exports its own URL into every shell it starts; a child
    # server must not inherit a stale one.
    env.pop("DSH_WEB_URL", None)
    return env


def dsh_command():
    """argv prefix that runs `dsh web`: [node, bin.js], or the shim, or []."""
    bin_js = os.path.expanduser(
        "~/.npm-global/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
    path = dsh_env()["PATH"]
    node = shutil.which("node", path=path)
    if os.path.exists(bin_js) and node:
        return [node, bin_js]
    shim = shutil.which("dsh", path=path) or os.path.expanduser(
        "~/.npm-global/bin/dsh")
    if os.path.exists(shim):
        return [shim]
    return []


def dsh_start(port=None, timeout=DSH_START_TIMEOUT):
    """Start `dsh web` detached and wait for the banner that carries its URL.

    Detached on purpose: the panel outlives any terminal, and the server must
    outlive the panel. `start_new_session` also keeps a panel restart from
    taking the server down with it.
    """
    current = dsh_status(port)
    if current["running"]:
        return {"ok": True, "started": False, "port": current["port"],
                "pid": current["pid"], "url": current["url"],
                "note": "already running on %d" % current["port"]}
    prefix = dsh_command()
    if not prefix:
        return {"ok": False, "error": "no dsh binary found (PATH, "
                                      "~/.npm-global/bin/dsh)"}
    preferred = int(port or dsh_saved_port() or DSH_DEFAULT_PORT)
    chosen, note = dsh_pick_port(preferred)
    if chosen is None:
        return {"ok": True, "started": False, "port": preferred,
                "url": dsh_known_url(preferred), "note": note}
    argv = prefix + ["web", "--no-open", "--port", str(chosen)]
    try:
        os.makedirs(os.path.dirname(DSH_PANEL_LOG), exist_ok=True)
        os.close(os.open(DSH_PANEL_LOG,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        os.chmod(DSH_PANEL_LOG, 0o600)
        log = open(DSH_PANEL_LOG, "ab", buffering=0)
    except OSError as exc:
        return {"ok": False, "error": "cannot write %s: %s" % (DSH_PANEL_LOG, exc)}
    try:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"),
                                env=dsh_env(), start_new_session=True,
                                close_fds=True)
    except OSError as exc:
        return {"ok": False, "error": "cannot run %s: %s" % (argv[0], exc)}
    finally:
        log.close()
    # The child is detached and long-lived; reap it in a thread so a server
    # that dies later cannot turn into a zombie of the panel.
    threading.Thread(target=proc.wait, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        banner = dsh_last_banner(DSH_PANEL_LOG)
        if banner is not None and dsh_token_live(banner[0], banner[1]):
            dsh_save_port(banner[0])
            return {"ok": True, "started": True, "port": banner[0],
                    "pid": proc.pid, "note": note, "log": DSH_PANEL_LOG,
                    "url": "http://127.0.0.1:%d/?token=%s" % banner}
        if proc.poll() is not None:
            return {"ok": False, "log": DSH_PANEL_LOG, "tail":
                    _read_tail(DSH_PANEL_LOG, 2000).strip(),
                    "error": "dsh web exited with code %s" % proc.returncode}
        time.sleep(0.4)
    return {"ok": False, "pid": proc.pid, "log": DSH_PANEL_LOG,
            "error": "timed out after %ds waiting for the startup URL" % int(timeout)}


def dsh_stop(timeout=DSH_STOP_TIMEOUT):
    """Terminate every `dsh web` process; SIGKILL only what ignores SIGTERM."""
    pids = [proc["pid"] for proc in dsh_web_processes()]
    if not pids:
        hint = dsh_saved_port() or DSH_DEFAULT_PORT
        still_up = port_in_use(hint)
        return {"ok": not still_up, "stopped": [], "killed": [],
                "note": ("port %d still answers but no dsh web process is "
                         "visible" % hint) if still_up else ""}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not [pid for pid in pids if _read_proc_cmdline(pid)]:
            break
        time.sleep(0.2)
    killed = []
    for pid in pids:
        if _read_proc_cmdline(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
    if killed:
        time.sleep(0.5)
    left = [pid for pid in pids if _read_proc_cmdline(pid)]
    return {"ok": not left, "stopped": [pid for pid in pids if pid not in left],
            "killed": killed,
            "note": "" if not left else "still alive: %s" % left}


def dsh_restart(port=None):
    """Stop the server, then start it again — same port while it stays free."""
    current = dsh_status(port)
    target = int(port or current["port"] or dsh_saved_port() or DSH_DEFAULT_PORT)
    stopped = dsh_stop()
    if not stopped["ok"]:
        return {"ok": False, "stop": stopped,
                "error": stopped.get("note") or "could not stop the server"}
    # Give the listening socket a moment to be released before rebinding it;
    # if it will not come back, dsh_start falls back to the next free port.
    for _ in range(20):
        if not port_in_use(target):
            break
        time.sleep(0.1)
    started = dsh_start(target)
    started["stop"] = stopped
    return started


def dsh_open_url(url):
    """Hand the authenticated URL to the desktop's browser opener."""
    if not url:
        return False
    for opener in ("xdg-open", "google-chrome"):
        path = shutil.which(opener)
        if not path:
            continue
        try:
            subprocess.Popen([path, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError:
            continue
    return False


PALETTE = {
    "panel": "#1C1812",
    "track": "#2A2520",
    "text": "#F5EFE4",
    "muted": "#8B816F",
    "green": "#7BD88F",
    "amber": "#FFB454",
    "red": "#FF5A4E",
}

CSS = """
#panel { padding: 10px 14px 10px 12px; }
.title { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
         font-size: 13px; font-weight: bold; color: #F5EFE4; }
.close { background: transparent; background-image: none; border: none;
         box-shadow: none; padding: 0 2px;
         font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
         font-size: 13px; color: #8B816F; }
.close:hover { color: #F5EFE4; }
.grip { background: transparent; background-image: none; border: none;
        box-shadow: none; padding: 0 3px 0 0;
        font-family: "DejaVu Sans", "IBM Plex Mono", monospace;
        font-size: 13px; color: #8B816F; }
.grip:hover { color: #F5EFE4; }
.refresh { background: transparent; background-image: none; border: none;
           box-shadow: none; padding: 0 2px;
           font-family: "DejaVu Sans Mono", "IBM Plex Mono", monospace;
           font-size: 15px; color: #8B816F; }
.refresh:hover { color: #F5EFE4; }
.refresh:disabled { color: #5A5346; }
.row-label { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
             font-size: 10px; color: #8B816F; }
.pct { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
       font-size: 12px; font-weight: bold; color: #F5EFE4; }
.pct.pct-green { color: #7BD88F; }
.pct.pct-amber { color: #FFB454; }
.pct.pct-red { color: #FF5A4E; }
.countdown { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
             font-size: 9px; color: #8B816F; }
.status { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
          font-size: 9px; color: #8B816F; }
.keyrow { border-top: 1px solid #2A2520; padding-top: 5px; }
.keyval { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
          font-size: 9px; color: #F5EFE4; }
.keyval.key-missing { color: #FF5A4E; }
.keybtn { background: transparent; border: 1px solid #2A2520; border-radius: 4px;
          padding: 1px 6px; font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
          font-size: 9px; color: #8B816F; }
.keybtn:hover { color: #F5EFE4; border-color: #8B816F; }
.dialog-label { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
                font-size: 10px; color: #F5EFE4; }
.dialog-hint { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
               font-size: 9px; color: #8B816F; }
.dialog-hint.hint-bad { color: #FF5A4E; }
.dialog-hint.hint-ok { color: #7BD88F; }
.dsh-state { font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace;
             font-size: 10px; font-weight: bold; color: #8B816F; }
.dsh-state.dsh-on { color: #7BD88F; }
.dsh-state.dsh-off { color: #FF5A4E; }
.dsh-state.dsh-busy { color: #FFB454; }
"""


def parse_color(hexcol, alpha=1.0):
    h = hexcol.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (r, g, b, alpha)


def rounded_rect(cr, x, y, w, h, radius):
    if radius <= 0:
        cr.rectangle(x, y, w, h)
        return
    deg = math.pi / 180.0
    cr.new_sub_path()
    cr.arc(x + w - radius, y + radius, radius, -90 * deg, 0 * deg)
    cr.arc(x + w - radius, y + h - radius, radius, 0 * deg, 90 * deg)
    cr.arc(x + radius, y + h - radius, radius, 90 * deg, 180 * deg)
    cr.arc(x + radius, y + radius, radius, 180 * deg, 270 * deg)
    cr.close_path()


def load_saved_position():
    """The (x, y) the user last dropped the panel at, or None.

    Never raises: a missing, truncated or hand-edited state file just means
    "no remembered position" and the panel falls back to its default spot.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return (int(data["x"]), int(data["y"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_position(x, y):
    """Persist the window position; returns False if it could not be written.

    Written to a temp file and renamed, so a crash mid-write cannot leave a
    half-written state file behind. A panel that cannot write its state file
    still works — the position is a convenience, not a requirement.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"x": int(x), "y": int(y)}, fh)
        os.replace(tmp, STATE_FILE)
        return True
    except OSError:
        return False


def forget_saved_position():
    """Drop the remembered position so the next start uses the default spot."""
    try:
        os.unlink(STATE_FILE)
        return True
    except OSError:
        return False


def read_api_key():
    """The main-agent key from the DSH credential store.

    Delegates to dsh_key_store, which enforces the same owner-only permission
    rule DSH does and never returns a key that is merely quoted or blank.
    """
    try:
        key = dsh_key_store.load_key(name=USAGE_REF)
    except dsh_key_store.KeyStoreError as exc:
        raise ValueError(str(exc))
    if not key:
        raise ValueError(
            "no %s stored in %s — set it from the panel's API key row, or with "
            "`python3 dsh_key_store.py set`"
            % (USAGE_REF, dsh_key_store.credentials_path())
        )
    return key


def read_openrouter_key():
    """The OpenRouter key from the DSH credential store.

    The key lives in the same owner-only credentials document as the OpenCode
    Zen keys, under ``refs.OPENROUTER_API_KEY``. The panel never prints it.
    """
    try:
        key = dsh_key_store.load_key(name=OPENROUTER_REF)
    except dsh_key_store.KeyStoreError as exc:
        raise ValueError(str(exc))
    if not key:
        raise ValueError(
            "no %s stored in %s — use set_openrouter_key.py to set it"
            % (OPENROUTER_REF, dsh_key_store.credentials_path())
        )
    return key


def parse_iso(ts):
    s = (ts or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_reset(ts):
    try:
        dt = parse_iso(ts)
    except Exception:
        return "reset --"
    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "resetting…"
    minutes = max(1, int(math.ceil(seconds / 60.0)))
    days, rem = divmod(minutes, 24 * 60)
    hours, rem = divmod(rem, 60)
    if days > 0:
        return "reset in %dd %dh" % (days, hours)
    if hours > 0:
        return "reset in %dh %dm" % (hours, rem)
    return "reset in %dm" % minutes


def humanize_tokens(value):
    """Compact token counts for the detail line: 0 / 12k / 1.4M."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.0fk" % (n / 1000.0)
    return "%d" % int(n)


def gemini_detail_text(data):
    """One Google account's detail line: daily requests, rpm and tpm."""
    return (
        "%d/%d usadas · %d restantes · %d/%d rpm · %s/%s tpm · %s"
        % (int(data.get("used", 0) or 0),
           int(data.get("limit", 0) or 0),
           int(data.get("remaining", 0) or 0),
           int(data.get("rpm_used", 0) or 0),
           int(data.get("rpm_limit", 0) or 0),
           humanize_tokens(data.get("tpm_used", 0)),
           humanize_tokens(data.get("tpm_limit", 0)),
           humanize_reset(data.get("resetsAt", "")))
    )


def next_utc_midnight():
    """The next daily reset boundary OpenRouter uses (00:00 UTC)."""
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)


#: Google's RPD quotas reset at midnight PACIFIC time, not at 00:00 UTC: the
#: Gemini API rate-limit docs say "Requests per day (RPD) quotas reset at
#: midnight Pacific time". Counting the Google rows on the UTC boundary showed a
#: reset that had already happened (or was still hours away). OpenRouter keeps
#: the UTC boundary, which is the one its own free-model counter uses.
GEMINI_TZ_NAME = "America/Los_Angeles"


def _gemini_tz():
    """Pacific time, or a fixed -08:00 when the tz database is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(GEMINI_TZ_NAME)
    except Exception:
        return timezone(timedelta(hours=-8))


def gemini_day_start(now=None):
    """The most recent Pacific midnight, as an aware UTC datetime."""
    tz = _gemini_tz()
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight > local:
        midnight -= timedelta(days=1)
    return midnight.astimezone(timezone.utc)


def next_gemini_midnight(now=None):
    """When Google's RPD counter resets: the next Pacific midnight, in UTC."""
    tz = _gemini_tz()
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight + timedelta(days=1)).astimezone(timezone.utc)


def fetch_usage():
    """Fetch usage for the subagent key. Never returns the key."""
    key = read_api_key()
    req = Request(
        ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            # endpoint rejects urllib's default Python-urllib UA with 403
            "User-Agent": "opencode-usage-widget/1.0",
        },
    )
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    usage = body.get("usage") or {}
    result = {}
    for win in WINDOWS:
        if win not in usage:
            continue
        info = usage[win] or {}
        try:
            percent = float(info.get("percent", 0) or 0)
        except (TypeError, ValueError):
            percent = 0.0
        result[win] = {
            "status": str(info.get("status", "ok")),
            "percent": percent,
            "resetsAt": info.get("resetsAt", "") or "",
        }
    return result


def fetch_openrouter_usage():
    """Fetch OpenRouter's free-model daily counter. Never returns the key."""
    key = read_openrouter_key()
    req = Request(
        OPENROUTER_ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": "opencode-usage-widget/1.0",
        },
    )
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = body.get("data") or {}
    free = data.get("free_model_daily_requests")
    if not isinstance(free, dict) or not free:
        raise ValueError("OpenRouter did not return free_model_daily_requests")
    try:
        limit = int(free.get("limit") or 0)
        used = int(free.get("used") or 0)
        remaining = int(
            free.get("remaining")
            if free.get("remaining") is not None
            else max(limit - used, 0)
        )
    except (TypeError, ValueError):
        raise ValueError("OpenRouter returned non-numeric daily limits")
    percent = (100.0 * used / limit) if limit > 0 else 0.0
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "percent": percent,
        "is_free_tier": bool(data.get("is_free_tier", False)),
        "resetsAt": next_utc_midnight().isoformat(),
    }


def _router_account_usage(connection_id, rpd, rpm, tpm, model=None):
    """Today's accepted requests and last-minute tokens for one Google account.

    Read-only on the router's storage (``mode=ro``), so a panel refresh can never
    write to it. Only rows with ``success = 1`` count: a 429/503 is a request
    Google rejected and did not charge against the daily quota. Counting every
    row is exactly what made this row read 147 % while the account was at 98 %.
    It never returns or touches the key — the router keeps it encrypted and the
    panel only counts rows.
    """
    if not os.path.exists(GEMINI_DB):
        raise ValueError("no hay base de OmniRoute en %s" % GEMINI_DB)
    day_start = gemini_day_start().isoformat().replace("+00:00", "Z")
    minute_start = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat().replace("+00:00", "Z")
    con = sqlite3.connect("file:%s?mode=ro" % GEMINI_DB, uri=True, timeout=5)
    try:
        used_today, tokens_today = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_input + tokens_output), 0) "
            "FROM usage_history "
            "WHERE connection_id = ? AND success = 1 AND timestamp >= ?",
            (connection_id, day_start)).fetchone()
        used_last_minute = con.execute(
            "SELECT COUNT(*) FROM usage_history "
            "WHERE connection_id = ? AND success = 1 AND timestamp >= ?",
            (connection_id, minute_start)).fetchone()[0]
        tpm_used = con.execute(
            "SELECT COALESCE(SUM(tokens_input), 0) FROM usage_history "
            "WHERE connection_id = ? AND success = 1 AND timestamp >= ?",
            (connection_id, minute_start)).fetchone()[0]
    finally:
        con.close()
    used_today = int(used_today or 0)
    percent = (100.0 * used_today / rpd) if rpd > 0 else 0.0
    return {
        "used": used_today,
        "limit": rpd,
        "remaining": max(rpd - used_today, 0),
        "percent": percent,
        "rpm_used": int(used_last_minute or 0),
        "rpm_limit": rpm,
        "tpm_used": int(tpm_used or 0),
        "tpm_limit": tpm,
        "tokens": int(tokens_today or 0),
        "connection": connection_id,
        "model": model,
        "resetsAt": next_gemini_midnight().isoformat(),
    }


def fetch_gemini_usage():
    """First Google account, from the router's usage table."""
    return _router_account_usage(
        GEMINI_CONNECTION_ID, GEMINI_RPD, GEMINI_RPM, GEMINI_TPM, GEMINI_MODEL)


def fetch_gemini2_usage():
    """Second Google account, from the router's usage table."""
    return _router_account_usage(
        GEMINI2_CONNECTION_ID, GEMINI2_RPD, GEMINI2_RPM, GEMINI2_TPM,
        GEMINI_MODEL)


def fetch_gcp_credit_estimate():
    """Estimate remaining promotion credit from the BigQuery billing export.

    Cloud Billing's supported API has no field for remaining promotional credit.
    A negative PROMOTION amount means credit was applied, so the estimate is
    the original amount plus the total applied promotion. The coverage date is
    returned alongside it because the export commonly lags behind live billing.
    """
    if not os.path.isfile(GCP_BILLING_KEY):
        raise ValueError("GCP service-account key not found")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except ImportError as exc:
        raise RuntimeError("google-auth is required for GCP billing status") from exc

    credentials = service_account.Credentials.from_service_account_file(
        GCP_BILLING_KEY,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(GoogleAuthRequest())
    query = (
        "SELECT "
        "FORMAT_TIMESTAMP('%%Y-%%m-%%d', MIN(usage_start_time)) AS first_usage, "
        "FORMAT_TIMESTAMP('%%Y-%%m-%%d', MAX(usage_end_time)) AS last_usage, "
        "ROUND(COALESCE(SUM(IF(c.type = 'PROMOTION', c.amount, 0)), 0), 2) "
        "AS promotion_applied "
        "FROM `%s` AS t LEFT JOIN UNNEST(t.credits) AS c" % GCP_BILLING_TABLE
    )
    request = Request(
        "https://bigquery.googleapis.com/bigquery/v2/projects/%s/queries"
        % GCP_BILLING_PROJECT,
        data=json.dumps({
            "query": query,
            "useLegacySql": False,
            "timeoutMs": 15000,
        }).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + credentials.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "opencode-usage-widget/1.0",
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT + 5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("jobComplete", True):
        raise RuntimeError("BigQuery credit query did not finish in time")
    rows = body.get("rows") or []
    fields = rows[0].get("f") if rows else None
    if not fields or len(fields) < 3:
        raise ValueError("BigQuery billing export has no usage dates yet")
    try:
        promotion_applied = float(fields[2].get("v") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("BigQuery returned an invalid promotion total") from exc
    remaining = round(max(0.0, min(GCP_CREDIT_ORIGINAL,
                                   GCP_CREDIT_ORIGINAL + promotion_applied)), 2)
    return {
        "first_usage": str(fields[0].get("v") or ""),
        "last_usage": str(fields[1].get("v") or ""),
        "promotion_applied": promotion_applied,
        "remaining": remaining,
    }


def run_once():
    data = {}
    errors = []
    try:
        data["opencode_zen"] = fetch_usage()
    except Exception as exc:
        errors.append("opencode_zen: %s" % exc)
    try:
        data["openrouter"] = fetch_openrouter_usage()
    except Exception as exc:
        errors.append("openrouter: %s" % exc)
    if gemini_row_enabled():
        try:
            data["gemini"] = fetch_gemini_usage()
        except Exception as exc:
            errors.append("gemini: %s" % exc)
    if gemini2_row_enabled():
        try:
            data["gemini2"] = fetch_gemini2_usage()
        except Exception as exc:
            errors.append("gemini2: %s" % exc)
    if not data:
        for err in errors:
            sys.stderr.write("error: %s\n" % err)
        return 1
    for err in errors:
        sys.stderr.write("warning: %s\n" % err)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def color_for(percent):
    if percent < 60:
        return "green"
    if percent < 85:
        return "amber"
    return "red"


class UsageBar(Gtk.DrawingArea):
    """Cairo progress bar (rounded track + fill at an exact color)."""

    def __init__(self, height=12):
        super().__init__()
        self.set_size_request(60, height)
        self.set_hexpand(True)
        self._fraction = 0.0
        self._color = PALETTE["muted"]
        self._track = PALETTE["track"]
        self.connect("draw", self._on_draw)

    def set_fraction(self, fraction, color):
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self._color = color
        self.queue_draw()

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        radius = h / 2.0
        cr.save()
        rounded_rect(cr, 0, 0, w, h, radius)
        cr.set_source_rgba(*parse_color(self._track))
        cr.fill()
        fw = w * self._fraction
        if fw > 1.0:
            rounded_rect(cr, 1.0, 1.0, max(1.0, fw - 2.0), h - 2.0, max(1.0, radius - 1.0))
            cr.set_source_rgba(*parse_color(self._color))
            cr.fill()
        cr.restore()
        return True


class UsageWidget:
    def __init__(self, interval, position=None, dsh_port=None):
        self.interval = max(5, int(interval))
        #: --position outranks the remembered spot and is used exactly as
        #: given; a remembered spot is only honoured while it still lands on an
        #: attached monitor.
        self._explicit_position = position is not None
        self._position = position if position is not None else load_saved_position()
        self._fetching = False
        self._last_ok = False
        self._openrouter_ok = False
        self._gemini_ok = False
        self._gemini2_ok = False
        # BigQuery is intentionally much slower than the normal quota poll.
        # Keep its last read-only status and only ask again on its own cadence.
        self._gcp_export_status = None
        self._gcp_export_error = False
        self._gcp_next_refresh = 0.0
        self._drag = None
        self._drag_active = False
        self._moving = False
        self._last_move = None
        self._save_source = 0
        #: Server row state: the last status it rendered, whether an action is
        #: in flight (the row's own gate, separate from the usage fetcher), and
        #: the preferred port (--dsh-port, else the remembered one).
        self._dsh_last = None
        self._dsh_busy = False
        self._dsh_port = dsh_port
        self.rows = {}
        self.key_values = {}
        self._build_window()
        # Detected after the window exists: creating it initialises GDK, and
        # only then does the display report which backend it really opened.
        self._wayland = self._detect_wayland()
        self._load_css()
        self._start()

    @staticmethod
    def _detect_wayland():
        """Is GDK really running on Wayland?

        WAYLAND_DISPLAY alone does not answer this: the launcher forces
        GDK_BACKEND=x11 (XWayland) so the panel is allowed to place itself, and
        under XWayland ``win.move()`` works — the cheaper and more precise of
        the two drag paths. Ask GDK which backend it actually opened and fall
        back to the environment only when that is unavailable.
        """
        try:
            display = Gdk.Display.get_default()
            name = GObject.type_name(display.__gtype__) if display else ""
        except Exception:
            name = ""
        if name:
            return "wayland" in name.lower()
        return bool(os.environ.get("WAYLAND_DISPLAY"))

    def _build_window(self):
        win = Gtk.Window()
        win.set_title(APP)
        try:
            win.set_wmclass(APP_ID, APP_ID)
        except Exception:
            pass
        win.set_default_size(320, -1)
        # No fixed height request: the panel grows with its rows (four usage
        # bars plus one row per managed key) instead of hard-coding a number
        # that every layout change would invalidate.
        win.set_resizable(False)
        win.set_decorated(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        try:
            win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        except Exception:
            pass
        win.set_app_paintable(True)
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        if cairo is not None:
            win.connect("draw", self._on_window_draw)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_name("panel")
        if cairo is None:
            panel.get_style_context().add_class("panel-fallback")
        if cairo is not None:
            panel.connect("draw", self._on_panel_draw)
        win.add(panel)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # The grip is the panel's title bar: a frameless window has none, so
        # this is the affordance that says "this moves". Braille dots read as a
        # texture rather than a character, which is what a handle should be.
        grip = Gtk.Button(label="⠿")
        grip.set_relief(Gtk.ReliefStyle.NONE)
        grip.set_focus_on_click(False)
        grip.set_can_focus(False)
        grip.set_tooltip_text("Drag to move the panel — the spot is remembered")
        grip.get_style_context().add_class("grip")
        grip.connect("realize", lambda *a: self._set_grip_cursor("grab"))
        title = Gtk.Label(label="OpenCode Usage", xalign=0.0, halign=Gtk.Align.FILL)
        title.get_style_context().add_class("title")
        refresh = Gtk.Button(label="↻")
        refresh.set_relief(Gtk.ReliefStyle.NONE)
        refresh.set_focus_on_click(False)
        refresh.set_can_focus(False)
        refresh.set_tooltip_text("Refresh now")
        refresh.get_style_context().add_class("refresh")
        refresh.connect("clicked", self._on_refresh_clicked)
        close = Gtk.Button(label="✕")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.get_style_context().add_class("close")
        close.connect("clicked", lambda *a: Gtk.main_quit())
        header.pack_start(grip, False, False, 0)
        header.pack_start(title, True, True, 0)
        header.pack_start(refresh, False, False, 0)
        header.pack_start(close, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        if subscription_row_enabled():
            body.pack_start(self._build_subscription_row(), False, False, 0)
        for key in WINDOWS:
            body.pack_start(self._build_row(key), False, False, 0)
        body.pack_start(self._build_row(OPENROUTER_ROW), False, False, 0)
        # Optional rows: packed only when this install has a source for them,
        # so a fresh clone shows no row that could only ever read zero.
        if gemini_row_enabled():
            body.pack_start(self._build_row(GEMINI_ROW), False, False, 0)
        if gemini2_row_enabled():
            body.pack_start(self._build_row(GEMINI2_ROW), False, False, 0)
        if gcp_row_enabled():
            body.pack_start(self._build_row(GCP_CREDIT_ROW), False, False, 0)
        body.pack_start(self._build_key_row(USAGE_REF, "main key"), False, False, 0)
        body.pack_start(self._build_key_row(SUBAGENT_REF, "subagent key"), False, False, 0)
        body.pack_start(self._build_dsh_section(), False, False, 0)

        self.status = Gtk.Label(label="loading…", xalign=0.0,
                                halign=Gtk.Align.START)
        self.status.get_style_context().add_class("status")

        panel.pack_start(header, False, False, 0)
        panel.pack_start(body, True, True, 0)
        panel.pack_start(self.status, False, False, 0)

        self.win = win
        self.panel = panel
        self.grip = grip
        self.refresh = refresh
        self._wire_events()
        self._refresh_key_row()
        self._render_gcp_credit()

    @staticmethod
    def _build_subscription_row():
        """Static subscription date, placed before the Zen quota windows."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("keyrow")
        name = Gtk.Label(label="OpenCode subscription", xalign=0.0,
                         halign=Gtk.Align.START)
        name.get_style_context().add_class("row-label")
        value = Gtk.Label(label="until " + OPENCODE_SUBSCRIPTION_ENDS,
                          xalign=1.0, halign=Gtk.Align.END)
        value.get_style_context().add_class("keyval")
        row.pack_start(name, False, False, 0)
        row.pack_start(value, True, True, 0)
        return row

    def _build_row(self, key):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if key == GCP_CREDIT_ROW:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    GCP_CLOUD_ICON, 16, 16, True)
                icon = Gtk.Image.new_from_pixbuf(pixbuf)
                top.pack_start(icon, False, False, 0)
            except Exception:
                pass
        name = Gtk.Label(label=WINDOW_LABELS.get(key, key), xalign=0.0,
                         halign=Gtk.Align.START)
        name.get_style_context().add_class("row-label")
        bar = UsageBar(12)
        pct = Gtk.Label(label="--", xalign=1.0, halign=Gtk.Align.END)
        pct.get_style_context().add_class("pct")
        top.pack_start(name, False, False, 0)
        top.pack_start(bar, True, True, 0)
        top.pack_start(pct, False, False, 0)
        reset = Gtk.Label(label="", xalign=0.0, halign=Gtk.Align.START)
        reset.get_style_context().add_class("countdown")
        row.pack_start(top, False, False, 0)
        row.pack_start(reset, False, False, 0)
        self.rows[key] = {"name": name, "bar": bar, "pct": pct,
                          "reset": reset, "box": row}
        return row

    # ---------------------------------------------------------------- key row

    def _build_key_row(self, ref, label):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("keyrow")
        name = Gtk.Label(label=label, xalign=0.0, halign=Gtk.Align.START)
        name.get_style_context().add_class("row-label")
        value = Gtk.Label(label="…", xalign=1.0, halign=Gtk.Align.END)
        value.get_style_context().add_class("keyval")
        button = Gtk.Button(label="change…")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_focus_on_click(False)
        button.get_style_context().add_class("keybtn")
        button.connect("clicked", lambda *a: self.open_key_dialog(ref, label))
        row.pack_start(name, False, False, 0)
        row.pack_start(value, True, True, 0)
        row.pack_start(button, False, False, 0)
        self.key_values[ref] = value
        return row

    def _refresh_key_row(self, ref=None):
        """Re-read the stored keys and render their masked fingerprints."""
        for name in (USAGE_REF, SUBAGENT_REF):
            if ref is not None and name != ref:
                continue
            label = self.key_values.get(name)
            if label is None:
                continue
            ctx = label.get_style_context()
            try:
                key = dsh_key_store.load_key(name=name)
            except dsh_key_store.KeyStoreError as exc:
                label.set_text("error")
                ctx.add_class("key-missing")
                self.status.set_text("⚠ %s" % exc)
                continue
            if key:
                label.set_text(dsh_key_store.masked(key))
                ctx.remove_class("key-missing")
            else:
                label.set_text("not set")
                ctx.add_class("key-missing")
        return False

    def _on_menu_set_key(self, *args):
        self.open_key_dialog(SUBAGENT_REF, "API key · subagents")
        return True

    def _on_menu_reset_position(self, *args):
        """Forget the remembered spot and go back to centre-right."""
        forget_saved_position()
        self._position = None
        self._explicit_position = False
        x, y = self._target_position(self.win)
        if x is not None:
            try:
                self.win.move(int(x), int(y))
            except Exception:
                pass
        self.status.set_text("position reset")
        return True

    # ------------------------------------------------------------- DSH server

    def _small_button(self, label, tooltip, handler):
        """The panel's one button shape: flat, tiny, keyboard-inert."""
        button = Gtk.Button(label=label)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_focus_on_click(False)
        button.set_can_focus(False)
        button.set_tooltip_text(tooltip)
        button.get_style_context().add_class("keybtn")
        button.connect("clicked", handler)
        return button

    def _build_dsh_section(self):
        """Status, the three controls and the URL line, in two compact rows."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("keyrow")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name = Gtk.Label(label=DSH_LABEL, xalign=0.0, halign=Gtk.Align.START)
        name.get_style_context().add_class("row-label")
        state = Gtk.Label(label="checking…", xalign=0.0, halign=Gtk.Align.START)
        state.get_style_context().add_class("dsh-state")
        self.dsh_start_btn = self._small_button(
            "▶", "Start the DSH server (first free port)", self._on_dsh_start)
        self.dsh_stop_btn = self._small_button(
            "■", "Stop the DSH server", self._on_dsh_stop)
        self.dsh_restart_btn = self._small_button(
            "⟳", "Restart the DSH server", self._on_dsh_restart)
        top.pack_start(name, False, False, 0)
        top.pack_start(state, True, True, 0)
        for button in (self.dsh_start_btn, self.dsh_stop_btn,
                       self.dsh_restart_btn):
            top.pack_start(button, False, False, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        detail = Gtk.Label(label="", xalign=0.0, halign=Gtk.Align.START)
        detail.get_style_context().add_class("countdown")
        self.dsh_open_btn = self._small_button(
            "open", "Open the DSH Web UI in the browser", self._on_dsh_open)
        self.dsh_copy_btn = self._small_button(
            "copy", "Copy the authenticated URL (it carries the launch token)",
            self._on_dsh_copy)
        bottom.pack_start(detail, True, True, 0)
        bottom.pack_start(self.dsh_open_btn, False, False, 0)
        bottom.pack_start(self.dsh_copy_btn, False, False, 0)

        box.pack_start(top, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        self.dsh_state = state
        self.dsh_detail = detail
        return box

    def _render_dsh(self, status):
        """Paint one status dict — the row's only writer, so it cannot drift."""
        status = status or {}
        running = bool(status.get("running"))
        ctx = self.dsh_state.get_style_context()
        for name in ("dsh-on", "dsh-off", "dsh-busy"):
            ctx.remove_class(name)
        if self._dsh_busy:
            ctx.add_class("dsh-busy")
            self.dsh_state.set_text("● working…")
        elif running:
            ctx.add_class("dsh-on")
            self.dsh_state.set_text("● running")
        else:
            ctx.add_class("dsh-off")
            self.dsh_state.set_text("● stopped")

        detail = []
        if running:
            detail.append(":%s" % status.get("port"))
            if status.get("pid"):
                detail.append("pid %s" % status["pid"])
            if status.get("uptime") is not None:
                detail.append("up %s" % humanize_age(status["uptime"]))
            # "url ?" is the honest answer for a server started elsewhere: the
            # port is up, but only the process that printed the token knows it.
            detail.append("url ✓" if status.get("url") else "url ?")
        else:
            # Before the first poll lands there is no status yet; fall back to
            # the port the panel would try, never to a KeyError.
            preferred = (status.get("preferred") or self._dsh_port
                         or DSH_DEFAULT_PORT)
            detail.append(":%d free" % preferred
                          if status.get("preferred_free")
                          else ":%d busy" % preferred)
        if status.get("note"):
            detail.append(status["note"])
        others = status.get("others") or []
        if others:
            detail.append("+%d more" % len(others))
        self.dsh_detail.set_text(" · ".join(detail))

        url = status.get("url")
        self.dsh_open_btn.set_sensitive(bool(url) and not self._dsh_busy)
        self.dsh_copy_btn.set_sensitive(bool(url) and not self._dsh_busy)
        self.dsh_start_btn.set_sensitive(not running and not self._dsh_busy)
        self.dsh_stop_btn.set_sensitive(running and not self._dsh_busy)
        self.dsh_restart_btn.set_sensitive(running and not self._dsh_busy)
        return False

    def _tick_dsh(self):
        """Poll the server off the GTK thread: /proc, plus a loopback probe.

        Cheap on purpose — it runs every `DSH_POLL_SECONDS` — and it is the
        only writer of the row's state, so a slow action cannot leave a stale
        "running" on screen.
        """
        if not self._dsh_busy:
            self._dsh_busy = True
            self._render_dsh(self._dsh_last)
            threading.Thread(target=self._dsh_worker, args=("status",),
                             daemon=True).start()
        return True

    def _dsh_worker(self, action):
        """Run one server action off the GTK thread, then re-read the status."""
        try:
            if action == "status":
                outcome = None
            elif action == "start":
                outcome = dsh_start(self._dsh_port)
            elif action == "stop":
                outcome = dsh_stop()
            else:
                outcome = dsh_restart(self._dsh_port)
            status = dsh_status(self._dsh_port)
        except Exception as exc:
            GLib.idle_add(self._on_dsh_error, action,
                          "%s: %s" % (type(exc).__name__, exc))
        else:
            status["action"] = action
            status["outcome"] = outcome
            GLib.idle_add(self._on_dsh_status, status)
        return False

    def _on_dsh_status(self, status):
        self._dsh_busy = False
        self._dsh_last = status
        self._render_dsh(status)
        self._report_dsh(status)
        return False

    def _on_dsh_error(self, action, message):
        self._dsh_busy = False
        self._render_dsh(self._dsh_last)
        self.status.set_text("⚠ dsh %s: %s" % (action, message))
        return False

    def _report_dsh(self, status):
        """One line at the bottom — but only for something worth saying."""
        action = status.get("action")
        outcome = status.get("outcome") or {}
        if action == "status":
            return
        if not outcome.get("ok"):
            self.status.set_text("⚠ dsh %s: %s" % (
                action, outcome.get("error") or "failed"))
            return
        note = outcome.get("note")
        if action == "start":
            if outcome.get("started"):
                self.status.set_text("dsh: started on :%s%s" % (
                    outcome.get("port"), " (%s)" % note if note else ""))
            else:
                self.status.set_text("dsh: %s" % (note or "already running"))
        elif action == "stop":
            stopped = outcome.get("stopped") or []
            self.status.set_text("dsh: stopped %s" % (
                ", ".join("pid %d" % pid for pid in stopped) or "nothing to stop"))
        else:
            self.status.set_text("dsh: restarted on :%s%s" % (
                outcome.get("port"), " (%s)" % note if note else ""))

    def _dsh_run(self, action):
        if self._dsh_busy:
            self.status.set_text("dsh: busy…")
            return
        self._dsh_busy = True
        self.status.set_text("dsh: %s…" % action)
        self._render_dsh(self._dsh_last)
        threading.Thread(target=self._dsh_worker, args=(action,),
                         daemon=True).start()

    def _on_dsh_start(self, *args):
        self._dsh_run("start")

    def _on_dsh_stop(self, *args):
        if self._confirm_dsh("stop"):
            self._dsh_run("stop")

    def _on_dsh_restart(self, *args):
        if self._confirm_dsh("restart"):
            self._dsh_run("restart")

    def _confirm_dsh(self, action):
        """Stop and restart cut every live session, so both ask first."""
        verb = "Restart" if action == "restart" else "Stop"
        dialog = Gtk.MessageDialog(
            transient_for=self.win, modal=True,
            message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE,
            text="%s the DSH server?" % verb)
        dialog.format_secondary_text(
            "dsh web is the Web UI's own server: stopping it ends every live "
            "session, including a running agent turn. The browser tab has to "
            "be reopened with the URL the server prints on its next start.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(verb, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _on_dsh_open(self, *args):
        url = (self._dsh_last or {}).get("url")
        if not url:
            self.status.set_text("dsh: no verified URL — start it from here")
            return
        if dsh_open_url(url):
            self.status.set_text("dsh: opening the Web UI…")
        else:
            self.status.set_text("⚠ dsh: no browser opener (xdg-open, chrome)")

    def _on_dsh_copy(self, *args):
        url = (self._dsh_last or {}).get("url")
        if not url:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(url, -1)
        clipboard.store()
        self.status.set_text("dsh: URL copied (it carries the launch token)")

    # ----------------------------------------------------------- key dialog

    def open_key_dialog(self, ref=SUBAGENT_REF, label="API key"):
        """Small dialog that validates and then stores a key for ``ref``."""
        dlg = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        dlg.set_title(label)
        dlg.set_transient_for(self.win)
        dlg.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dlg.set_default_size(430, -1)
        dlg.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)

        title = Gtk.Label(label=label, xalign=0.0)
        title.get_style_context().add_class("dialog-label")
        source = Gtk.Label(xalign=0.0)
        source.get_style_context().add_class("dialog-hint")
        try:
            source.set_text("%s · %s" % (ref, dsh_key_store.credentials_path()))
        except Exception:
            source.set_text(ref)
        hint = Gtk.Label(xalign=0.0)
        hint.get_style_context().add_class("dialog-hint")
        hint.set_text("Validated against the live endpoint before it is stored. "
                      "DSH picks it up on the next request — no restart.")
        hint.set_line_wrap(True)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_placeholder_text("paste the new key")
        entry.set_activates_default(True)

        shadow = Gtk.Label(xalign=0.0)
        shadow.get_style_context().add_class("dialog-hint")
        env = dsh_key_store.describe_environment_shadow()
        if env.get("shadowed"):
            shadow.set_text("⚠ %s is set in the environment and outranks this "
                            "file; DSH will ignore a write here."
                            % dsh_key_store.REF_NAME)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cancel = Gtk.Button(label="Cancel")
        save = Gtk.Button(label="Validate & save")
        save.set_sensitive(False)
        buttons.pack_end(save, False, False, 0)
        buttons.pack_end(cancel, False, False, 0)

        for widget in (title, source, hint, entry, shadow, buttons):
            box.pack_start(widget, False, False, 0)
        dlg.add(box)

        state = {"busy": False}

        def set_hint(text, kind=None):
            hctx = hint.get_style_context()
            for cls in ("hint-bad", "hint-ok"):
                hctx.remove_class(cls)
            if kind:
                hctx.add_class("hint-" + kind)
            hint.set_text(text)

        def on_changed(*a):
            if not state["busy"]:
                save.set_sensitive(bool(entry.get_text().strip()))

        def on_cancel(*a):
            dlg.destroy()

        def on_save(*a):
            if state["busy"]:
                return
            key = entry.get_text().strip()
            if not key:
                return
            state["busy"] = True
            save.set_sensitive(False)
            set_hint("validating against Zen…")
            threading.Thread(target=_save_worker, args=(key,), daemon=True).start()

        def _save_worker(key):
            ok, detail = dsh_key_store.validate_key(key)
            if not ok:
                GLib.idle_add(_save_failed, detail)
                return
            try:
                report = dsh_key_store.set_key(key, validate=False, name=ref)
            except dsh_key_store.KeyStoreError as exc:
                GLib.idle_add(_save_failed, str(exc))
                return
            GLib.idle_add(_save_ok, report, detail)

        def _save_failed(detail):
            state["busy"] = False
            save.set_sensitive(bool(entry.get_text().strip()))
            set_hint("✕ not stored — %s" % detail, "bad")
            return False

        def _save_ok(report, detail):
            state["busy"] = False
            set_hint("✓ stored (%s) — %s" % (report["current"]["prefix"], detail),
                     "ok")
            self._refresh_key_row(ref)
            self.status.set_text("key updated %s" % time.strftime("%H:%M:%S"))
            if ref == USAGE_REF:
                self._kickoff_fetch()
            GLib.timeout_add_seconds(2, _close_later)
            return False

        def _close_later():
            if dlg.get_mapped():
                dlg.destroy()
            return False

        def _on_dialog_key(widget, event):
            """Escape closes the dialog only; the panel keeps running."""
            if event.keyval == Gdk.KEY_Escape:
                dlg.destroy()
                return True
            return False

        entry.connect("changed", on_changed)
        cancel.connect("clicked", on_cancel)
        save.connect("clicked", on_save)
        dlg.connect("key-press-event", _on_dialog_key)
        dlg.show_all()
        entry.grab_focus()
        # Returns None on purpose: this is used directly as a signal handler
        # (menu "activate"), and GTK warns when one returns a value.

    def _load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _wire_events(self):
        # The panel body stays draggable, but the grip is the discoverable way
        # to do it: it carries the same handlers plus a grab cursor.
        drag_mask = (
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON_MOTION_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        for widget in (self.panel, self.grip):
            widget.add_events(drag_mask)
            widget.connect("button-press-event", self._on_press)
            widget.connect("button-release-event", self._on_release)
            widget.connect("motion-notify-event", self._on_motion)
        self.win.connect("key-press-event", self._on_key)
        self.win.connect("realize", self._on_realize)
        # Under Wayland the compositor owns the move and the button release may
        # never reach us, so every configure event during a drag is also a
        # chance to remember where the window ended up.
        self.win.connect("configure-event", self._on_configure)
        self.win.connect("destroy", Gtk.main_quit)

    def _start(self):
        GLib.idle_add(self._kickoff_fetch)
        GLib.timeout_add_seconds(self.interval, self._tick)
        # The server row keeps its own, much faster clock.
        self._tick_dsh()
        GLib.timeout_add_seconds(DSH_POLL_SECONDS, self._tick_dsh)

    def _tick(self):
        self._kickoff_fetch()
        return True

    def _kickoff_fetch(self):
        if self._fetching:
            return False
        self._fetching = True
        # Single place that marks the panel busy; _finish_fetch is the single
        # place that clears it, so the ↻ button always reflects reality.
        self.refresh.set_sensitive(False)
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        return False

    def _on_refresh_clicked(self, *args):
        """The ↻ button: the timer's worker, but with visible feedback."""
        if self._fetching:
            self.status.set_text("already refreshing…")
            return
        self.status.set_text("refreshing…")
        self._kickoff_fetch()

    def _fetch_worker(self):
        # try/finally, not a trailing call: _fetching gates every later poll,
        # so a worker that dies unexpectedly must still hand the panel back its
        # idle state instead of freezing the countdown forever.
        try:
            try:
                data = fetch_usage()
            except Exception as exc:
                GLib.idle_add(self._on_fetch_error,
                              "%s: %s" % (type(exc).__name__, exc))
            else:
                GLib.idle_add(self._on_fetch_ok, data)
            try:
                or_data = fetch_openrouter_usage()
            except Exception as exc:
                GLib.idle_add(self._on_openrouter_error,
                              "%s: %s" % (type(exc).__name__, exc))
            else:
                GLib.idle_add(self._on_openrouter_ok, or_data)
            if gemini_row_enabled():
                try:
                    gem_data = fetch_gemini_usage()
                except Exception as exc:
                    GLib.idle_add(self._on_gemini_error,
                                  "%s: %s" % (type(exc).__name__, exc))
                else:
                    GLib.idle_add(self._on_gemini_ok, gem_data)
            if gemini2_row_enabled():
                try:
                    gem2_data = fetch_gemini2_usage()
                except Exception as exc:
                    GLib.idle_add(self._on_gemini2_error,
                                  "%s: %s" % (type(exc).__name__, exc))
                else:
                    GLib.idle_add(self._on_gemini2_ok, gem2_data)
            if gcp_row_enabled() and time.monotonic() >= self._gcp_next_refresh:
                self._gcp_next_refresh = (
                    time.monotonic() + GCP_BILLING_REFRESH_SECONDS)
                try:
                    gcp_data = fetch_gcp_credit_estimate()
                except Exception:
                    # Do not put cloud API details in the on-screen panel. The
                    # verified balance stays visible and the status is concise.
                    GLib.idle_add(self._on_gcp_export_error)
                else:
                    GLib.idle_add(self._on_gcp_export_ok, gcp_data)
        finally:
            GLib.idle_add(self._finish_fetch)

    def _set_pct(self, row, percent, warn=False):
        ctx = row["pct"].get_style_context()
        for name in ("pct-green", "pct-amber", "pct-red"):
            ctx.remove_class(name)
        category = color_for(percent)
        cls = "pct-%s" % category
        ctx.add_class(cls)
        text = "%d%%" % int(round(percent))
        if warn:
            text = "⚠ " + text
        row["pct"].set_text(text)
        return category

    def _on_fetch_ok(self, data):
        self._last_ok = True
        for key in WINDOWS:
            row = self.rows.get(key)
            if row is None:
                continue
            if key not in data:
                row["box"].hide()
                continue
            row["box"].show()
            info = data[key]
            category = self._set_pct(row, info["percent"],
                                     warn=info["status"] != "ok")
            row["bar"].set_fraction(info["percent"] / 100.0, PALETTE[category])
            row["reset"].set_text(humanize_reset(info["resetsAt"]))
        self.status.set_text("updated %s" % time.strftime("%H:%M:%S"))
        return False

    def _on_openrouter_ok(self, data):
        self._openrouter_ok = True
        row = self.rows.get(OPENROUTER_ROW)
        if row is None:
            return False
        row["box"].show()
        percent = float(data.get("percent", 0.0) or 0.0)
        category = self._set_pct(row, percent)
        row["bar"].set_fraction(min(max(percent, 0.0), 100.0) / 100.0,
                                PALETTE[category])
        used = int(data.get("used", 0) or 0)
        limit = int(data.get("limit", 0) or 0)
        remaining = int(data.get("remaining", 0) or 0)
        row["reset"].set_text(
            "%d/%d usadas · %d restantes · %s"
            % (used, limit, remaining,
               humanize_reset(data.get("resetsAt", "")))
        )
        return False

    def _on_openrouter_error(self, message):
        row = self.rows.get(OPENROUTER_ROW)
        if row is None:
            return False
        if not self._openrouter_ok:
            self._set_pct(row, 0.0)
            row["bar"].set_fraction(0.0, PALETTE["muted"])
            row["reset"].set_text("⚠ OpenRouter: %s" % message)
        return False

    def _render_gemini_row(self, row, data):
        """Draw one Google account row: bar on RPD, detail line on rpm + tpm."""
        percent = float(data.get("percent", 0.0) or 0.0)
        category = self._set_pct(row, percent)
        row["bar"].set_fraction(min(max(percent, 0.0), 100.0) / 100.0,
                                PALETTE[category])
        row["reset"].set_text(gemini_detail_text(data))
        return False

    def _render_gcp_credit(self):
        """Render BigQuery's estimated GCP credit, or the verified fallback."""
        row = self.rows.get(GCP_CREDIT_ROW)
        if row is None:
            return
        estimated = self._gcp_export_status is not None
        remaining = (float(self._gcp_export_status.get("remaining", 0.0))
                     if estimated else GCP_CREDIT_REMAINING)
        remaining_pct = (100.0 * remaining / GCP_CREDIT_ORIGINAL)
        used_pct = 100.0 - remaining_pct
        category = self._set_pct(row, used_pct)
        # The bar is intentionally the remaining balance, unlike quota rows
        # whose bars represent consumption. The text makes that distinction
        # explicit while retaining the normal green/amber/red warning colors.
        pct_label = "%d%% est." if estimated else "%d%% left"
        row["pct"].set_text(pct_label % int(round(remaining_pct)))
        row["bar"].set_fraction(remaining_pct / 100.0, PALETTE[category])
        if estimated:
            first = self._gcp_export_status.get("first_usage", "?")
            last = self._gcp_export_status.get("last_usage", "?")
            export_text = "estimate · BQ %s to %s" % (first, last)
        elif self._gcp_export_error:
            export_text = "verified · BigQuery unavailable"
        else:
            export_text = "verified · BigQuery checking…"
        row["reset"].set_text(
            "$%.2f / $%.2f remaining · expires %s · %s"
            % (remaining, GCP_CREDIT_ORIGINAL,
               GCP_CREDIT_EXPIRES, export_text)
        )

    def _on_gcp_export_ok(self, data):
        self._gcp_export_status = data
        self._gcp_export_error = False
        self._render_gcp_credit()
        return False

    def _on_gcp_export_error(self):
        self._gcp_export_error = True
        self._render_gcp_credit()
        return False

    def _on_gemini_ok(self, data):
        self._gemini_ok = True
        row = self.rows.get(GEMINI_ROW)
        if row is None:
            return False
        row["box"].show()
        return self._render_gemini_row(row, data)

    def _on_gemini_error(self, message):
        row = self.rows.get(GEMINI_ROW)
        if row is None:
            return False
        if not self._gemini_ok:
            self._set_pct(row, 0.0)
            row["bar"].set_fraction(0.0, PALETTE["muted"])
            row["reset"].set_text("⚠ Gemini 1: %s" % message)
        return False

    def _on_gemini2_ok(self, data):
        self._gemini2_ok = True
        row = self.rows.get(GEMINI2_ROW)
        if row is None:
            return False
        row["box"].show()
        return self._render_gemini_row(row, data)

    def _on_gemini2_error(self, message):
        row = self.rows.get(GEMINI2_ROW)
        if row is None:
            return False
        if not self._gemini2_ok:
            self._set_pct(row, 0.0)
            row["bar"].set_fraction(0.0, PALETTE["muted"])
            row["reset"].set_text("⚠ Gemini 2: %s" % message)
        return False

    def _finish_fetch(self):
        self._fetching = False
        self.refresh.set_sensitive(True)
        return False

    def _on_fetch_error(self, message):
        if not self._last_ok and ("key" in message.lower()
                                  or "credential" in message.lower()
                                  or ".credentials" in message):
            for key in WINDOWS:
                row = self.rows.get(key)
                if row is None:
                    continue
                row["box"].show()
                self._set_pct(row, 0.0)
                row["reset"].set_text("no data")
                row["bar"].set_fraction(0.0, PALETTE["muted"])
            try:
                where = dsh_key_store.credentials_path()
            except Exception:
                where = "$DSH_HOME/.credentials.yaml"
            self.status.set_text("⚠ no %s — set it in %s" % (USAGE_REF, where))
            self._refresh_key_row()
            return False
        msg = "⚠ last update failed — retrying"
        self.status.set_text(msg)
        return False

    def _on_window_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        return False

    def _on_panel_draw(self, widget, cr):
        alloc = widget.get_allocation()
        rounded_rect(cr, 0.5, 0.5, alloc.width - 1.0, alloc.height - 1.0, 14.0)
        cr.set_source_rgba(*parse_color(PALETTE["panel"], 0.97))
        cr.fill()
        # Must return False: GTK's default container draw (which renders the
        # child widgets) only runs when the draw signal is not handled.
        return False

    def _on_press(self, widget, event):
        if event.button == 1:
            self._moving = True
            # Last drag's intent must not leak into this one.
            self._last_move = None
            self._set_grip_cursor("grabbing")
            if self._wayland:
                try:
                    self.win.begin_move_drag(event.button, int(event.x_root),
                                             int(event.y_root), event.time)
                    return True
                except Exception:
                    pass
            try:
                x, y = self.win.get_position()
                self._drag = (int(event.x_root) - x, int(event.y_root) - y)
            except Exception:
                self._drag = None
            self._drag_active = True
            return True
        if event.button == 3:
            self._show_menu(event)
            return True
        return False

    def _on_motion(self, widget, event):
        if self._wayland or not self._drag_active or self._drag is None:
            return False
        nx = int(event.x_root) - self._drag[0]
        ny = int(event.y_root) - self._drag[1]
        try:
            self.win.move(nx, ny)
        except Exception:
            pass
        else:
            # Remember what we asked for: on release this is a truer answer
            # than Gtk.Window.get_position(), whose cache trails the moves.
            self._last_move = (nx, ny)
        return True

    def _on_release(self, widget, event):
        moved = self._drag_active or self._moving
        self._drag_active = False
        self._drag = None
        self._moving = False
        self._set_grip_cursor("grab")
        if moved:
            # Two writes on purpose, both a few bytes. The immediate one means
            # closing the panel the instant the mouse is released cannot lose
            # the spot, and it uses the last position we asked for because GDK's
            # cached geometry can lag a motion event behind; the deferred one
            # re-reads the window and settles any adjustment the WM made.
            self._write_position(intent=self._last_move)
            self._schedule_position_save()
        return False

    def _on_configure(self, widget, event):
        """A move in progress is worth remembering; a resize is not."""
        if self._moving:
            self._schedule_position_save()
        return False

    def _schedule_position_save(self):
        """Write the position shortly after the last movement, not on each one.

        The path that needs this most is Wayland, where the compositor runs the
        drag and the button release never reaches the panel: the configure
        events are then the only signal that the window is moving.
        """
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(400, self._save_deferred)

    def _save_deferred(self):
        # The source has already fired: clearing the id first keeps the next
        # _schedule_position_save from trying to remove a dead source.
        self._save_source = 0
        self._write_position()
        return False

    def _write_position(self, intent=None):
        """Persist the window position; returns False if it could not be written.

        ``intent`` is the position the panel last asked the window manager for.
        It is preferred when known because it cannot be stale; otherwise the
        window is queried, which is the only option on the Wayland path where
        the compositor did the moving.
        """
        if intent is not None:
            x, y = intent
        else:
            try:
                x, y = self.win.get_position()
            except Exception:
                return False
        return save_position(x, y)

    def _set_grip_cursor(self, name):
        """Swap the grip's cursor; GTK3 has no widget.set_cursor.

        The GdkWindow only exists once the widget is realized, so every failure
        (called too early, or an unknown cursor name) is ignored: the cursor is
        decoration and the drag works without it.
        """
        grip = getattr(self, "grip", None)
        if grip is None:
            return
        win = grip.get_window()
        if win is None:
            return
        try:
            cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name)
        except Exception:
            return
        if cursor is not None:
            win.set_cursor(cursor)

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()
            return True
        return False

    def _show_menu(self, event):
        menu = Gtk.Menu()
        menu.attach_to_widget(self.panel, None)
        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self._on_refresh_clicked)
        setkey = Gtk.MenuItem(label="Change API key…")
        setkey.connect("activate", self._on_menu_set_key)
        resetpos = Gtk.MenuItem(label="Reset position")
        resetpos.connect("activate", self._on_menu_reset_position)
        dsh_start = Gtk.MenuItem(label="Start DSH server")
        dsh_start.connect("activate", self._on_dsh_start)
        dsh_stop = Gtk.MenuItem(label="Stop DSH server")
        dsh_stop.connect("activate", self._on_dsh_stop)
        dsh_restart = Gtk.MenuItem(label="Restart DSH server")
        dsh_restart.connect("activate", self._on_dsh_restart)
        dsh_open = Gtk.MenuItem(label="Open DSH Web UI")
        dsh_open.connect("activate", self._on_dsh_open)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *a: Gtk.main_quit())
        menu.append(refresh)
        menu.append(setkey)
        menu.append(resetpos)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(dsh_start)
        menu.append(dsh_stop)
        menu.append(dsh_restart)
        menu.append(dsh_open)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(quit_item)
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            try:
                menu.popup(None, None, None, None, event.button, event.time)
            except Exception:
                pass

    def _on_realize(self, widget):
        x, y = self._target_position(widget)
        if x is None:
            return
        try:
            widget.move(int(x), int(y))
        except Exception:
            pass

    def _target_position(self, widget):
        """The remembered spot, the --position override, or centre-right.

        Uses the modern Gdk API (Display.get_monitor_at_window + Monitor.
        get_workarea) because Gdk.Screen.get_monitor_geometry is deprecated and
        reports the full monitor box on Wayland, so it never raised and the
        window silently stayed unpositioned. The workarea also excludes the
        top bar and the dock, which the raw geometry does not.
        """
        if self._position is not None:
            # An explicit --position is taken at face value; a remembered spot
            # is only used while it still points at a monitor that exists.
            if self._explicit_position or self._position_is_reachable(widget):
                return self._position
        try:
            display = widget.get_display()
            gdkwin = widget.get_window()
            mon = None
            if gdkwin is not None:
                mon = display.get_monitor_at_window(gdkwin)
            if mon is None:
                mon = display.get_primary_monitor() or display.get_monitor(0)
            wa = mon.get_workarea()
            w = widget.get_allocated_width() or 320
            h = widget.get_allocated_height() or 232
            x = wa.x + wa.width - w - MARGIN_RIGHT
            y = wa.y + (wa.height - h) // 2      # vertically centred
            return (x, y)
        except Exception:
            return (None, None)

    def _position_is_reachable(self, widget):
        """Does the remembered spot still land on an attached monitor?

        A saved position outlives the layout that produced it: unplug the screen
        it was dropped on and the panel would come back off-screen — invisible
        and unreachable. Requiring a usable slice of the panel (60 px across,
        40 px down) inside some workarea sends it back to the default spot.
        """
        x, y = self._position
        w = widget.get_allocated_width() or 320
        h = widget.get_allocated_height() or 232
        try:
            display = widget.get_display()
            for i in range(display.get_n_monitors()):
                mon = display.get_monitor(i)
                if mon is None:
                    continue
                wa = mon.get_workarea()
                if (wa.x - w + 60 <= x <= wa.x + wa.width - 60
                        and wa.y - h + 40 <= y <= wa.y + wa.height - 40):
                    return True
            return False
        except Exception:
            # No usable monitor API: trusting the saved spot beats refusing it.
            return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usage_widget", description=APP)
    ap.add_argument("--once", action="store_true",
                    help="fetch data, print JSON, exit 0")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll interval in seconds (default 60)")
    ap.add_argument("--position", type=str, default=None,
                    help="place window at x,y (outranks the remembered spot)")
    ap.add_argument("--forget-position", action="store_true",
                    help="drop the remembered window position, exit")
    ap.add_argument("--show-key", action="store_true",
                    help="print masked fingerprints of both managed keys, exit")
    ap.add_argument("--check-key", action="store_true",
                    help="validate a stored key against Zen, exit")
    ap.add_argument("--set-key", action="store_true",
                    help="store a new key read from stdin, exit")
    ap.add_argument("--ref", type=str, default=None,
                    help="which key for --check-key/--set-key: main (default) "
                         "or sub")
    ap.add_argument("--key-file", type=str, default=None,
                    help="with --set-key, read the key from this file")
    ap.add_argument("--no-validate", action="store_true",
                    help="with --set-key, skip the live endpoint check")
    ap.add_argument("--dsh-status", action="store_true",
                    help="print the DSH server status as JSON, exit")
    ap.add_argument("--dsh-start", action="store_true",
                    help="start the DSH server (first free port wins), exit")
    ap.add_argument("--dsh-stop", action="store_true",
                    help="stop the DSH server, exit")
    ap.add_argument("--dsh-restart", action="store_true",
                    help="restart the DSH server, exit")
    ap.add_argument("--dsh-open", action="store_true",
                    help="open the DSH Web UI with its token, exit")
    ap.add_argument("--dsh-port", type=int, default=None,
                    help="preferred port for --dsh-start/--dsh-restart and for "
                         "the GUI's server row (default 3080)")
    args = ap.parse_args(argv)

    if args.dsh_status:
        print(json.dumps(dsh_status(args.dsh_port), indent=2, ensure_ascii=False))
        return 0

    if args.dsh_start or args.dsh_restart:
        action = dsh_start if args.dsh_start else dsh_restart
        result = action(args.dsh_port)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.dsh_stop:
        result = dsh_stop()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.dsh_open:
        url = dsh_status(args.dsh_port).get("url")
        if not url:
            sys.stderr.write("error: no running DSH server with a verified "
                             "URL (start it with --dsh-start)\n")
            return 1
        print(url)
        return 0 if dsh_open_url(url) else 1

    if args.show_key:
        print(json.dumps(dsh_key_store.status(), indent=2, ensure_ascii=False))
        return 0

    if args.forget_position:
        forget_saved_position()
        print("forgot the remembered position (%s)" % STATE_FILE)
        return 0

    if args.check_key:
        try:
            ref = dsh_key_store._resolve_ref(args.ref)
        except dsh_key_store.KeyStoreError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        try:
            key = dsh_key_store.load_key(name=ref)
        except dsh_key_store.KeyStoreError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        if not key:
            sys.stderr.write("error: no %s stored\n" % ref)
            return 1
        usable, detail, windows = dsh_key_store.probe_key(key)
        print("%s %s — %s (fingerprint %s)" % (
            ref, "USABLE" if usable else "NOT USABLE", detail,
            dsh_key_store.masked(key)))
        for win, info in sorted(windows.items()):
            info = info or {}
            print("   %-8s %s%%  resets %s" % (
                win, info.get("percent"), info.get("resetsAt")))
        return 0 if usable else 1

    if args.set_key:
        try:
            ref = dsh_key_store._resolve_ref(args.ref)
        except dsh_key_store.KeyStoreError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        if args.key_file:
            try:
                with open(args.key_file, "r", encoding="utf-8") as fh:
                    new_key = fh.read()
            except OSError as exc:
                sys.stderr.write("error: %s\n" % exc)
                return 1
        else:
            if sys.stdin.isatty():
                sys.stderr.write("Paste the new %s and press Enter: " % ref)
                sys.stderr.flush()
            new_key = sys.stdin.readline()
        if not new_key.strip():
            sys.stderr.write("error: no key supplied\n")
            return 2
        try:
            report = dsh_key_store.set_key(
                new_key, validate=not args.no_validate, name=ref)
        except dsh_key_store.KeyStoreError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        print("stored %s as %s in %s (sha256_8 %s)" % (
            report["current"]["prefix"], report["ref"], report["path"],
            report["current"]["sha256_8"]))
        if report["environment"].get("shadowed"):
            sys.stderr.write(
                "warning: %s is set in the environment and outranks the file\n"
                % dsh_key_store.REF_NAME)
        return 0

    if args.once:
        return run_once()

    position = None
    if args.position:
        parts = args.position.split(",")
        try:
            position = (int(parts[0].strip()), int(parts[1].strip()))
        except Exception:
            position = None

    GLib.set_prgname(APP_ID)
    widget = UsageWidget(interval=args.interval, position=position,
                         dsh_port=args.dsh_port)
    widget.win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
