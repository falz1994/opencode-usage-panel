#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression checks for the panel's window behavior (needs a display).

Covers the three things the header controls promise:

  * the `⠿` grip drags the window and the drop position is remembered;
  * a fresh start reopens the panel at that remembered spot, but only while it
    still lands on an attached monitor (an off-screen spot falls back to the
    default place instead of opening where nobody can reach it);
  * `↻` refreshes on demand: visible status, insensitive while the fetch runs,
    and always re-enabled afterwards.

Run it the way the launcher runs the panel:

    GDK_BACKEND=x11 python3 test_window_drag.py

The state file is redirected to a temp directory, so the panel's real
`~/.config/opencode-usage-widget/window.json` is never touched. The refresh
section waits for a real fetch; offline it still passes, because a fetch that
reports a failure has also finished.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

import usage_widget as uw  # noqa: E402

TMP = tempfile.mkdtemp(prefix="panel-state-")
uw.STATE_DIR = TMP
uw.STATE_FILE = os.path.join(TMP, "window.json")

FAILS = []


def check(label, cond, detail=""):
    print("%s %s%s" % ("PASS " if cond else "FAIL ", label,
                       (" — %s" % (detail,)) if detail else ""))
    if not cond:
        FAILS.append(label)


def pump(ms):
    """Run a GTK main loop for ``ms`` milliseconds."""
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, lambda: (loop.quit(), False)[1])
    loop.run()


class Ev:
    """The slice of a Gdk.EventButton the drag handlers actually read."""

    def __init__(self, x, y, button=1):
        self.button = button
        self.x_root = x
        self.y_root = y
        self.time = 0


def drag(widget, frm, to, steps=3):
    """Press on the grip, move, release — the real signal handlers."""
    widget._on_press(widget.grip, Ev(frm[0], frm[1]))
    for i in range(1, steps + 1):
        x = frm[0] + (to[0] - frm[0]) * i // steps
        y = frm[1] + (to[1] - frm[1]) * i // steps
        widget._on_motion(widget.grip, Ev(x, y))
    widget._on_release(widget.grip, Ev(to[0], to[1]))


def state():
    try:
        with open(uw.STATE_FILE) as fh:
            return json.load(fh)
    except OSError:
        return None


def wait_for(predicate, seconds=60):
    while seconds > 0 and not predicate():
        pump(500)
        seconds -= 0.5
    return predicate()


print("== backend (GDK_BACKEND=%s, WAYLAND_DISPLAY=%s) =="
      % (os.environ.get("GDK_BACKEND"), os.environ.get("WAYLAND_DISPLAY")))

w1 = uw.UsageWidget(interval=3600, position=(400, 300))
w1.win.show_all()
pump(400)

check("backend detected as X11, not Wayland", w1._wayland is False, w1._wayland)
check("explicit --position honoured", w1.win.get_position() == (400, 300),
      w1.win.get_position())
check("grip button exists with .grip class",
      w1.grip.get_style_context().has_class("grip"))
check("refresh button exists with .refresh class",
      w1.refresh.get_style_context().has_class("refresh"))

print("\n== drag the grip ==")
drag(w1, (410, 310), (760, 560))
# No pump first: the drop must be remembered even if the panel is closed before
# the event loop runs again. Gtk.Window.get_position() is only trustworthy once
# the queued moves have been dispatched, so it is checked after a pump.
check("drop position persisted immediately (no pump)",
      state() == {"x": 750, "y": 550}, state())
pump(700)                      # dispatch the moves, then past the debounce
pos = w1.win.get_position()
check("window followed the drag (offset 10,10)", pos == (750, 550), pos)
check("still correct after the debounce fires",
      state() == {"x": 750, "y": 550}, state())

print("\n== a fresh start reuses the remembered spot ==")
w1.win.hide()
w2 = uw.UsageWidget(interval=3600)
w2.win.show_all()
pump(400)
check("no --position: remembered spot loaded", w2._position == (750, 550),
      w2._position)
check("window opened at the remembered spot",
      w2.win.get_position() == (750, 550), w2.win.get_position())

print("\n== refresh button ==")
check("idle after the startup fetch",
      wait_for(lambda: not w2._fetching and w2.refresh.get_sensitive()),
      "status=%r" % w2.status.get_text())
w2._on_refresh_clicked()
check("status says refreshing", w2.status.get_text() == "refreshing…",
      w2.status.get_text())
check("button disabled while fetching", not w2.refresh.get_sensitive())
w2._on_refresh_clicked()
check("second click is refused", w2.status.get_text() == "already refreshing…",
      w2.status.get_text())
check("button re-enabled when the fetch finished",
      wait_for(lambda: w2.refresh.get_sensitive()),
      "status=%r" % w2.status.get_text())
check("fetch produced a result",
      "updated" in w2.status.get_text() or "⚠" in w2.status.get_text(),
      w2.status.get_text())

print("\n== an off-screen remembered spot falls back to the default ==")
w2.win.hide()
with open(uw.STATE_FILE, "w") as fh:
    json.dump({"x": 99999, "y": 99999}, fh)
w3 = uw.UsageWidget(interval=3600)
w3.win.show_all()
pump(400)
check("off-screen spot rejected", w3._position_is_reachable(w3.win) is False)
check("fell back to centre-right", w3.win.get_position() == (1572, 403),
      w3.win.get_position())

print("\n== a corrupt state file is survivable ==")
with open(uw.STATE_FILE, "w") as fh:
    fh.write("{not json")
check("load_saved_position returns None", uw.load_saved_position() is None)

print("\n== helpers ==")
uw.save_position(12, 34)
check("save/load round-trip", uw.load_saved_position() == (12, 34))
check("forget removes the file",
      uw.forget_saved_position() and uw.load_saved_position() is None)

for w in (w1, w2, w3):
    w.win.hide()
shutil.rmtree(TMP, ignore_errors=True)

print("\n%s" % ("ALL CHECKS PASSED" if not FAILS
                else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
