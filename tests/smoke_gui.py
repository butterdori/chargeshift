"""Run as a desktop user with GTK/libadwaita; accesses only demo state."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chargeshift.app import Application, Gdk, GLib, Gtk
from chargeshift.core import BUS_NAME

if os.geteuid() == 0:
    raise SystemExit("Run this demo smoke test as your desktop user, not root.")

app = Application(demo=True)
errors = []


def finish(error=None):
    if error:
        errors.append(str(error))
        print("FAIL:", error, file=sys.stderr)
    app.quit()
    return False


def reenabled():
    try:
        window = app.get_active_window()
        if window.working:
            return True
        assert window.state["config"]["management_enabled"]
        assert window.management.get_active()
        assert window.override_controls.get_sensitive()
        assert window.state["target_limit"] is not None
        app.about()
        print("PASS: GTK demo, three tabs, scalable icon lookup, master off/on and About.")
        GLib.timeout_add_seconds(1, finish)
        return False
    except Exception as exc:
        return finish(exc)


def disabled():
    try:
        window = app.get_active_window()
        if window.working:
            return True
        assert not window.state["config"]["management_enabled"]
        assert not window.management.get_active()
        assert not window.override_controls.get_sensitive()
        assert window.schedule_page.get_sensitive()
        assert window.state["target_limit"] is None
        assert window.state["next_event"] is None
        assert window.state["next_wake"] is None
        assert window.state["actual_limit"] == window.state["config"]["base_limit"]
        window.management.set_active(True)
        GLib.timeout_add_seconds(1, reenabled)
        return False
    except Exception as exc:
        return finish(exc)


def verify():
    try:
        window = app.get_active_window()
        assert window and window.state, "The demo window did not receive its state."
        assert window.demo
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for name in (BUS_NAME, BUS_NAME + "-symbolic"):
            assert theme.has_icon(name), f"Missing icon: {name}"
            for scale in (1, 2):
                icon = theme.lookup_icon(name, None, 128, scale, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_REGULAR if name == BUS_NAME else Gtk.IconLookupFlags.FORCE_SYMBOLIC)
                assert icon.get_file() and icon.get_file().get_basename().endswith(".svg"), f"Non-SVG fallback: {name}"
        for page in ("overview", "schedule", "system"):
            window.stack.set_visible_child_name(page)
        window.stack.set_visible_child_name("overview")
        window.management.set_active(False)
        GLib.timeout_add_seconds(1, disabled)
        return False
    except Exception as exc:
        return finish(exc)


def timeout():
    return finish("GUI test timed out")


GLib.timeout_add_seconds(3, verify)
GLib.timeout_add_seconds(25, timeout)
app.run([sys.argv[0]])
raise SystemExit(bool(errors))
