import ast
from pathlib import Path
import unittest


source = Path(__file__).parents[1] / "speaker" / "skat-client.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "speech_light")
namespace = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
speech_light = namespace["speech_light"]


class SpeechLightTest(unittest.TestCase):
    def test_fast_attack_and_smooth_release(self):
        high, bright = speech_light(0, 7000)
        self.assertEqual(high, 70)
        self.assertGreater(bright, 0.7)
        falling, dimmer = speech_light(high, 0)
        self.assertGreater(falling, 0)
        self.assertLess(dimmer, bright)

    def test_output_is_bounded(self):
        level, brightness = speech_light(100, 100000)
        self.assertEqual(level, 100)
        self.assertEqual(brightness, 1)


if __name__ == "__main__":
    unittest.main()
