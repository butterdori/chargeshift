"""CLI for the same restricted D-Bus API used by the desktop app."""
import argparse
import json
import sys

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from .core import BUS_NAME, INTERFACE, OBJECT_PATH


def main():
    parser = argparse.ArgumentParser(description="Inspect or control Charge Shift")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "refresh", "clear", "reconcile", "restore-base", "enable", "disable"):
        commands.add_parser(name)
    override = commands.add_parser("override")
    override.add_argument("limit", type=int)
    override.add_argument("--for", dest="duration", choices=("next", "1h", "4h", "cutoff"), default="next")
    args = parser.parse_args()
    methods = {"status": ("GetStatus", "", ()), "refresh": ("Refresh", "", ()), "clear": ("ClearOverride", "", ()), "reconcile": ("Reconcile", "", ()), "restore-base": ("RestoreBase", "", ()), "enable": ("SetManagement", "b", (True,)), "disable": ("SetManagement", "b", (False,))}
    if args.command == "override":
        if not 50 <= args.limit <= 100:
            parser.error("limit must be from 50 to 100")
        method, signature, values = "SetOverride", "us", (args.limit, args.duration)
    else:
        method, signature, values = methods[args.command]
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        result = connection.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, GLib.Variant("(" + signature + ")", values), GLib.VariantType.new("(s)"), Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 180000, None)
        status = json.loads(result.unpack()[0])
        print(json.dumps(status, indent=2))
        return 1 if status["errors"] and args.command != "status" else 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
