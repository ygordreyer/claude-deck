import tempfile
import unittest
from pathlib import Path

from deck_tools import device_name, find_controller


class DeviceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sysfs = self.root / "class/hidraw"
        self.sysfs.mkdir(parents=True)

    def add(self, node, product="1205", vendor="28DE", physical=False, uhid=False):
        device = self.root / "devices" / ("virtual/misc/uhid" if uhid else "pci") / node
        device.mkdir(parents=True)
        raw = self.sysfs / node
        raw.mkdir()
        (raw / "device").symlink_to(device)
        (device / "uevent").write_text(
            f"HID_ID=0003:0000{vendor}:0000{product}\nHID_NAME=Controller\n"
            f"HID_PHYS={'usb/input2' if physical else ''}\n")

    def test_real_deck(self):
        self.add("hidraw0")
        self.add("hidraw1")
        self.add("hidraw2", physical=True)
        self.assertEqual(find_controller(self.sysfs), "/dev/hidraw2")

    def test_legion_virtual(self):
        self.add("hidraw1", "61EB", "17EF")
        self.add("hidraw4", "12FB", uhid=True)
        self.assertEqual(find_controller(self.sysfs), "/dev/hidraw4")

    def test_prefer_real_deck(self):
        self.add("hidraw0", physical=True, uhid=True)
        self.add("hidraw1", "12FB", uhid=True)
        self.add("hidraw9", physical=True)
        self.assertEqual(find_controller(self.sysfs), "/dev/hidraw9")

    def test_none(self):
        self.assertIsNone(find_controller(self.sysfs))
        self.add("hidraw0", "1102")
        self.add("hidraw1", "12FB", "17EF", uhid=True)
        self.add("hidraw2")
        self.add("hidraw3")
        (self.sysfs / "hidraw3/device/uevent").write_text("HID_ID=invalid\n")
        (self.sysfs / "hidraw4").mkdir()
        self.assertIsNone(find_controller(self.sysfs))

    def test_emulated_products(self):
        self.add("hidraw4")
        event = self.sysfs / "hidraw4/device/uevent"
        for product in ("12f0", "12FA", "12FB", "12FC", "12FD", "12FE", "12FF"):
            with self.subTest(product=product):
                event.write_text(f"HID_ID=0003:000028de:0000{product}\n")
                self.assertEqual(find_controller(self.sysfs), "/dev/hidraw4")

    def test_uhid_deck(self):
        self.add("hidraw4", uhid=True)
        self.assertEqual(find_controller(self.sysfs), "/dev/hidraw4")
        (self.sysfs / "hidraw4/device/uevent").write_text("HID_ID=0003:000028DE:000012AB\n")
        self.assertEqual(find_controller(self.sysfs), "/dev/hidraw4")

    def test_device_name(self):
        self.assertEqual(device_name(self.root), "Steam Deck")
        for key, value in {"sys_vendor": "LENOVO", "product_name": "83N1",
                           "product_family": "Legion Go 8AHP2", "board_name": "LNVNB161216"}.items():
            (self.root / key).write_text(value + "\n")
        self.assertEqual(device_name(self.root), "Legion Go 8AHP2")
        for board in ("Jupiter", "Galileo"):
            (self.root / "board_name").write_text(board)
            self.assertEqual(device_name(self.root), "Steam Deck")


if __name__ == "__main__":
    unittest.main()
