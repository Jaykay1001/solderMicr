#!/usr/bin/env python3
"""Low-latency HDMI microscope for Raspberry Pi 5 and the HQ camera."""

import argparse
import math
import os
import sys
from dataclasses import dataclass


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--fps", type=positive_float, default=60, help="target camera fps (default: 60)")
    result.add_argument("--width", type=int, default=1332, help="maximum preview width (default: 1332)")
    result.add_argument("--camera", type=int, default=0, help="camera index (default: 0)")
    result.add_argument("--buffers", type=int, choices=range(3, 7), default=3,
                        help="camera buffers; try 4 if frames stall (default: 3)")
    result.add_argument("--list-modes", action="store_true", help="print sensor modes and exit")
    result.add_argument("--mode", type=int, help="override the fast profile with a mode from --list-modes")
    result.add_argument("--profile", choices=("fast", "detail"), default="fast",
                        help="initial capture profile (default: fast)")
    result.add_argument("--windowed", action="store_true", help="start in a window")
    return result


def choose_mode(modes, fps, index=None):
    """Prefer the smallest sensor readout meeting the requested frame rate."""
    usable = [(i, m) for i, m in enumerate(modes)
              if all(key in m for key in ("size", "fps", "bit_depth")) and m["fps"] > 0]
    if index is not None:
        for i, mode in usable:
            if i == index:
                return i, mode
        raise ValueError("Invalid sensor mode; check --list-modes.")
    if not usable:
        raise ValueError("No usable CSI sensor modes found; this application requires a Pi camera.")
    candidates = [(i, m) for i, m in usable if m["fps"] >= fps]
    if not candidates:
        fastest = max(m["fps"] for _, m in usable)
        candidates = [(i, m) for i, m in usable if m["fps"] == fastest]
    return min(candidates, key=lambda item: (
        item[1]["size"][0] * item[1]["size"][1], -item[1]["bit_depth"]))



def choose_detail_mode(modes):
    """Use the largest sensor readout for inspection, accepting its lower fps."""
    usable = [(i, m) for i, m in enumerate(modes)
              if all(key in m for key in ("size", "fps", "bit_depth")) and m["fps"] > 0]
    if not usable:
        raise ValueError("No usable CSI sensor modes found.")
    return max(usable, key=lambda item: (
        item[1]["size"][0] * item[1]["size"][1], item[1]["bit_depth"], item[1]["fps"]))


@dataclass
class Zoom:
    """Centered crops in full-sensor coordinates, including a mode's offsets."""

    base: tuple
    minimum_size: tuple
    factor: float = 1.0

    @classmethod
    def from_limits(cls, bounds, minimum, output_size):
        x, y, width, height = bounds
        # Keep all crop edges even and inside the configured mode's readout.
        left, top = (x + 1) // 2 * 2, (y + 1) // 2 * 2
        right, bottom = (x + width) // 2 * 2, (y + height) // 2 * 2
        width, height = right - left, bottom - top
        ratio = output_size[0] / output_size[1]
        if width / height > ratio:
            width = int(height * ratio) // 2 * 2
        else:
            height = int(width / ratio) // 2 * 2
        left += (right - left - width) // 4 * 2
        top += (bottom - top - height) // 4 * 2
        min_width = max(2, (minimum[2] + 1) // 2 * 2)
        min_height = max(2, (minimum[3] + 1) // 2 * 2)
        if width < min_width or height < min_height:
            raise ValueError("Camera crop limits cannot accommodate the preview aspect ratio.")
        return cls((left, top, width, height), (min_width, min_height))

    @property
    def maximum_factor(self):
        return min(4.0, self.base[2] / self.minimum_size[0],
                   self.base[3] / self.minimum_size[1])

    @property
    def crop(self):
        x, y, width, height = self.base
        crop_width = max(self.minimum_size[0], int(width / self.factor) // 2 * 2)
        crop_height = max(self.minimum_size[1], int(height / self.factor) // 2 * 2)
        return (x + (width - crop_width) // 4 * 2,
                y + (height - crop_height) // 4 * 2, crop_width, crop_height)

    def change(self, direction):
        if direction == 0:
            self.factor = 1.0
        else:
            self.factor = clamp(self.factor * 2 ** (direction / 4), (1.0, self.maximum_factor))
        return {"ScalerCrop": self.crop}

    def applied_factor(self, metadata):
        crop = metadata.get("ScalerCrop")
        return self.base[2] / crop[2] if crop and crop[2] > 0 else None


def preview_size(sensor_size, maximum_width):
    width, height = sensor_size
    scale = min(1, maximum_width / width)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def frame_duration(fps, mode_fps, limits):
    """Use configured camera limits, which are more precise than mode fps."""
    minimum, maximum, _ = limits
    requested = 1_000_000 / min(fps, mode_fps)
    return math.ceil(max(minimum, min(maximum, requested)))


def clamp(value, limits):
    return max(limits[0], min(limits[1], value))


@dataclass
class Exposure:
    gain_limits: tuple
    exposure_limits: tuple
    auto: bool = True
    gain: float = 1.0
    exposure_us: int = 5000

    @classmethod
    def from_controls(cls, controls, duration):
        gain = controls["AnalogueGain"][:2]
        minimum, maximum = controls["ExposureTime"][:2]
        maximum = min(maximum, duration)
        if minimum > maximum:
            raise ValueError("Exposure limits are incompatible with the requested frame rate.")
        return cls(gain, (minimum, maximum), gain=clamp(1.0, gain),
                   exposure_us=int(clamp(5000, (minimum, maximum))))

    def change(self, action, metadata):
        if action == "auto" and not self.auto:
            self.auto = True
            # Clear manual overrides as well as re-enabling the AE algorithm.
            return {"AeEnable": True, "ExposureTime": 0, "AnalogueGain": 0.0}
        if self.auto:
            self.gain = clamp(metadata.get("AnalogueGain", self.gain), self.gain_limits)
            self.exposure_us = int(clamp(metadata.get("ExposureTime", self.exposure_us),
                                         self.exposure_limits))
        self.auto = False
        step = 2 ** (1 / 3)  # One third of a stop per key press.
        if action in ("gain_up", "gain_down"):
            self.gain = clamp(self.gain * (step if action == "gain_up" else 1 / step),
                              self.gain_limits)
        elif action in ("exposure_up", "exposure_down"):
            self.exposure_us = int(clamp(round(self.exposure_us * (
                step if action == "exposure_up" else 1 / step)), self.exposure_limits))
        elif action != "auto":
            raise ValueError(f"Unknown exposure action: {action}")
        return {"AeEnable": False, "AnalogueGain": self.gain, "ExposureTime": self.exposure_us}



class CameraSession:
    """Manage camera profiles, rebuilding the GUI preview through callbacks."""

    def __init__(self, camera, args, noise_reduction_off):
        self.camera = camera
        self.args = args
        self.noise_reduction_off = noise_reduction_off
        # Query once before streaming: enumerating sensor modes can reconfigure it.
        self.modes = camera.sensor_modes

    @property
    def label(self):
        if self.profile == "detail":
            return "DETAIL"
        return "CUSTOM" if self.args.mode is not None else "FAST"

    def configure(self, profile, previous_exposure=None):
        args, camera = self.args, self.camera
        index, mode = (choose_detail_mode(self.modes) if profile == "detail" else
                       choose_mode(self.modes, args.fps, args.mode))
        config = camera.create_preview_configuration(
            main={"size": preview_size(mode["size"], args.width), "format": "XRGB8888"},
            sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]},
            raw=None, buffer_count=args.buffers, queue=False,
        )
        camera.configure(config)
        duration = frame_duration(args.fps, mode["fps"], camera.camera_controls["FrameDurationLimits"])
        exposure = Exposure.from_controls(camera.camera_controls, duration)
        initial = {"FrameDurationLimits": (duration, duration), "AeEnable": True,
                   "ExposureTime": 0, "AnalogueGain": 0.0}
        if previous_exposure is not None and not previous_exposure.auto:
            initial.update(exposure.change("auto", {
                "AnalogueGain": previous_exposure.gain,
                "ExposureTime": previous_exposure.exposure_us,
            }))
        if "NoiseReductionMode" in camera.camera_controls:
            initial["NoiseReductionMode"] = self.noise_reduction_off
        size = camera.camera_configuration()["main"]["size"]
        zoom = Zoom.from_limits(camera.camera_properties["ScalerCropMaximum"],
                                camera.camera_controls["ScalerCrop"][0], size)
        initial["ScalerCrop"] = zoom.crop
        camera.set_controls(initial)
        self.profile, self.index, self.mode = profile, index, mode
        self.exposure, self.zoom, self.size = exposure, zoom, size
        self.target_fps = 1_000_000 / duration
        print(f"{self.label}: sensor mode {index}, {mode['size']}, {mode['bit_depth']} bit, "
              f"maximum {mode['fps']:.2f} fps; preview {size}, target {self.target_fps:.2f} fps, "
              f"{args.buffers} buffers; zoom reset to 1x.", flush=True)
        if self.target_fps < args.fps * 0.99:
            print("This sensor mode has a lower frame-rate limit; using its supported rate.", flush=True)


    def switch_profile(self, profile, detach_preview, attach_preview):
        previous_profile, previous_exposure = self.profile, self.exposure
        previous_zoom = self.zoom.factor
        self.camera.stop()
        detach_preview()
        try:
            self.configure(profile, previous_exposure)
            attach_preview()
            self.camera.start()
        except Exception as error:
            # Rebuild the previous configuration if switching fails.
            self.camera.stop()
            detach_preview()
            self.configure(previous_profile, previous_exposure)
            self.zoom.factor = min(previous_zoom, self.zoom.maximum_factor)
            self.camera.set_controls({"ScalerCrop": self.zoom.crop})
            attach_preview()
            self.camera.start()
            return f"Mode switch failed; restored {self.label}: {error}"
        return ""


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.width < 64 or args.width > 4096:
        arguments.error("--width must be between 64 and 4096")
    if args.camera < 0:
        arguments.error("--camera must be non-negative")
    platform = os.environ.get("QT_QPA_PLATFORM", "xcb")
    display_variable = "WAYLAND_DISPLAY" if platform == "wayland" else "DISPLAY"
    if not args.list_modes and not os.environ.get(display_variable):
        print("Start this from a terminal on the Pi's desktop with an HDMI monitor. "
              f"The selected Qt backend requires {display_variable}.", file=sys.stderr)
        return 1
    # XWayland also works on the standard Wayland desktop and supports older
    # distribution Picamera2 packages. Users with newer packages can override it.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    try:
        from picamera2 import Picamera2

        if args.list_modes:
            with Picamera2(args.camera) as camera:
                for i, mode in enumerate(camera.sensor_modes):
                    print(f"{i}: {mode.get('size')}  {mode.get('fps')} fps  "
                          f"{mode.get('bit_depth')} bit  crop={mode.get('crop_limits')}")
            return 0
        from microscope_ui import run

        return run(args)
    except ImportError as error:
        print(f"Missing camera/GUI dependency: {error}\n"
              "Install the apt packages in README.md and run /usr/bin/python3 microscope.py.",
              file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Cannot run microscope: {error}\n"
              "Check rpicam-hello --list-cameras, close other camera apps, "
              "and consult README.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
