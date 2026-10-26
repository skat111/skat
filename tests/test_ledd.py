import importlib.util
from pathlib import Path
import sys
import types
import unittest


bleak = types.ModuleType("bleak")
bleak.BleakClient = bleak.BleakScanner = object
sys.modules.setdefault("bleak", bleak)
spec = importlib.util.spec_from_file_location("ledd", Path(__file__).parents[1] / "pi" / "ledd.py")
ledd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledd)


class DmrrbaCommandsTest(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(ledd.rgb_cmd(1, 2, 255), bytes.fromhex("5a07010102ff"))
        self.assertEqual(ledd.brightness_cmd(42), bytes.fromhex("5a03012a00"))
        self.assertEqual(ledd.power_cmd(True), bytes.fromhex("5a010201"))
        self.assertEqual(ledd.power_cmd(False), bytes.fromhex("5a010200"))


if __name__ == "__main__":
    unittest.main()
