#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the DSH-server half of the panel (usage_widget.py).

The interesting parts of that feature are the ones that must not guess: which
port to start on when the preferred one is taken, whether the thing listening on
a port really is a Harness server, and whether a token found in a log still
authenticates against the process that is running *now*. So the tests stand up a
fake server that speaks the two answers the real one gives — 401 without a
token, 303 with a valid one — and drive the module's functions against it.

No GTK window is created and no real `dsh web` is started or stopped.

    python3 test_dsh_server.py            # unittest, exit 0 when green
    python3 test_dsh_server.py -v
"""

import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_widget  # noqa: E402

SECRET = "test-token_ABC-123"


def free_port():
    """A port nothing is listening on, straight from the OS."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FakeDSH(http.server.BaseHTTPRequestHandler):
    """The two behaviours the panel depends on, and nothing else."""

    def do_GET(self):  # noqa: N802 - http.server's own spelling
        if self.path == "/?token=%s" % SECRET:
            self.send_response(303)
            self.send_header("location", "/")
            self.end_headers()
            return
        self.send_response(401)
        self.send_header("content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"unauthorized")

    def log_message(self, *args):
        pass


class FakeServer:
    def __init__(self):
        self.port = free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                     _FakeDSH)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class PlainServer:
    """A listener that is *not* a Harness: it answers 200 to everything."""

    def __init__(self):
        self.port = free_port()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"hello")

            def log_message(self, *args):
                pass

        self.hits = 0
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                     Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class DshTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsh-panel-test-")
        # Every path the module writes is redirected: the real logs and the
        # real remembered port belong to the running panel, not to a test.
        self._saved = {name: getattr(usage_widget, name) for name in
                       ("DSH_PANEL_LOG", "DSH_LAUNCHER_LOG", "DSH_STATE_FILE")}
        usage_widget.DSH_PANEL_LOG = os.path.join(self.tmp.name, "panel.log")
        usage_widget.DSH_LAUNCHER_LOG = os.path.join(self.tmp.name, "launcher.log")
        usage_widget.DSH_STATE_FILE = os.path.join(self.tmp.name, "dsh.json")
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        for name, value in self._saved.items():
            setattr(usage_widget, name, value)
        self.tmp.cleanup()

    def fake(self):
        server = FakeServer()
        self.servers.append(server)
        return server

    def plain(self):
        server = PlainServer()
        self.servers.append(server)
        return server

    # ------------------------------------------------------------ detection

    def test_process_match_accepts_the_real_command_lines(self):
        real = [
            "node /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web --no-open --port 3080",
            "/usr/bin/node /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web",
            "node /opt/dsh/lib/bin.js --profile web --port 9000",
            "/usr/local/bin/dsh web",
        ]
        for cmdline in real:
            self.assertTrue(usage_widget._looks_like_dsh_web(cmdline), cmdline)

    def test_process_match_rejects_lookalikes(self):
        lookalikes = [
            "",
            "python3 /opt/opencode-usage-panel/usage_widget.py --interval 300",
            "bash -c grep 'dsh/lib/bin.js web' /var/log/syslog",
            "node /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js --profile headless run tests",
            "vim /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js",
        ]
        for cmdline in lookalikes:
            self.assertFalse(usage_widget._looks_like_dsh_web(cmdline), cmdline)

    # --------------------------------------------------------------- banner

    def test_last_banner_wins_and_the_lan_url_is_ignored(self):
        with open(usage_widget.DSH_PANEL_LOG, "w", encoding="utf-8") as fh:
            fh.write("booting\n")
            fh.write("dsh web: http://127.0.0.1:3080/?token=old-token "
                     "(LAN: http://100.64.0.1:3080/?token=old-token)\n")
            fh.write("dsh web: http://127.0.0.1:3081/?token=new-token\n")
        self.assertEqual(usage_widget.dsh_last_banner(usage_widget.DSH_PANEL_LOG),
                         (3081, "new-token"))

    def test_last_banner_on_a_missing_log_is_none(self):
        self.assertIsNone(usage_widget.dsh_last_banner("/nonexistent/log"))

    def test_saved_port_round_trip(self):
        self.assertIsNone(usage_widget.dsh_saved_port())
        usage_widget.dsh_save_port(3099)
        self.assertEqual(usage_widget.dsh_saved_port(), 3099)
        with open(usage_widget.DSH_STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertIsNone(usage_widget.dsh_saved_port())

    # ----------------------------------------------------------- port rules

    def test_free_port_is_used_as_is(self):
        port = free_port()
        self.assertEqual(usage_widget.dsh_pick_port(port), (port, ""))

    def test_port_taken_by_another_service_falls_through(self):
        busy = self.plain()
        chosen, note = usage_widget.dsh_pick_port(busy.port)
        self.assertGreater(chosen, busy.port)
        self.assertIn("busy", note)
        self.assertFalse(usage_widget.port_in_use(chosen))

    def test_port_taken_by_a_harness_means_nothing_to_start(self):
        server = self.fake()
        chosen, note = usage_widget.dsh_pick_port(server.port)
        self.assertIsNone(chosen)
        self.assertIn("already running", note)

    # ---------------------------------------------------------------- probe

    def test_a_harness_answers_401_and_a_plain_server_does_not(self):
        harness = self.fake()
        plain = self.plain()
        self.assertTrue(usage_widget.dsh_is_server(harness.port))
        self.assertFalse(usage_widget.dsh_is_server(plain.port))
        self.assertIsNone(usage_widget.dsh_http_code(free_port()))

    def test_token_is_live_only_against_the_running_process(self):
        server = self.fake()
        self.assertTrue(usage_widget.dsh_token_live(server.port, SECRET))
        self.assertFalse(usage_widget.dsh_token_live(server.port, "stale-token"))

    def test_known_url_requires_a_verified_token(self):
        server = self.fake()
        with open(usage_widget.DSH_PANEL_LOG, "w", encoding="utf-8") as fh:
            fh.write("dsh web: http://127.0.0.1:%d/?token=%s\n"
                     % (server.port, SECRET))
        self.assertEqual(usage_widget.dsh_known_url(server.port),
                         "http://127.0.0.1:%d/?token=%s" % (server.port, SECRET))
        # A banner from a previous process must not be offered as a link.
        with open(usage_widget.DSH_PANEL_LOG, "w", encoding="utf-8") as fh:
            fh.write("dsh web: http://127.0.0.1:%d/?token=stale-token\n"
                     % server.port)
        self.assertIsNone(usage_widget.dsh_known_url(server.port))

    def test_known_url_reads_the_desktop_launcher_log_too(self):
        server = self.fake()
        with open(usage_widget.DSH_LAUNCHER_LOG, "w", encoding="utf-8") as fh:
            fh.write("dsh web: http://127.0.0.1:%d/?token=%s\n"
                     % (server.port, SECRET))
        self.assertIsNotNone(usage_widget.dsh_known_url(server.port))

    # --------------------------------------------------------------- status

    def test_status_of_a_running_server_with_a_recoverable_url(self):
        server = self.fake()
        with open(usage_widget.DSH_PANEL_LOG, "w", encoding="utf-8") as fh:
            fh.write("dsh web: http://127.0.0.1:%d/?token=%s\n"
                     % (server.port, SECRET))
        status = usage_widget.dsh_status(server.port)
        self.assertTrue(status["running"])
        self.assertEqual(status["port"], server.port)
        self.assertIsNotNone(status["url"])
        self.assertFalse(status["preferred_free"])

    def test_status_of_a_running_server_without_a_token_says_so(self):
        server = self.fake()
        status = usage_widget.dsh_status(server.port)
        self.assertTrue(status["running"])
        self.assertIsNone(status["url"])

    def test_status_of_a_stopped_server_reports_a_free_port(self):
        port = free_port()
        status = usage_widget.dsh_status(port)
        self.assertFalse(status["running"])
        self.assertIsNone(status["port"])
        self.assertTrue(status["preferred_free"])

    def test_status_remembers_the_port_this_panel_started(self):
        server = self.fake()
        usage_widget.dsh_save_port(server.port)
        status = usage_widget.dsh_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["port"], server.port)

    # ------------------------------------------------------- start and stop

    def test_start_refuses_when_the_server_is_already_up(self):
        server = self.fake()
        result = usage_widget.dsh_start(server.port)
        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("already running", result["note"])

    def test_stop_signals_every_server_process(self):
        victim = subprocess.Popen(["sleep", "60"])
        original = usage_widget.dsh_web_processes
        try:
            usage_widget.dsh_web_processes = lambda: [
                {"pid": victim.pid, "port": 3080, "uptime": 1.0, "cmdline": "x"}]
            result = usage_widget.dsh_stop(timeout=5)
            self.assertTrue(result["ok"])
            self.assertIn(victim.pid, result["stopped"])
            victim.wait(timeout=5)
            self.assertIsNotNone(victim.returncode)
        finally:
            # Restore, or the next test stops this fake "server" again.
            usage_widget.dsh_web_processes = original
            if victim.poll() is None:
                victim.kill()

    def test_stop_with_nothing_to_stop_reports_the_port_state(self):
        result = usage_widget.dsh_stop()
        self.assertEqual(result["stopped"], [])
        self.assertEqual(result["killed"], [])

    # ----------------------------------------------------------- formatting

    def test_humanize_age(self):
        cases = [(0, "0s"), (45, "45s"), (60, "1m"), (599, "9m"),
                 (3600, "1h00m"), (3660, "1h01m"), (86400, "1d00h")]
        for seconds, expected in cases:
            self.assertEqual(usage_widget.humanize_age(seconds), expected, seconds)

    def test_port_in_use_agrees_with_a_real_listener(self):
        server = self.fake()
        self.assertTrue(usage_widget.port_in_use(server.port))
        self.assertFalse(usage_widget.port_in_use(free_port()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
