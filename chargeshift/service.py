"""System D-Bus service. Only this process talks to the EC."""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from .controller import Controller
from .core import BUS_NAME, INTERFACE, OBJECT_PATH, read_json, safe_resuspend

ACTION = "io.github.chargeshift.manage"
LOGIN = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
LOGIN_IFACE = LOGIN + ".Manager"
METHODS = {"GetStatus": "", "Refresh": "", "Reconcile": "", "RestoreBase": "", "UpdateSettings": "s", "SetOverride": "us", "ClearOverride": "", "SetManagement": "b", "CancelReturn": ""}
XML = "<node><interface name='" + INTERFACE + "'>" + "".join("<method name='" + name + "'>" + "".join(f"<arg name='in{i}' type='{kind}' direction='in'/>" for i, kind in enumerate(signature)) + "<arg name='status' type='s' direction='out'/></method>" for name, signature in METHODS.items()) + "<signal name='Changed'/></interface></node>"


class Service:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.engine = Controller(Path("/var/lib/chargeshift/state.json"))
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chargeshift")
        self.pending = 0
        self.auth_pending = 0
        self.refresh_at = 0
        self.sleep_snapshot = None
        self.generation = 0
        self.registration = self.connection.register_object(OBJECT_PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], self.on_method, None, None)
        self.owner = Gio.bus_own_name_on_connection(self.connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, self.on_acquired, self.on_lost)
        self.connection.signal_subscribe(LOGIN, LOGIN_IFACE, "PrepareForSleep", LOGIN_PATH, None, Gio.DBusSignalFlags.NONE, self.on_sleep)
        GLib.timeout_add_seconds(30, self.tick)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)

    def quit(self, *_):
        self.loop.quit()
        return False

    def on_acquired(self, *_):
        self.submit(lambda: self.engine.reconcile("service start"))

    def on_lost(self, *_):
        logging.error("Lost the Charge Shift system bus name")
        self.loop.quit()

    def reply(self, invocation, value):
        if invocation:
            invocation.return_value(GLib.Variant("(s)", (json.dumps(value),)))

    def submit(self, function, invocation=None):
        if self.pending >= 8:
            if invocation:
                invocation.return_dbus_error(INTERFACE + ".Busy", "Charge Shift is busy. Try again shortly.")
            return
        self.pending += 1
        future = self.pool.submit(function)
        def finished():
            self.pending -= 1
            try:
                value = future.result()
                self.reply(invocation, value if value is not None else self.engine.status)
                self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, "Changed", None)
            except Exception as exc:
                logging.exception("Charge Shift operation failed")
                if invocation:
                    invocation.return_dbus_error(INTERFACE + ".Failed", str(exc))
            return False
        future.add_done_callback(lambda _: GLib.idle_add(finished))

    def uid(self, sender):
        response = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)), GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
        return response.unpack()[0]

    def authorize(self, sender, invocation, function):
        if self.auth_pending >= 4:
            invocation.return_dbus_error(INTERFACE + ".Busy", "Another authorization is in progress.")
            return
        self.auth_pending += 1
        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        parameters = GLib.Variant("((sa{sv})sa{ss}us)", (subject, ACTION, {}, 1, ""))
        def checked(connection, result, *_):
            self.auth_pending -= 1
            try:
                authorized = connection.call_finish(result).unpack()[0][0]
                if not authorized:
                    raise PermissionError("Authorization was cancelled or denied.")
                self.submit(function, invocation)
            except Exception as exc:
                invocation.return_dbus_error(INTERFACE + ".NotAuthorized", str(exc))
        self.connection.call("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority", "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization", parameters, None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 120000, None, checked)

    def on_method(self, connection, sender, path, interface, method, parameters, invocation, *_):
        try:
            args = parameters.unpack()
            if method == "GetStatus":
                self.reply(invocation, self.engine.status)
                return
            if method == "CancelReturn":
                self.generation += 1
                self.engine.return_at = None
                self.engine.sleep_note = "Return to sleep cancelled by user"
                self.submit(lambda: self.engine.reconcile("stay awake"), invocation)
                return
            if method == "Refresh":
                if time.monotonic() - self.refresh_at < 5:
                    self.reply(invocation, self.engine.status)
                else:
                    self.refresh_at = time.monotonic()
                    self.submit(lambda: self.engine.reconcile("refresh"), invocation)
                return
            if method in ("Reconcile", "RestoreBase"):
                if self.uid(sender) != 0:
                    raise PermissionError("This method is reserved for root-owned system services.")
                function = self.engine.restore_base if method == "RestoreBase" else lambda: self.engine.reconcile("system timer")
                self.submit(function, invocation)
                return
            def update_settings():
                result = self.engine.configure(read_json(args[0]))
                self.generation += 1
                return result
            def set_management():
                result = self.engine.set_management(args[0])
                self.generation += 1
                return result
            operations = {"UpdateSettings": update_settings, "SetManagement": set_management, "SetOverride": lambda: self.engine.set_override(*args), "ClearOverride": self.engine.clear_override}
            if method not in operations:
                raise ValueError("Unknown method")
            self.authorize(sender, invocation, operations[method])
        except Exception as exc:
            invocation.return_dbus_error(INTERFACE + ".Failed", str(exc))

    def tick(self):
        if self.pending == 0:
            self.submit(lambda: self.engine.reconcile("periodic check"))
        return True

    def on_sleep(self, connection, sender, path, interface, member, parameters, *_):
        going_down = parameters.unpack()[0]
        self.generation += 1
        self.engine.return_at = None
        if going_down:
            self.sleep_snapshot = {"at": time.time(), "event": self.engine.status.get("next_wake")}
        else:
            snapshot, self.sleep_snapshot = self.sleep_snapshot, None
            generation = self.generation
            def after_delay():
                self.submit(lambda: self.after_resume(snapshot, generation))
                return False
            GLib.timeout_add_seconds(3, after_delay)

    def after_resume(self, snapshot, generation):
        self.engine.reconcile("resume")
        now = time.time()
        expected = snapshot.get("event") if snapshot else None
        near = bool(expected and snapshot["at"] < expected and abs(now - expected) <= 90)
        slept = now - snapshot["at"] if snapshot else 0
        if generation == self.generation and self.engine.config["management_enabled"] and self.engine.config["wake"] and self.engine.config["resuspend"] and not self.engine.status["errors"] and near and slept >= 30:
            self.engine.return_at = now + 45
            self.engine.sleep_note = "Checking whether it is safe to return to sleep in 45 seconds"
            def arm():
                def later():
                    self.submit(lambda: self.return_to_sleep(generation, near, slept))
                    return False
                GLib.timeout_add_seconds(45, later)
                return False
            GLib.idle_add(arm)
        else:
            self.engine.sleep_note = ""
        return self.engine.publish(self.engine.status["actual_limit"], self.engine.status["errors"])

    def login_call(self, method, parameters=None):
        return self.connection.call_sync(LOGIN, LOGIN_PATH, LOGIN_IFACE, method, parameters, None, Gio.DBusCallFlags.NONE, 10000, None).unpack()

    def return_to_sleep(self, generation, near, slept):
        self.engine.return_at = None
        if generation != self.generation or self.engine.stopping or not self.engine.config["management_enabled"] or not self.engine.config["wake"] or not self.engine.config["resuspend"]:
            return self.engine.publish(self.engine.status["actual_limit"], self.engine.status["errors"])
        try:
            response = self.connection.call_sync(LOGIN, LOGIN_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (LOGIN_IFACE,)), None, Gio.DBusCallFlags.NONE, 5000, None)
            props = response.unpack()[0]
            locks = self.login_call("ListInhibitors")[0]
            blocked = any("sleep" in item[0].split(":") and item[3] in ("block", "block-weak") for item in locks)
            allowed = safe_resuspend(enabled=self.engine.config["management_enabled"] and self.engine.config["wake"] and self.engine.config["resuspend"], woke_near_event=near, slept_seconds=slept, lid_closed=props.get("LidClosed") is True, docked=props.get("Docked", True), idle=props.get("IdleHint") is True, inhibited=blocked, power_known=props.get("OnExternalPower") is True)
            if not allowed:
                self.engine.sleep_note = "Stayed awake: lid, dock, idle, power, or inhibitor checks did not pass"
            else:
                self.engine.sleep_note = "Returning to suspend after a scheduled wake"
                self.engine.record(self.engine.sleep_note)
                self.login_call("SuspendWithFlags", GLib.Variant("(t)", (1,)))
        except Exception as exc:
            self.engine.sleep_note = f"Stayed awake: {exc}"
        return self.engine.publish(self.engine.status["actual_limit"], self.engine.status["errors"])

    def run(self):
        self.loop.run()
        self.pool.shutdown(wait=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run the installed chargeshift.service, not this daemon as a desktop user.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    Service().run()


if __name__ == "__main__":
    raise SystemExit(main())
