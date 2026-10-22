# Raspberry Pi soldering microscope

A fullscreen camera preview for a **Raspberry Pi 5, official HQ camera
(IMX477), Raspberry Pi OS Desktop, and an HDMI monitor**. Keyboard shortcuts
adjust analogue gain and exposure while the camera runs.

The application uses Picamera2's OpenGL preview to display camera buffers,
without encoding, network streaming, or copying each frame into Python.
It targets 60 camera frames per second by default. Actual motion-to-screen
latency must be measured on your Pi and monitor; 60 fps is not a latency guarantee.

## Install on the Pi

Use the current 64-bit Raspberry Pi OS Desktop with the normal libcamera camera
stack. In a terminal on the Pi:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-pyqt5 python3-opengl python3-numpy python3-pil fonts-dejavu-core rpicam-apps xwayland
```

Copy or clone this repository onto the Pi, then open a terminal in its directory.
Use the distribution's Python (`/usr/bin/python3`), since camera bindings and
GUI dependencies are installed through apt. A virtual environment is unnecessary.
Python dependencies are also listed in [requirements.txt](requirements.txt). To
use that file in a virtual environment, install the apt packages above first, then:

```bash
sudo apt install python3-venv
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python microscope.py
```

The system packages provide the Raspberry Pi camera bindings, including libcamera;
`requirements.txt` alone cannot install the complete camera stack. Versions are
intentionally unpinned so the environment can reuse the matched Raspberry Pi OS
packages. The remaining examples use the system Python; substitute
`.venv/bin/python` when using the virtual environment.

Check detection before launching:

```bash
rpicam-hello --list-cameras
```

The HQ camera should appear as `imx477`. Stop other applications using it.
The connected monitor should be configured for 60 Hz in the desktop settings.

## Run

```bash
/usr/bin/python3 microscope.py
```

Run as your normal desktop user, without `sudo`. The preview starts fullscreen
with automatic exposure and white balance. Black borders preserve the picture's
proportions. Focus and aperture are adjusted on your lens.

| Key | Action |
| --- | --- |
| `+` or `=` | Increase analogue gain by one third of a stop |
| `-` | Decrease analogue gain by one third of a stop |
| Up / Down | Increase / decrease exposure time by one third of a stop |
| `A` | Toggle automatic / manual exposure and gain |
| `Z` / `X` | Zoom in / out around the image centre |
| `R` | Reset zoom to 1x |
| `D` | Switch between fast and detail capture profiles |
| `F` | Toggle fullscreen |
| `H` | Hide / show the status overlay |
| Escape | Quit and release the camera |

Click the window if another application has keyboard focus. Holding the gain,
exposure, or zoom keys repeats adjustments. Ctrl+C in the launching terminal also exits.

Adjusting gain or exposure switches both controls to manual. It starts from the
latest applied settings, then applies the requested adjustment. Press `A` to
restore automatic exposure. White balance remains automatic in both modes.

The overlay shows **applied analogue gain, applied exposure, camera fps, capture
profile, sensor/preview resolutions, and applied/requested zoom**.
In manual mode it also shows the requested values. The sensor may take several
frames to apply changes, and values can be rounded by the driver. Gain is shown
as a multiplier, not a calibrated ISO number: higher gain brightens the image
but increases noise. Camera fps comes from sensor timestamps, not monitor
presentation timing or a latency measurement.

## Zoom and inspection mode

`Z` zooms in, `X` zooms out, and `R` restores the full view of the current mode.
Zoom is centred, with four steps per doubling, from 1x to 4x (or the camera's
crop limit, if lower). Position the board so the area of interest is in the centre.
The camera's `ScalerCrop` control crops the sensor image **before** scaling it
into the preview; Python never crops or enlarges camera frames itself.

`D` toggles between two profiles:

- **FAST:** the original low-latency configuration, targeting 60 fps by default.
  Digital zoom enlarges what this sensor mode captures; it cannot recover detail
  absent from that readout.
- **DETAIL:** the largest available sensor readout, accepting its lower frame
  rate. On the HQ camera this uses its full 4056 x 3040 sensor mode. The crop has
  more source pixels available before scaling, which helps inspection at higher
  zoom. The terminal and overlay report the actual frame-rate target.

The output preview width remains capped by `--width` in both profiles. Detail
mode therefore does not send a full-resolution image through the display path:
its extra sensor pixels are available for cropping upstream. Zoom beyond the
available source detail still requires enlargement; it is not optical zoom.

Switching profiles briefly pauses capture and resets zoom to 1x. Field of view
can change because different sensor modes read different areas. Manual gain and
exposure are preserved, clamped to the new mode's limits; automatic exposure
stays automatic. If switching fails, the app attempts to restore the previous
profile and zoom and prints the error in the terminal.

Start directly in inspection mode with:

```bash
/usr/bin/python3 microscope.py --profile detail
```

`--mode N` customises the fast profile, labelled **CUSTOM** in the overlay; `D`
then switches between that custom mode and the largest-resolution detail mode.

## Frame rate and image size

At startup the application queries available sensor modes. The default **fast**
profile chooses the smallest readout that meets the requested fps. If none does, it chooses the
fastest available mode and reports the reduced target in the terminal.
It explicitly configures the selected sensor mode; merely reducing the preview
size would not necessarily increase the sensor's frame rate.

The preview width is capped at 1332 pixels by default, with an even height
calculated from the sensor mode's proportions. The GPU scales it to the window.
Fast sensor modes may crop the field of view compared with full-resolution
modes. The startup log reports the selected sensor and preview sizes.

```bash
# Inspect the camera's actual modes (does not require a desktop session).
/usr/bin/python3 microscope.py --list-modes

# Start in a window, or reduce preview processing size.
/usr/bin/python3 microscope.py --windowed
/usr/bin/python3 microscope.py --width 1000

# Target a different frame rate.
/usr/bin/python3 microscope.py --fps 50

# Pick a mode explicitly; replace N with an index from --list-modes.
/usr/bin/python3 microscope.py --mode N --fps 30 --width 1920

/usr/bin/python3 microscope.py --help
```

Both automatic and manual exposure use a fixed frame-duration budget. Manual
exposure is capped at that duration and the camera's advertised limits. Use more
light or gain if the picture is too dark at the exposure limit. A shorter exposure
can make moving tools clearer. Stable lighting also helps avoid flicker bands.

Three camera buffers are used by default, with Picamera2's retained capture
queue disabled and noise reduction disabled. These settings are a starting point
for responsiveness, not a guarantee of a particular latency. If frames stall or
arrive unevenly, try `--buffers 4`. Increasing RAM does not reduce sensor or
display latency. You can hide the overlay with `H` to compare performance.

## Troubleshooting and hardware acceptance check

- **No camera:** check `rpicam-hello --list-cameras` and the physical connection.
  `--camera 1` selects a second camera if one is present. The Pi 5 needs the
  Standard–Mini ribbon cable for the HQ camera.
- **Missing Python modules:** install the apt packages above and use
  `/usr/bin/python3`, outside any isolated virtual environment.
- **Display / EGL / Qt error:** launch from a terminal in the Pi's local desktop
  session. The default backend is Qt `xcb`, using XWayland on a Wayland desktop.
  This works with older Picamera2 QtGL implementations as well as newer ones.
  Do not use SSH X forwarding to evaluate latency. If the camera works but EGL
  initialization fails, check the default KMS graphics configuration and graphics
  packages. New Picamera2 versions also support native Wayland; with those you
  can try `QT_QPA_PLATFORM=wayland /usr/bin/python3 microscope.py`.
- **Dark or noisy image:** add light, open the lens aperture, or increase gain.
  The lens affects optics and lighting, not the application's camera interface.
- **Frozen preview:** the overlay reports missing frames after two seconds.
  Check terminal errors and try four buffers. Quit before restarting the app.

On the Pi, verify:

1. The camera opens, the image has correct proportions, and measured camera fps
   settles near the displayed target.
2. Gain and exposure keys affect the picture, requested/applied settings agree
   after a few frames, and `A` restores automatic exposure.
3. Holding Up stops increasing exposure at the frame budget without lowering fps.
4. Fullscreen, overlay hiding, Escape, and Ctrl+C work. Restarting immediately
   after quitting successfully reacquires the camera.
5. Hold `Z` and `X` to check zoom stops at its limits, keeps the same centre,
   and does not distort the picture. `R` should restore the original view.
6. Press `D` in automatic and manual exposure modes. Confirm the sensor size and
   fps target change, zoom resets, and switching back restores the fast preview.
   Check `H`, fullscreen, and quitting after several mode switches.
7. Moving a tool under the lens feels responsive in the fast profile. To estimate end-to-end latency,
   film a blinking LED and its monitor image together with a high-frame-rate
   external camera, and compare the transition times over several cycles. If the
   monitor offers a low-latency or game mode, compare that setting too.

This repository's automated checks do not exercise the Pi sensor, EGL renderer,
or display timing. Those require the hardware check above.

## Local checks

The mode-selection, exposure, zoom-geometry, and simulated mode-switch lifecycle
tests use only Python's standard library:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile microscope.py microscope_ui.py
```

## References

- [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
- [Raspberry Pi camera software](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [Official Picamera2 QtGL implementation](https://github.com/raspberrypi/picamera2/blob/main/picamera2/previews/q_gl_picamera2.py)
