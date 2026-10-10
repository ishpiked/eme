import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emyfx


class AudioEffectsTests(unittest.TestCase):
    def test_every_preset_builds_a_limited_stereo_render(self):
        for name in emyfx.PRESETS:
            with self.subTest(effect=name):
                args = emyfx.build_ffmpeg_args("source.mp3", "output.mp3", name)
                graph = args[args.index("-filter_complex") + 1]
                self.assertIn("[out]", graph)
                self.assertIn("alimiter=", graph)
                self.assertEqual(args[args.index("-ac") + 1], "2")

    def test_360_preset_has_a_slow_orbit_and_centered_low_end(self):
        graph = emyfx.PRESETS["360"]["build"](120.0)
        self.assertIn("apulsator=hz=0.065", graph)
        self.assertIn("amount=0.66", graph)
        self.assertIn("lowpass=f=160", graph)
        self.assertIn("aecho=", graph)

    def test_3d_preset_adds_soft_depth_while_preserving_direct_center(self):
        graph = emyfx.PRESETS["3d"]["build"](120.0)
        self.assertIn("asplit=3[directsrc][basssrc][space]", graph)
        self.assertIn("lowpass=f=190", graph)
        self.assertIn("aecho=0.92:0.94:22|46|78:0.075|0.045|0.022", graph)
        self.assertIn("volume=0.30[depth]", graph)
        self.assertIn("volume=0.78[direct]", graph)


if __name__ == "__main__":
    unittest.main()
