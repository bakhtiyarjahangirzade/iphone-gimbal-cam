"""GimbalCam for Windows — iPhone webcam + Zhiyun gimbal control + tracking."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from .gimbal import Gimbal
from .server import RECORDINGS_DIR, PhoneServer, new_token
from .tracker import FollowController, Tracker

PREVIEW_W, PREVIEW_H = 960, 540
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "GimbalCam" / "settings.json"
QUALITIES = [("Kayıt 4K30", "4k30"), ("Kayıt 1080p30", "1080p30"), ("Kayıt 720p60", "720p60")]
ZOOMS = [0.5, 1, 2, 5]
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
            try:
                self.cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, backend="obs")
            except Exception as e:  # noqa: BLE001
                self.error = ("Sanal kamera açılamadı. OBS Studio kurulu mu? "
                              f"(obsproject.com) — {e}")
                self.cam = None
                raise
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("GimbalCam")
        root.geometry("1400x760")
        root.minsize(1100, 640)

        self.settings = self._load_settings()
        self.ui_q = queue.Queue()

        self.tracker = Tracker()
        self.follow = FollowController()
        self.vcam = VirtualCam()
        self.phone_fps = 30
        self.manual = {}  # active manual directions
        self.latest_frame = None
        self.frame_event = threading.Event()
        self.preview_rgb = None
        self.preview_lock = threading.Lock()
        self.proc_fps = 0.0

        token = self.settings.get("token") or new_token()
        self.settings["token"] = token
        self.receiver = PhoneServer(
            token=token,
            on_frame=self._on_frame,
            on_status=lambda s: self.post(self.phone_status.set, s),
            on_json=lambda d: self.post(self._on_phone_json, d),
            on_file=lambda ev, info: self.post(self._on_file, ev, info),
            on_connection=lambda on: self.post(self._on_connection, on))
        self.qr_big = None
        self.qr_small = None
        self.phone_connected = False
        self.gimbal = Gimbal(
            on_status=lambda s: self.post(self.gimbal_status.set, s),
            on_info=lambda d: self.post(self._on_gimbal_info, d))

        self._build_ui()
        self._apply_settings()

        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(30, self._ui_tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.receiver.start()
        self._make_qr()
        self._save_settings()

    # ---------- helpers ----------

    def post(self, fn, *args):
        self.ui_q.put((fn, args))

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {
            "token": self.settings.get("token"),
            "invert_pan": self.inv_pan.get(),
            "invert_tilt": self.inv_tilt.get(),
            "kp": self.kp.get(),
            "max_speed": self.max_speed.get(),
            "smoothing": self.smoothing.get(),
            "deadzone": self.deadzone.get(),
            "lead": self.lead.get(),
            "sharp_stop": self.sharp_stop.get(),
            "vcam_size": self.vcam_size.get(),
            "vcam_on": self.vcam_on.get(),
            "manual_speed": self.manual_speed.get(),
        }
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    # ---------- UI ----------

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, width=PREVIEW_W, height=PREVIEW_H, bg="#111", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_waiting())
        self.canvas_img = None
        self.info_line = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.info_line).pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(main, width=380)
        right.pack(side="right", fill="y", padx=(10, 0))

        # --- Phone ---
        f = ttk.LabelFrame(right, text="iPhone", padding=8)
        f.pack(fill="x", pady=(0, 8))
        self.phone_status = tk.StringVar(value="Sunucu başlatılıyor…")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.qr_label = ttk.Label(row)
        self.qr_label.pack(side="left")
        ttk.Label(row, textvariable=self.phone_status, wraplength=200).pack(side="left", padx=8, anchor="n")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        self.quality_var = tk.StringVar(value="4k30")
        for label, val in QUALITIES:
            ttk.Radiobutton(row, text=label.replace("Kayıt ", ""), value=val, variable=self.quality_var,
                            command=lambda: self.receiver.send_json({"cmd": "quality", "value": self.quality_var.get()})
                            ).pack(side="left", padx=(0, 6))

        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Zoom:").pack(side="left")
        for z in ZOOMS:
            ttk.Button(row, text=f"{z:g}×", width=4,
                       command=lambda z=z: self.receiver.send_json({"cmd": "zoom", "value": z})).pack(side="left", padx=1)

        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        self.rec_btn = ttk.Button(row, text="● Kaydı başlat", command=self._toggle_record)
        self.rec_btn.pack(side="left")
        ttk.Button(row, text="Kayıtlar klasörü", command=self._open_recordings).pack(side="left", padx=6)
        self.file_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.file_status, wraplength=340).pack(anchor="w")

        # --- Gimbal ---
        g = ttk.LabelFrame(right, text="Gimbal", padding=8)
        g.pack(fill="x", pady=(0, 8))
        self.gimbal_status = tk.StringVar(value="Bağlı değil")
        row = ttk.Frame(g)
        row.pack(fill="x")
        ttk.Button(row, text="Gimbale bağlan", command=self.gimbal.connect).pack(side="left")
        ttk.Label(row, textvariable=self.gimbal_status, wraplength=220).pack(side="left", padx=6)

        pad = ttk.Frame(g)
        pad.pack(pady=6)
        for (r, c, txt, d) in ((0, 1, "▲", "up"), (1, 0, "◀", "left"), (1, 2, "▶", "right"), (2, 1, "▼", "down")):
            b = ttk.Button(pad, text=txt, width=4)
            b.grid(row=r, column=c, padx=2, pady=2)
            b.bind("<ButtonPress-1>", lambda e, d=d: self._manual_press(d))
            b.bind("<ButtonRelease-1>", lambda e, d=d: self._manual_release(d))
        ttk.Button(pad, text="■", width=4, command=self._manual_stop_all).grid(row=1, column=1)

        row = ttk.Frame(g)
        row.pack(fill="x")
        ttk.Label(row, text="Manuel hız:").pack(side="left")
        self.manual_speed = tk.DoubleVar(value=0.4)
        ttk.Scale(row, from_=0.05, to=1.0, variable=self.manual_speed).pack(side="left", fill="x", expand=True, padx=4)
        row = ttk.Frame(g)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Mod:").pack(side="left")
        for m in ("PF", "L", "F", "POV", "GO"):
            ttk.Button(row, text=m, width=4, command=lambda m=m: self.gimbal.set_mode(m)).pack(side="left", padx=1)
        ttk.Label(g, text="Klavye: ok tuşları = gimbal, Boşluk = durdur", foreground="#666").pack(anchor="w", pady=(6, 0))

        # --- Tracking ---
        t = ttk.LabelFrame(right, text="Takip", padding=8)
        t.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(t)
        row.pack(fill="x")
        self.track_mode = tk.StringVar(value="off")
        for txt, val in (("Kapalı", "off"), ("Yüz / kafa", "face"), ("Vücut", "body")):
            ttk.Radiobutton(row, text=txt, value=val, variable=self.track_mode,
                            command=self._on_track_mode).pack(side="left", padx=(0, 8))
        def slider(label, var, lo, hi, fmt):
            row = ttk.Frame(t)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=11).pack(side="left")
            val = ttk.Label(row, width=5)
            val.pack(side="right")
            sc = ttk.Scale(row, from_=lo, to=hi, variable=var,
                           command=lambda v: val.configure(text=fmt(float(v))))
            sc.pack(side="left", fill="x", expand=True, padx=4)
            var.trace_add("write", lambda *a: val.configure(text=fmt(var.get())))
            val.configure(text=fmt(var.get()))

        self.max_speed = tk.DoubleVar(value=1.0)
        self.kp = tk.DoubleVar(value=3.0)
        self.smoothing = tk.DoubleVar(value=0.3)
        self.deadzone = tk.DoubleVar(value=0.04)
        self.lead = tk.DoubleVar(value=0.12)
        slider("Hız (maks):", self.max_speed, 0.1, 1.0, lambda v: f"%{v * 100:.0f}")
        slider("Tepki:", self.kp, 0.5, 8.0, lambda v: f"{v:.1f}")
        slider("Yumuşak kalkış:", self.smoothing, 0.0, 0.9, lambda v: f"{v:.2f}")
        slider("Ölü bölge:", self.deadzone, 0.0, 0.15, lambda v: f"%{v * 100:.0f}")
        slider("Öngörü:", self.lead, 0.0, 0.4, lambda v: f"{v * 1000:.0f}ms")
        row = ttk.Frame(t)
        row.pack(fill="x", pady=(2, 0))
        self.sharp_stop = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Keskin durma (hedef merkeze gelince anında dur)",
                        variable=self.sharp_stop).pack(side="left")
        row = ttk.Frame(t)
        row.pack(fill="x", pady=(2, 0))
        for name, vals in (("Yavaş", (0.4, 2.0, 0.5, 0.06, 0.05)),
                           ("Normal", (0.8, 3.0, 0.3, 0.04, 0.12)),
                           ("Hızlı", (1.0, 5.5, 0.1, 0.03, 0.2))):
            ttk.Button(row, text=name, width=7,
                       command=lambda v=vals: self._tracking_preset(*v)).pack(side="left", padx=(0, 4))
        row = ttk.Frame(t)
        row.pack(fill="x")
        self.inv_pan = tk.BooleanVar(value=False)
        self.inv_tilt = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Pan ters", variable=self.inv_pan).pack(side="left")
        ttk.Checkbutton(row, text="Tilt ters", variable=self.inv_tilt).pack(side="left", padx=8)

        # --- Virtual camera ---
        v = ttk.LabelFrame(right, text="Sanal webcam", padding=8)
        v.pack(fill="x")
        row = ttk.Frame(v)
        row.pack(fill="x")
        self.vcam_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Açık", variable=self.vcam_on).pack(side="left")
        ttk.Label(row, text="Boyut:").pack(side="left", padx=(10, 0))
        self.vcam_size = tk.StringVar(value="1080p")
        ttk.Combobox(row, textvariable=self.vcam_size, values=list(VCAM_SIZES), width=7, state="readonly").pack(side="left", padx=4)
        self.vcam_status = tk.StringVar(value="Zoom/Teams/OBS'de “OBS Virtual Camera” olarak görünür.")
        ttk.Label(v, textvariable=self.vcam_status, wraplength=340, foreground="#666").pack(anchor="w", pady=(4, 0))

        # keyboard
        keymap = {"Up": "up", "Down": "down", "Left": "left", "Right": "right"}
        self.root.bind("<KeyPress>", lambda e: keymap.get(e.keysym) and self._manual_press(keymap[e.keysym]))
        self.root.bind("<KeyRelease>", lambda e: keymap.get(e.keysym) and self._manual_release(keymap[e.keysym]))
        self.root.bind("<space>", lambda e: self._manual_stop_all())

    def _apply_settings(self):
        s = self.settings
        self.inv_pan.set(s.get("invert_pan", False))
        self.inv_tilt.set(s.get("invert_tilt", False))
        self.kp.set(s.get("kp", 3.0))
        self.max_speed.set(s.get("max_speed", 1.0))
        self.smoothing.set(s.get("smoothing", 0.3))
        self.deadzone.set(s.get("deadzone", 0.04))
        self.lead.set(s.get("lead", 0.12))
        self.sharp_stop.set(s.get("sharp_stop", True))
        self.vcam_size.set(s.get("vcam_size", "1080p"))
        self.vcam_on.set(s.get("vcam_on", True))
        self.manual_speed.set(s.get("manual_speed", 0.4))

    # ---------- actions ----------

    def _make_qr(self):
        import qrcode
        url = self.receiver.url
        img = qrcode.make(url, box_size=8, border=2).convert("RGB")
        self.qr_pil = img
        self.qr_small = ImageTk.PhotoImage(img.resize((170, 170), Image.NEAREST))
        self.qr_label.configure(image=self.qr_small)
        self.qr_big = ImageTk.PhotoImage(img.resize((360, 360), Image.NEAREST))
        self._draw_waiting()

    def _draw_waiting(self):
        if self.phone_connected or self.qr_big is None:
            return
        c = self.canvas
        cw, ch = max(1, c.winfo_width()), max(1, c.winfo_height())
        c.delete("all")
        c.create_image(cw // 2, ch // 2 - 40, image=self.qr_big)
        c.create_text(cw // 2, ch // 2 + 175, fill="#eee", font=("Segoe UI", 15, "bold"),
                      text="iPhone Kamera ile bu QR kodu tarat")
        c.create_text(cw // 2, ch // 2 + 205, fill="#aaa", font=("Segoe UI", 11),
                      text="Safari \"bağlantı gizli değil\" derse: Ayrıntıları Göster → bu web sitesini ziyaret et (bir kere)")
        c.create_text(cw // 2, ch // 2 + 228, fill="#777", font=("Segoe UI", 10), text=self.receiver.url)

    def _on_connection(self, on):
        self.phone_connected = on
        if not on:
            self.root.after(300, self._draw_waiting)

    def _toggle_record(self):
        self.receiver.send_json({"cmd": "record"})

    def _open_recordings(self):
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(RECORDINGS_DIR)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(RECORDINGS_DIR)])

    def _on_canvas_click(self, e):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        # preview is letterboxed into the canvas; map click to normalized image coords
        scale = min(cw / PREVIEW_W, ch / PREVIEW_H)
        w, h = PREVIEW_W * scale, PREVIEW_H * scale
        x0, y0 = (cw - w) / 2, (ch - h) / 2
        x, y = (e.x - x0) / w, (e.y - y0) / h
        if 0 <= x <= 1 and 0 <= y <= 1:
            self.receiver.send_json({"cmd": "focus", "x": x, "y": y})

    def _manual_press(self, d):
        self.manual[d] = True
        self._apply_manual()

    def _manual_release(self, d):
        self.manual.pop(d, None)
        self._apply_manual()

    def _manual_stop_all(self):
        self.manual.clear()
        self.track_mode.set("off")
        self._on_track_mode()
        self.gimbal.stop()

    def _apply_manual(self):
        s = self.manual_speed.get()
        pan = (s if "right" in self.manual else 0) - (s if "left" in self.manual else 0)
        tilt = (s if "up" in self.manual else 0) - (s if "down" in self.manual else 0)
        self.gimbal.set_velocity(pan=pan, tilt=tilt)

    def _tracking_preset(self, speed, kp, smooth, dz, lead):
        self.max_speed.set(speed)
        self.kp.set(kp)
        self.smoothing.set(smooth)
        self.deadzone.set(dz)
        self.lead.set(lead)

    def _on_track_mode(self):
        self.follow.reset()
        self.tracker.mode = self.track_mode.get()
        if self.tracker.mode == "off" and not self.manual:
            self.gimbal.stop()

    # ---------- callbacks ----------

    def _on_phone_json(self, d):
        if d.get("type") == "hello":
            if d.get("quality"):
                self.quality_var.set(d["quality"])
            self._set_rec_button(bool(d.get("recording")))
        elif d.get("type") == "recording":
            self._set_rec_button(bool(d.get("value")))

    def _set_rec_button(self, recording):
        self.rec_btn.configure(text="■ Kaydı durdur" if recording else "● Kaydı başlat")

    def _on_file(self, ev, info):
        size = max(1, int(info.get("size", 1)))
        name = os.path.basename(info.get("path", ""))
        if ev == "begin":
            self.file_status.set(f"Kayıt aktarılıyor: {name}")
        elif ev == "progress":
            self.file_status.set(f"Aktarılıyor: {name} — %{100 * info['received'] // size}")
        elif ev == "done":
            self.file_status.set(f"Kaydedildi: {info.get('path')}")

    def _on_gimbal_info(self, d):
        if "firmware" in d:
            self.gimbal_status.set(f"{self.gimbal_status.get()} · firmware {d['firmware']:.2f}")

    def _on_frame(self, frame):
        # called on receiver thread — hand over to worker, never block
        self.latest_frame = frame
        self.frame_event.set()

    # ---------- processing worker ----------

    def _worker(self):
        last = time.monotonic()
        n = 0
        while True:
            self.frame_event.wait()
            self.frame_event.clear()
            frame = self.latest_frame
            if frame is None:
                continue

            out_w, out_h = VCAM_SIZES.get(self.vcam_size.get(), (1920, 1080))
            if frame.height > frame.width:
                out_w, out_h = out_h, out_w
            if frame.height < out_h or frame.width < out_w:
                out_w, out_h = frame.width, frame.height
            rgb = frame.reformat(width=out_w, height=out_h, format="rgb24").to_ndarray()

            if self.vcam_on.get():
                try:
                    self.vcam.send(rgb, self.phone_fps)
                    if "OBS" not in self.vcam_status.get():
                        self.post(self.vcam_status.set, "Zoom/Teams/OBS'de “OBS Virtual Camera” olarak görünür.")
                except Exception:  # noqa: BLE001
                    self.post(self.vcam_status.set, self.vcam.error)
                    self.vcam_on_off_later()
            else:
                self.vcam.close()

            # tracking
            if self.tracker.mode != "off":
                small = cv2.resize(rgb, (480, int(480 * out_h / out_w)), interpolation=cv2.INTER_AREA)
                try:
                    point = self.tracker.process(small)
                except Exception as e:  # noqa: BLE001
                    point = None
                    self.post(self.gimbal_status.set, f"Takip hatası: {e}")
                self.follow.kp = self.kp.get()
                self.follow.max_speed = self.max_speed.get()
                self.follow.smoothing = self.smoothing.get()
                self.follow.deadzone = self.deadzone.get()
                self.follow.lead = self.lead.get()
                self.follow.sharp_stop = self.sharp_stop.get()
                self.follow.invert_pan = self.inv_pan.get()
                self.follow.invert_tilt = self.inv_tilt.get()
                pan, tilt = self.follow.update(point)
                if not self.manual:
                    self.gimbal.set_velocity(pan=pan, tilt=tilt)

            sc = min(PREVIEW_W / out_w, PREVIEW_H / out_h)
            pw, ph = max(1, int(out_w * sc)), max(1, int(out_h * sc))
            preview = cv2.resize(rgb, (pw, ph), interpolation=cv2.INTER_AREA)
            box = self.tracker.last_box
            if box:
                x0, y0, x1, y1 = (int(box[0] * pw), int(box[1] * ph), int(box[2] * pw), int(box[3] * ph))
                cv2.rectangle(preview, (x0, y0), (x1, y1), (60, 220, 90), 2)
            with self.preview_lock:
                self.preview_rgb = preview

            n += 1
            now = time.monotonic()
            if now - last >= 1:
                self.proc_fps = n / (now - last)
                n = 0
                last = now

    def vcam_on_off_later(self):
        self.post(self.vcam_on.set, False)

    # ---------- UI loop ----------

    def _ui_tick(self):
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass

        with self.preview_lock:
            img = self.preview_rgb
            self.preview_rgb = None
        if img is not None and self.phone_connected:
            cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
            ih, iw = img.shape[:2]
            scale = min(cw / iw, ch / ih)
            pil = Image.fromarray(img)
            if abs(scale - 1) > 0.02:
                pil = pil.resize((max(1, int(iw * scale)), max(1, int(ih * scale))))
            self.canvas_img = ImageTk.PhotoImage(pil)
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2, image=self.canvas_img)

        r = self.receiver
        mbps = r.stats_bytes * 8 / 1e6
        r.stats_bytes = 0
        # stats_bytes accumulates per tick (~30 ms); show a smoothed estimate
        self._mbps = getattr(self, "_mbps", 0.0) * 0.95 + mbps * (1000 / 30) * 0.05
        self.info_line.set(
            f"{'Telefon bağlı (Wi-Fi)' if r.connected else 'Telefon bağlı değil'}  ·  "
            f"işlenen {self.proc_fps:.0f} fps  ·  {self._mbps:.1f} Mbps  ·  "
            f"gecikme {('~%d ms' % r.latency_ms) if r.latency_ms is not None else '—'}")
        self.root.after(30, self._ui_tick)

    def _on_close(self):
        self._save_settings()
        self.gimbal.stop()
        self.gimbal.disconnect()
        self.receiver.stop()
        self.vcam.close()
        self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
