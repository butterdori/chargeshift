"""Pure scheduling, validation and parsing; no privileged operations."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BUS_NAME = "io.github.chargeshift.ChargeShift"
OBJECT_PATH = "/io/github/chargeshift/ChargeShift"
INTERFACE = BUS_NAME
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULTS = {"management_enabled": True, "enabled": True, "days": [0, 1, 2, 3, 4], "start": "05:00", "end": "17:00", "base_limit": 80, "full_limit": 100, "wake": True, "resuspend": False}


def local_zone():
    with Path("/etc/localtime").open("rb") as stream:
        return ZoneInfo.from_file(stream)


def read_json(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError("Settings must be a JSON object smaller than 8 KiB.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate setting: {key}")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def validate_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("Settings contain missing or unknown fields.")
    for key in ("management_enabled", "enabled", "wake", "resuspend"):
        if type(value[key]) is not bool:
            raise ValueError(f"{key} must be true or false.")
    days = value["days"]
    if not isinstance(days, list) or not days or len(days) > 7:
        raise ValueError("Choose at least one weekday.")
    if any(type(day) is not int or not 0 <= day <= 6 for day in days) or len(set(days)) != len(days):
        raise ValueError("Weekdays must be unique values from 0 to 6.")
    for key in ("start", "end"):
        if not isinstance(value[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]):
            raise ValueError("Times must use 24-hour HH:MM format.")
    if value["start"] >= value["end"]:
        raise ValueError("The morning time must be before the daily safety cutoff; overnight windows are not supported.")
    for key in ("base_limit", "full_limit"):
        if type(value[key]) is not int or not 50 <= value[key] <= 100:
            raise ValueError("Charge limits must be whole numbers between 50 and 100.")
    if value["base_limit"] >= value["full_limit"]:
        raise ValueError("The everyday limit must be below the full-charge limit.")
    if value["resuspend"] and not value["wake"]:
        raise ValueError("Enable scheduled wake before enabling return to sleep.")
    return {**value, "days": sorted(days)}


def validate_override(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"limit", "created", "expires"}:
        raise ValueError("Invalid override state.")
    if type(value["limit"]) is not int or not 50 <= value["limit"] <= 100:
        raise ValueError("Invalid override limit.")
    for key in ("created", "expires"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
            raise ValueError("Invalid override time.")
    if not 0 < value["expires"] - value["created"] <= 26 * 3600:
        raise ValueError("An override must expire within 26 hours.")
    return dict(value)


def scheduled_limit(config: dict, now: datetime) -> int:
    active = config["enabled"] and now.weekday() in config["days"]
    active = active and config["start"] <= now.strftime("%H:%M") < config["end"]
    return config["full_limit"] if active else config["base_limit"]


def events(config: dict, now: datetime, days: int = 9) -> list[dict]:
    """Future local-time starts plus a safety reset every day."""
    if not config["management_enabled"]:
        return []
    result = []
    for offset in range(days):
        date = now.date() + timedelta(days=offset)
        entries = [(config["end"], config["base_limit"], "Daily safety reset")]
        if config["enabled"] and date.weekday() in config["days"]:
            entries.append((config["start"], config["full_limit"], "Full-charge window"))
        for clock, limit, label in entries:
            hour, minute = map(int, clock.split(":"))
            dt = datetime(date.year, date.month, date.day, hour, minute, tzinfo=now.tzinfo)
            stamp = dt.timestamp()
            if stamp > now.timestamp():
                result.append({"at": stamp, "limit": limit, "label": label})
    return sorted(result, key=lambda event: event["at"])


def next_cutoff(config: dict, now: datetime) -> float:
    return next(event["at"] for event in events(config, now) if event["label"] == "Daily safety reset")


def live_override(config: dict, override: dict | None, now: datetime):
    if not config["management_enabled"] or not override or not override["created"] <= now.timestamp() < override["expires"]:
        return None
    created = datetime.fromtimestamp(override["created"], now.tzinfo)
    if now.timestamp() >= next_cutoff(config, created):
        return None
    return override


def target_limit(config: dict, override: dict | None, now: datetime) -> int | None:
    if not config["management_enabled"]:
        return None
    active = live_override(config, override, now)
    return active["limit"] if active else scheduled_limit(config, now)


def new_override(config: dict, limit: int, duration: str, now: datetime) -> dict:
    if not config["management_enabled"]:
        raise ValueError("Enable charging management before selecting an override.")
    if type(limit) is not int or not 50 <= limit <= 100:
        raise ValueError("Override limit must be a whole number from 50 to 100.")
    if duration not in ("next", "1h", "4h", "cutoff"):
        raise ValueError("Choose next event, 1 hour, 4 hours, or daily cutoff.")
    cutoff = next_cutoff(config, now)
    expires = {"next": events(config, now)[0]["at"], "1h": now.timestamp() + 3600, "4h": now.timestamp() + 14400, "cutoff": cutoff}[duration]
    return {"limit": limit, "created": now.timestamp(), "expires": min(expires, cutoff)}


def next_event(config: dict, override: dict | None, now: datetime) -> dict | None:
    if not config["management_enabled"]:
        return None
    upcoming = events(config, now)
    active = live_override(config, override, now)
    if active:
        after = datetime.fromtimestamp(active["expires"], now.tzinfo)
        upcoming = [event for event in upcoming if event["at"] >= active["expires"]]
        upcoming.append({"at": active["expires"], "limit": scheduled_limit(config, after), "label": "Override ends"})
    return min(upcoming, key=lambda event: (event["at"], event["label"] != "Override ends"))


def timer_dropin(config: dict, override: dict | None) -> str:
    lines = ["[Timer]", "OnCalendar="]
    if not config["management_enabled"]:
        return "\n".join(["[Timer]", "OnClockChange=false", "OnTimezoneChange=false", "WakeSystem=false", ""])
    if config["enabled"]:
        weekdays = ",".join(DAY_NAMES[day] for day in config["days"])
        lines.append(f"OnCalendar={weekdays} *-*-* {config['start']}:00")
    lines.append(f"OnCalendar=*-*-* {config['end']}:00")
    if override:
        dt = datetime.fromtimestamp(math.ceil(override["expires"]), timezone.utc)
        lines.append(f"OnCalendar={dt:%Y-%m-%d %H:%M:%S} UTC")
    lines += ["OnClockChange=true", "OnTimezoneChange=true", f"WakeSystem={'true' if config['wake'] else 'false'}", ""]
    return "\n".join(lines)


def parse_limit(output: str) -> int:
    """Never guess a limit from unrelated battery percentages or error text."""
    plain = re.sub(r"\x1b\[[0-9;]*[mK]", "", output)
    patterns = [r"(?im)^\s*Minimum\s+\d{1,3}%\s*,\s*Maximum\s+(\d{1,3})%\s*$", r"(?im)^\s*(?:max(?:imum)?\s+)?(?:battery\s+)?charge\s+limit\s*[:=]\s*(\d{1,3})\s*%?\s*$", r"(?im)^\s*(?:max(?:imum)?\s+)?(?:battery\s+)?charge\s+limit\s+(?:is|set to)\s+(\d{1,3})\s*%?\s*$", r"\A\s*(\d{1,3})\s*%?\s*\Z"]
    values = {int(match) for pattern in patterns for match in re.findall(pattern, plain)}
    if len(values) != 1 or not 0 <= next(iter(values)) <= 100:
        raise ValueError("Unknown charge-limit output. See Diagnostics; no percentage has been guessed.")
    return values.pop()


def safe_resuspend(*, enabled: bool, woke_near_event: bool, slept_seconds: float, lid_closed: bool, docked: bool, idle: bool, inhibited: bool, power_known: bool) -> bool:
    return bool(enabled and woke_near_event and slept_seconds >= 30 and lid_closed and not docked and idle and not inhibited and power_known)
