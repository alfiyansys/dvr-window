// Phase 15.1: classical real-time enhancement for the focused/modal
// stream only — see PLAN.md "Phase 15.1 design". Deliberately never
// applied to the grid cells: that would compete with the exact
// resource contention Phase 14's background throttling exists to
// relieve.
//
// Loaded at the bottom of body, after the overlay HTML (#overlayEnhance/
// #enhanceCanvas) — unlike auth.js, which is safe in <head> because it
// only ever builds its own elements dynamically, this file looks up
// pre-existing ones at load time and needs them to already be in the DOM.
(function () {
  // Combined single-pass shader: auto-levels/gamma (targets this DVR's
  // dark analog/IR-night footage) + a small unsharp mask (targets the
  // softness the H.265->H.264 transcode on channel 10 introduces,
  // ARCHITECTURE.md "H.265→H.264 transcode for main streams"). One pass,
  // not two separate render targets — comfortably 60fps at 1080p for a
  // single stream, so performance isn't a concern worth the extra
  // framebuffer ping-pong two passes would need.
  const ENHANCE_VERTEX_SRC = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

  const ENHANCE_FRAGMENT_SRC = `#version 300 es
precision mediump float;
uniform sampler2D uFrame;
uniform vec2 uTexel;
in vec2 vUv;
out vec4 outColor;

const float BLACK_POINT = 0.03;
const float WHITE_POINT = 0.92;
const float GAMMA = 0.8;
const float SHARPEN_AMOUNT = 0.6;

vec3 autoLevels(vec3 c) {
  vec3 stretched = clamp((c - BLACK_POINT) / (WHITE_POINT - BLACK_POINT), 0.0, 1.0);
  return pow(stretched, vec3(GAMMA));
}

void main() {
  vec3 center = texture(uFrame, vUv).rgb;
  vec3 blur = (
    texture(uFrame, vUv + uTexel * vec2(1.0, 1.0)).rgb +
    texture(uFrame, vUv + uTexel * vec2(-1.0, 1.0)).rgb +
    texture(uFrame, vUv + uTexel * vec2(1.0, -1.0)).rgb +
    texture(uFrame, vUv + uTexel * vec2(-1.0, -1.0)).rgb
  ) * 0.25;
  vec3 sharpened = center + (center - blur) * SHARPEN_AMOUNT;
  outColor = vec4(autoLevels(sharpened), 1.0);
}`;

  // One long-lived instance shares a single WebGL2 context/program/texture
  // across however many times the modal opens/closes or Prev/Next re-targets
  // it, rather than tearing down and recreating a context per cell —
  // canvas.getContext() returns the *same* context on a canvas that already
  // has one, so re-running full init on every openOverlay() call would leak
  // a fresh set of shader/program/texture objects each time without ever
  // freeing the previous set.
  class EnhancementPipeline {
    constructor(canvas) {
      this.canvas = canvas;
      this.gl = null;
      this.program = null;
      this.texture = null;
      this.uFrameLoc = null;
      this.uTexelLoc = null;
      this.video = null;
      this.rvfcHandle = null;
      this.running = false;
      // Set by the caller — invoked if a per-frame render throws after
      // enhancement was already showing, so the caller can swap back to
      // plain <video> (fallback policy: never a broken/blank picture).
      this.onFailure = null;
      // Set by the caller — invoked once this pipeline has actually drawn
      // its first real frame from the *currently targeted* video. Needed
      // because the canvas is a single long-lived element (see class
      // comment above): right after retarget(), it's still showing
      // whatever the previous video last rendered into it until a fresh
      // frame arrives, so the caller must not reveal the canvas (or hide
      // the <video>) until this fires — otherwise a mode toggle or a new
      // openOverlay() briefly displays the *previous* channel's picture
      // under the newly-focused channel's name/controls. Confirmed for
      // real: opening IPCamera 02 with Classical on, then switching to
      // Garasi, showed IPCamera 02's last frame in the canvas for one
      // enhance-render cycle before Garasi's frame arrived.
      this.onFirstFrame = null;
      this._firstFrameDone = false;
    }

    _compileShader(type, src) {
      const gl = this.gl;
      const shader = gl.createShader(type);
      gl.shaderSource(shader, src);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        const info = gl.getShaderInfoLog(shader);
        gl.deleteShader(shader);
        throw new Error(`shader compile failed: ${info}`);
      }
      return shader;
    }

    _ensureInit() {
      if (this.gl) return true;
      const gl = this.canvas.getContext('webgl2');
      if (!gl) return false;
      this.gl = gl;
      try {
        const vs = this._compileShader(gl.VERTEX_SHADER, ENHANCE_VERTEX_SRC);
        const fs = this._compileShader(gl.FRAGMENT_SHADER, ENHANCE_FRAGMENT_SRC);
        const program = gl.createProgram();
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
          throw new Error(gl.getProgramInfoLog(program));
        }
        this.program = program;

        // One static fullscreen-quad vertex buffer — the frame texture
        // itself is what changes every draw, not the geometry.
        const posBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        const aPos = gl.getAttribLocation(program, 'aPos');
        gl.enableVertexAttribArray(aPos);
        gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

        this.texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this.texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        // texImage2D uploads row 0 of the video frame (its top row) to
        // texture v=0, but the vertex shader's UV mapping puts v=0 at the
        // bottom of the screen (gl_Position y=-1) - without this, the
        // rendered frame comes out vertically flipped versus the plain
        // <video> it's replacing. Standard WebGL fix: flip on unpack
        // instead of changing the UV math, so uTexel's neighbor-pixel
        // offsets in the shader stay simple screen-space deltas.
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);

        this.uFrameLoc = gl.getUniformLocation(program, 'uFrame');
        this.uTexelLoc = gl.getUniformLocation(program, 'uTexel');
        return true;
      } catch (err) {
        console.warn('[enhance] WebGL2 init failed, staying on plain video:', err);
        this.gl = null;
        return false;
      }
    }

    // Returns whether a frame was actually drawn, so start()'s loop can
    // tell a real draw apart from the early-return no-op below (video
    // metadata not loaded yet) — see onFirstFrame above for why that
    // distinction matters.
    _renderFrame() {
      const gl = this.gl, video = this.video;
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return false; // metadata not loaded yet on this burst
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      gl.viewport(0, 0, w, h);
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
      gl.useProgram(this.program);
      gl.uniform1i(this.uFrameLoc, 0);
      gl.uniform2f(this.uTexelLoc, 1 / w, 1 / h);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      return true;
    }

    // Points this already-initialized pipeline at a different video element
    // — reused across Prev/Next and repeated open/close (see the class
    // comment above for why a fresh context per cell isn't done instead).
    retarget(video) {
      this.video = video;
    }

    // Caller must have already confirmed WebGL2 + requestVideoFrameCallback
    // are both available (see enhanceCapable() below) — this class assumes
    // it, rather than falling back to a requestAnimationFrame polling loop
    // if rVFC is missing (the fallback policy is "skip enhancement
    // entirely" in that case, not a lower-fidelity frame source).
    start() {
      if (this.running) return true;
      if (!this._ensureInit()) return false;
      this.running = true;
      this._firstFrameDone = false;
      const loop = () => {
        if (!this.running) return;
        let drew;
        try {
          drew = this._renderFrame();
        } catch (err) {
          console.warn('[enhance] render failed, falling back to plain video:', err);
          this.running = false;
          this.onFailure?.();
          return;
        }
        if (drew && !this._firstFrameDone) {
          this._firstFrameDone = true;
          this.onFirstFrame?.();
        }
        this.rvfcHandle = this.video.requestVideoFrameCallback(loop);
      };
      this.rvfcHandle = this.video.requestVideoFrameCallback(loop);
      return true;
    }

    stop() {
      if (!this.running) return;
      this.running = false;
      if (this.rvfcHandle != null) this.video?.cancelVideoFrameCallback(this.rvfcHandle);
      this.rvfcHandle = null;
    }
  }

  // WebGL2 support and requestVideoFrameCallback support checked together
  // — either missing means enhancement silently stays unavailable (falls
  // back to plain <video>, never a broken/blank picture, never a "coming
  // soon" state that does nothing when picked).
  function enhanceCapable() {
    const probe = document.createElement('canvas');
    const hasWebgl2 = !!(probe.getContext && probe.getContext('webgl2'));
    const hasRvfc = typeof HTMLVideoElement !== 'undefined' && 'requestVideoFrameCallback' in HTMLVideoElement.prototype;
    return hasWebgl2 && hasRvfc;
  }

  const ENHANCE_STORAGE_KEY = 'enhanceMode';
  // 'ai' joins this list in Phase 15.3, once it's a real, working option —
  // not added as a disabled placeholder now.
  const ENHANCE_MODES = ['off', 'classical'];
  let enhanceMode = ENHANCE_MODES.includes(localStorage.getItem(ENHANCE_STORAGE_KEY))
    ? localStorage.getItem(ENHANCE_STORAGE_KEY)
    : 'off';
  const enhancePipeline = new EnhancementPipeline(document.getElementById('enhanceCanvas'));
  enhancePipeline.onFailure = () => {
    document.getElementById('enhanceCanvas').style.display = 'none';
    const video = document.getElementById('overlaySlot').querySelector('video');
    if (video) video.style.display = '';
  };

  function updateEnhanceButton() {
    document.getElementById('overlayEnhance').textContent = `Enhance: ${enhanceMode === 'classical' ? 'Classical' : 'Off'}`;
  }

  // Reverts to plain <video> — called both when actually turning
  // enhancement off and as the first step of re-applying it (so retargeting
  // to a new video, or a mode toggle, always starts from a clean state
  // rather than layering on top of whatever was showing before).
  function stopEnhancement() {
    enhancePipeline.stop();
    document.getElementById('enhanceCanvas').style.display = 'none';
    const video = document.getElementById('overlaySlot').querySelector('video');
    if (video) video.style.display = '';
  }

  function applyEnhancement() {
    stopEnhancement();
    if (enhanceMode !== 'classical') return;
    if (!enhanceCapable()) return; // fallback policy: silently stay on plain <video>
    const video = document.getElementById('overlaySlot').querySelector('video');
    if (!video) return;
    enhancePipeline.retarget(video);
    // Swap video/canvas visibility only once the pipeline has actually
    // drawn a real frame from *this* video, not as soon as start() returns
    // — see onFirstFrame's comment on EnhancementPipeline for why (the
    // canvas can still be showing a previous channel's last frame here).
    enhancePipeline.onFirstFrame = () => {
      video.style.display = 'none';
      document.getElementById('enhanceCanvas').style.display = 'block';
    };
    enhancePipeline.start();
  }

  document.getElementById('overlayEnhance').onclick = () => {
    const idx = ENHANCE_MODES.indexOf(enhanceMode);
    enhanceMode = ENHANCE_MODES[(idx + 1) % ENHANCE_MODES.length];
    localStorage.setItem(ENHANCE_STORAGE_KEY, enhanceMode);
    updateEnhanceButton();
    applyEnhancement();
  };
  updateEnhanceButton();

  // Minimal export surface, same pattern as auth.js - openOverlay()/
  // closeOverlay()/showAdjacent() in index.html call these two, everything
  // else here (the pipeline, shader sources, mode state) stays private.
  window.applyEnhancement = applyEnhancement;
  window.stopEnhancement = stopEnhancement;
})();
