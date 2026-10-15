"""GimbalCam core: phone link, gimbal, tracking, virtual camera. UI-independent.

The control panel (panel.py + web/panel.html) talks to this object through
`state()` (pushed to the UI) and `command(dict)` (from the UI).
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

from .gimbal import Gimbal
from .scan import SCAN_PORT, ScanSession
from .server import RECORDINGS_DIR, PhoneServer, new_token
from .tracker import FollowController, Tracker

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
}
VCAM_SIZES = {"1080p": (1920, 1080), "720p": (1280, 720)}


class VirtualCam:
    def __init__(self):
        self.cam = None
        self.size = None
        self.error = ""

    def send(self, rgb, fps):
        h, w = rgb.shape[:2]
        if self.cam is None or self.size != (w, h, fps):
            self.close()
            import pyvirtualcam
            self.cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, backend="obs")
            self.size = (w, h, fps)
            self.error = ""
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

        self.lock = threading.Lock()
        self.version = 0                 # bumps whenever state changes
        self.paused = False
        self.manual = set()
        self.track_mode = "off"
        self.phone = {"connected": False, "status": "starting", "detail": "", "quality": self.settings["quality"],
                      "recording": False, "rec_started": None, "zoom": 1}
        self.gimbal_state = {"connected": False, "status": "idle", "detail": "", "firmware": None}
        self.file = {"event": None, "name": "", "path": "", "received": 0}
        self.vcam_state = {"ok": None, "error": ""}
        self.proc_fps = 0.0

        self.tracker = Tracker()
        self.follow = FollowController()
        self.vcam = VirtualCam()

        self.latest_frame = None
        self.frame_event = threading.Event()
        self.jpeg = None                 # latest preview JPEG bytes
        self.jpeg_seq = 0
        self.jpeg_listeners = []         # callables notified on new JPEG
        self.running = True

        self.server = PhoneServer(
            token=self.settings["token"],
            on_frame=self._on_frame,
            on_status=self._on_phone_status,
            on_json=self._on_phone_json,
            on_file=self._on_file,
            on_connection=self._on_connection)
        self.gimbal = Gimbal(on_status=self._on_gimbal_status, on_info=self._on_gimbal_info)
        self.scan = ScanSession(self)
        self.qr_png = None
        self.scan_qr_png = None

    # ---------------- lifecycle ----------------

    def start(self):
        self.server.extra_sites.append((self.scan.make_app(), "0.0.0.0", SCAN_PORT))
        self.server.start()
        self.server.started.wait(timeout=10)
        self.qr_png = self._make_qr()
        self.scan_qr_png = self._make_qr(self.scan.qr_text())
        threading.Thread(target=self._worker, daemon=True).start()
        self._apply_follow_settings()

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
        self.vcam.close()
        self.frame_event.set()
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

    # ---------------- QR ----------------

    def phone_url(self):
        return f"{self.server.url}&l={self.settings['lang']}"

    def _make_qr(self, text=None):
        import qrcode
        img = qrcode.make(text or self.phone_url(), box_size=10, border=2)
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
            "vcam": self.vcam_state,
            "stats": {
                "latency_ms": round(self.server.latency_ms) if self.server.latency_ms is not None else None,
                "fps": round(self.proc_fps),
                "mbps": round(self.server.stats_mbps, 1),
            },
            "qr": self.qr_png,
            "scan_qr": self.scan_qr_png,
            "scan": self.scan.state(),
            "url": self.phone_url(),
            "recordings_dir": str(RECORDINGS_DIR),
        }

    # ---------------- commands from UI ----------------

    def command(self, c):
        cmd = c.get("cmd")
        v = c.get("value")
        if cmd and (cmd.startswith("scan_") or cmd.startswith("sweep_")):
            self.scan.command(c)
            self._bump()
            return
        if cmd == "stop":                       # emergency stop: all motion + tracking off
            self.scan.stop_sweep()
            self.manual.clear()
            self.track_mode = "off"
            self.tracker.mode = "off"
            self.follow.reset()
            self.gimbal.stop()
        elif cmd == "pause":
            self.paused = bool(v)
            if self.paused:
                self.scan.stop_sweep()
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
            self.track_mode = v if v in ("off", "face", "body") else "off"
            self.tracker.mode = self.track_mode
            self.follow.reset()
            if self.track_mode == "off" and not self.manual:
                self.gimbal.stop()
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
                    self.scan_qr_png = self._make_qr(self.scan.qr_text())
                    self.server.send_json({"cmd": "lang", "value": v})
                    self.scan.send({"cmd": "lang", "value": v})
                if k == "vcam_on" and not v:
                    self.vcam.close()
                    self.vcam_state = {"ok": None, "error": ""}
                self._save()
        elif cmd == "gimbal_connect":
            self.gimbal.connect()
        elif cmd == "gimbal_disconnect":
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
            if self.paused:
                self.server.send_json({"cmd": "pause", "value": True})
        self._bump()

    def _on_phone_json(self, d):
        t = d.get("type")
        if t == "hello":
            if d.get("quality"):
                self.phone["quality"] = d["quality"]
            self.phone["recording"] = bool(d.get("recording"))
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
        self.gimbal_state["connected"] = key == "connected" or (
            self.gimbal_state["connected"] and key == "write_error")
        if key in ("lost", "error", "not_found"):
            self.gimbal_state["connected"] = False
        self._bump()

    def _on_gimbal_info(self, d):
        if "firmware" in d:
            self.gimbal_state["firmware"] = d["firmware"]
            self._bump()

    def _on_frame(self, frame):
        self.latest_frame = frame
        self.frame_event.set()

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
        out_w, out_h = VCAM_SIZES.get(s["vcam_size"], (1920, 1080))
        if frame.height > frame.width:
            out_w, out_h = out_h, out_w
        if frame.height < out_h or frame.width < out_w:
            out_w, out_h = frame.width, frame.height
        rgb = frame.reformat(width=out_w, height=out_h, format="rgb24").to_ndarray()

        if s["vcam_on"] and not self.paused:
            try:
                self.vcam.send(rgb, 30)
                if not self.vcam_state["ok"]:
                    self.vcam_state = {"ok": True, "error": ""}
                    self._bump()
            except Exception as e:  # noqa: BLE001
                self.vcam_state = {"ok": False, "error": str(e)}
                s["vcam_on"] = False
                self._bump()

        if self.track_mode != "off" and not self.paused:
            small = cv2.resize(rgb, (480, max(1, int(480 * out_h / out_w))), interpolation=cv2.INTER_AREA)
            try:
                point = self.tracker.process(small)
            except Exception as e:  # noqa: BLE001
                point = None
                print("tracking error:", e)
            pan, tilt = self.follow.update(point)
            if not self.manual:
                self.gimbal.set_velocity(pan=pan, tilt=tilt)
        else:
            self.tracker.last_box = None

        sc = min(PREVIEW_MAX[0] / out_w, PREVIEW_MAX[1] / out_h)
        pw, ph = max(1, int(out_w * sc)), max(1, int(out_h * sc))
        preview = cv2.resize(rgb, (pw, ph), interpolation=cv2.INTER_AREA)
        box = self.tracker.last_box
        if box:
            x0, y0, x1, y1 = (int(box[0] * pw), int(box[1] * ph), int(box[2] * pw), int(box[3] * ph))
            cv2.rectangle(preview, (x0, y0), (x1, y1), (80, 220, 120), 2)
        ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(preview, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.jpeg = jpg.tobytes()
            self.jpeg_seq += 1
            for fn in list(self.jpeg_listeners):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
