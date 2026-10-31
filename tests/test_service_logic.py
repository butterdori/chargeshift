"""Service dispatch and resuspend guards without a real system bus."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from chargeshift.controller import Controller
from test_controller import FakeHardware, FakeTimers
from test_core import at
from test_ui_logic import load_headless

SERVICE = load_headless("service")


class ServiceLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = at("2026-09-28T05:00:03")
        self.engine = Controller(Path(self.temp.name) / "state.json", FakeHardware(), FakeTimers(), lambda: self.now)
        self.engine.config["resuspend"] = True
        self.service = SERVICE.Service.__new__(SERVICE.Service)
        self.service.engine = self.engine
        self.service.generation = 1
        self.service.connection = MagicMock()
        self.service.authorize = MagicMock()
        self.service.submit = MagicMock()
        self.service.login_call = MagicMock()
        self.snapshot = {"at": self.now.timestamp() - 300, "event": self.now.timestamp() - 3}
        SERVICE.GLib.reset_mock()

    def dispatch(self, method, args):
        invocation = MagicMock()
        parameters = MagicMock()
        parameters.unpack.return_value = args
        self.service.on_method(None, ":1.9", None, None, method, parameters, invocation)
        return invocation

    def test_master_switch_requires_authorization(self):
        self.dispatch("SetManagement", (False,))
        self.assertTrue(self.engine.config["management_enabled"])
        self.service.authorize.assert_called_once()
        operation = self.service.authorize.call_args.args[2]
        state = operation()
        self.assertFalse(state["config"]["management_enabled"])
        self.assertEqual(self.service.generation, 2)

    def test_status_read_does_not_require_authorization(self):
        invocation = self.dispatch("GetStatus", ())
        self.service.authorize.assert_not_called()
        invocation.return_value.assert_called_once()

    def test_unprivileged_internal_reconcile_is_rejected(self):
        self.service.uid = lambda sender: 1000
        invocation = self.dispatch("Reconcile", ())
        invocation.return_dbus_error.assert_called_once()
        self.service.submit.assert_not_called()

    def test_wake_test_methods_are_removed_from_interface(self):
        self.assertNotIn("ArmWakeTest", SERVICE.METHODS)
        self.assertNotIn("CancelWakeTest", SERVICE.METHODS)
        self.assertEqual(SERVICE.METHODS["SetManagement"], "b")

    def test_disabled_management_does_not_schedule_resuspend(self):
        self.engine.set_management(False)
        with patch.object(SERVICE.time, "time", return_value=self.now.timestamp()):
            state = self.service.after_resume(self.snapshot, 1)
        self.assertIsNone(state["return_at"])
        SERVICE.GLib.idle_add.assert_not_called()

    def test_enabled_near_event_can_schedule_guarded_return(self):
        with patch.object(SERVICE.time, "time", return_value=self.now.timestamp()):
            state = self.service.after_resume(self.snapshot, 1)
        self.assertEqual(state["return_at"], self.now.timestamp() + 45)
        SERVICE.GLib.idle_add.assert_called_once()

    def test_old_generation_cannot_schedule_return(self):
        with patch.object(SERVICE.time, "time", return_value=self.now.timestamp()):
            state = self.service.after_resume(self.snapshot, 0)
        self.assertIsNone(state["return_at"])
        SERVICE.GLib.idle_add.assert_not_called()

    def test_hardware_or_timer_error_prevents_automatic_return(self):
        self.engine.timers.error = True
        with patch.object(SERVICE.time, "time", return_value=self.now.timestamp()):
            state = self.service.after_resume(self.snapshot, 1)
        self.assertIsNone(state["return_at"])
        SERVICE.GLib.idle_add.assert_not_called()

    def test_pending_return_cannot_suspend_after_disabling(self):
        self.engine.set_management(False)
        self.service.return_to_sleep(1, True, 300)
        self.service.connection.call_sync.assert_not_called()
        self.service.login_call.assert_not_called()

    def test_pending_return_cannot_suspend_after_wake_is_disabled(self):
        self.engine.config["wake"] = False
        self.service.return_to_sleep(1, True, 300)
        self.service.connection.call_sync.assert_not_called()
        self.service.login_call.assert_not_called()

    def test_cancelled_generation_cannot_suspend(self):
        self.service.return_to_sleep(0, True, 300)
        self.service.connection.call_sync.assert_not_called()
        self.service.login_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
