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

from microscope import CameraSession


class FrameStatus:
    """Keep metadata only; never copy or retain a camera image/request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.metadata = {}
        self.timestamps = deque(maxlen=120)
        self.last_frame = time.monotonic()

    def reset(self):
        with self.lock:
            self.metadata = {}
            self.timestamps.clear()
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
    def __init__(self, session, status):
        super().__init__()
        self.session = session
        self.camera = session.camera
        self.status = status
        self.notice = ""
        self.preview = None
        self.overlay_visible = True
        self.setWindowTitle("Soldering microscope")
        self.resize(1100, 820)
        self.attach_preview()
        try:
            self.font = ImageFont.truetype("DejaVuSans.ttf", 19)
        except OSError:
            self.font = ImageFont.load_default()
        self.shortcuts = []
        for key, action in (("+", "gain_up"), ("=", "gain_up"), ("-", "gain_down"),
                            ("Up", "exposure_up"), ("Down", "exposure_down"), ("A", "auto")):
            self.bind(key, lambda action=action: self.adjust(action))
        self.bind("Z", lambda: self.adjust_zoom(1))
        self.bind("X", lambda: self.adjust_zoom(-1))
        self.bind("R", lambda: self.adjust_zoom(0))
        self.bind("D", self.toggle_profile)
        self.bind("F", self.toggle_fullscreen)
        self.bind("H", self.toggle_overlay)
        self.bind("Escape", self.close)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.guard(self.refresh_overlay))
        self.timer.start(250)

    @property
    def exposure(self):
        return self.session.exposure

    def attach_preview(self):
        self.status.reset()
        self.preview = QGlPicamera2(self.camera, width=1100, height=820, keep_ar=True,
                                   bg_colour=(0, 0, 0))
        self.setCentralWidget(self.preview)
        width, height = self.session.size
        self.overlay_size = (1000, max(160, round(1000 * height / width)))

    def detach_preview(self):
        widget = self.takeCentralWidget()
        self.preview = None
        if widget is not None:
            # Release textures and held camera buffers before reconfiguration.
            widget.close()
            widget.deleteLater()

    def adjust_zoom(self, direction):
        self.camera.set_controls(self.session.zoom.change(direction))
        self.notice = ""
        self.refresh_overlay()

    def toggle_profile(self):
        self.timer.stop()
        try:
            profile = "detail" if self.session.profile == "fast" else "fast"
            self.notice = self.session.switch_profile(profile, self.detach_preview, self.attach_preview)
            if self.notice:
                print(self.notice, file=sys.stderr)
            self.refresh_overlay()
        finally:
            self.timer.start(250)

    def bind(self, key, callback):
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.setAutoRepeat(key in ("+", "=", "-", "Up", "Down", "Z", "X"))
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
        if not self.overlay_visible or self.preview is None:
            return
        metadata, fps, age = self.status.snapshot()
        gain = metadata.get("AnalogueGain", 0)
        exposure_ms = metadata.get("ExposureTime", 0) / 1000
        mode = "AUTO" if self.exposure.auto else "MANUAL"
        first = (f"{mode}   Gain {gain:.2f}x   Exposure {exposure_ms:.2f} ms   "
                 f"Camera {fps:.1f} fps / target {self.session.target_fps:.1f}")
        if age > 2:
            first = "Waiting for camera frames - check the terminal if this persists"
        elif not metadata:
            first = "Starting camera..."
        zoom = self.session.zoom
        applied = zoom.applied_factor(metadata)
        applied_text = f"{applied:.2f}x" if applied is not None else "pending"
        sensor_width, sensor_height = self.session.mode["size"]
        width, height = self.session.size
        second = (f"{self.session.label}   Sensor {sensor_width}x{sensor_height}   "
                  f"Preview {width}x{height}   Zoom {applied_text} (set {zoom.factor:.2f}x)")
        requested = "Automatic exposure"
        if not self.exposure.auto:
            requested = (f"Requested: {self.exposure.gain:.2f}x gain, "
                         f"{self.exposure.exposure_us / 1000:.2f} ms")
        if self.notice:
            requested = "Mode switch failed; previous mode restored. See terminal."
        # Only the overlay texture is copied; camera images stay in DMA buffers.
        image = Image.new("RGBA", self.overlay_size)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.overlay_size[0], 138), fill=(0, 0, 0, 185))
        for y, text, colour in (
            (7, first, "white"),
            (33, second, "white"),
            (59, requested, "#ffdd88"),
            (85, "+/- gain   Up/Down exposure   A auto/manual   Z/X zoom   R reset", "white"),
            (111, "D fast/detail   F fullscreen   H hide   Esc quit", "white"),
        ):
            draw.text((10, y), text, font=self.font, fill=colour)
        self.preview.set_overlay(np.asarray(image))



def run(args):
    app = QApplication([sys.argv[0]])
    camera = None
    window = None
    old_signals = {}
    try:
        camera = Picamera2(args.camera)
        session = CameraSession(camera, args, controls.draft.NoiseReductionModeEnum.Off)
        session.configure(args.profile)
        print("+/- gain | Up/Down exposure | A auto/manual | Z/X zoom | R reset | "
              "D fast/detail | F fullscreen | H overlay | Esc quit", flush=True)
        status = FrameStatus()
        camera.post_callback = status.update
        window = MicroscopeWindow(session, status)
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
                        window.detach_preview()
                finally:
                    camera.close()
