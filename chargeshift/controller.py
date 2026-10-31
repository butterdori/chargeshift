"""Root-side controller, testable with fake hardware and timers."""
from __future__ import annotations

import copy
import json
import logging
import os
import stat
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .core import DEFAULTS, events, live_override, local_zone, new_override, next_event, parse_limit, target_limit, timer_dropin, validate_config, validate_override

LOG = logging.getLogger("chargeshift")
ENV = {"PATH": "/usr/bin:/usr/sbin", "LANG": "C", "LC_ALL": "C", "HOME": "/", "NO_COLOR": "1"}


def atomic_write(path: Path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".chargeshift-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(argv: list[str], timeout: int = 12) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV, cwd="/", check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{Path(argv[0]).name} timed out after {timeout} seconds.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): {detail}")
    return result.stdout


def battery_status(root: Path = Path("/sys/class/power_supply")) -> dict:
    result = {"percent": None, "state": "Unknown", "ac": None}
    online = []
    for device in sorted(root.glob("*")):
        try:
            kind = (device / "type").read_text().strip()
            if kind == "Battery" and result["percent"] is None:
                if (device / "scope").exists() and (device / "scope").read_text().strip() == "Device":
                    continue
                if (device / "present").exists() and (device / "present").read_text().strip() != "1":
                    continue
                level = int((device / "capacity").read_text().strip())
                if 0 <= level <= 100:
                    result.update(percent=level, state=(device / "status").read_text().strip())
            elif kind != "Battery" and (device / "online").exists():
                online.append((device / "online").read_text().strip() == "1")
        except (OSError, ValueError):
            continue
    if online:
        result["ac"] = any(online)
    return result


class Hardware:
    def __init__(self):
        self.raw = "Not read yet"
        self.driver = "auto"

    def command(self) -> list[str]:
        path = Path("/usr/bin/framework_tool")
        for item in (path.resolve(), *path.resolve().parents):
            info = item.stat()
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError("framework_tool and its directories must be root-owned and not group/world writable.")
        config_path = Path("/etc/chargeshift/hardware.json")
        settings = json.loads(config_path.read_text()) if config_path.exists() else {"driver": "auto"}
        if not isinstance(settings, dict) or set(settings) != {"driver"} or settings["driver"] not in ("auto", "portio", "cros-ec"):
            raise RuntimeError("Invalid /etc/chargeshift/hardware.json; driver must be auto, portio, or cros-ec.")
        self.driver = settings["driver"]
        return [str(path)] + ([] if self.driver == "auto" else ["--driver", self.driver])

    def read(self) -> int:
        self.raw = "Read did not complete"
        try:
            self.raw = run(self.command() + ["--charge-limit"]).strip()[:4096]
            return parse_limit(self.raw)
        except Exception as exc:
            if self.raw == "Read did not complete":
                self.raw = str(exc)
            raise

    def write(self, limit: int):
        if type(limit) is not int or not 50 <= limit <= 100:
            raise ValueError("Refusing an invalid hardware limit.")
        run(self.command() + ["--charge-limit", str(limit)])

    def battery(self) -> dict:
        return battery_status()


class Timers:
    def __init__(self, path: Path = Path("/run/systemd/system/chargeshift-event.timer.d/schedule.conf")):
        self.path = path
        self.last = None
        self.state = "Unknown"

    def sync(self, config: dict, override: dict | None):
        text = timer_dropin(config, override)
        try:
            if not config["management_enabled"]:
                run(["/usr/bin/systemctl", "stop", "chargeshift-event.timer", "chargeshift-event.service"], timeout=20)
                if text != self.last:
                    atomic_write(self.path, text, 0o644)
                    run(["/usr/bin/systemctl", "daemon-reload"], timeout=20)
                    self.last = text
                self.state = run(["/usr/bin/systemctl", "show", "chargeshift-event.timer", "--property=ActiveState", "--value"]).strip()
                if self.state != "inactive":
                    raise RuntimeError(f"Schedule timer is {self.state or 'unknown'}, not stopped.")
                return
            self.state = run(["/usr/bin/systemctl", "show", "chargeshift-event.timer", "--property=ActiveState", "--value"]).strip()
            if text != self.last or self.state not in ("active", "activating"):
                atomic_write(self.path, text, 0o644)
                run(["/usr/bin/systemctl", "daemon-reload"], timeout=20)
                run(["/usr/bin/systemctl", "restart", "chargeshift-event.timer"], timeout=20)
                self.last = text
            self.state = run(["/usr/bin/systemctl", "show", "chargeshift-event.timer", "--property=ActiveState", "--value"]).strip()
            if self.state != "active":
                raise RuntimeError(f"Schedule timer is {self.state or 'unknown'}, not active.")
        except Exception:
            self.state = "Error"
            raise


class Controller:
    def __init__(self, state_path: Path, hardware=None, timers=None, clock=None):
        self.path = state_path
        self.hw = hardware or Hardware()
        self.timers = timers or Timers()
        self.clock = clock or (lambda: datetime.now(local_zone()))
        self.config = copy.deepcopy(DEFAULTS)
        self.override = None
        self.disable_warning = None
        self.sleep_note = ""
        self.return_at = None
        self.load_error = None
        self.history = []
        self.status = {}
        self.checked_at = 0
        self.stopping = False
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text())
                if not isinstance(value, dict) or type(value.get("version")) is not int:
                    raise ValueError("Unsupported settings file")
                if value["version"] == 1 and set(value) == {"version", "config", "override"}:
                    legacy = value["config"]
                    if not isinstance(legacy, dict) or set(legacy) != set(DEFAULTS) - {"management_enabled"}:
                        raise ValueError("Invalid legacy settings")
                    config = {**legacy, "management_enabled": True}
                    warning = None
                elif value["version"] == 2 and set(value) == {"version", "config", "override", "disable_warning"}:
                    config, warning = value["config"], value["disable_warning"]
                    if warning is not None and (not isinstance(warning, str) or len(warning) > 4096):
                        raise ValueError("Invalid saved restoration warning")
                else:
                    raise ValueError("Unsupported settings file")
                self.config = validate_config(config)
                self.override = validate_override(value["override"]) if self.config["management_enabled"] else None
                self.disable_warning = warning if not self.config["management_enabled"] else None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.config = {**copy.deepcopy(DEFAULTS), "management_enabled": False, "enabled": False}
            self.override = None
            self.disable_warning = None
            self.load_error = f"Saved settings could not be loaded: {exc}. Management is off; save valid settings before enabling it."
        self.publish(None, ["Starting service; waiting for the first hardware read."])

    def save(self):
        atomic_write(self.path, json.dumps({"version": 2, "config": self.config, "override": self.override, "disable_warning": self.disable_warning}, indent=2) + "\n")

    def record(self, text: str):
        if not self.history or self.history[0]["message"] != text:
            self.history.insert(0, {"at": self.clock().timestamp(), "message": text})
            del self.history[30:]
            LOG.info(text)

    def read_hardware(self):
        self.checked_at = self.clock().timestamp()
        return self.hw.read()

    def publish(self, actual, errors):
        now = self.clock()
        managed = self.config["management_enabled"]
        upcoming = next_event(self.config, self.override, now) if not self.stopping else None
        wake_events = events(self.config, now) if not self.stopping else []
        if managed and not self.stopping and self.override and self.override["expires"] > now.timestamp():
            wake_events.append({"at": self.override["expires"]})
        wake_at = min(item["at"] for item in wake_events) if wake_events and self.config["wake"] and self.timers.state == "active" else None
        desired = self.config["base_limit"] if self.stopping and managed else target_limit(self.config, self.override, now)
        self.status = {"config": copy.deepcopy(self.config), "override": copy.deepcopy(live_override(self.config, self.override, now)), "actual_limit": actual, "target_limit": desired, "verified": managed and actual is not None and actual == desired and not errors, "checked_at": self.checked_at, "battery": self.hw.battery(), "next_event": upcoming, "next_wake": wake_at, "timer_state": self.timers.state, "errors": errors, "raw_output": self.hw.raw, "driver": self.hw.driver, "disable_warning": self.disable_warning, "sleep_note": self.sleep_note, "return_at": self.return_at, "history": copy.deepcopy(self.history), "timezone": now.strftime("%Z")}
        return copy.deepcopy(self.status)

    def reconcile(self, reason: str = "periodic") -> dict:
        if self.stopping:
            return copy.deepcopy(self.status)
        now = self.clock()
        errors = [item for item in (self.load_error, self.disable_warning) if item]
        active = live_override(self.config, self.override, now)
        if self.override and not active:
            self.override = None
            self.record("Override expired; schedule resumed")
            try:
                self.save()
            except OSError as exc:
                errors.append(f"Could not save expired override: {exc}")
        timer_ok = True
        try:
            self.timers.sync(self.config, self.override)
        except Exception as exc:
            timer_ok = False
            errors.append(f"Timer setup failed: {exc}" if self.config["management_enabled"] else f"Could not stop schedule timer: {exc}")
        desired = target_limit(self.config, self.override, now)
        apply = desired if timer_ok else self.config["base_limit"]
        actual = None
        try:
            actual = self.read_hardware()
            if self.config["management_enabled"] and actual != apply:
                actual = None
                self.hw.write(apply)
                for attempt in range(3):
                    actual = None
                    actual = self.read_hardware()
                    if actual == apply:
                        break
                    if attempt < 2:
                        time.sleep(0.3)
                if actual != apply:
                    raise RuntimeError(f"Requested {apply}%, but the EC reports {actual}%.")
                self.record(f"Applied and verified {apply}% ({reason})")
        except Exception as exc:
            errors.append(f"Hardware: {exc}")
            if "Unknown charge-limit output" in str(exc):
                actual = None
        if errors:
            self.record("; ".join(errors))
        return self.publish(actual, errors)

    def configure(self, value: dict) -> dict:
        config = validate_config(value)
        if config["management_enabled"] != self.config["management_enabled"]:
            raise ValueError("Use the Charging management switch to enable or disable management.")
        previous = self.config, self.override, self.load_error
        self.config, self.override, self.load_error = config, None, None
        try:
            self.save()
        except Exception:
            self.config, self.override, self.load_error = previous
            raise
        self.return_at = None
        self.sleep_note = ""
        self.record("Schedule saved; existing override cleared")
        return self.reconcile("schedule changed")

    def set_management(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Charging management must be true or false.")
        if enabled == self.config["management_enabled"]:
            return self.reconcile("management unchanged")
        if enabled and self.load_error:
            raise ValueError("Save valid settings before enabling charging management.")
        previous = self.config, self.override, self.disable_warning
        self.config = {**self.config, "management_enabled": enabled}
        self.override = None
        self.disable_warning = None if enabled else "Everyday-limit restoration was not verified. Check the EC readback."
        try:
            self.save()
        except Exception:
            self.config, self.override, self.disable_warning = previous
            raise
        self.return_at = None
        self.sleep_note = ""
        self.record("Charging management enabled" if enabled else "Charging management disabled; overrides cleared")
        if not enabled:
            try:
                self.timers.sync(self.config, None)
            except Exception as exc:
                self.record(f"Could not stop schedule timer: {exc}")
            try:
                self.hw.write(self.config["base_limit"])
                actual = self.read_hardware()
                if actual != self.config["base_limit"]:
                    raise RuntimeError(f"EC reports {actual}%")
                self.disable_warning = None
                self.record(f"Restored {self.config['base_limit']}%; charging is now unmanaged")
            except Exception as exc:
                self.disable_warning = f"Management is off, but restoring {self.config['base_limit']}% failed: {exc}"[:4096]
                self.record(self.disable_warning)
            try:
                self.save()
            except OSError as exc:
                self.disable_warning = f"Management is off; could not save the restoration result: {exc}"[:4096]
                self.record(self.disable_warning)
        return self.reconcile("management changed")

    def set_override(self, limit: int, duration: str) -> dict:
        previous = self.override
        self.override = new_override(self.config, limit, duration, self.clock())
        try:
            self.save()
        except Exception:
            self.override = previous
            raise
        self.record(f"Temporary {limit}% limit selected")
        return self.reconcile("override")

    def clear_override(self) -> dict:
        previous = self.override
        self.override = None
        try:
            self.save()
        except Exception:
            self.override = previous
            raise
        self.record("Schedule resumed" if self.config["management_enabled"] else "Override cleared")
        return self.reconcile("override cleared")

    def restore_base(self) -> dict:
        if self.stopping:
            return copy.deepcopy(self.status)
        self.stopping = True
        self.return_at = None
        errors = [self.disable_warning] if self.disable_warning else []
        try:
            self.timers.sync({**self.config, "management_enabled": False}, None)
        except Exception as exc:
            errors.append(f"Could not stop schedule timer: {exc}")
        actual = None
        try:
            if self.config["management_enabled"]:
                self.hw.write(self.config["base_limit"])
            actual = self.read_hardware()
            if self.config["management_enabled"]:
                if actual != self.config["base_limit"]:
                    raise RuntimeError(f"EC still reports {actual}%")
                self.record("Everyday limit restored before service stop")
        except Exception as exc:
            errors.append(f"Could not restore everyday limit: {exc}" if self.config["management_enabled"] else f"Hardware: {exc}")
            self.record(errors[-1])
        return self.publish(actual, errors)
