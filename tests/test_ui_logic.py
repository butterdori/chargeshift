"""Presentation logic with widget doubles; this is not a GTK integration test."""
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from chargeshift.core import BUS_NAME, DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


def load_headless(module_name):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None
    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace(ApplicationWindow=object, Application=object, Toast=types.SimpleNamespace(new=lambda value: value))
    repository.Gtk = types.SimpleNamespace(License=types.SimpleNamespace(MIT_X11=1))
    repository.Gdk = MagicMock()
    repository.Gio = MagicMock()
    repository.GLib = MagicMock()
    name = "chargeshift._headless_" + module_name
    spec = importlib.util.spec_from_file_location(name, ROOT / "chargeshift" / (module_name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        spec.loader.exec_module(module)
    return module


APP = load_headless("app")


def window_double():
    window = APP.Window.__new__(APP.Window)
    window.state = None
    window.demo = False
    window.dirty = False
    window.loading = False
    window.working = False
    window.reading = False
    window.initialized = True
    window.syncing_management = False
    window.request_epoch = 0
    for name in ("management", "schedule_page", "override_controls", "duration", "resume_button", "stay_button", "limit_label", "checked_label", "verified_label", "banner", "battery_label", "battery_bar", "power_label", "next_limit", "next_time", "next_kind", "mode_row", "base_button", "full_button", "override_note", "sleep_note", "diagnostic_text", "history_label", "save_button", "toast", "spinner", "client"):
        setattr(window, name, MagicMock())
    window.system_rows = {key: MagicMock() for key in ("service", "target", "actual", "timer", "wake", "zone")}
    window.load_settings = MagicMock()
    return window


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.Client(demo=True)
        self.addCleanup(self.client.pool.shutdown)
        self.window = window_double()

    def test_enabled_state_renders_requested_and_actual(self):
        state = self.client.simulate("GetStatus", ())
        self.window.render(state)
        self.window.management.set_active.assert_called_with(True)
        self.window.override_controls.set_sensitive.assert_called_with(True)
        self.window.system_rows["target"].set_subtitle.assert_called_with(f"{state['target_limit']}%")

    def test_disabled_state_has_no_target_or_event_but_keeps_readback(self):
        state = self.client.simulate("SetManagement", (False,))
        self.window.render(state)
        self.window.management.set_active.assert_called_with(False)
        self.window.limit_label.set_label.assert_called_with("80%")
        self.window.verified_label.set_label.assert_called_with("Read only")
        self.window.next_time.set_label.assert_called_with("No scheduled changes")
        self.window.system_rows["target"].set_subtitle.assert_called_with("Not managed")
        self.window.override_controls.set_sensitive.assert_called_with(False)
        self.window.schedule_page.set_sensitive.assert_called_with(True)

    def test_disabled_restore_error_stays_visible(self):
        state = self.client.simulate("SetManagement", (False,))
        state["errors"] = ["Management is off, but restoring 80% failed"]
        state["actual_limit"] = 100
        self.window.render(state)
        self.window.banner.set_revealed.assert_called_with(True)
        self.window.verified_label.set_label.assert_called_with("Check status")
        self.window.limit_label.set_label.assert_called_with("100%")

    def test_refresh_does_not_overwrite_unsaved_schedule_edits(self):
        self.window.dirty = True
        self.window.render(self.client.simulate("GetStatus", ()))
        self.window.load_settings.assert_not_called()

    def test_master_toggle_uses_separate_dbus_method_and_waits_for_result(self):
        self.window.render(self.client.simulate("GetStatus", ()))
        self.window.management.get_active.return_value = False
        self.window.toggle_management()
        self.window.management.set_active.assert_called_with(True)
        call = self.window.client.call.call_args.args
        self.assertEqual(call[:3], ("SetManagement", "b", (False,)))
        call[3](self.client.simulate("SetManagement", (False,)), None)
        self.window.management.set_active.assert_called_with(False)

    def test_denied_master_toggle_preserves_actual_state(self):
        self.window.render(self.client.simulate("GetStatus", ()))
        self.window.management.get_active.return_value = False
        self.window.toggle_management()
        callback = self.window.client.call.call_args.args[3]
        callback(None, "Authorization denied")
        self.assertTrue(self.window.state["config"]["management_enabled"])
        self.window.management.set_active.assert_called_with(True)
        self.window.toast.add_toast.assert_called_with("Authorization denied")

    def test_stale_status_request_cannot_undo_completed_toggle(self):
        enabled = self.client.simulate("GetStatus", ())
        self.window.render(enabled)
        self.window.refresh()
        refresh_done = self.window.client.call.call_args.args[3]
        self.window.mutate("SetManagement", "b", (False,))
        mutation_done = self.window.client.call.call_args.args[3]
        mutation_done(self.client.simulate("SetManagement", (False,)), None)
        refresh_done(enabled, None)
        self.assertFalse(self.window.state["config"]["management_enabled"])
        self.window.management.set_active.assert_called_with(False)

    def test_offline_state_disables_mutations(self):
        self.window.render(self.client.simulate("GetStatus", ()))
        self.window.refresh()
        self.window.client.call.call_args.args[3](None, "Service unavailable")
        self.assertIsNone(self.window.state)
        self.window.management.set_sensitive.assert_called_with(False)
        self.window.override_controls.set_sensitive.assert_called_with(False)
        self.window.schedule_page.set_sensitive.assert_called_with(False)

    def test_disabled_override_buttons_cannot_invoke_backend(self):
        self.window.render(self.client.simulate("SetManagement", (False,)))
        self.window.override(True)
        self.window.client.call.assert_not_called()

    def test_demo_disabled_schedule_edit_does_not_touch_limit(self):
        self.client.simulate("SetManagement", (False,))
        config = {**self.client.config, "base_limit": 75}
        state = self.client.simulate("UpdateSettings", (json.dumps(config),))
        self.assertEqual(state["actual_limit"], 80)
        self.assertIsNone(state["target_limit"])
        self.assertIsNone(state["next_event"])

    def test_demo_rejects_override_while_off(self):
        self.client.simulate("SetManagement", (False,))
        with self.assertRaises(ValueError):
            self.client.simulate("SetOverride", (100, "1h"))

    def test_demo_does_not_show_wake_when_wake_option_is_off(self):
        state = self.client.simulate("UpdateSettings", (json.dumps({**DEFAULTS, "wake": False}),))
        self.assertIsNone(state["next_wake"])

    def test_about_uses_application_icon_not_stock_symbolic(self):
        fake_app = MagicMock()
        dialog_factory = MagicMock()
        with patch.object(APP.Adw, "AboutDialog", dialog_factory, create=True):
            APP.Application.about(fake_app)
        self.assertEqual(dialog_factory.call_args.kwargs["application_icon"], BUS_NAME)
        self.assertIn("Framework laptops", dialog_factory.call_args.kwargs["comments"])


if __name__ == "__main__":
    unittest.main()
