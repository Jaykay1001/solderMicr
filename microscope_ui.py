"""Qt/OpenGL display; imported only when running on the Pi desktop."""

import signal
import sys
import threading
import time
from collections import deque

import numpy as np
from libcamera import controls
from picamera2 import Picamera2
from picamera2.previews.qt import QGlPicamera2
from PIL import Image, ImageDraw, ImageFont
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QApplication, QMainWindow, QShortcut

from microscope import Exposure, choose_mode, frame_duration, preview_size


class FrameStatus:
    """Keep metadata only; never copy or retain a camera image/request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.metadata = {}
        self.timestamps = deque(maxlen=120)
        self.last_frame = time.monotonic()

    def update(self, request):
        metadata = request.get_metadata()
        with self.lock:
            self.metadata = metadata
            self.last_frame = time.monotonic()
            timestamp = metadata.get("SensorTimestamp")
            if timestamp is not None:
                self.timestamps.append(timestamp)

    def snapshot(self):
        with self.lock:
            fps = 0.0
            if len(self.timestamps) > 1 and self.timestamps[-1] > self.timestamps[0]:
                fps = (len(self.timestamps) - 1) * 1e9 / (self.timestamps[-1] - self.timestamps[0])
            age = time.monotonic() - self.last_frame
            return self.metadata.copy(), fps if age < 2 else 0.0, age


class MicroscopeWindow(QMainWindow):
    def __init__(self, camera, exposure, status, size, target_fps):
        super().__init__()
        self.camera = camera
        self.exposure = exposure
        self.status = status
        self.target_fps = target_fps
        self.overlay_visible = True
        self.setWindowTitle("Soldering microscope")
        self.resize(1100, 820)
        self.preview = QGlPicamera2(camera, width=1100, height=820, keep_ar=True,
                                   bg_colour=(0, 0, 0))
        self.setCentralWidget(self.preview)
        # The overlay is a separate GL texture; camera pixels stay in DMA buffers.
        self.overlay_size = (1000, max(100, round(1000 * size[1] / size[0])))
        try:
            self.font = ImageFont.truetype("DejaVuSans.ttf", 19)
        except OSError:
            self.font = ImageFont.load_default()
        self.shortcuts = []
        for key, action in (("+", "gain_up"), ("=", "gain_up"), ("-", "gain_down"),
                            ("Up", "exposure_up"), ("Down", "exposure_down"), ("A", "auto")):
            self.bind(key, lambda action=action: self.adjust(action))
        self.bind("F", self.toggle_fullscreen)
        self.bind("H", self.toggle_overlay)
        self.bind("Escape", self.close)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.guard(self.refresh_overlay))
        self.timer.start(250)

    def bind(self, key, callback):
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.setAutoRepeat(key in ("+", "=", "-", "Up", "Down"))
        shortcut.activated.connect(lambda: self.guard(callback))
        self.shortcuts.append(shortcut)

    def guard(self, callback):
        try:
            callback()
        except Exception as error:
            # Exceptions escaping Qt slots can abort Python before cleanup runs.
            print(f"Microscope error: {error}", file=sys.stderr)
            QApplication.exit(1)

    def adjust(self, action):
        metadata, _, _ = self.status.snapshot()
        self.camera.set_controls(self.exposure.change(action, metadata))
        self.refresh_overlay()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def toggle_overlay(self):
        self.overlay_visible = not self.overlay_visible
        if not self.overlay_visible:
            self.preview.set_overlay(None)
        else:
            self.refresh_overlay()

    def refresh_overlay(self):
        if not self.overlay_visible:
            return
        metadata, fps, age = self.status.snapshot()
        gain = metadata.get("AnalogueGain", 0)
        exposure_ms = metadata.get("ExposureTime", 0) / 1000
        mode = "AUTO" if self.exposure.auto else "MANUAL"
        first = (f"{mode}   Gain {gain:.2f}x   Exposure {exposure_ms:.2f} ms   "
                 f"Camera {fps:.1f} fps / target {self.target_fps:.1f}")
        if age > 2:
            first = "Waiting for camera frames - check the terminal if this persists"
        elif not metadata:
            first = "Starting camera..."
        second = "+/- gain   Up/Down exposure   A auto/manual   F fullscreen   H hide   Esc quit"
        # Use three short lines so the controls remain readable at ordinary sizes.
        image = Image.new("RGBA", self.overlay_size)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.overlay_size[0], 86), fill=(0, 0, 0, 185))
        draw.text((10, 7), first, font=self.font, fill="white")
        if not self.exposure.auto:
            requested = (f"Requested: {self.exposure.gain:.2f}x, "
                         f"{self.exposure.exposure_us / 1000:.2f} ms")
            draw.text((10, 32), requested, font=self.font, fill="#ffdd88")
        draw.text((10, 59), second, font=self.font, fill="white")
        self.preview.set_overlay(np.asarray(image))


def run(args):
    app = QApplication([sys.argv[0]])
    camera = None
    window = None
    old_signals = {}
    try:
        camera = Picamera2(args.camera)
        index, mode = choose_mode(camera.sensor_modes, args.fps, args.mode)
        size = preview_size(mode["size"], args.width)
        config = camera.create_preview_configuration(
            main={"size": size, "format": "XRGB8888"},
            sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]},
            raw=None, buffer_count=args.buffers, queue=False,
        )
        camera.configure(config)
        duration = frame_duration(args.fps, mode["fps"], camera.camera_controls["FrameDurationLimits"])
        exposure = Exposure.from_controls(camera.camera_controls, duration)
        initial = {"FrameDurationLimits": (duration, duration), "AeEnable": True}
        if "NoiseReductionMode" in camera.camera_controls:
            initial["NoiseReductionMode"] = controls.draft.NoiseReductionModeEnum.Off
        camera.set_controls(initial)
        target_fps = 1_000_000 / duration
        size = camera.camera_configuration()["main"]["size"]
        print(f"Sensor mode {index}: {mode['size']}, {mode['bit_depth']} bit, "
              f"maximum {mode['fps']:.2f} fps; preview {size}, target {target_fps:.2f} fps, "
              f"{args.buffers} buffers.", flush=True)
        if target_fps < args.fps * 0.99:
            print("Requested frame rate exceeds this mode's limits; using the supported rate.", flush=True)
        print("+/- gain | Up/Down exposure | A auto/manual | F fullscreen | H overlay | Esc quit", flush=True)
        status = FrameStatus()
        camera.post_callback = status.update
        window = MicroscopeWindow(camera, exposure, status, size, target_fps)
        window.showNormal() if args.windowed else window.showFullScreen()
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, lambda *_: app.quit())
        camera.start()
        return app.exec_()
    finally:
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
        if window is not None:
            window.timer.stop()
        if camera is not None:
            try:
                camera.stop()
            finally:
                try:
                    if window is not None:
                        window.preview.close()
                finally:
                    camera.close()
