"""Hardware-independent checks for camera mode and live exposure decisions."""

import argparse
import unittest

from microscope import Exposure, choose_mode, frame_duration, positive_float, preview_size


MODES = [
    {"size": (4056, 3040), "fps": 10.0, "bit_depth": 12},
    {"size": (2028, 1520), "fps": 40.0, "bit_depth": 12},
    {"size": (1332, 990), "fps": 120.0, "bit_depth": 10},
]


class CameraModeTests(unittest.TestCase):
    def test_selects_fast_small_readout_for_60hz_monitor(self):
        self.assertEqual(choose_mode(MODES, 60)[0], 2)

    def test_resolution_priority_can_be_overridden_explicitly(self):
        self.assertEqual(choose_mode(MODES, 30, 1)[0], 1)

    def test_unsupported_rate_falls_back_to_fastest_mode(self):
        self.assertEqual(choose_mode(MODES, 240)[0], 2)

    def test_same_size_prefers_more_bit_depth_if_fast_enough(self):
        modes = MODES + [{"size": (1332, 990), "fps": 60.0, "bit_depth": 12}]
        self.assertEqual(choose_mode(modes, 60)[0], 3)
        self.assertEqual(choose_mode(modes, 90)[0], 2)

    def test_rejects_invalid_indices_and_non_csi_modes(self):
        for modes, index in ((MODES, -1), (MODES, 99), ([], None),
                             ([{"format": "MJPEG"}], None)):
            with self.subTest(modes=modes, index=index), self.assertRaises(ValueError):
                choose_mode(modes, 60, index)

    def test_preview_preserves_proportions_without_upscaling(self):
        self.assertEqual(preview_size((1332, 990), 1920), (1332, 990))
        width, height = preview_size((1332, 990), 1000)
        self.assertEqual((width % 2, height % 2), (0, 0))
        self.assertLess(abs(width / height - 1332 / 990), 0.005)

    def test_frame_duration_obeys_camera_limits(self):
        self.assertEqual(frame_duration(60, 120, (8333, 1000000, None)), 16667)
        self.assertEqual(frame_duration(60, 40, (25000, 1000000, None)), 25000)
        self.assertEqual(frame_duration(60, 120, (17000, 1000000, None)), 17000)
        self.assertEqual(frame_duration(0.1, 120, (8333, 1000000, None)), 1000000)
        self.assertEqual(frame_duration(1e-310, 120, (8333, 1000000, None)), 1000000)

    def test_rejects_nonfinite_or_nonpositive_fps(self):
        for value in ("nan", "inf", "-inf", "0", "-20"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive_float(value)


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.controls = {"AnalogueGain": (1.0, 16.0, 1.0),
                         "ExposureTime": (100, 1000000, 10000)}
        self.exposure = Exposure.from_controls(self.controls, 16667)

    def test_manual_toggle_freezes_applied_auto_settings(self):
        settings = self.exposure.change("auto", {"AnalogueGain": 3.0, "ExposureTime": 8000})
        self.assertEqual(settings, {"AeEnable": False, "AnalogueGain": 3.0, "ExposureTime": 8000})

    def test_gain_shortcut_leaves_auto_and_preserves_exposure(self):
        settings = self.exposure.change("gain_up", {"AnalogueGain": 2.0, "ExposureTime": 7000})
        self.assertFalse(settings["AeEnable"])
        self.assertAlmostEqual(settings["AnalogueGain"], 2 * 2 ** (1 / 3))
        self.assertEqual(settings["ExposureTime"], 7000)

    def test_repeated_presses_accumulate_despite_delayed_metadata(self):
        stale = {"AnalogueGain": 2.0, "ExposureTime": 7000}
        for _ in range(3):
            settings = self.exposure.change("gain_up", stale)
        self.assertAlmostEqual(settings["AnalogueGain"], 4.0)

    def test_limits_prevent_gain_overflow_and_slow_exposure(self):
        for _ in range(100):
            self.exposure.change("gain_up", {})
            self.exposure.change("exposure_up", {})
        self.assertEqual(self.exposure.gain, 16.0)
        self.assertEqual(self.exposure.exposure_us, 16667)
        for _ in range(100):
            self.exposure.change("gain_down", {})
            self.exposure.change("exposure_down", {})
        self.assertEqual(self.exposure.gain, 1.0)
        self.assertEqual(self.exposure.exposure_us, 100)

    def test_auto_releases_both_manual_overrides(self):
        self.exposure.change("gain_up", {})
        self.assertEqual(self.exposure.change("auto", {}),
                         {"AeEnable": True, "ExposureTime": 0, "AnalogueGain": 0.0})
        self.assertTrue(self.exposure.auto)

    def test_entering_manual_clamps_old_frame_to_new_limits(self):
        settings = self.exposure.change("auto", {"AnalogueGain": 32, "ExposureTime": 50000})
        self.assertEqual(settings["ExposureTime"], 16667)
        self.assertEqual(settings["AnalogueGain"], 16.0)

    def test_camera_exposure_limit_can_be_shorter_than_frame(self):
        self.controls["ExposureTime"] = (100, 10000, 5000)
        exposure = Exposure.from_controls(self.controls, 16667)
        self.assertEqual(exposure.exposure_limits, (100, 10000))

    def test_incompatible_exposure_limits_fail_clearly(self):
        with self.assertRaises(ValueError):
            Exposure.from_controls(self.controls, 50)


if __name__ == "__main__":
    unittest.main()
