import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chargeshift.controller import Controller, battery_status
from chargeshift.core import DEFAULTS
from test_core import at


class FakeHardware:
    def __init__(self, limit=80):
        self.limit = limit
        self.writes = []
        self.raw = "Minimum 0%, Maximum 80%"
        self.driver = "fake"
        self.error = False
        self.ignore = False

    def read(self):
        if self.error:
            raise ValueError("Unknown charge-limit output")
        self.raw = f"Minimum 0%, Maximum {self.limit}%"
        return self.limit

    def write(self, value):
        self.writes.append(value)
        if not self.ignore:
            self.limit = value

    def battery(self):
        return {"percent": 74, "state": "Charging", "ac": True}


class FakeTimers:
    state = "active"
    def __init__(self):
        self.error = False
        self.calls = []

    def sync(self, config, override):
        self.calls.append((copy.deepcopy(config), copy.deepcopy(override)))
        if self.error:
            self.state = "Error"
            raise RuntimeError("No RTC or timer permission")
        self.state = "active" if config["management_enabled"] else "inactive"


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = at("2026-09-28T06:00")
        self.hw = FakeHardware()
        self.timers = FakeTimers()
        self.engine = Controller(self.path, self.hw, self.timers, lambda: self.now)

    def test_startup_reconciles_current_window(self):
        state = self.engine.reconcile("boot")
        self.assertEqual(self.hw.writes, [100])
        self.assertTrue(state["verified"])
        self.assertEqual(state["actual_limit"], 100)

    def test_identical_target_does_not_write_again(self):
        self.engine.reconcile()
        self.engine.reconcile()
        self.assertEqual(self.hw.writes, [100])

    def test_ec_reset_repaired_while_awake(self):
        self.engine.reconcile()
        self.hw.limit = 80
        self.engine.reconcile()
        self.assertEqual(self.hw.writes, [100, 100])

    def test_missed_events_compute_present_not_replay(self):
        self.engine.reconcile()
        self.now = at("2026-09-28T19:00")
        state = self.engine.reconcile("resume")
        self.assertEqual(state["actual_limit"], 80)
        self.assertEqual(self.hw.writes, [100, 80])

    def test_override_persists_and_expires_after_restart(self):
        self.now = at("2026-09-28T16:30")
        self.engine.set_override(100, "4h")
        other = Controller(self.path, self.hw, self.timers, lambda: self.now)
        self.assertIsNotNone(other.override)
        self.now = at("2026-09-28T18:00")
        state = other.reconcile("boot")
        self.assertIsNone(state["override"])
        self.assertEqual(state["actual_limit"], 80)
        self.assertIsNone(json.loads(self.path.read_text())["override"])

    def test_schedule_save_clears_override(self):
        self.engine.set_override(80, "1h")
        state = self.engine.configure(copy.deepcopy(DEFAULTS))
        self.assertIsNone(state["override"])
        self.assertEqual(state["actual_limit"], 100)

    def test_disabled_schedule_sets_baseline(self):
        self.hw.limit = 100
        state = self.engine.configure({**DEFAULTS, "enabled": False})
        self.assertEqual(state["actual_limit"], 80)

    def test_timer_failure_never_elevates_limit(self):
        self.timers.error = True
        state = self.engine.reconcile()
        self.assertEqual(self.hw.limit, 80)
        self.assertFalse(state["verified"])
        self.assertTrue(state["errors"])

    def test_timer_failure_restores_baseline_if_already_high(self):
        self.hw.limit = 100
        self.timers.error = True
        state = self.engine.reconcile()
        self.assertEqual(state["actual_limit"], 80)
        self.assertIn("Timer setup failed", state["errors"][0])

    def test_unknown_readback_does_not_claim_success(self):
        self.hw.error = True
        state = self.engine.reconcile()
        self.assertIsNone(state["actual_limit"])
        self.assertEqual(self.hw.writes, [])
        self.assertFalse(state["verified"])

    def test_post_write_read_failure_is_unknown(self):
        with patch.object(self.hw, "read", side_effect=[80, RuntimeError("device disappeared")]):
            state = self.engine.reconcile()
        self.assertIsNone(state["actual_limit"])
        self.assertFalse(state["verified"])

    def test_command_write_failure_is_unknown(self):
        with patch.object(self.hw, "write", side_effect=RuntimeError("write failed")):
            state = self.engine.reconcile()
        self.assertIsNone(state["actual_limit"])
        self.assertFalse(state["verified"])

    @patch("chargeshift.controller.time.sleep")
    def test_failed_write_verification(self, sleep):
        self.hw.ignore = True
        state = self.engine.reconcile()
        self.assertFalse(state["verified"])
        self.assertEqual(state["actual_limit"], 80)
        self.assertIn("EC reports 80", state["errors"][0])

    def test_corrupt_state_falls_back_with_visible_error(self):
        self.path.write_text("{broken")
        other = Controller(self.path, self.hw, self.timers, lambda: self.now)
        state = other.reconcile()
        self.assertIsNone(state["target_limit"])
        self.assertEqual(self.hw.writes, [])
        self.assertTrue(state["errors"])
        self.assertEqual(self.path.read_text(), "{broken")
        fixed = other.configure({**DEFAULTS, "management_enabled": False})
        self.assertFalse(fixed["errors"])

    def test_state_file_is_private(self):
        self.engine.configure(copy.deepcopy(DEFAULTS))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_save_failure_rolls_back_override(self):
        self.engine.set_override(80, "1h")
        before = copy.deepcopy(self.engine.override)
        with patch.object(self.engine, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.engine.set_override(100, "4h")
        self.assertEqual(self.engine.override, before)

    def test_restore_base_on_service_stop(self):
        self.engine.reconcile()
        state = self.engine.restore_base()
        self.assertEqual(state["actual_limit"], 80)
        self.assertEqual(state["target_limit"], 80)
        self.engine.reconcile()
        self.assertEqual(self.hw.limit, 80)

    def test_battery_reads_skip_accessory_battery(self):
        root = Path(self.temp.name) / "power"
        for name, fields in {"BAT0": {"type": "Battery", "capacity": "74", "status": "Charging", "scope": "System"}, "AAA": {"type": "Battery", "capacity": "99", "status": "Discharging", "scope": "Device"}, "AC": {"type": "Mains", "online": "1"}}.items():
            device = root / name
            device.mkdir(parents=True)
            for key, value in fields.items():
                (device / key).write_text(value)
        self.assertEqual(battery_status(root), {"percent": 74, "state": "Charging", "ac": True})

    def test_missing_battery_is_unknown(self):
        self.assertIsNone(battery_status(Path(self.temp.name))["percent"])


if __name__ == "__main__":
    unittest.main()
