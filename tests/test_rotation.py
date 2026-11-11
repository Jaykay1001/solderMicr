"""Rotation and viewport checks without Qt, OpenGL, or camera hardware."""

import unittest

from microscope_preview import RotationPreviewMixin


class FakePreview:
    def __init__(self):
        self.redraws = []

    def width(self):
        return 1100

    def height(self):
        return 820

    def resizeEvent(self, event):
        self.repaint("current frame")

    def repaint(self, request, update_viewport=False):
        self.redraws.append((request, update_viewport))

    def _repaint(self, request):
        self.redraws.append(request)


class Preview(RotationPreviewMixin, FakePreview):
    def _set_rotation_uniform(self):
        self.applied_rotation = self.rotation


class WaylandPreview(Preview):
    def makeCurrent(self):
        raise AssertionError("Qt must manage the GL context during painting")

    def devicePixelRatioF(self):
        return 2

    def update(self):
        self._repaint("current frame")


class RotationTests(unittest.TestCase):
    def test_four_presses_rotate_and_redraw_even_without_overlay_updates(self):
        preview = Preview(image_size=(1332, 990))
        for angle in (90, 180, 270, 0):
            preview.rotate()
            self.assertEqual(preview.rotation, angle)
            self.assertEqual(preview.applied_rotation, angle)
            self.assertEqual(preview.redraws[-1], ("current frame", True))
        self.assertEqual(len(preview.redraws), 4)

    def test_rotated_view_is_centered_fits_window_and_keeps_proportions(self):
        preview = Preview(image_size=(1332, 990))
        for angle in (0, 90, 180, 270):
            preview.rotation = angle
            x, y, width, height = preview.recalculate_viewport()
            expected_ratio = 990 / 1332 if angle % 180 else 1332 / 990
            self.assertLess(abs(width / height - expected_ratio), 0.002)
            self.assertLessEqual(width, preview.width())
            self.assertLessEqual(height, preview.height())
            self.assertLessEqual(abs(2 * x + width - preview.width()), 1)
            self.assertLessEqual(abs(2 * y + height - preview.height()), 1)

    def test_portrait_input_rotates_to_landscape(self):
        preview = Preview(image_size=(990, 1332), rotation=90)
        self.assertEqual(preview.recalculate_viewport(), (0, 1, 1100, 817))

    def test_rebuilt_preview_starts_at_preserved_angle(self):
        previous = Preview(image_size=(1332, 990))
        previous.rotate()
        rebuilt = Preview(image_size=(1332, 998), rotation=previous.rotation)
        self.assertEqual(rebuilt.rotation, 90)
        rebuilt.rotate()
        self.assertEqual(rebuilt.applied_rotation, 180)

    def test_wayland_redraw_and_viewport_use_qt_device_pixels(self):
        preview = WaylandPreview(image_size=(1332, 990))
        preview.rotate()
        self.assertEqual(preview.applied_rotation, 90)
        self.assertEqual(preview.redraws, ["current frame"])
        self.assertEqual(preview.recalculate_viewport(), (491, 0, 1218, 1640))


if __name__ == "__main__":
    unittest.main()
