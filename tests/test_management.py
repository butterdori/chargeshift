import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chargeshift.controller import Controller, Timers
from chargeshift.core import DEFAULTS, events, live_override, new_override, next_event, target_limit, timer_dropin
from test_controller import FakeHardware, FakeTimers
from test_core import at


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = at("2026-09-28T06:00")
        self.hw = FakeHardware()
        self.timers = FakeTimers()
        self.engine = Controller(self.path, self.hw, self.timers, lambda: self.now)

    def restart(self):
        self.engine = Controller(self.path, self.hw, self.timers, lambda: self.now)
        return self.engine.reconcile("restart")

    def test_disable_restores_once_and_cancels_override_and_events(self):
        self.engine.set_override(100, "4h")
        self.engine.return_at = self.now.timestamp() + 45
        state = self.engine.set_management(False)
        self.assertEqual(self.hw.writes, [100, 80])
        self.assertFalse(state["config"]["management_enabled"])
        self.assertIsNone(state["override"])
        self.assertIsNone(state["next_event"])
        self.assertIsNone(state["next_wake"])
        self.assertIsNone(state["return_at"])
        self.assertIsNone(state["target_limit"])
        self.assertEqual(state["timer_state"], "inactive")
        self.assertFalse(state["verified"])
        self.assertEqual(state["errors"], [])

    def test_off_survives_restart_and_leaves_manual_limit_untouched(self):
        self.engine.set_management(False)
        self.hw.limit = 95
        state = self.restart()
        self.assertFalse(state["config"]["management_enabled"])
        self.assertEqual(state["actual_limit"], 95)
        self.assertEqual(self.hw.writes, [80])
        self.assertIsNone(state["target_limit"])

    def test_disabled_periodic_resume_refresh_and_timer_never_write(self):
        self.engine.set_management(False)
        self.hw.limit = 90
        for reason in ("periodic", "resume", "refresh", "system timer"):
            self.engine.reconcile(reason)
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(self.hw.limit, 90)

    def test_repeated_disable_does_not_restore_again(self):
        self.engine.set_management(False)
        self.hw.limit = 90
        self.engine.set_management(False)
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(self.hw.limit, 90)

    def test_off_stays_read_only_during_service_stop(self):
        self.engine.set_management(False)
        self.hw.limit = 95
        state = self.engine.restore_base()
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(state["actual_limit"], 95)
        self.assertIsNone(state["target_limit"])
        self.assertIsNone(state["next_wake"])

    def test_enabled_service_stop_restores_only_once(self):
        self.engine.reconcile()
        self.engine.restore_base()
        self.engine.restore_base()
        self.assertEqual(self.hw.writes, [100, 80])
        self.assertEqual(self.timers.state, "inactive")

    def test_disabled_schedule_save_changes_plan_not_hardware(self):
        self.engine.set_management(False)
        state = self.engine.configure({**self.engine.config, "base_limit": 75, "start": "04:00"})
        self.assertEqual(state["actual_limit"], 80)
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(state["config"]["base_limit"], 75)
        self.assertIsNone(state["next_event"])

    def test_disabled_clear_override_is_read_only(self):
        self.engine.set_management(False)
        self.hw.limit = 95
        self.engine.clear_override()
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(self.hw.limit, 95)

    def test_overrides_are_rejected_while_disabled(self):
        self.engine.set_management(False)
        with self.assertRaisesRegex(ValueError, "Enable charging management"):
            self.engine.set_override(100, "1h")
        self.assertEqual(self.hw.writes, [80])

    def test_reenable_applies_current_window(self):
        self.engine.set_management(False)
        state = self.engine.set_management(True)
        self.assertEqual(self.hw.writes, [80, 100])
        self.assertTrue(state["verified"])
        self.assertEqual(state["timer_state"], "active")
        self.assertIsNotNone(state["next_wake"])

    def test_reenable_after_cutoff_does_not_replay_morning(self):
        self.engine.set_management(False)
        self.now = at("2026-09-28T19:00")
        state = self.engine.set_management(True)
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(state["target_limit"], 80)

    def test_weekly_toggle_remains_distinct_from_master(self):
        self.engine.configure({**DEFAULTS, "enabled": False})
        self.engine.set_management(False)
        state = self.engine.set_management(True)
        self.assertFalse(state["config"]["enabled"])
        self.assertTrue(state["config"]["management_enabled"])
        self.assertEqual(state["target_limit"], 80)

    def test_settings_cannot_bypass_master_transition(self):
        with self.assertRaisesRegex(ValueError, "Charging management switch"):
            self.engine.configure({**DEFAULTS, "management_enabled": False})
        self.assertEqual(self.hw.writes, [])
        self.assertTrue(self.engine.config["management_enabled"])

    def test_boolean_validation(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.set_management(value)
        self.assertTrue(self.engine.config["management_enabled"])

    def test_failed_disable_save_rolls_back_without_hardware_changes(self):
        self.engine.set_override(100, "1h")
        previous = copy.deepcopy(self.engine.override)
        with patch.object(self.engine, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.engine.set_management(False)
        self.assertTrue(self.engine.config["management_enabled"])
        self.assertEqual(self.engine.override, previous)
        self.assertEqual(self.hw.writes, [100])

    def test_failed_restore_remains_off_with_persistent_warning(self):
        self.hw.limit = 100
        self.hw.ignore = True
        state = self.engine.set_management(False)
        self.assertFalse(state["config"]["management_enabled"])
        self.assertIn("restoring 80% failed", state["disable_warning"])
        self.assertEqual(state["actual_limit"], 100)
        self.assertFalse(state["verified"])
        state = self.restart()
        self.assertIn("restoring 80% failed", state["errors"][0])
        self.assertEqual(self.hw.writes, [80])

    def test_write_exception_never_retries_while_off(self):
        with patch.object(self.hw, "write", side_effect=RuntimeError("EC unavailable")) as writer:
            state = self.engine.set_management(False)
            self.engine.reconcile()
            self.restart()
            self.assertEqual(writer.call_count, 1)
        self.assertIn("EC unavailable", state["disable_warning"])

    def test_restart_after_interrupted_disable_does_not_write_again(self):
        with patch.object(self.hw, "write", side_effect=SystemExit("simulated interruption")):
            with self.assertRaises(SystemExit):
                self.engine.set_management(False)
        state = self.restart()
        self.assertFalse(state["config"]["management_enabled"])
        self.assertIn("not verified", state["disable_warning"])
        self.assertEqual(self.hw.writes, [])

    def test_failed_final_save_keeps_disabled_state_on_disk(self):
        save = self.engine.save
        calls = []
        def fail_second_save():
            calls.append(True)
            if len(calls) == 2:
                raise OSError("disk full")
            save()
        with patch.object(self.engine, "save", side_effect=fail_second_save):
            state = self.engine.set_management(False)
        self.assertIn("could not save", state["disable_warning"])
        self.assertFalse(json.loads(self.path.read_text())["config"]["management_enabled"])
        self.restart()
        self.assertEqual(self.hw.writes, [80])

    def test_off_with_timer_stop_failure_never_elevates_limit(self):
        self.timers.error = True
        state = self.engine.set_management(False)
        self.assertFalse(state["config"]["management_enabled"])
        self.assertIn("Could not stop schedule timer", state["errors"][0])
        self.hw.limit = 90
        self.engine.reconcile()
        self.assertEqual(self.hw.writes, [80])
        self.assertEqual(self.hw.limit, 90)

    def test_reenable_timer_failure_keeps_baseline(self):
        self.engine.set_management(False)
        self.timers.error = True
        state = self.engine.set_management(True)
        self.assertEqual(self.hw.writes, [80])
        self.assertFalse(state["verified"])
        self.assertIsNone(state["next_wake"])

    def test_reenable_clears_historical_disable_warning(self):
        self.hw.limit = 100
        self.hw.ignore = True
        self.engine.set_management(False)
        self.hw.ignore = False
        state = self.engine.set_management(True)
        self.assertIsNone(state["disable_warning"])
        self.assertEqual(state["errors"], [])

    def test_v1_settings_preserve_schedule_and_override(self):
        config = {key: value for key, value in DEFAULTS.items() if key != "management_enabled"}
        config.update(start="04:30", days=[1, 3], base_limit=75)
        override = new_override(DEFAULTS, 90, "1h", self.now)
        self.path.write_text(json.dumps({"version": 1, "config": config, "override": override}))
        state = self.restart()
        self.assertTrue(state["config"]["management_enabled"])
        self.assertEqual(state["config"]["start"], "04:30")
        self.assertEqual(state["actual_limit"], 90)
        self.engine.set_management(False)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["config"]["base_limit"], 75)

    def test_v1_disabled_weekly_schedule_migrates_as_managed(self):
        config = {key: value for key, value in DEFAULTS.items() if key != "management_enabled"}
        config["enabled"] = False
        self.path.write_text(json.dumps({"version": 1, "config": config, "override": None}))
        state = self.restart()
        self.assertTrue(state["config"]["management_enabled"])
        self.assertFalse(state["config"]["enabled"])
        self.assertEqual(state["target_limit"], 80)

    def test_corrupt_state_never_enables_management(self):
        self.path.write_text("{invalid")
        state = self.restart()
        self.assertFalse(state["config"]["management_enabled"])
        self.assertEqual(self.hw.writes, [])
        with self.assertRaisesRegex(ValueError, "Save valid settings"):
            self.engine.set_management(True)
        self.engine.configure(copy.deepcopy(self.engine.config))
        self.assertTrue(self.engine.set_management(True)["config"]["management_enabled"])

    def test_disabled_saved_override_cannot_become_active(self):
        self.engine.set_management(False)
        saved = json.loads(self.path.read_text())
        saved["override"] = new_override(DEFAULTS, 100, "1h", self.now)
        self.path.write_text(json.dumps(saved))
        state = self.restart()
        self.assertIsNone(state["override"])
        self.assertEqual(self.hw.writes, [80])

    def test_status_publish_without_read_does_not_refresh_read_timestamp(self):
        first = self.engine.reconcile()
        self.now = at("2026-09-28T06:04")
        second = self.engine.publish(first["actual_limit"], [])
        self.assertEqual(second["checked_at"], first["checked_at"])


class DisabledCoreTests(unittest.TestCase):
    def setUp(self):
        self.config = {**DEFAULTS, "management_enabled": False}
        self.now = at("2026-09-28T06:00")

    def test_disabled_has_no_target_or_events(self):
        self.assertIsNone(target_limit(self.config, None, self.now))
        self.assertEqual(events(self.config, self.now), [])
        self.assertIsNone(next_event(self.config, None, self.now))

    def test_disabled_ignores_previous_override(self):
        override = new_override(DEFAULTS, 100, "4h", self.now)
        self.assertIsNone(live_override(self.config, override, self.now))

    def test_disabled_rejects_new_override(self):
        with self.assertRaises(ValueError):
            new_override(self.config, 100, "1h", self.now)

    def test_disabled_dropin_turns_off_wake_and_clock_triggers(self):
        text = timer_dropin(self.config, None)
        self.assertIn("WakeSystem=false", text)
        self.assertIn("OnClockChange=false", text)
        self.assertIn("OnTimezoneChange=false", text)
        self.assertNotIn("05:00", text)


class TimerControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.timers = Timers(Path(self.temp.name) / "schedule.conf")

    @patch("chargeshift.controller.run")
    def test_disabling_stops_timer_and_event_without_restart(self, run):
        run.return_value = "inactive\n"
        self.timers.sync({**DEFAULTS, "management_enabled": False}, None)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/systemctl", "stop", "chargeshift-event.timer", "chargeshift-event.service"], commands)
        self.assertFalse(any("restart" in command for command in commands))
        self.assertEqual(self.timers.state, "inactive")
        self.assertIn("WakeSystem=false", self.timers.path.read_text())

    @patch("chargeshift.controller.run")
    def test_stopped_timer_state_is_verified(self, run):
        run.return_value = "active\n"
        with self.assertRaisesRegex(RuntimeError, "not stopped"):
            self.timers.sync({**DEFAULTS, "management_enabled": False}, None)
        self.assertEqual(self.timers.state, "Error")

    @patch("chargeshift.controller.run")
    def test_enabling_restarts_timer(self, run):
        states = iter(["inactive\n", "active\n"])
        run.side_effect = lambda command, **kwargs: next(states) if "show" in command else ""
        self.timers.sync(DEFAULTS, None)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/systemctl", "restart", "chargeshift-event.timer"], commands)
        self.assertEqual(self.timers.state, "active")

    @patch("chargeshift.controller.run", side_effect=RuntimeError("systemctl failed"))
    def test_timer_exception_cannot_leave_stale_active_status(self, run):
        self.timers.state = "active"
        with self.assertRaises(RuntimeError):
            self.timers.sync(DEFAULTS, None)
        self.assertEqual(self.timers.state, "Error")


if __name__ == "__main__":
    unittest.main()
