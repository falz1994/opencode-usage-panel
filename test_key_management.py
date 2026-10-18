#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the two-key credential management (dsh_key_store + panel UI paths).

Run:  python3 test_key_management.py

Nothing here touches the live credential store: every write goes to a temp
document selected through DSH_KEY_STORE_PATH, and the suite asserts at the end
that ~/.dsh/.credentials.yaml is byte-for-byte unchanged. The GUI section
replaces gi.repository with stubs before importing the widget, so it exercises
the construction and dialog code paths without needing a display.
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REAL_CREDENTIALS = os.path.join(
    os.path.expanduser(os.environ.get("DSH_HOME") or "~/.dsh"),
    ".credentials.yaml")
REAL_BEFORE = (open(REAL_CREDENTIALS, "rb").read()
               if os.path.exists(REAL_CREDENTIALS) else None)

WORK = tempfile.mkdtemp(prefix="dsh-key-tests-")
FAKE = os.path.join(WORK, "creds", ".credentials.yaml")

MAIN = "oc_sk_MAINMAINMAINMAINMAINMAINMAINMAINMAINMAINMAIN"
SUB = "oc_sk_SUBSUBSUBSUBSUBSUBSUBSUBSUBSUBSUBSUBSUBSUBSUB"

import dsh_key_store as S  # noqa: E402

store_results = []
gui_results = []


def record(bucket, name, cond, extra=""):
    bucket.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


def check_store(name, cond, extra=""):
    record(store_results, name, cond, extra)


def check_gui(name, cond, extra=""):
    record(gui_results, name, cond, extra)


# ==========================================================================
# 1. store: document handling, isolation, atomicity, locking
# ==========================================================================

os.environ["DSH_KEY_STORE_PATH"] = FAKE
os.makedirs(os.path.dirname(FAKE), mode=0o700, exist_ok=True)

SEED = """# managed by dsh
version: 1
refs:
  OPENCODE_API_KEY: %s
  OTHER_KEY: keep-me-untouched
records:
  client-connection/browser-session:
    kind: grant
    payload:
      version: 1
      secret: LONGSECRETVALUE1234567890abcdefghijklmno
""" % MAIN
open(FAKE, "w").write(SEED)
os.chmod(FAKE, 0o600)

check_store("path override respeta DSH_KEY_STORE_PATH", S.credentials_path() == FAKE)
check_store("load_key() por defecto = key principal", S.load_key() == MAIN)
check_store("load_key(name=SUB) esta vacia al inicio",
            S.load_key(name=S.SUBAGENT_REF_NAME) is None)
check_store("masked no revela la key", "MAINMAIN" not in S.masked(MAIN), S.masked(MAIN))

# --- escribir la key de SUBAGENTES no debe tocar la principal
rep = S.set_key(SUB, validate=False, name=S.SUBAGENT_REF_NAME)
after = open(FAKE).read()
check_store("key de subagentes almacenada", S.load_key(name=S.SUBAGENT_REF_NAME) == SUB)
check_store("key PRINCIPAL intacta tras escribir la de subagentes", S.load_key() == MAIN)
check_store("report ref correcto", rep["ref"] == S.SUBAGENT_REF_NAME)
check_store("permisos 0600", (os.stat(FAKE).st_mode & 0o777) == 0o600)
check_store("records intacto", "LONGSECRETVALUE1234567890abcdefghijklmno" in after)
check_store("otra ref intacta", "OTHER_KEY: keep-me-untouched" in after)
check_store("comentario inicial intacto", after.startswith("# managed by dsh"))
check_store("dos refs presentes", after.count("OPENCODE_API_KEY") == 1
            and after.count("OPENCODE_SUBAGENT_API_KEY") == 1)
check_store("load_all devuelve ambas", S.load_all() == {
    S.REF_NAME: MAIN, S.SUBAGENT_REF_NAME: SUB})

# --- escribir la principal no debe tocar la de subagentes
MAIN2 = "oc_sk_MAIN2MAIN2MAIN2MAIN2MAIN2MAIN2MAIN2MAIN2MAIN2"
S.set_key(MAIN2, validate=False)
check_store("principal actualizada", S.load_key() == MAIN2)
check_store("subagentes intacta tras escribir la principal",
            S.load_key(name=S.SUBAGENT_REF_NAME) == SUB)

# --- idempotencia y estructura
before_text = open(FAKE).read()
S.set_key(MAIN2, validate=False)
check_store("reescritura identica -> contenido identico", open(FAKE).read() == before_text)
check_store("una sola linea por ref", open(FAKE).read().count("OPENCODE_API_KEY:") == 1)

# --- rechazos
for bad, label in (("", "key vacia"),
                   ("oc_sk_x\noc_sk_y", "key con newline"),
                   ("oc sk con espacio", "key con espacio")):
    try:
        S.set_key(bad, validate=False)
        check_store("rechaza " + label, False, "no rechazo")
    except S.KeyStoreError as exc:
        check_store("rechaza " + label, True, str(exc)[:55])

# --- permisos flojos
os.chmod(FAKE, 0o644)
try:
    S.load_key()
    check_store("detecta permisos flojos", False, "no aviso")
except S.KeyStoreError as exc:
    check_store("detecta permisos flojos", "readable beyond its owner" in str(exc))
os.chmod(FAKE, 0o600)

# --- locking
lock = FAKE + ".lock"
open(lock, "w").write("other")
try:
    S.set_key("oc_sk_LOCKEDLOCKEDLOCKEDLOCKEDLOCKEDLOCKED", validate=False)
    check_store("lock contendido no se pisa", False, "escribio igual")
except S.KeyStoreError as exc:
    check_store("lock contendido no se pisa", "writer lock" in str(exc))
finally:
    os.unlink(lock)

open(lock, "w").write("stale")
os.utime(lock, (0, 0))
try:
    S.set_key("oc_sk_AFTERSTALEAFTERSTALEAFTERSTALEAFTERSTAL", validate=False)
    check_store("lock abandonado se rompe", S.load_key().startswith("oc_sk_AFTERSTALE"))
finally:
    if os.path.exists(lock):
        os.unlink(lock)

# --- documento sin refs
open(FAKE, "w").write("version: 1\nrecords: {}\n")
os.chmod(FAKE, 0o600)
try:
    S.set_key("oc_sk_WHATEVER", validate=False)
    check_store("rechaza doc sin refs", False, "acepto")
except S.KeyStoreError as exc:
    check_store("rechaza doc sin refs", "no top-level 'refs:'" in str(exc))

# --- crear desde cero
os.unlink(FAKE)
S.set_key("oc_sk_CREATEDCREATEDCREATEDCREATEDCREATEDCRE", validate=False)
check_store("crea documento desde cero", open(FAKE).read().startswith("version: 1\nrefs:\n"))
check_store("creado con 0600", (os.stat(FAKE).st_mode & 0o777) == 0o600)

# --- aliases del CLI
check_store("alias 'sub' resuelve a la ref de subagentes",
            S._resolve_ref("sub") == S.SUBAGENT_REF_NAME)
check_store("alias 'main' resuelve a la principal", S._resolve_ref("main") == S.REF_NAME)
check_store("nombre completo aceptado",
            S._resolve_ref(S.SUBAGENT_REF_NAME) == S.SUBAGENT_REF_NAME)
try:
    S._resolve_ref("nope")
    check_store("ref invalida rechazada", False, "acepto")
except S.KeyStoreError:
    check_store("ref invalida rechazada", True)

# --- shadowing por entorno
check_store("detecta entorno shadow",
            S.describe_environment_shadow({S.REF_NAME: "x"})["shadowed"] is True)
check_store("sin env no hay shadow", S.describe_environment_shadow({})["shadowed"] is False)

# --- probe_key distingue autenticacion de cuota (sin red)
_real_urlopen = S.urlopen


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    def read(self):
        return self._payload.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_response(payload):
    S.urlopen = lambda *a, **k: _FakeResp(payload)


_patch_response('{"usage": {"rolling": {"status": "ok", "percent": 5},'
                ' "monthly": {"status": "ok", "percent": 55}}}')
usable, detail, windows = S.probe_key("oc_sk_x")
check_store("probe_key: key con cuota -> usable", usable and len(windows) == 2, detail)

_patch_response('{"usage": {"rolling": {"status": "ok", "percent": 0},'
                ' "monthly": {"status": "rate-limited", "percent": 100}}}')
usable, detail, windows = S.probe_key("oc_sk_x")
check_store("probe_key: workspace agotado -> NO usable", not usable, detail)
check_store("probe_key nombra la ventana agotada", "monthly" in detail, detail)

S.urlopen = _real_urlopen

# ==========================================================================
# 2. GUI paths, with gi.repository stubbed
# ==========================================================================

os.environ["DSH_KEY_STORE_PATH"] = FAKE
os.makedirs(os.path.dirname(FAKE), mode=0o700, exist_ok=True)
open(FAKE, "w").write("version: 1\nrefs:\n  OPENCODE_API_KEY: %s\n" % MAIN)
os.chmod(FAKE, 0o600)

import types  # noqa: E402


class StyleContextStub:
    def __init__(self):
        self._c = set()

    def add_class(self, n):
        self._c.add(n)

    def remove_class(self, n):
        self._c.discard(n)

    def list_classes(self):
        return sorted(self._c)

    def has_class(self, n):
        return n in self._c


ENTRIES = []
HANDLERS = {}


class StubWidget:
    def __init__(self, *a, **k):
        self._text = k.get("label", "")
        self._handlers = {}

    def connect(self, sig, cb):
        HANDLERS.setdefault(sig, []).append(cb)
        return 1

    def __getattr__(self, item):
        if item == "get_style_context":
            return lambda *a, **k: StyleContextStub()
        if item == "get_mapped":
            return lambda *a, **k: True

        def any_method(*a, **k):
            if item.startswith("get_") or item == "list_classes":
                return []
            return None

        return any_method


class GdkNS(types.ModuleType):
    class EventMask:
        BUTTON_PRESS_MASK = 1
        BUTTON_RELEASE_MASK = 2
        BUTTON_MOTION_MASK = 4
        POINTER_MOTION_MASK = 8

    class WindowTypeHint:
        UTILITY = 1

    KEY_Escape = 0xff1b

    class Screen(StubWidget):
        @staticmethod
        def get_default():
            return None

        def get_rgba_visual(self):
            return None


class GdkPixbufNS(types.ModuleType):
    """Just enough for `from gi.repository import GdkPixbuf` to resolve.

    The widget only reaches for a pixbuf while building the Google Cloud credit
    row, inside a `try/except` — and that row is not built at all unless the
    BigQuery project and export are configured. So the import has to succeed and
    nothing else has to work.
    """

    class Pixbuf:
        @staticmethod
        def new_from_file_at_scale(path, width, height, preserve):
            raise RuntimeError("no pixbuf in tests")


class GObjectNS(types.ModuleType):
    """`GObject.type_name` — the only GObject call the widget makes.

    It asks for the display's type name to tell X11 from Wayland. The stub
    display has no `__gtype__`, so this is reached only in the real toolkit;
    returning a string keeps the shape honest either way.
    """

    @staticmethod
    def type_name(gtype):
        return str(gtype)


class GtkNS(types.ModuleType):
    class Orientation:
        VERTICAL = 1
        HORIZONTAL = 2

    class Align:
        START = 1
        END = 2
        FILL = 3

    class ReliefStyle:
        NONE = 0

    class WindowType:
        TOPLEVEL = 0

    class WindowPosition:
        CENTER_ON_PARENT = 1

    STYLE_PROVIDER_PRIORITY_APPLICATION = object()

    class DrawingArea(StubWidget):
        pass

    class Window(StubWidget):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._title = ""

        def set_title(self, t):
            self._title = t

        def get_screen(self):
            return GdkNS.Screen()

    class Box(StubWidget):
        pass

    class Label(StubWidget):
        def set_text(self, t):
            self._text = t

        def get_text(self):
            return self._text

    class Button(StubWidget):
        def set_label(self, t):
            self._text = t

    class Entry(StubWidget):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._text = ""
            ENTRIES.append(self)

        def get_text(self):
            return self._text

        def set_text(self, t):
            self._text = t

    class Menu(StubWidget):
        pass

    class MenuItem(StubWidget):
        pass

    class CssProvider(StubWidget):
        def load_from_data(self, d):
            self._data = d

    class StyleContext(StubWidget):
        @staticmethod
        def add_provider_for_screen(*a, **k):
            return None

    @staticmethod
    def main_quit():
        pass

    @staticmethod
    def main():
        pass


class GLibNS(types.ModuleType):
    @staticmethod
    def idle_add(fn, *a, **k):
        fn(*a, **k)
        return 1

    @staticmethod
    def timeout_add_seconds(*a, **k):
        return 1

    @staticmethod
    def set_prgname(*a, **k):
        return None


class _Anything:
    """A permissive stand-in for whatever these stubs do not define.

    The credential logic is what is under test here, not GTK. The widget is
    free to grow new toolkit calls, and a stub that had to enumerate them all
    would break on the next one with an `AttributeError` that says nothing about
    credentials. Anything the stubs below do not define explicitly resolves to
    this instead: callable, subscriptable, and happy to return more of itself.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Anything()

    def __getattr__(self, name):
        return _Anything()

    def __getitem__(self, key):
        return _Anything()


#: Every stub namespace answers unknown attributes with `_Anything` (PEP 562
#: lookup on a ModuleType subclass), so only the calls whose *return value* the
#: test inspects need an explicit implementation above.
for _ns in (GtkNS, GdkNS, GdkPixbufNS, GLibNS, GObjectNS):
    _ns.__getattr__ = lambda self, name: _Anything


gi = types.ModuleType("gi")
gi.require_version = lambda *a, **k: None
repo = types.ModuleType("gi.repository")
repo.Gtk = GtkNS("gi.repository.Gtk")
repo.Gdk = GdkNS("gi.repository.Gdk")
repo.GdkPixbuf = GdkPixbufNS("gi.repository.GdkPixbuf")
repo.GLib = GLibNS("gi.repository.GLib")
repo.GObject = GObjectNS("gi.repository.GObject")
gi.repository = repo
sys.modules["gi"] = gi
sys.modules["gi.repository"] = repo
sys.modules["cairo"] = types.ModuleType("cairo")

import usage_widget as W  # noqa: E402

check_gui("read_api_key usa la ref principal", W.read_api_key() == MAIN)

import ast as _ast  # noqa: E402

_names = {n.id for n in _ast.walk(_ast.parse(open(W.__file__).read()))
          if isinstance(n, _ast.Name)}
check_gui("no queda codigo que use auth.json/AUTH_PATH",
          not ({"AUTH_PATH", "KEY_PATH"} & _names))

w = W.UsageWidget(interval=300)
check_gui("UsageWidget se construye", w is not None)
check_gui("hay una fila por cada key gestionada",
          set(w.key_values) == {W.USAGE_REF, W.SUBAGENT_REF}, sorted(w.key_values))
check_gui("fila principal muestra fingerprint",
          w.key_values[W.USAGE_REF].get_text() == S.masked(MAIN),
          w.key_values[W.USAGE_REF].get_text())
check_gui("fila de subagentes dice 'not set'",
          w.key_values[W.SUBAGENT_REF].get_text() == "not set")
check_gui("fingerprint no revela la key",
          "MAINMAIN" not in w.key_values[W.USAGE_REF].get_text())
check_gui("4 filas de ventana", len(w.rows) == 4)
check_gui("fila OpenRouter diaria presente",
          W.OPENROUTER_ROW in w.rows)

# --- rotar la key de subagentes desde el dialogo
calls = {}


def _accept(key, **k):
    calls["validated"] = key
    return True, "HTTP 200"


W.dsh_key_store.validate_key = _accept

ENTRIES.clear()
HANDLERS.clear()
w.open_key_dialog(W.SUBAGENT_REF, "subagent key")
check_gui("dialogo de subagentes se abre", len(ENTRIES) == 1)
ENTRIES[0].set_text(SUB)
for cb in HANDLERS.get("changed", []):
    cb()
for cb in HANDLERS.get("clicked", []):
    try:
        cb()
    except TypeError:
        pass
import time as _t  # noqa: E402

_t.sleep(1.0)

check_gui("valido la key tipeada", calls.get("validated") == SUB)
check_gui("key de subagentes guardada", S.load_key(name=S.SUBAGENT_REF_NAME) == SUB)
check_gui("key PRINCIPAL no se toco", S.load_key() == MAIN)
check_gui("la fila de subagentes se actualizo",
          w.key_values[W.SUBAGENT_REF].get_text() == S.masked(SUB),
          w.key_values[W.SUBAGENT_REF].get_text())

# --- key invalida: no escribe
W.dsh_key_store.validate_key = lambda key, **k: (False, "HTTP 401 (key rejected)")
before = S.load_key(name=S.SUBAGENT_REF_NAME)
ENTRIES.clear()
HANDLERS.clear()
w.open_key_dialog(W.SUBAGENT_REF, "subagent key")
ENTRIES[0].set_text("oc_sk_BADKEYBADKEYBADKEYBADKEYBADKEYBADKEY")
for cb in HANDLERS.get("changed", []):
    cb()
for cb in HANDLERS.get("clicked", []):
    try:
        cb()
    except TypeError:
        pass
_t.sleep(1.0)
check_gui("key invalida NO se escribe", S.load_key(name=S.SUBAGENT_REF_NAME) == before)

# --- menu
w._show_menu(types.SimpleNamespace(button=3, time=0, x_root=0, y_root=0))
check_gui("menu construido sin error", True)

# --- sin key principal: estado de error legible
open(FAKE, "w").write("version: 1\nrefs:\n  OPENCODE_SUBAGENT_API_KEY: %s\n" % SUB)
os.chmod(FAKE, 0o600)
w._refresh_key_row()
check_gui("sin key principal la fila lo dice",
          w.key_values[W.USAGE_REF].get_text() == "not set")
try:
    W.read_api_key()
    check_gui("read_api_key falla sin key principal", False, "no fallo")
except ValueError as exc:
    check_gui("read_api_key falla sin key principal",
              "no OPENCODE_API_KEY stored" in str(exc))

# --- CLI del widget
os.remove(FAKE)
check_gui("main --show-key exit 0", W.main(["--show-key"]) == 0)
check_gui("main --check-key sin key exit 1", W.main(["--check-key"]) == 1)
check_gui("main --check-key --ref bogus exit 2",
          W.main(["--check-key", "--ref", "bogus"]) == 2)

# ==========================================================================
# 3. the live credential store was never touched
# ==========================================================================

still = (open(REAL_CREDENTIALS, "rb").read()
         if os.path.exists(REAL_CREDENTIALS) else None)
check_store("credencial real intacta byte a byte", still == REAL_BEFORE)

rc = subprocess.run([sys.executable, "-m", "py_compile",
                     os.path.join(HERE, "usage_widget.py"),
                     os.path.join(HERE, "dsh_key_store.py")],
                    capture_output=True)
check_store("ambos modulos compilan", rc.returncode == 0,
            rc.stderr.decode()[:200])

shutil.rmtree(WORK, ignore_errors=True)

store_fails = [r for r in store_results if not r[1]]
gui_fails = [r for r in gui_results if not r[1]]
print("\n--- store: %d/%d PASS ---" % (
    len(store_results) - len(store_fails), len(store_results)))
print("--- gui:   %d/%d PASS ---" % (
    len(gui_results) - len(gui_fails), len(gui_results)))
print("=== %d fallas ===" % (len(store_fails) + len(gui_fails)))
sys.exit(1 if (store_fails or gui_fails) else 0)
