"""GPU rotation hooks for Picamera2's X11 and native Wayland previews."""


class RotationPreviewMixin:
    """Rotate only the camera texture, leaving the status overlay upright."""

    def __init__(self, *args, image_size, rotation=0, **kwargs):
        self.image_size = image_size
        self.rotation = rotation
        super().__init__(*args, **kwargs)

    def init_gl(self):
        # X11/XWayland initializes GL during widget construction.
        super().init_gl()
        self._install_rotation_shader()

    def _build_programs(self):
        # Native Wayland initializes GL later, when Qt shows the widget.
        super()._build_programs()
        self._install_rotation_shader()

    def _install_rotation_shader(self):
        import numpy as np
        from OpenGL import GLES2 as gl
        from OpenGL.GL import shaders

        vertex = shaders.compileShader("""
            attribute vec2 aPosition;
            uniform int quarter_turns;
            varying vec2 texcoord;
            void main() {
                gl_Position = vec4(2.0 * aPosition - 1.0, 0.0, 1.0);
                vec2 uv = vec2(aPosition.x, 1.0 - aPosition.y);
                if (quarter_turns == 1) uv = vec2(uv.y, 1.0 - uv.x);
                else if (quarter_turns == 2) uv = vec2(1.0) - uv;
                else if (quarter_turns == 3) uv = vec2(1.0 - uv.y, uv.x);
                texcoord = uv;
            }
        """, gl.GL_VERTEX_SHADER)
        fragment = shaders.compileShader("""
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            uniform samplerExternalOES camera_texture;
            varying vec2 texcoord;
            void main() {
                gl_FragColor = texture2D(camera_texture, texcoord);
            }
        """, gl.GL_FRAGMENT_SHADER)
        program = shaders.compileProgram(vertex, fragment, validate=False)
        gl.glDeleteProgram(self.program_image)
        self.program_image = program
        # GLES client arrays must remain alive for the lifetime of the preview.
        self._rotation_positions = np.array(
            [(0, 0), (1, 0), (1, 1), (0, 1)], dtype=np.float32)
        location = gl.glGetAttribLocation(program, "aPosition")
        gl.glVertexAttribPointer(location, 2, gl.GL_FLOAT, False, 0, self._rotation_positions)
        gl.glEnableVertexAttribArray(location)
        self._rotation_uniform = gl.glGetUniformLocation(program, "quarter_turns")
        gl.glUseProgram(program)
        gl.glUniform1i(gl.glGetUniformLocation(program, "camera_texture"), 0)

    def _set_rotation_uniform(self):
        from OpenGL import GLES2 as gl

        gl.glUseProgram(self.program_image)
        gl.glUniform1i(self._rotation_uniform, self.rotation // 90)

    def repaint(self, completed_request, update_viewport=False):
        # Called with the EGL context current by the X11 preview.
        self._set_rotation_uniform()
        super().repaint(completed_request, update_viewport=True)

    def _repaint(self, completed_request):
        # Called with Qt's GL context current by the native Wayland preview.
        self._set_rotation_uniform()
        super()._repaint(completed_request)

    def recalculate_viewport(self):
        # X11 uses native window pixels; Qt's Wayland FBO uses device pixels.
        scale = self.devicePixelRatioF() if hasattr(self, "makeCurrent") else 1
        window_w, window_h = int(self.width() * scale), int(self.height() * scale)
        image_w, image_h = self.image_size
        if self.rotation % 180:
            image_w, image_h = image_h, image_w
        if image_w * window_h > window_w * image_h:
            width, height = window_w, window_w * image_h // image_w
        else:
            width, height = window_h * image_w // image_h, window_h
        return (window_w - width) // 2, (window_h - height) // 2, width, height

    def rotate(self):
        self.rotation = (self.rotation + 90) % 360
        if hasattr(self, "makeCurrent"):
            self.update()
        else:
            # Also redraw immediately when the overlay is hidden.
            self.resizeEvent(None)


def create_preview(camera, image_size, rotation):
    import os

    # Recent Picamera2 releases expose QGlPicamera2 through a factory. Select
    # the concrete class so the rendering hooks can be overridden on both paths.
    if os.environ.get("QT_QPA_PLATFORM") == "wayland":
        from picamera2.previews.qt import QGlPicamera2Wl as Preview
    else:
        from picamera2.previews.q_gl_picamera2 import QGlPicamera2 as Preview

    class RotatingPreview(RotationPreviewMixin, Preview):
        pass

    return RotatingPreview(camera, image_size=image_size, rotation=rotation,
                           width=1100, height=820, keep_ar=True, bg_colour=(0, 0, 0))
