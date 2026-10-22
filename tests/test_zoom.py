"""Crop geometry and camera reconfiguration checks without Pi hardware."""

import contextlib
import io
import math
import unittest

from microscope import CameraSession, Zoom, choose_detail_mode, parser


MODES = [
    {"size": (4056, 3040), "fps": 10.0, "bit_depth": 12},
    {"size": (2028, 1520), "fps": 40.0, "bit_depth": 12},
    {"size": (1332, 990), "fps": 120.0, "bit_depth": 10},
]


class ZoomTests(unittest.TestCase):
    def make_zoom(self, minimum=(0, 0, 64, 64)):
        return Zoom.from_limits((696, 530, 2664, 1980), minimum, (1332, 990))

    def test_reset_uses_mode_offsets_not_full_sensor_origin(self):
        zoom = self.make_zoom()
        for _ in range(5):
            zoom.change(1)
        self.assertEqual(zoom.change(0)["ScalerCrop"], (696, 530, 2664, 1980))

    def test_crop_stays_centered_and_in_bounds_with_even_coordinates(self):
        zoom = self.make_zoom()
        for _ in range(40):
            x, y, width, height = zoom.change(1)["ScalerCrop"]
            self.assertTrue(all(value % 2 == 0 for value in (x, y, width, height)))
            self.assertGreaterEqual(x, 696)
            self.assertGreaterEqual(y, 530)
            self.assertLessEqual(x + width, 3360)
            self.assertLessEqual(y + height, 2510)
            self.assertLessEqual(abs(x + width / 2 - 2028), 2)
            self.assertLessEqual(abs(y + height / 2 - 1520), 2)
            self.assertLess(abs(width / height - 1332 / 990), 0.01)
        self.assertEqual(zoom.factor, 4.0)

    def test_zoom_out_clamps_at_one(self):
        zoom = self.make_zoom()
        for _ in range(40):
            zoom.change(-1)
        self.assertEqual(zoom.factor, 1)
        self.assertEqual(zoom.crop, zoom.base)

    def test_hardware_minimum_crop_limits_zoom(self):
        zoom = self.make_zoom((0, 0, 1000, 800))
        for _ in range(40):
            zoom.change(1)
        self.assertLess(zoom.factor, 4)
        self.assertGreaterEqual(zoom.crop[2], 1000)
        self.assertGreaterEqual(zoom.crop[3], 800)

    def test_aspect_fit_and_odd_bounds_remain_inside_readout(self):
        zoom = Zoom.from_limits((3, 5, 4055, 3039), (0, 0, 64, 64), (1280, 720))
        x, y, width, height = zoom.base
        self.assertTrue(all(v % 2 == 0 for v in zoom.base))
        self.assertGreaterEqual(x, 3)
        self.assertGreaterEqual(y, 5)
        self.assertLessEqual(x + width, 4058)
        self.assertLessEqual(y + height, 3044)
        self.assertLess(abs(width / height - 16 / 9), 0.01)

    def test_applied_zoom_comes_from_metadata(self):
        zoom = self.make_zoom()
        self.assertIsNone(zoom.applied_factor({}))
        self.assertEqual(zoom.applied_factor({"ScalerCrop": (1362, 1024, 1332, 990)}), 2)

    def test_detail_prefers_largest_sensor_over_faster_small_modes(self):
        self.assertEqual(choose_detail_mode(MODES)[0], 0)
        with self.assertRaises(ValueError):
            choose_detail_mode([])


class FakeCamera:
    """Model the lifecycle constraint that GL buffers must be released first."""

    sensor_modes = MODES

    def __init__(self):
        self.started = False
        self.preview_attached = False
        self.fail_at = None
        self.last_controls = {}

    def create_preview_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        assert not self.started, "Camera must stop before configuring"
        assert not self.preview_attached, "Old preview must release its buffers"
        if self.fail_at == "configure":
            self.fail_at = None
            raise RuntimeError("simulated configuration failure")
        self.config = config
        size = config["sensor"]["output_size"]
        mode = next(m for m in MODES if m["size"] == size)
        bounds = (696, 530, 2664, 1980) if size == (1332, 990) else (0, 0, 4056, 3040)
        self.camera_properties = {"ScalerCropMaximum": bounds}
        self.camera_controls = {
            "AnalogueGain": (1.0, 16.0, 1.0),
            "ExposureTime": (100, 1000000, 5000),
            "FrameDurationLimits": (math.ceil(1000000 / mode["fps"]), 1000000, None),
            "ScalerCrop": ((0, 0, 64, 64), bounds, bounds),
            "NoiseReductionMode": (0, 4, 0),
        }

    def camera_configuration(self):
        return self.config

    def set_controls(self, controls):
        self.last_controls.update(controls)

    def start(self):
        assert self.preview_attached, "Preview must attach before camera starts"
        if self.fail_at == "start":
            self.fail_at = None
            raise RuntimeError("simulated startup failure")
        self.started = True

    def stop(self):
        self.started = False

    def attach(self):
        assert not self.started
        self.preview_attached = True

    def detach(self):
        assert not self.started
        self.preview_attached = False


class ProfileTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.camera = FakeCamera()
        self.session = CameraSession(self.camera, parser().parse_args([]), 0)
        self.session.configure("fast")
        self.camera.attach()
        self.camera.start()

    def switch(self, profile):
        return self.session.switch_profile(profile, self.camera.detach, self.camera.attach)

    def test_detail_reads_full_sensor_but_keeps_output_small_for_scaling(self):
        self.assertEqual(self.switch("detail"), "")
        config = self.camera.config
        self.assertEqual(config["sensor"]["output_size"], (4056, 3040))
        self.assertEqual(config["main"]["size"][0], 1332)
        self.assertIsNone(config["raw"])
        self.assertFalse(config["queue"])
        self.assertEqual(self.session.target_fps, 10)
        self.assertTrue(self.camera.started)

    def test_switch_resets_zoom_and_returns_to_fast_mode(self):
        self.session.zoom.change(1)
        self.switch("detail")
        self.assertEqual(self.session.zoom.factor, 1)
        self.switch("fast")
        self.assertEqual(self.session.mode["size"], (1332, 990))
        self.assertAlmostEqual(self.session.target_fps, 60, places=2)

    def test_manual_exposure_is_preserved_and_clamped_when_returning_to_fast(self):
        self.switch("detail")
        self.session.exposure.change("auto", {"AnalogueGain": 3, "ExposureTime": 80000})
        self.switch("fast")
        self.assertFalse(self.session.exposure.auto)
        self.assertEqual(self.camera.last_controls["AnalogueGain"], 3)
        self.assertEqual(self.camera.last_controls["ExposureTime"], 16667)

    def test_auto_mode_remains_auto_across_switch(self):
        self.switch("detail")
        self.assertTrue(self.session.exposure.auto)
        self.assertTrue(self.camera.last_controls["AeEnable"])
        self.assertEqual(self.camera.last_controls["ExposureTime"], 0)

    def test_failed_configure_or_start_restores_profile_zoom_and_preview(self):
        for failure in ("configure", "start"):
            with self.subTest(failure=failure):
                self.session.zoom.change(1)
                previous_factor = self.session.zoom.factor
                self.camera.fail_at = failure
                message = self.switch("detail")
                self.assertIn("restored FAST", message)
                self.assertEqual(self.session.profile, "fast")
                self.assertEqual(self.session.zoom.factor, previous_factor)
                self.assertEqual(self.camera.last_controls["ScalerCrop"], self.session.zoom.crop)
                self.assertTrue(self.camera.started)
                self.assertTrue(self.camera.preview_attached)


if __name__ == "__main__":
    unittest.main()
