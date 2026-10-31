import copy
import json
import math
import shutil
import subprocess
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from chargeshift.core import DEFAULTS, events, live_override, new_override, next_event, parse_limit, read_json, safe_resuspend, scheduled_limit, target_limit, timer_dropin, validate_config, validate_override

ZONE = ZoneInfo("America/New_York")


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULTS)

    def test_weekday_boundaries(self):
        for value, expected in (("2026-09-28T04:59:59", 80), ("2026-09-28T05:00:00", 100), ("2026-09-28T16:59:59", 100), ("2026-09-28T17:00:00", 80), ("2026-09-28T23:00:00", 80)):
            with self.subTest(value=value):
                self.assertEqual(scheduled_limit(self.config, at(value)), expected)

    def test_weekends(self):
        for value in ("2026-09-26T09:00", "2026-09-27T09:00"):
            self.assertEqual(scheduled_limit(self.config, at(value)), 80)

    def test_disabled_schedule(self):
        self.config["enabled"] = False
        self.assertEqual(scheduled_limit(self.config, at("2026-09-28T08:00")), 80)

    def test_custom_days_and_limits(self):
        self.config.update(days=[6], base_limit=70, full_limit=95)
        self.assertEqual(scheduled_limit(self.config, at("2026-09-27T08:00")), 95)
        self.assertEqual(scheduled_limit(self.config, at("2026-09-28T08:00")), 70)

    def test_sunday_next_event(self):
        event = next_event(self.config, None, at("2026-09-27T19:00"))
        self.assertEqual(event["at"], at("2026-09-28T05:00").timestamp())
        self.assertEqual(event["limit"], 100)

    def test_safety_reset_is_daily(self):
        event = next_event(self.config, None, at("2026-09-26T10:00"))
        self.assertEqual(event["at"], at("2026-09-26T17:00").timestamp())

    def test_spring_dst_uses_local_five(self):
        self.config["days"] = list(range(7))
        event = next_event(self.config, None, at("2026-03-07T23:00"))
        self.assertEqual(event["at"], at("2026-03-08T05:00").timestamp())
        self.assertEqual(event["at"] - at("2026-03-07T23:00").timestamp(), 5 * 3600)

    def test_fall_dst_uses_local_five(self):
        self.config["days"] = list(range(7))
        event = next_event(self.config, None, at("2026-10-31T23:00"))
        self.assertEqual(event["at"] - at("2026-10-31T23:00").timestamp(), 7 * 3600)

    def test_four_hour_override_capped_at_seventeen(self):
        now = at("2026-09-28T16:30")
        override = new_override(self.config, 100, "4h", now)
        self.assertEqual(override["expires"], at("2026-09-28T17:00").timestamp())
        self.assertEqual(target_limit(self.config, override, at("2026-09-28T17:00")), 80)

    def test_evening_override_is_explicit_and_bounded(self):
        override = new_override(self.config, 100, "4h", at("2026-09-28T18:00"))
        self.assertEqual(override["expires"], at("2026-09-28T22:00").timestamp())

    def test_low_override_resumes_at_next_start(self):
        override = new_override(self.config, 80, "next", at("2026-09-28T04:00"))
        self.assertEqual(target_limit(self.config, override, at("2026-09-28T05:00")), 100)

    def test_override_until_cutoff_spans_start(self):
        override = new_override(self.config, 80, "cutoff", at("2026-09-28T04:00"))
        self.assertEqual(target_limit(self.config, override, at("2026-09-28T06:00")), 80)
        self.assertEqual(next_event(self.config, override, at("2026-09-28T04:00"))["at"], override["expires"])

    def test_override_invalidated_by_clock_rollback(self):
        override = new_override(self.config, 100, "1h", at("2026-09-28T12:00"))
        self.assertIsNone(live_override(self.config, override, at("2026-09-28T11:00")))

    def test_cutoff_wins_even_with_later_persisted_expiry(self):
        override = new_override(self.config, 100, "1h", at("2026-09-28T16:30"))
        override["expires"] = at("2026-09-28T20:00").timestamp()
        self.assertIsNone(live_override(self.config, override, at("2026-09-28T17:01")))

    def test_timer_contains_validated_days(self):
        text = timer_dropin(self.config, None)
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 05:00:00", text)
        self.assertIn("OnCalendar=*-*-* 17:00:00", text)
        self.assertIn("WakeSystem=true", text)

    def test_timer_utc_override(self):
        override = new_override(self.config, 100, "1h", at("2026-09-28T12:00"))
        self.assertIn("OnCalendar=2026-09-28 17:00:00 UTC", timer_dropin(self.config, override))

    def test_fractional_expiry_alarm_is_not_early(self):
        override = new_override(self.config, 100, "1h", at("2026-09-28T12:00:00.900000"))
        self.assertIn("OnCalendar=2026-09-28 17:00:01 UTC", timer_dropin(self.config, override))

    def test_timer_disable_wake_keeps_schedule(self):
        self.config["wake"] = False
        self.assertIn("WakeSystem=false", timer_dropin(self.config, None))
        self.assertIn("05:00:00", timer_dropin(self.config, None))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_systemd_accepts_generated_calendar_expressions(self):
        override = new_override(self.config, 100, "1h", at("2026-09-28T12:00"))
        lines = timer_dropin(self.config, override).splitlines()
        for line in lines:
            if line.startswith("OnCalendar=") and line != "OnCalendar=":
                result = subprocess.run(["systemd-analyze", "calendar", line.split("=", 1)[1]], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


class ValidationTests(unittest.TestCase):
    def test_defaults_valid(self):
        self.assertEqual(validate_config(copy.deepcopy(DEFAULTS)), DEFAULTS)

    def test_bad_fields(self):
        bad = {"start": ["5:00", "24:00", "05:60", "05:00\nExecStart=/bin/sh"], "end": ["04:00", "05:00"], "days": [[], [7], [0, 0], [True], "Mon"], "base_limit": [True, 0, 101, 80.0, "80"], "enabled": [1, "true"], "management_enabled": [1, "true", None]}
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_config({**copy.deepcopy(DEFAULTS), field: value})

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({**DEFAULTS, "command": "/bin/sh"})

    def test_resuspend_requires_wake(self):
        with self.assertRaises(ValueError):
            validate_config({**DEFAULTS, "resuspend": True, "wake": False})

    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(ValueError):
            read_json('{"enabled": true, "enabled": false}')

    def test_non_object_or_oversized_json(self):
        for text in ("[]", "null", " " * 8193):
            with self.assertRaises(ValueError):
                read_json(text)

    def test_override_validation(self):
        for limit in (True, 49, 101, "100"):
            with self.assertRaises(ValueError):
                new_override(DEFAULTS, limit, "1h", at("2026-09-28T12:00"))
        with self.assertRaises(ValueError):
            new_override(DEFAULTS, 100, "forever", at("2026-09-28T12:00"))

    def test_nonfinite_persisted_expiry_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                validate_override({"created": 0, "expires": value, "limit": 100})

    def test_unbounded_persisted_override_rejected(self):
        with self.assertRaises(ValueError):
            validate_override({"created": 0, "expires": 1e10, "limit": 100})

    def test_framework_output_uses_maximum_not_minimum(self):
        self.assertEqual(parse_limit("Minimum 0%, Maximum 80%\n"), 80)
        self.assertEqual(parse_limit("Minimum 75%, Maximum 100%"), 100)

    def test_labelled_and_ansi_output(self):
        self.assertEqual(parse_limit("\x1b[32mCharge Limit: 80%\x1b[0m"), 80)
        self.assertEqual(parse_limit("100\n"), 100)

    def test_parser_refuses_ambiguous_or_unrelated_output(self):
        for value in ("Battery: 82%", "Error: expected 100%", "Minimum 0%, Maximum 150%", "Charge Limit: 80%\nCharge Limit: 100%", "unable to read", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_limit(value)

    def test_safe_resuspend_requires_all_checks(self):
        options = dict(enabled=True, woke_near_event=True, slept_seconds=100, lid_closed=True, docked=False, idle=True, inhibited=False, power_known=True)
        self.assertTrue(safe_resuspend(**options))
        for field in ("enabled", "woke_near_event", "lid_closed", "idle", "power_known"):
            self.assertFalse(safe_resuspend(**{**options, field: False}))
        for field in ("docked", "inhibited"):
            self.assertFalse(safe_resuspend(**{**options, field: True}))
        self.assertFalse(safe_resuspend(**{**options, "slept_seconds": 1}))


if __name__ == "__main__":
    unittest.main()
