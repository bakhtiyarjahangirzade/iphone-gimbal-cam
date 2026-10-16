"""GimbalCam core: phone link, gimbal, tracking, auto-framing, gestures, virtual camera.

UI-independent. The control panel (panel.py + web/panel.html) talks to this object through
`state()` (pushed to the UI) and `command(dict)` (from the UI).

Webcam reliability rules:
  * the virtual camera is fed by its own thread at a constant frame rate and a constant size,
    so Zoom/Teams never see the camera stall, resize or disappear;
  * if the phone stops sending, the last frame is held briefly, then a "waiting for phone"
    card is shown — never a frozen or black device.
"""
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .gestures import GestureDetector
from .gimbal import Gimbal
from .server import RECORDINGS_DIR, PhoneServer, new_token
from .tracker import AutoFramer, FollowController, Tracker

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GimbalCam"
SETTINGS_PATH = APP_DIR / "settings.json"
PREVIEW_MAX = (960, 540)
LANGS = ("tr", "en", "es", "ru", "zh", "ar")

DEFAULTS = {
    "lang": "tr",
    "token": None,
    "quality": "4k30",
    "invert_pan": False,
    "invert_tilt": False,
    "kp": 3.0,
    "max_speed": 1.0,
    "smoothing": 0.3,
    "deadzone": 0.04,
    "lead": 0.12,
    "sharp_stop": True,
    "manual_speed": 0.4,
    "vcam_on": True,
    "vcam_size": "1080p",
    "stream_res": "1080p",
    "stream_fps": 30,
    "mirror": False,
    "framing": False,
    "framing_zoom": 1.4,
    "gestures": False,
    "gimbal_auto": False,
}
VCAM_SIZES = {"1080p": (1920, 1080), "720p": (1280, 720)}
HOLD_LAST_FRAME = 1.5          # seconds to keep showing the last frame before the placeholder

PLACEHOLDER_TEXT = {
    "tr": ("Telefon bekleniyor…", "QR kodu iPhone ile tarat"),
    "en": ("Waiting for phone…", "Scan the QR code with your iPhone"),
    "es": ("Esperando al teléfono…", "Escanea el código QR con tu iPhone"),
    "ru": ("Ожидание телефона…", "Отсканируйте QR-код iPhone"),
    "zh": ("等待手机…", "用 iPhone 扫描二维码"),
    "ar": ("في انتظار الهاتف…", "امسح رمز QR بهاتف iPhone"),
}


def fit_into(rgb, w, h):
    """Letterbox/pillarbox `rgb` into a w×h black canvas (keeps aspect)."""
    ih, iw = rgb.shape[:2]
    if (iw, ih) == (w, h):
        return rgb
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    img = cv2.resize(rgb, (nw, nh), interpolation=interp)
    if (nw, nh) == (w, h):
        return img
    out = np.zeros((h, w, 3), np.uint8)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    out[y0:y0 + nh, x0:x0 + nw] = img
    return out


class VirtualCam:
    def __init__(self):
        self.cam = None
        self.size = None

    def send(self, rgb, fps):
        h, w = rgb.shape[:2]
        if self.cam is None or self.size != (w, h, fps):
            self.close()
            import pyvirtualcam
            self.cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, backend="obs")
            self.size = (w, h, fps)
        self.cam.send(rgb)

    def close(self):
        if self.cam:
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                pass
        self.cam = None
        self.size = None


class Core:
    def __init__(self):
        self.settings = dict(DEFAULTS)
        self.settings.update(self._load())
        if not self.settings.get("token"):
            self.settings["token"] = new_token()
        self._save()

        self.version = 0                 # bumps whenever state changes
        self.paused = False
        self.manual = set()
        self.track_mode = "off"
        self.phone = {"connected": False, "status": "starting", "detail": "", "quality": self.settings["quality"],
                      "recording": False, "rec_started": None, "zoom": 1}
        self.gimbal_state = {"connected": False, "status": "idle", "detail": "", "firmware": None}
        self.file = {"event": None, "name": "", "path": "", "received": 0}
        self.vcam_state = {"ok": None, "error": ""}
        self.gesture_event = None
        self.proc_fps = 0.0
        self.vcam_fps = 0.0

        self.tracker = Tracker()
        self.frame_tracker = Tracker()   # face detector used only for auto-framing
        self.frame_tracker.mode = "face"
        self.follow = FollowController()
        self.framer = AutoFramer()
        self.gestures = GestureDetector()
        self.vcam = VirtualCam()

        self.latest_frame = None
        self.frame_event = threading.Event()
        self.out_frame = None            # what the virtual camera shows (vcam size, RGB)
        self.out_frame_t = 0.0
        self.jpeg = None                 # latest preview JPEG bytes
        self.jpeg_seq = 0
        self.jpeg_listeners = []
        self._placeholder = {}
        self._frame_no = 0
        self.running = True

        self.server = PhoneServer(
            token=self.settings["token"],
            on_frame=self._on_frame,
            on_status=self._on_phone_status,
            on_json=self._on_phone_json,
            on_file=self._on_file,
            on_connection=self._on_connection)
        self.gimbal = Gimbal(on_status=self._on_gimbal_status, on_info=self._on_gimbal_info)
        self.qr_png = None

    # ---------------- lifecycle ----------------

    def start(self):
        self.server.start()
        self.server.started.wait(timeout=10)
        self.qr_png = self._make_qr()
        self._apply_follow_settings()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._vcam_loop, daemon=True).start()
        threading.Thread(target=self._gimbal_keeper, daemon=True).start()

    def shutdown(self):
        if not self.running:
            return
        self.running = False
        self._save()
        try:
            self.gimbal.stop()
            time.sleep(0.15)
            self.gimbal.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.frame_event.set()
        time.sleep(0.1)
        self.vcam.close()
        self.server.stop()

    # ---------------- settings ----------------

    def _load(self):
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(self.settings), encoding="utf-8")
        except OSError:
            pass

    def _apply_follow_settings(self):
        s, f = self.settings, self.follow
        f.kp, f.max_speed, f.smoothing = s["kp"], s["max_speed"], s["smoothing"]
        f.deadzone, f.lead, f.sharp_stop = s["deadzone"], s["lead"], s["sharp_stop"]
        f.invert_pan, f.invert_tilt = s["invert_pan"], s["invert_tilt"]

    def _bump(self):
        self.version += 1

    def _send_stream_settings(self):
        self.server.send_json({"cmd": "stream", "res": self.settings["stream_res"],
                               "fps": int(self.settings["stream_fps"])})

    # ---------------- QR ----------------

    def phone_url(self):
        return f"{self.server.url}&l={self.settings['lang']}"

    def _make_qr(self):
        import qrcode
        img = qrcode.make(self.phone_url(), box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    # ---------------- state for UI ----------------

    def state(self):
        p = self.phone
        rec_secs = int(time.time() - p["rec_started"]) if p["recording"] and p["rec_started"] else 0
        return {
            "version": self.version,
            "settings": self.settings,
            "paused": self.paused,
            "track_mode": self.track_mode,
            "phone": {**p, "rec_secs": rec_secs},
            "gimbal": self.gimbal_state,
            "file": self.file,
            "vcam": {**self.vcam_state, "fps": round(self.vcam_fps)},
            "gesture": self.gesture_event,
            "stats": {
                "latency_ms": round(self.server.latency_ms) if self.server.latency_ms is not None else None,
                "fps": round(self.proc_fps),
                "mbps": round(self.server.stats_mbps, 1),
            },
            "qr": self.qr_png,
            "url": self.phone_url(),
            "recordings_dir": str(RECORDINGS_DIR),
        }

    # ---------------- commands from UI ----------------

    def command(self, c):
        cmd = c.get("cmd")
        v = c.get("value")
        if cmd == "stop":                       # emergency stop: all motion + tracking off
            self._stop_motion()
        elif cmd == "pause":
            self.paused = bool(v)
            if self.paused:
                self.manual.clear()
                self.gimbal.stop()
                self.follow.reset()
            self.server.send_json({"cmd": "pause", "value": self.paused})
        elif cmd == "manual":                   # {"dir": "left", "on": true}
            d = c.get("dir")
            if c.get("on"):
                self.manual.add(d)
            else:
                self.manual.discard(d)
            self._apply_manual()
        elif cmd == "track":
            self._set_track(v)
        elif cmd == "preset":
            presets = {"slow": (0.4, 2.0, 0.5, 0.06, 0.05),
                       "normal": (0.8, 3.0, 0.3, 0.04, 0.12),
                       "fast": (1.0, 5.5, 0.1, 0.03, 0.2)}
            if v in presets:
                for k, val in zip(("max_speed", "kp", "smoothing", "deadzone", "lead"), presets[v]):
                    self.settings[k] = val
                self._apply_follow_settings()
                self._save()
        elif cmd == "set":                      # {"key": ..., "value": ...}
            k = c.get("key")
            if k in DEFAULTS and k != "token":
                self.settings[k] = v
                self._apply_follow_settings()
                if k == "lang" and v in LANGS:
                    self.qr_png = self._make_qr()
                    self._placeholder.clear()
                    self.server.send_json({"cmd": "lang", "value": v})
                if k in ("stream_res", "stream_fps"):
                    self._send_stream_settings()
                if k == "framing" and not v:
                    self.framer.reset()
                if k == "vcam_on" and not v:
                    self.vcam_state = {"ok": None, "error": ""}
                self._save()
        elif cmd == "gimbal_connect":
            self.settings["gimbal_auto"] = True
            self._save()
            self.gimbal.connect()
        elif cmd == "gimbal_disconnect":
            self.settings["gimbal_auto"] = False
            self._save()
            self.gimbal.disconnect()
        elif cmd == "gimbal_mode":
            self.gimbal.set_mode(v)
        elif cmd == "record":
            self.server.send_json({"cmd": "record", "value": bool(v)})
        elif cmd == "quality":
            self.settings["quality"] = v
            self._save()
            self.server.send_json({"cmd": "quality", "value": v})
        elif cmd == "zoom":
            self.server.send_json({"cmd": "zoom", "value": v})
            self.phone["zoom"] = v
        elif cmd == "open_folder":
            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            target = c.get("path") or str(RECORDINGS_DIR)
            if sys.platform == "win32":
                if os.path.isfile(target):
                    subprocess.Popen(["explorer", "/select,", target])
                else:
                    os.startfile(target)  # noqa: S606
        self._bump()

    def _stop_motion(self):
        self.manual.clear()
        self._set_track("off")
        self.gimbal.stop()

    def _set_track(self, mode):
        self.track_mode = mode if mode in ("off", "face", "body") else "off"
        self.tracker.mode = self.track_mode
        self.follow.reset()
        if self.track_mode == "off" and not self.manual:
            self.gimbal.stop()

    def _apply_manual(self):
        if self.paused:
            return
        s = self.settings["manual_speed"]
        m = self.manual
        pan = (s if "right" in m else 0) - (s if "left" in m else 0)
        tilt = (s if "up" in m else 0) - (s if "down" in m else 0)
        self.gimbal.set_velocity(pan=pan, tilt=tilt)

    # ---------------- callbacks ----------------

    def _on_phone_status(self, key, detail):
        self.phone["status"], self.phone["detail"] = key, detail
        self._bump()

    def _on_connection(self, on):
        self.phone["connected"] = on
        if not on:
            self.phone["recording"] = False
        else:
            self.server.send_json({"cmd": "lang", "value": self.settings["lang"]})
            self._send_stream_settings()
            if self.paused:
                self.server.send_json({"cmd": "pause", "value": True})
        self._bump()

    def _on_phone_json(self, d):
        t = d.get("type")
        if t == "hello":
            if d.get("quality"):
                self.phone["quality"] = d["quality"]
            self.phone["recording"] = bool(d.get("recording"))
            self._send_stream_settings()
        elif t == "recording":
            self.phone["recording"] = bool(d.get("value"))
            self.phone["rec_started"] = time.time() if d.get("value") else None
        elif t == "zoom":
            self.phone["zoom"] = d.get("value")
        self._bump()

    def _on_file(self, ev, info):
        self.file = {"event": ev, "name": info.get("name", ""), "path": info.get("path", ""),
                     "received": info.get("received", 0)}
        self._bump()

    def _on_gimbal_status(self, key, detail):
        self.gimbal_state["status"], self.gimbal_state["detail"] = key, detail
        if key == "connected":
            self.gimbal_state["connected"] = True
        elif key in ("lost", "error", "not_found"):
            self.gimbal_state["connected"] = False
        self._bump()

    def _on_gimbal_info(self, d):
        if "firmware" in d:
            self.gimbal_state["firmware"] = d["firmware"]
            self._bump()

    def _on_frame(self, frame):
        self.latest_frame = frame
        self.frame_event.set()

    # ---------------- gimbal auto-connect / auto-reconnect ----------------

    def _gimbal_keeper(self):
        time.sleep(1.0)
        last_try = 0.0
        while self.running:
            g = self.gimbal_state
            busy = g["status"] in ("searching", "connecting")
            if self.settings.get("gimbal_auto") and not g["connected"] and not busy \
                    and time.monotonic() - last_try > 8:
                last_try = time.monotonic()
                self.gimbal.connect()
            time.sleep(1.0)

    # ---------------- virtual camera (constant rate, constant size) ----------------

    def _placeholder_frame(self, w, h):
        key = (w, h, self.settings["lang"])
        if key not in self._placeholder:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (w, h), (13, 15, 19))
            d = ImageDraw.Draw(img)

            def font(size, bold=False):
                for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf", "msyh.ttc", "DejaVuSans.ttf"):
                    try:
                        return ImageFont.truetype(name, size)
                    except OSError:
                        continue
                return ImageFont.load_default()

            title, sub = PLACEHOLDER_TEXT.get(self.settings["lang"], PLACEHOLDER_TEXT["en"])
            cx, cy = w // 2, h // 2
            r = h // 14
            d.ellipse((cx - r, cy - 3 * r, cx + r, cy - r), outline=(79, 140, 255), width=max(2, h // 180))
            for txt, f, y, col in ((title, font(h // 18, True), cy + r // 2, (233, 237, 243)),
                                   (sub, font(h // 32), cy + 2 * r, (139, 149, 165)),
                                   ("GimbalCam", font(h // 40, True), h - h // 12, (95, 104, 120))):
                tw = d.textlength(txt, font=f)
                d.text((cx - tw / 2, y), txt, font=f, fill=col)
            self._placeholder[key] = np.asarray(img).copy()
        return self._placeholder[key]

    def _vcam_loop(self):
        next_t = time.monotonic()
        n, last = 0, time.monotonic()
        retry_at = 0.0
        while self.running:
            fps = 60 if int(self.settings.get("stream_fps", 30)) == 60 else 30
            period = 1.0 / fps
            now = time.monotonic()
            if self.settings["vcam_on"] and now >= retry_at:
                w, h = VCAM_SIZES.get(self.settings["vcam_size"], (1920, 1080))
                frame = self.out_frame
                if frame is None or now - self.out_frame_t > HOLD_LAST_FRAME or frame.shape[:2] != (h, w):
                    frame = self._placeholder_frame(w, h)
                try:
                    self.vcam.send(frame, fps)
                    if not self.vcam_state["ok"]:
                        self.vcam_state = {"ok": True, "error": ""}
                        self._bump()
                    n += 1
                except Exception as e:  # noqa: BLE001
                    self.vcam.close()
                    if self.vcam_state.get("ok") is not False:
                        self.vcam_state = {"ok": False, "error": str(e)}
                        self._bump()
                    retry_at = now + 5          # e.g. OBS not installed yet: retry quietly
            elif not self.settings["vcam_on"] and self.vcam.cam is not None:
                self.vcam.close()
            if now - last >= 1:
                self.vcam_fps = n / (now - last)
                n, last = 0, now
            next_t += period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()       # we fell behind: don't try to catch up in a burst

    # ---------------- processing ----------------

    def _worker(self):
        last = time.monotonic()
        n = 0
        while self.running:
            if not self.frame_event.wait(timeout=0.5):
                continue
            self.frame_event.clear()
            frame = self.latest_frame
            if frame is None or not self.running:
                continue
            try:
                self._process(frame)
            except Exception as e:  # noqa: BLE001
                print("worker error:", e)
            n += 1
            now = time.monotonic()
            if now - last >= 1:
                self.proc_fps = n / (now - last)
                n, last = 0, now

    def _process(self, frame):
        s = self.settings
        self._frame_no += 1
        rgb = frame.to_ndarray(format="rgb24")
        fh, fw = rgb.shape[:2]
        small = None

        def get_small():
            nonlocal small
            if small is None:
                small = cv2.resize(rgb, (480, max(1, int(480 * fh / fw))), interpolation=cv2.INTER_AREA)
            return small

        # --- subject detection (shared by gimbal tracking and auto-framing)
        point, box = None, None
        if self.track_mode != "off" and not self.paused:
            try:
                point = self.tracker.process(get_small())
                box = self.tracker.last_box
            except Exception as e:  # noqa: BLE001
                print("tracking error:", e)
            pan, tilt = self.follow.update(point)
            if not self.manual:
                self.gimbal.set_velocity(pan=pan, tilt=tilt)
        elif s["framing"]:
            try:
                point = self.frame_tracker.process(get_small())
                box = self.frame_tracker.last_box
            except Exception as e:  # noqa: BLE001
                print("framing detect error:", e)
        if self.track_mode == "off":
            self.tracker.last_box = None

        # --- hand gestures (~10 Hz)
        if s["gestures"] and not self.paused and self._frame_no % 3 == 0:
            try:
                g = self.gestures.process(get_small())
            except Exception as e:  # noqa: BLE001
                g = None
                print("gesture error:", e)
            if g == "palm":
                self._set_track("off" if self.track_mode != "off" else "face")
                self.gesture_event = {"name": "track_on" if self.track_mode != "off" else "track_off", "t": time.time()}
                self._bump()
            elif g == "fist":
                self._stop_motion()
                self.gesture_event = {"name": "stop", "t": time.time()}
                self._bump()

        # --- auto-framing (digital crop that follows the face)
        x0, y0, cw, ch = 0, 0, fw, fh
        if s["framing"]:
            fx, fy, fwid = self.framer.update(point, float(s["framing_zoom"]))
            cw, ch = max(16, int(fw * fwid)), max(16, int(fh * fwid))
            x0 = int(min(max(0, fx * fw - cw / 2), fw - cw))
            y0 = int(min(max(0, fy * fh - ch / 2), fh - ch))
            view = rgb[y0:y0 + ch, x0:x0 + cw]
        else:
            view = rgb

        # --- webcam output (constant size)
        vw, vh = VCAM_SIZES.get(s["vcam_size"], (1920, 1080))
        out = fit_into(view, vw, vh)
        if s["mirror"]:
            out = np.ascontiguousarray(out[:, ::-1])
        self.out_frame = out
        self.out_frame_t = time.monotonic()

        # --- panel preview (what Zoom sees + tracking box)
        pw, ph = PREVIEW_MAX
        preview = cv2.resize(out, (pw, ph), interpolation=cv2.INTER_AREA)
        if box:
            # map the normalized box (full frame) -> crop -> letterboxed output -> preview
            k = min(vw / cw, vh / ch)
            offx, offy = (vw - cw * k) / 2, (vh - ch * k) / 2
            rx, ry = pw / vw, ph / vh
            bx0 = ((box[0] * fw - x0) * k + offx) * rx
            by0 = ((box[1] * fh - y0) * k + offy) * ry
            bx1 = ((box[2] * fw - x0) * k + offx) * rx
            by1 = ((box[3] * fh - y0) * k + offy) * ry
            if s["mirror"]:
                bx0, bx1 = pw - bx1, pw - bx0
            cv2.rectangle(preview, (int(bx0), int(by0)), (int(bx1), int(by1)), (80, 220, 120), 2)
        ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(preview, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.jpeg = jpg.tobytes()
            self.jpeg_seq += 1
            for fn in list(self.jpeg_listeners):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
