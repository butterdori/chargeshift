import configparser
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IntegrationFileTests(unittest.TestCase):
    def test_system_bus_policy_has_root_ownership(self):
        tree = ET.parse(ROOT / "data/io.github.chargeshift.ChargeShift.conf")
        owners = [(policy.attrib, child.attrib) for policy in tree.getroot().findall("policy") for child in policy.findall("allow") if "own" in child.attrib]
        self.assertEqual(owners, [({"user": "root"}, {"own": "io.github.chargeshift.ChargeShift"})])

    def test_polkit_requires_active_admin_authentication(self):
        root = ET.parse(ROOT / "data/io.github.chargeshift.policy").getroot()
        action = root.find("action")
        self.assertEqual(action.attrib["id"], "io.github.chargeshift.manage")
        self.assertEqual(action.findtext("defaults/allow_active"), "auth_admin_keep")
        self.assertEqual(action.findtext("defaults/allow_inactive"), "no")
        self.assertEqual(action.findtext("defaults/allow_any"), "no")

    def test_launchers_use_isolated_python(self):
        for name in ("chargeshift", "chargeshiftctl", "chargeshiftd"):
            text = (ROOT / "bin" / name).read_text()
            self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
            self.assertIn('sys.path.insert(0, "/usr/lib/chargeshift")', text)

    def test_desktop_launcher_is_unprivileged(self):
        parser = configparser.ConfigParser()
        parser.read(ROOT / "data/io.github.chargeshift.ChargeShift.desktop")
        self.assertEqual(parser["Desktop Entry"]["Exec"], "chargeshift")
        self.assertEqual(parser["Desktop Entry"]["Terminal"], "false")

    def test_new_icons_are_vector_only(self):
        for folder, name in (("scalable", "io.github.chargeshift.ChargeShift"), ("symbolic", "io.github.chargeshift.ChargeShift-symbolic")):
            icon = ROOT / f"data/icons/hicolor/{folder}/apps/{name}.svg"
            svg = ET.parse(icon).getroot()
            self.assertTrue(svg.attrib.get("viewBox"))
            self.assertFalse(svg.findall(".//{http://www.w3.org/2000/svg}image"))
            self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")

    def test_launcher_and_polkit_use_the_new_app_icon(self):
        self.assertIn("Icon=io.github.chargeshift.ChargeShift", (ROOT / "data/io.github.chargeshift.ChargeShift.desktop").read_text())
        policy = ET.parse(ROOT / "data/io.github.chargeshift.policy").getroot()
        self.assertEqual(policy.findtext("action/icon_name"), "io.github.chargeshift.ChargeShift")

    def test_two_requested_explanations_are_preserved(self):
        text = (ROOT / "chargeshift/app.py").read_text()
        self.assertIn("This changes the charging limit, not the battery’s current percentage. Firmware controls charging and any discharge behavior.", text)
        self.assertIn("Status you can verify.", text)
        self.assertIn("The requested target and the EC readback are tracked separately.", text)

    def test_removed_ui_copy_and_dead_controls_are_absent(self):
        text = (ROOT / "chargeshift/app.py").read_text()
        for item in ("No cloud account", "running window", "can be closed", "continues after closing", "BIOS configuration", "Wake capability", "2-minute", "ArmWakeTest", "bios_limit", "test_result", "test_at", "A full battery for work", "not guaranteed; test on this laptop"):
            with self.subTest(item=item):
                self.assertNotIn(item, text)
        self.assertIn('subtitle="For Framework laptops"', text)

    def test_cli_master_commands_replace_wake_test_commands(self):
        text = (ROOT / "chargeshift/cli.py").read_text()
        self.assertIn('"enable": ("SetManagement", "b", (True,))', text)
        self.assertIn('"disable": ("SetManagement", "b", (False,))', text)
        self.assertNotIn("wake-test", text)
        self.assertNotIn("cancel-test", text)

    def test_service_has_stop_reset_and_no_arbitrary_command(self):
        text = (ROOT / "data/chargeshift.service").read_text()
        self.assertIn("ExecStop=-/usr/bin/chargeshiftctl restore-base", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertNotIn("/bin/sh", text)
        self.assertNotIn("PrivateDevices=yes", text)


if __name__ == "__main__":
    unittest.main()
