import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_dreamy_has_filtered_spacious_reverb_over_a_clear_dry_signal(self):
        graph = emyfx.PRESETS["dreamy"]["build"](120.0)
        self.assertIn("highpass=f=180,lowpass=f=12000", graph)
        self.assertIn("aecho=0.90:0.94:45|95|180|350|700|1200", graph)
        self.assertIn("stereowiden=", graph)
        self.assertIn("[dryw][wet]amix=inputs=2", graph)


class EffectRenderLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_duration_probe_is_skipped_when_preset_does_not_use_duration(self):
        process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"", b"")),
            returncode=0,
        )
        with patch.object(
            emyfx.asyncio, "create_subprocess_exec",
            AsyncMock(return_value=process),
        ), patch.object(
            emyfx, "probe_duration", new_callable=AsyncMock
        ) as probe_duration, patch.object(
            emyfx, "build_ffmpeg_args", return_value=["ffmpeg"]
        ):
            with patch.object(
                emyfx.Path, "exists", return_value=True
            ), patch.object(
                emyfx.Path, "stat",
                return_value=SimpleNamespace(st_size=1024),
            ):
                await emyfx.render_to_path("source.mp3", "output.mp3", "8d")

        probe_duration.assert_not_awaited()

    async def test_duration_dependent_preset_still_probes_source(self):
        process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"", b"")),
            returncode=0,
        )
        with patch.object(
            emyfx.asyncio, "create_subprocess_exec",
            AsyncMock(return_value=process),
        ), patch.object(
            emyfx, "probe_duration", new_callable=AsyncMock,
            return_value=120.0,
        ) as probe_duration, patch.object(
            emyfx, "build_ffmpeg_args", return_value=["ffmpeg"]
        ), patch.object(
            emyfx.Path, "exists", return_value=True
        ), patch.object(
            emyfx.Path, "stat", return_value=SimpleNamespace(st_size=1024)
        ):
            await emyfx.render_to_path("source.mp3", "output.mp3", "vinyl")

        probe_duration.assert_awaited_once_with("source.mp3")


if __name__ == "__main__":
    unittest.main()
