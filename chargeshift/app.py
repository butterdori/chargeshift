"""Native GTK 4 / libadwaita interface. Never run this application as root."""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from .core import BUS_NAME, DEFAULTS, INTERFACE, OBJECT_PATH, live_override, local_zone, new_override, next_event, target_limit, validate_config

CSS = """
.hero { padding: 28px; border-radius: 22px; }
.hero-icon { padding: 8px; }
.hero-number { font-size: 54px; font-weight: 800; letter-spacing: -2px; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 1.4px; }
.status-pill { padding: 5px 11px; border-radius: 99px; font-size: 11px; font-weight: 700; background: alpha(@accent_bg_color, 0.12); color: @accent_color; }
.status-pill.unmanaged { color: @window_fg_color; background: alpha(@window_fg_color, 0.08); }
.status-pill.warning { color: @warning_color; background: alpha(@warning_color, 0.14); }
.stat-card { padding: 18px 20px; border-radius: 16px; }
.stat-number { font-size: 24px; font-weight: 700; }
.page-heading { font-size: 28px; font-weight: 800; letter-spacing: -0.7px; }
.day-button { min-width: 28px; min-height: 32px; border-radius: 10px; }
.override-button { padding: 12px 20px; border-radius: 12px; font-weight: 700; }
.small-note { font-size: 12px; }
.mono { font-family: monospace; }
.diagnostics { padding: 14px; border-radius: 12px; }
"""


def label(text="", style=None, align=0, wrap=False):
    widget = Gtk.Label(label=text, xalign=align, wrap=wrap)
    if style:
        for item in style.split():
            widget.add_css_class(item)
    return widget


def box(spacing=12, vertical=True):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, callback, style=None):
    widget = Gtk.Button(label=text)
    if style:
        for item in style.split():
            widget.add_css_class(item)
    widget.connect("clicked", callback)
    return widget


def when(stamp, include_day=True):
    if not stamp:
        return "Not scheduled"
    value = datetime.fromtimestamp(stamp)
    today = datetime.now().date()
    prefix = "Today" if value.date() == today else value.strftime("%a %d %b")
    return f"{prefix} · {value:%H:%M}" if include_day else value.strftime("%H:%M")


class Client:
    def __init__(self, demo=False):
        self.demo = demo
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.config = copy.deepcopy(DEFAULTS)
        self.override = None
        self.actual_limit = 80

    def call(self, method, signature, args, callback):
        def execute():
            if self.demo:
                return self.simulate(method, args)
            connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            parameters = GLib.Variant("(" + signature + ")", args)
            response = connection.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, parameters, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 180000, None)
            return json.loads(response.unpack()[0])
        future = self.pool.submit(execute)
        def finished():
            try:
                result, error = future.result(), None
            except Exception as exc:
                result, error = None, str(exc)
            callback(result, error)
            return False
        future.add_done_callback(lambda _: GLib.idle_add(finished))

    def simulate(self, method, args):
        now = datetime.now(local_zone())
        if method == "UpdateSettings":
            config = validate_config(json.loads(args[0]))
            if config["management_enabled"] != self.config["management_enabled"]:
                raise ValueError("Use the Charging management switch to enable or disable management.")
            self.config = config
            self.override = None
        elif method == "SetManagement":
            if args[0] != self.config["management_enabled"]:
                self.config = {**self.config, "management_enabled": args[0]}
                self.override = None
                if not args[0]:
                    self.actual_limit = self.config["base_limit"]
        elif method == "SetOverride":
            self.override = new_override(self.config, args[0], args[1], now)
        elif method == "ClearOverride":
            self.override = None
        self.override = live_override(self.config, self.override, now)
        managed = self.config["management_enabled"]
        limit = target_limit(self.config, self.override, now)
        if managed:
            self.actual_limit = limit
        upcoming = next_event(self.config, self.override, now)
        return {"config": copy.deepcopy(self.config), "override": copy.deepcopy(self.override), "actual_limit": self.actual_limit, "target_limit": limit, "verified": managed, "checked_at": time.time(), "battery": {"percent": 74, "state": "Charging", "ac": True}, "next_event": upcoming, "next_wake": upcoming["at"] if upcoming and self.config["wake"] else None, "timer_state": "Demo only" if managed else "inactive", "errors": [], "raw_output": f"Minimum 0%, Maximum {self.actual_limit}%", "driver": "auto", "disable_warning": None, "sleep_note": "", "return_at": None, "history": [{"at": time.time(), "message": "Demonstration data; no hardware accessed"}], "timezone": now.strftime("%Z")}


class Window(Adw.ApplicationWindow):
    def __init__(self, application, demo):
        super().__init__(application=application, title="Charge Shift", default_width=780, default_height=850)
        self.set_size_request(560, 540)
        self.client = Client(demo)
        self.demo = demo
        self.state = None
        self.dirty = False
        self.loading = False
        self.reading = False
        self.working = False
        self.initialized = False
        self.syncing_management = False
        self.request_epoch = 0
        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        layout = Adw.ToolbarView()
        self.toast.set_child(layout)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Charge Shift", subtitle="For Framework laptops"))
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh hardware status")
        refresh.connect("clicked", lambda _: self.refresh(True))
        header.pack_end(refresh)
        menu = Gio.Menu()
        menu.append("About Charge Shift", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        layout.add_top_bar(header)
        management_group = Adw.PreferencesGroup()
        self.management = Adw.SwitchRow(title="Charging management", subtitle="Connecting…", sensitive=False)
        self.management.set_tooltip_text("Turning off restores the everyday limit once, then stops charging changes and scheduled wakes.")
        self.management.connect("notify::active", self.toggle_management)
        management_group.add(self.management)
        management_clamp = Adw.Clamp(maximum_size=652, tightening_threshold=512)
        management_clamp.set_child(management_group)
        for side in ("start", "end"):
            getattr(management_clamp, "set_margin_" + side)(24)
        management_clamp.set_margin_bottom(12)
        layout.add_top_bar(management_clamp)
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_bottom(10)
        layout.add_top_bar(switcher)
        self.banner = Adw.Banner(title="Connecting to the system service…", revealed=True)
        self.banner.set_button_label("Details")
        self.banner.connect("button-clicked", lambda _: self.stack.set_visible_child_name("system"))
        layout.add_top_bar(self.banner)
        layout.set_content(self.stack)
        self.build_overview()
        self.build_schedule()
        self.build_system()
        self.refresh()
        GLib.timeout_add_seconds(5, self.periodic)

    def page(self, name, title, icon):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=700, tightening_threshold=560)
        content = box(24)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, "set_margin_" + side)(24)
        clamp.set_child(content)
        scroll.set_child(clamp)
        self.stack.add_titled_with_icon(scroll, name, title, icon)
        return content

    def heading(self, page, title, subtitle):
        group = box(6)
        group.append(label(title, "page-heading"))
        group.append(label(subtitle, "dim-label", wrap=True))
        page.append(group)

    def build_overview(self):
        page = self.page("overview", "Overview", BUS_NAME + "-symbolic")
        hero = box(24, False)
        hero.add_css_class("card")
        hero.add_css_class("hero")
        icon = Gtk.Image.new_from_icon_name(BUS_NAME)
        icon.set_pixel_size(72)
        icon.add_css_class("hero-icon")
        icon.set_valign(Gtk.Align.CENTER)
        hero.append(icon)
        readout = box(2)
        readout.set_hexpand(True)
        readout.append(label("EC CHARGE LIMIT", "eyebrow dim-label"))
        self.limit_label = label("—", "hero-number")
        readout.append(self.limit_label)
        self.checked_label = label("Waiting for hardware", "small-note dim-label", wrap=True)
        readout.append(self.checked_label)
        hero.append(readout)
        self.verified_label = label("Connecting", "status-pill")
        self.verified_label.set_valign(Gtk.Align.START)
        hero.append(self.verified_label)
        page.append(hero)
        stats = box(12, False)
        stats.set_homogeneous(True)
        battery = box(8)
        battery.add_css_class("card")
        battery.add_css_class("stat-card")
        battery.append(label("BATTERY NOW", "eyebrow dim-label"))
        self.battery_label = label("Unknown", "stat-number")
        battery.append(self.battery_label)
        self.battery_bar = Gtk.ProgressBar()
        battery.append(self.battery_bar)
        self.power_label = label("Power source unknown", "small-note dim-label", wrap=True)
        battery.append(self.power_label)
        stats.append(battery)
        next_card = box(8)
        next_card.add_css_class("card")
        next_card.add_css_class("stat-card")
        next_card.append(label("NEXT CHANGE", "eyebrow dim-label"))
        self.next_limit = label("—", "stat-number")
        next_card.append(self.next_limit)
        self.next_time = label("Waiting for schedule", "small-note", wrap=True)
        next_card.append(self.next_time)
        self.next_kind = label("", "small-note dim-label", wrap=True)
        next_card.append(self.next_kind)
        stats.append(next_card)
        page.append(stats)
        self.mode_group = Adw.PreferencesGroup(title="Current plan")
        self.mode_row = Adw.ActionRow(title="Connecting")
        self.mode_row.add_prefix(Gtk.Image.new_from_icon_name("alarm-symbolic"))
        self.mode_group.add(self.mode_row)
        page.append(self.mode_group)
        overrides = Adw.PreferencesGroup(title="Temporary override")
        duration_row = Adw.ActionRow(title="Keep override")
        self.duration = Gtk.DropDown.new_from_strings(["Until next event", "For 1 hour", "For 4 hours", "Until daily cutoff"])
        self.duration.set_valign(Gtk.Align.CENTER)
        duration_row.add_suffix(self.duration)
        overrides.add(duration_row)
        page.append(overrides)
        choices = box(12, False)
        choices.set_homogeneous(True)
        self.base_button = button("Use 80%", lambda _: self.override(False), "override-button")
        self.full_button = button("Charge to 100%", lambda _: self.override(True), "suggested-action override-button")
        choices.append(self.base_button)
        choices.append(self.full_button)
        self.override_controls = choices
        page.append(choices)
        self.resume_button = button("Resume schedule", lambda _: self.mutate("ClearOverride"), "flat")
        self.resume_button.set_halign(Gtk.Align.CENTER)
        self.resume_button.set_visible(False)
        page.append(self.resume_button)
        self.override_note = label("No temporary override", "dim-label small-note", wrap=True)
        page.append(self.override_note)
        page.append(label("This changes the charging limit, not the battery’s current percentage. Firmware controls charging and any discharge behavior.", "dim-label small-note", wrap=True))

    def changed(self, *_):
        if not self.loading:
            self.dirty = True
            self.save_button.set_label("Save changes")

    def switch_row(self, group, title, subtitle, active=False):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
        row.set_subtitle_lines(3)
        row.connect("notify::active", self.changed)
        group.add(row)
        return row

    def entry_row(self, group, title, subtitle):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        entry = Gtk.Entry(width_chars=5, max_length=5, valign=Gtk.Align.CENTER)
        entry.set_alignment(0.5)
        entry.add_css_class("mono")
        entry.connect("changed", self.changed)
        row.add_suffix(entry)
        group.add(row)
        return entry

    def spin_row(self, group, title, subtitle, value):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        spin = Gtk.SpinButton.new_with_range(50, 100, 5)
        spin.set_value(value)
        spin.set_valign(Gtk.Align.CENTER)
        spin.connect("value-changed", self.changed)
        row.add_suffix(spin)
        row.add_suffix(label("%", "dim-label"))
        group.add(row)
        return spin

    def build_schedule(self):
        page = self.page("schedule", "Schedule", "x-office-calendar-symbolic")
        self.schedule_page = page
        self.heading(page, "Schedule", "Local time")
        schedule = Adw.PreferencesGroup(title="Full-charge window")
        self.enabled = self.switch_row(schedule, "Use a weekly schedule", "When off, keep the everyday limit unless you select an override.")
        page.append(schedule)
        self.days = []
        daybox = box(6, False)
        daybox.set_homogeneous(True)
        for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            day = Gtk.ToggleButton(label=name, tooltip_text=name)
            day.add_css_class("day-button")
            day.connect("toggled", self.changed)
            self.days.append(day)
            daybox.append(day)
        page.append(daybox)
        times = Adw.PreferencesGroup()
        self.start = self.entry_row(times, "Full charge from", "24-hour time · selected weekdays")
        self.end = self.entry_row(times, "Daily safety cutoff", "24-hour time · every day, including weekends")
        page.append(times)
        limits = Adw.PreferencesGroup(title="Charge limits")
        self.base = self.spin_row(limits, "Everyday limit", "Outside the full-charge window", 80)
        self.full = self.spin_row(limits, "Full-charge limit", "During the full-charge window", 100)
        page.append(limits)
        sleep = Adw.PreferencesGroup(title="While the laptop is asleep")
        self.wake = self.switch_row(sleep, "Wake for scheduled changes", "Wake from sleep, not from power off.")
        self.resuspend = self.switch_row(sleep, "Return to sleep after a scheduled wake", "Experimental · after 45 seconds, when closed, undocked, idle, on AC and not sleep-inhibited.")
        page.append(sleep)
        bottom = box(12)
        self.save_button = button("Save schedule", self.save_settings, "suggested-action pill")
        self.save_button.set_halign(Gtk.Align.END)
        self.save_button.set_tooltip_text("Clears any override. Applies immediately only while charging management is on.")
        bottom.append(self.save_button)
        page.append(bottom)

    def build_system(self):
        page = self.page("system", "System", "emblem-system-symbolic")
        self.heading(page, "Status you can verify.", "The requested target and the EC readback are tracked separately.")
        group = Adw.PreferencesGroup(title="Service & hardware")
        self.system_rows = {}
        for key, title in (("service", "System service"), ("target", "Requested target"), ("actual", "EC readback"), ("timer", "Schedule timer"), ("wake", "Next wake request"), ("zone", "Local time zone")):
            row = Adw.ActionRow(title=title, subtitle="Unknown")
            row.set_subtitle_selectable(True)
            group.add(row)
            self.system_rows[key] = row
        page.append(group)
        self.sleep_note = label("", "dim-label small-note", wrap=True)
        page.append(self.sleep_note)
        self.stay_button = button("Stay awake — cancel return", lambda _: self.mutate("CancelReturn"))
        self.stay_button.set_visible(False)
        page.append(self.stay_button)
        group = Adw.PreferencesGroup(title="Diagnostics")
        group.set_header_suffix(button("Copy", self.copy_diagnostics, "flat"))
        self.diagnostic_text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.diagnostic_text.add_css_class("diagnostics")
        scrolled = Gtk.ScrolledWindow(min_content_height=170, max_content_height=240, propagate_natural_height=True)
        scrolled.set_child(self.diagnostic_text)
        scrolled.add_css_class("card")
        group.add(scrolled)
        page.append(group)
        self.history_label = label("No events yet", "small-note dim-label", wrap=True)
        self.history_label.set_selectable(True)
        history = Adw.PreferencesGroup(title="Recent activity · this service session")
        history.add(self.history_label)
        page.append(history)

    def load_settings(self, config):
        self.loading = True
        self.enabled.set_active(config["enabled"])
        for index, day in enumerate(self.days):
            day.set_active(index in config["days"])
        self.start.set_text(config["start"])
        self.end.set_text(config["end"])
        self.base.set_value(config["base_limit"])
        self.full.set_value(config["full_limit"])
        self.wake.set_active(config["wake"])
        self.resuspend.set_active(config["resuspend"])
        self.loading = False
        self.initialized = True

    def save_settings(self, *_):
        if not self.state or self.working:
            return
        config = {"management_enabled": self.state["config"]["management_enabled"], "enabled": self.enabled.get_active(), "days": [i for i, day in enumerate(self.days) if day.get_active()], "start": self.start.get_text().strip(), "end": self.end.get_text().strip(), "base_limit": self.base.get_value_as_int(), "full_limit": self.full.get_value_as_int(), "wake": self.wake.get_active(), "resuspend": self.resuspend.get_active()}
        try:
            config = validate_config(config)
        except ValueError as exc:
            self.toast.add_toast(Adw.Toast.new(str(exc)))
            return
        self.mutate("UpdateSettings", "s", (json.dumps(config),))

    def override(self, full):
        if not self.state or not self.state["config"]["management_enabled"]:
            return
        limit = self.state["config"]["full_limit" if full else "base_limit"]
        duration = ("next", "1h", "4h", "cutoff")[self.duration.get_selected()]
        self.mutate("SetOverride", "us", (limit, duration))

    def sync_management(self, enabled):
        self.syncing_management = True
        self.management.set_active(enabled)
        self.syncing_management = False

    def toggle_management(self, *_):
        if self.syncing_management or self.working or not self.state:
            return
        enabled = self.management.get_active()
        if enabled == self.state["config"]["management_enabled"]:
            return
        self.sync_management(self.state["config"]["management_enabled"])
        self.mutate("SetManagement", "b", (enabled,))

    def update_controls(self):
        available = bool(self.state) and not self.working
        managed = available and self.state["config"]["management_enabled"]
        self.management.set_sensitive(available)
        self.schedule_page.set_sensitive(available)
        for widget in (self.override_controls, self.duration, self.resume_button):
            widget.set_sensitive(managed)
        self.stay_button.set_sensitive(available)

    def busy(self, active):
        self.working = active
        self.spinner.set_spinning(active)
        self.update_controls()

    def mutate(self, method, signature="", args=()):
        if self.working or not self.state:
            return
        self.request_epoch += 1
        self.busy(True)
        def done(state, error):
            self.request_epoch += 1
            self.busy(False)
            if error:
                self.sync_management(self.state["config"]["management_enabled"])
                self.toast.add_toast(Adw.Toast.new(error[:240]))
                self.refresh()
                return
            if method == "UpdateSettings":
                self.dirty = False
                self.load_settings(state["config"])
                self.save_button.set_label("Save schedule")
            self.render(state)
            if state["errors"]:
                message = "Check System: " + state["errors"][0][:160]
            elif method == "SetManagement":
                message = "Charging management enabled" if state["config"]["management_enabled"] else "Charging management disabled"
            else:
                message = "Schedule saved" if method == "UpdateSettings" else "Updated"
            self.toast.add_toast(Adw.Toast.new(message))
        self.client.call(method, signature, args, done)

    def periodic(self):
        if self.get_visible():
            self.refresh()
        return True

    def refresh(self, hardware=False):
        if self.reading or self.working:
            return
        epoch = self.request_epoch
        self.reading = True
        def done(state, error):
            self.reading = False
            if epoch != self.request_epoch:
                return
            if error:
                self.banner.set_title("System service unavailable — see System for setup")
                self.banner.set_revealed(True)
                self.verified_label.set_label("Offline")
                self.checked_label.set_label("No current hardware reading")
                self.limit_label.set_label("Unknown")
                self.system_rows["service"].set_subtitle("Unavailable")
                self.battery_label.set_label("Unknown")
                self.battery_bar.set_fraction(0)
                self.power_label.set_label("No current power reading")
                self.next_limit.set_label("Unknown")
                self.next_time.set_label("Service unavailable")
                self.next_kind.set_label("")
                self.state = None
                self.management.set_subtitle("Service unavailable")
                self.update_controls()
                self.diagnostic_text.get_buffer().set_text("Enable the installed service:\n\nsudo systemd-tmpfiles --create /usr/lib/tmpfiles.d/chargeshift.conf\nsudo systemctl enable --now chargeshift.service\n\n" + error)
                return
            self.render(state)
        self.client.call("Refresh" if hardware else "GetStatus", "", (), done)

    def render(self, state):
        self.state = state
        config = state["config"]
        managed = config["management_enabled"]
        self.sync_management(managed)
        self.management.set_subtitle("Enabled" if managed else "Disabled · read-only status")
        self.update_controls()
        if not self.initialized or not self.dirty:
            self.load_settings(config)
        errors = state["errors"]
        actual = state["actual_limit"]
        age = max(0, int(time.time() - state["checked_at"]))
        stale = age > 90
        self.limit_label.set_label("Unknown" if actual is None else f"{actual}%")
        self.checked_label.set_label("No hardware reading" if not state["checked_at"] else f"Last EC read · {age}s ago" + (" · stale" if stale else ""))
        verified = state["verified"] and not stale
        self.verified_label.set_label("Check status" if errors or stale or actual is None else "Read only" if not managed else "Demo" if self.demo else "Verified" if verified else "Check status")
        for name in ("warning", "unmanaged"):
            self.verified_label.remove_css_class(name)
        if errors or stale or actual is None or (managed and not verified):
            self.verified_label.add_css_class("warning")
        elif not managed:
            self.verified_label.add_css_class("unmanaged")
        warning = "Check System: " + errors[0][:125] if errors else "Demo data — no hardware or system settings are changed" if self.demo else "Hardware status is stale; refresh to verify" if stale else ""
        self.banner.set_title(warning)
        self.banner.set_revealed(bool(warning))
        battery = state["battery"]
        pct = battery["percent"]
        self.battery_label.set_label("Unknown" if pct is None else f"{pct}%")
        self.battery_bar.set_fraction((pct or 0) / 100)
        source = "Plugged in" if battery["ac"] is True else "On battery" if battery["ac"] is False else "Power source unknown"
        self.power_label.set_label(f"{source} · {battery['state']}")
        event = state["next_event"]
        self.next_limit.set_label(f"{event['limit']}% limit" if event else "—")
        self.next_time.set_label(when(event["at"]) if event else "No scheduled changes")
        self.next_kind.set_label(event["label"] if event else "")
        active = state["override"]
        self.mode_row.set_title("Management disabled" if not managed else "Temporary override" if active else "Weekly schedule" if config["enabled"] else "Everyday limit")
        days = ", ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[i] for i in config["days"])
        plan = f"{days} · {config['start']}–{config['end']} · {config['full_limit']}%" if config["enabled"] else f"Keeping the limit at {config['base_limit']}%"
        self.mode_row.set_subtitle(plan if managed else "")
        self.base_button.set_label(f"Use {config['base_limit']}%")
        self.full_button.set_label(f"Charge to {config['full_limit']}%")
        self.resume_button.set_visible(bool(active) and managed)
        self.override_note.set_label(f"{active['limit']}% until {when(active['expires'])}" if active else f"Daily reset · {config['end']} → {config['base_limit']}%" if managed else "")
        self.override_note.set_visible(managed)
        requested = f"{state['target_limit']}%" if state["target_limit"] is not None else "Not managed"
        wake = when(state["next_wake"]) if managed and config["wake"] else "Disabled"
        values = {"service": "Running" if managed else "Running · read-only", "target": requested, "actual": "Unknown" if actual is None else f"{actual}% · checked {age}s ago", "timer": "Stopped" if state["timer_state"] == "inactive" else state["timer_state"], "wake": wake, "zone": state["timezone"]}
        for key, value in values.items():
            self.system_rows[key].set_subtitle(value)
        self.sleep_note.set_label(state["sleep_note"])
        self.sleep_note.set_visible(bool(state["sleep_note"]) and managed)
        self.stay_button.set_visible(bool(state.get("return_at")) and managed)
        diagnostic = ("\n".join(errors) + "\n\n" if errors else "") + "framework_tool --charge-limit\n" + state["raw_output"] + f"\n\nDriver: {state['driver']}\nRequested target: {requested}\nEC readback: {actual if actual is not None else 'Unknown'}"
        self.diagnostic_text.get_buffer().set_text(diagnostic)
        self.history_label.set_label("\n\n".join(f"{when(item['at'])}  {item['message']}" for item in state["history"][:6]) or "No changes this session")

    def copy_diagnostics(self, *_):
        if self.state:
            Gdk.Display.get_default().get_clipboard().set(json.dumps(self.state, indent=2))
            self.toast.add_toast(Adw.Toast.new("Diagnostics copied"))


class Application(Adw.Application):
    def __init__(self, demo=False):
        super().__init__(application_id=BUS_NAME + (".Demo" if demo else ""), flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo = demo

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(BUS_NAME)
        icons = Path(__file__).resolve().parents[1] / "data/icons"
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons))
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, callback in (("quit", lambda *_: self.quit()), ("about", self.about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q", "<Primary>w"])

    def do_activate(self):
        window = self.get_active_window() or Window(self, self.demo)
        window.present()

    def about(self, *_):
        properties = {"application_name": "Charge Shift", "application_icon": BUS_NAME, "version": __version__, "comments": "Charge scheduling for Framework laptops.", "license_type": Gtk.License.MIT_X11}
        if hasattr(Adw, "AboutDialog"):
            dialog = Adw.AboutDialog(**properties)
            dialog.present(self.get_active_window())
        else:
            dialog = Adw.AboutWindow(transient_for=self.get_active_window(), modal=True, **properties)
            dialog.present()


def main():
    demo = "--demo" in sys.argv
    if os.geteuid() == 0:
        raise SystemExit("Launch Charge Shift as your desktop user, never with sudo.")
    raise SystemExit(Application(demo).run([sys.argv[0]]))


if __name__ == "__main__":
    main()
