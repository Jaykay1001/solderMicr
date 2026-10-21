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
    result.add_argument("--mode", type=int, help="select a sensor mode from --list-modes")
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
