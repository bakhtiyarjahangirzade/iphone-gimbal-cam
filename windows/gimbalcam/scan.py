"""3D room scanning with the GimbalScan iPhone app (LiDAR).

The phone connects to  ws://<pc>:8444/scanws?t=<token>  (plain WebSocket: a native app
has no certificate trouble on the local network).

phone -> PC   text JSON : hello | state | pose | room | instruction | reset | done | file_begin | file_end
              binary    : [1][uuid16][4x4 f32 col-major][nV u32][nF u32][verts f32*3][faces u32*3]  mesh anchor
                          [2][bytes]                                                                file chunk (USDZ)
                          [3][uuid16]                                                               anchor removed
PC -> phone   JSON      : {"cmd": "start", "mode": "room"|"mesh"} | {"cmd": "stop"} | {"cmd": "mode"} | {"cmd": "lang"}
"""
import asyncio
import datetime
import json
import math
import os
import secrets
import struct
import threading
import time
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

SCAN_PORT = 8444
SCANS_DIR = Path.home() / "Documents" / "GimbalScan"


def mat(t):
    """Column-major 16 list -> 4x4 row-major numpy matrix."""
    return np.array(t, dtype=np.float64).reshape(4, 4).T


def parse_anchor(b):
    uuid = bytes(b[1:17])
    m = np.frombuffer(b, "<f4", 16, 17).astype(np.float64).reshape(4, 4).T
    vc, fc = struct.unpack_from("<II", b, 81)
    verts = np.frombuffer(b, "<f4", vc * 3, 89).reshape(-1, 3)
    faces = np.frombuffer(b, "<u4", fc * 3, 89 + vc * 12).reshape(-1, 3)
    return uuid, m, verts, faces


class ScanSession:
    def __init__(self, core):
        self.core = core
        self.ws = None
        self.loop = None
        self.connected = False
        self.device = ""
        self.phone_state = {"scanning": False, "busy": False, "mode": "room", "supported": True}
        self.instruction = ""
        self.track = "normal"
        self.pose = None            # 4x4
        self.pose_t = 0.0
        self.yaw = None
        self.pitch = None
        self.room = None            # latest room dict
        self.room_final = False
        self.anchors = {}           # uuid -> raw bytes (for panel replay)
        self.anchor_geo = {}        # uuid -> (m, verts, faces)
        self.file = {"event": None, "name": "", "path": "", "received": 0}
        self._fh = None
        self.export = {"dir": "", "files": []}
        self.sweep = {"active": False, "phase": "idle", "level": 0, "levels": [20, 0, -20],
                      "progress": 0.0, "status": "idle", "speed": 0.22}
        self._sweep_stop = threading.Event()
        self.listeners = []         # fn(kind, payload) -> panel pushes ("room", dict) | ("mesh", bytes) | ("reset", None)
        self.version = 0

    # ------------------------------------------------------------ helpers
    def _bump(self):
        self.version += 1
        self.core._bump()

    def _emit(self, kind, payload=None):
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def qr_text(self):
        ip = self.core.server.ips[0]
        return f"gimbalscan://connect?h={ip}&p={SCAN_PORT}&t={self.core.settings['token']}&l={self.core.settings['lang']}"

    def state(self):
        return {
            "connected": self.connected,
            "device": self.device,
            "phone": self.phone_state,
            "instruction": self.instruction,
            "track": self.track,
            "pose": None if self.pose is None else [round(v, 4) for v in self.pose.T.flatten().tolist()],
            "yaw": None if self.yaw is None else round(self.yaw, 1),
            "pitch": None if self.pitch is None else round(self.pitch, 1),
            "room_final": self.room_final,
            "stats": self.stats(),
            "file": self.file,
            "export": self.export,
            "sweep": self.sweep,
        }

    def send(self, obj):
        ws, loop = self.ws, self.loop
        if ws is None or ws.closed or loop is None:
            return False
        asyncio.run_coroutine_threadsafe(ws.send_str(json.dumps(obj)), loop)
        return True

    # ------------------------------------------------------------ websocket server
    def make_app(self):
        app = web.Application(client_max_size=128 * 1024 * 1024)

        async def ws_handler(request):
            if not secrets.compare_digest(request.query.get("t", ""), self.core.settings["token"]):
                return web.Response(status=403, text="bad token")
            ws = web.WebSocketResponse(max_msg_size=128 * 1024 * 1024, heartbeat=8)
            await ws.prepare(request)
            if self.ws is not None and not self.ws.closed:
                await self.ws.close()
            self.ws, self.loop = ws, asyncio.get_running_loop()
            self.connected = True
            self.send({"cmd": "lang", "value": self.core.settings["lang"]})
            self._bump()
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        self._on_binary(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        try:
                            self._on_text(json.loads(msg.data))
                        except ValueError:
                            pass
            finally:
                if self.ws is ws:
                    self.ws = None
                    self.connected = False
                    self._bump()
            return ws

        app.router.add_get("/scanws", ws_handler)
        return app

    def _on_text(self, d):
        t = d.get("type")
        if t == "pose":
            self.pose = mat(d["t"])
            self.pose_t = time.monotonic()
            self.track = d.get("track", "normal")
            fwd = -self.pose[:3, 2]          # camera looks down -Z
            fwd = fwd / (np.linalg.norm(fwd) or 1)
            self.pitch = math.degrees(math.asin(max(-1.0, min(1.0, fwd[1]))))
            self.yaw = math.degrees(math.atan2(-fwd[0], -fwd[2]))
            self.core.version += 0      # pose is pushed by the periodic state tick
            return
        if t == "hello":
            self.device = d.get("device", "")
        elif t == "state":
            self.phone_state = {k: d.get(k) for k in ("scanning", "busy", "mode", "supported")}
        elif t == "instruction":
            self.instruction = d.get("value", "")
        elif t == "reset":
            self.room, self.room_final = None, False
            self.anchors.clear()
            self.anchor_geo.clear()
            self._emit("reset")
        elif t == "room":
            self.room = d
            self.room_final = bool(d.get("final"))
            self._emit("room", d)
            if self.room_final:
                threading.Thread(target=self.export_all, kwargs={"auto": True}, daemon=True).start()
        elif t == "done":
            threading.Thread(target=self.export_all, kwargs={"auto": True}, daemon=True).start()
        elif t == "file_begin":
            folder = self._ensure_export_dir()
            name = os.path.basename(d.get("name") or "model.usdz")
            path = folder / name
            self._fh = open(path, "wb")
            self.file = {"event": "begin", "name": name, "path": str(path), "received": 0, "size": d.get("size", 0)}
        elif t == "file_end":
            if self._fh:
                self._fh.close()
                self._fh = None
                self.file["event"] = "done"
                if self.file["path"] not in self.export["files"]:
                    self.export["files"].append(self.file["path"])
        self._bump()

    def _on_binary(self, b):
        kind = b[0]
        if kind == 1:
            try:
                uuid, m, verts, faces = parse_anchor(b)
            except (ValueError, struct.error):
                return
            self.anchors[uuid] = bytes(b)
            self.anchor_geo[uuid] = (m, verts, faces)
            self._emit("mesh", bytes(b))
        elif kind == 2 and self._fh:
            self._fh.write(b[1:])
            self.file["received"] += len(b) - 1
            self.file["event"] = "progress"
        elif kind == 3:
            uuid = bytes(b[1:17])
            self.anchors.pop(uuid, None)
            self.anchor_geo.pop(uuid, None)
            self._emit("mesh", bytes(b))
        self._bump()

    # ------------------------------------------------------------ commands
    def command(self, c):
        cmd = c.get("cmd")
        if cmd == "scan_start":
            self.send({"cmd": "start", "mode": c.get("mode") or self.phone_state.get("mode", "room")})
        elif cmd == "scan_stop":
            self.stop_sweep()
            self.send({"cmd": "stop"})
        elif cmd == "scan_mode":
            self.send({"cmd": "mode", "value": c.get("value")})
        elif cmd == "sweep_start":
            self.start_sweep(c.get("speed"))
        elif cmd == "sweep_stop":
            self.stop_sweep()
            self.sweep["status"] = "stopped"
        elif cmd == "sweep_speed":
            self.sweep["speed"] = float(c.get("value", 0.22))
        elif cmd == "scan_export":
            threading.Thread(target=self.export_all, daemon=True).start()
        elif cmd == "scan_clear":
            self.room, self.room_final = None, False
            self.anchors.clear()
            self.anchor_geo.clear()
            self._emit("reset")
        elif cmd == "scan_open_folder":
            folder = Path(self.export["dir"]) if self.export["dir"] else SCANS_DIR
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)  # noqa: S606
        self._bump()

    # ------------------------------------------------------------ gimbal 360° sweep
    def start_sweep(self, speed=None):
        if self.sweep["active"]:
            return
        if not self.core.gimbal_state.get("connected"):
            self.sweep["status"] = "no_gimbal"
            return
        if speed:
            self.sweep["speed"] = float(speed)
        self.core.command({"cmd": "track", "value": "off"})
        self._sweep_stop.clear()
        self.sweep.update(active=True, phase="start", level=0, progress=0.0, status="running")
        threading.Thread(target=self._sweep_run, daemon=True).start()

    def stop_sweep(self):
        self._sweep_stop.set()
        self.core.gimbal.stop()

    def _pose_fresh(self):
        return self.pose is not None and time.monotonic() - self.pose_t < 1.0

    def _wait_pose(self, timeout=3.0):
        t0 = time.monotonic()
        while not self._pose_fresh():
            if self._sweep_stop.is_set() or time.monotonic() - t0 > timeout:
                return False
            self.core.gimbal.stop()
            time.sleep(0.1)
        return True

    def _tilt_to(self, target):
        g = self.core.gimbal
        sign = -1.0 if self.core.settings.get("invert_tilt") else 1.0
        t0 = time.monotonic()
        checked = False
        start_err = None
        while not self._sweep_stop.is_set():
            if not self._wait_pose():
                return "no_pose"
            err = target - self.pitch
            if start_err is None:
                start_err = abs(err)
            if abs(err) < 2.0:
                break
            # after a moment, verify we move the right way; flip if not
            if not checked and time.monotonic() - t0 > 0.8:
                checked = True
                if abs(err) > start_err + 1.5:
                    sign = -sign
            speed = min(0.5, max(0.12, abs(err) / 45))
            g.set_velocity(pan=0, tilt=sign * speed * (1 if err > 0 else -1))
            if time.monotonic() - t0 > 10:
                break
            time.sleep(0.05)
        g.set_velocity(pan=0, tilt=0)
        time.sleep(0.4)
        return "ok"

    def _pan_full(self, direction, n, m):
        g = self.core.gimbal
        acc, last = 0.0, None
        t0 = last_move = time.monotonic()
        while not self._sweep_stop.is_set():
            if not self._wait_pose():
                return "no_pose"
            y = self.yaw
            if last is not None:
                d = (y - last + 540) % 360 - 180      # unwrap
                acc += abs(d)
                if abs(d) > 0.2:
                    last_move = time.monotonic()
            last = y
            self.sweep["progress"] = min(1.0, acc / 370)
            self.sweep["phase"] = "pan"
            self.sweep["pct"] = round(self.sweep["progress"] * 100)
            self.core._bump()
            if acc >= 370:                          # full circle + a little overlap
                break
            if time.monotonic() - last_move > 3.0:
                g.stop()
                return "no_motion"
            if time.monotonic() - t0 > 180:
                break
            g.set_velocity(pan=direction * self.sweep["speed"], tilt=0)
            time.sleep(0.05)
        g.set_velocity(pan=0, tilt=0)
        time.sleep(0.3)
        return "ok"

    def _sweep_run(self):
        result = "done"
        try:
            if not self._wait_pose(5):
                result = "no_pose"
                return
            levels = self.sweep["levels"]
            for i, level in enumerate(levels):
                if self._sweep_stop.is_set():
                    result = "stopped"
                    break
                self.sweep.update(phase="tilt", level=i, target=level, progress=0.0)
                self.core._bump()
                r = self._tilt_to(level)
                if r != "ok":
                    result = r
                    break
                r = self._pan_full(1 if i % 2 == 0 else -1, i + 1, len(levels))
                if r != "ok":
                    result = r
                    break
            else:
                self._tilt_to(0)
            if self._sweep_stop.is_set() and result == "done":
                result = "stopped"
        finally:
            self.core.gimbal.stop()
            self.sweep.update(active=False, phase="idle", status=result)
            self.core._bump()

    # ------------------------------------------------------------ measurements
    def _surfaces(self, key):
        return (self.room or {}).get(key) or []

    def stats(self):
        walls = self._surfaces("walls")
        out = {"walls": len(walls), "doors": len(self._surfaces("doors")),
               "windows": len(self._surfaces("windows")), "objects": len(self._surfaces("objects")),
               "area": None, "size": None, "height": None,
               "tris": int(sum(len(g[2]) for g in self.anchor_geo.values()))}
        pts = []
        heights = []
        for w in walls:
            m = mat(w["t"])
            c, x = m[:3, 3], m[:3, 0]
            half = w["dims"][0] / 2
            pts += [c - x * half, c + x * half]
            heights.append(w["dims"][1])
        if pts:
            p = np.array(pts)
            out["size"] = [round(float(p[:, 0].max() - p[:, 0].min()), 2), round(float(p[:, 2].max() - p[:, 2].min()), 2)]
            out["height"] = round(float(max(heights)), 2)
        floors = self._surfaces("floors")
        if floors and floors[0].get("poly"):
            out["area"] = round(self._poly_area(floors[0]), 2)
        elif out["size"]:
            out["area"] = round(out["size"][0] * out["size"][1], 2)
        return out

    @staticmethod
    def _world_poly(surface):
        m = mat(surface["t"])
        return [(m @ np.array([p[0], p[1], p[2], 1.0]))[:3] for p in surface["poly"]]

    def _poly_area(self, surface):
        pts = self._world_poly(surface)
        a = 0.0
        for i in range(len(pts)):
            x1, z1 = pts[i][0], pts[i][2]
            x2, z2 = pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][2]
            a += x1 * z2 - x2 * z1
        return abs(a) / 2

    # ------------------------------------------------------------ export
    def _ensure_export_dir(self):
        if not self.export["dir"]:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            d = SCANS_DIR / f"Tarama_{stamp}"
            d.mkdir(parents=True, exist_ok=True)
            self.export = {"dir": str(d), "files": []}
        return Path(self.export["dir"])

    def export_all(self, auto=False):
        folder = self._ensure_export_dir()
        files = set(self.export["files"])
        if self.room:
            p = folder / "oda.json"
            p.write_text(json.dumps(self.room), encoding="utf-8")
            files.add(str(p))
            p = folder / "kat_plani.svg"
            p.write_text(self.floorplan_svg(), encoding="utf-8")
            files.add(str(p))
        if self.anchor_geo:
            files.add(self.write_obj(folder / "mesh.obj"))
            files.add(self.write_ply(folder / "mesh.ply"))
        self.export["files"] = sorted(files | set(self.export["files"]))   # keep files that arrived meanwhile
        self.export["saved_at"] = time.time()
        self._bump()
        return folder

    def _world_mesh(self):
        vs, fs, off = [], [], 0
        for m, verts, faces in list(self.anchor_geo.values()):
            h = np.hstack([verts.astype(np.float64), np.ones((len(verts), 1))])
            w = (h @ m.T)[:, :3]
            vs.append(w.astype(np.float32))
            fs.append(faces.astype(np.int64) + off)
            off += len(verts)
        if not vs:
            return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int64)
        return np.vstack(vs), np.vstack(fs)

    def write_obj(self, path):
        v, f = self._world_mesh()
        with open(path, "w", encoding="ascii") as fh:
            fh.write("# GimbalScan LiDAR mesh (metres, Y up)\n")
            np.savetxt(fh, v, fmt="v %.4f %.4f %.4f")
            np.savetxt(fh, f + 1, fmt="f %d %d %d")
        return str(path)

    def write_ply(self, path):
        v, f = self._world_mesh()
        with open(path, "wb") as fh:
            fh.write((f"ply\nformat binary_little_endian 1.0\ncomment GimbalScan LiDAR mesh\n"
                      f"element vertex {len(v)}\nproperty float x\nproperty float y\nproperty float z\n"
                      f"element face {len(f)}\nproperty list uchar int vertex_indices\nend_header\n").encode())
            fh.write(v.astype("<f4").tobytes())
            rec = np.zeros(len(f), dtype=[("n", "u1"), ("i", "<i4", (3,))])
            rec["n"] = 3
            rec["i"] = f
            fh.write(rec.tobytes())
        return str(path)

    def floorplan_svg(self):
        """Top-down floor plan (x right, z down) with wall lengths, doors, windows and furniture."""
        segs = []

        def seg(s, kind):
            m = mat(s["t"])
            c, x = m[:3, 3], m[:3, 0]
            half = s["dims"][0] / 2
            a, b = c - x * half, c + x * half
            segs.append((kind, (a[0], a[2]), (b[0], b[2]), s["dims"][0]))

        for w in self._surfaces("walls"):
            seg(w, "wall")
        for d in self._surfaces("doors"):
            seg(d, "door")
        for w in self._surfaces("windows"):
            seg(w, "window")
        for o in self._surfaces("openings"):
            seg(o, "opening")
        objs = []
        for o in self._surfaces("objects"):
            m = mat(o["t"])
            c, x, z = m[:3, 3], m[:3, 0], m[:3, 2]
            hx, hz = o["dims"][0] / 2, o["dims"][2] / 2
            corners = [c + x * sx * hx + z * sz * hz for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            objs.append((o.get("cat", ""), [(p[0], p[2]) for p in corners]))

        allpts = [p for s in segs for p in (s[1], s[2])] + [p for _, c in objs for p in c]
        if not allpts:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"></svg>'
        xs, zs = [p[0] for p in allpts], [p[1] for p in allpts]
        minx, minz, maxx, maxz = min(xs), min(zs), max(xs), max(zs)
        S, M = 100.0, 60.0                           # 1 m = 100 px
        W, H = (maxx - minx) * S + 2 * M, (maxz - minz) * S + 2 * M

        def P(p):
            return ((p[0] - minx) * S + M, (p[1] - minz) * S + M)

        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" '
               'font-family="Segoe UI, Arial, sans-serif">',
               f'<rect width="{W:.0f}" height="{H:.0f}" fill="#ffffff"/>']
        floors = self._surfaces("floors")
        if floors and floors[0].get("poly"):
            pts = " ".join(f"{P((p[0], p[2]))[0]:.1f},{P((p[0], p[2]))[1]:.1f}" for p in self._world_poly(floors[0]))
            out.append(f'<polygon points="{pts}" fill="#eef3fb" stroke="none"/>')
        for cat, corners in objs:
            pts = " ".join(f"{P(c)[0]:.1f},{P(c)[1]:.1f}" for c in corners)
            cx = sum(P(c)[0] for c in corners) / 4
            cy = sum(P(c)[1] for c in corners) / 4
            out.append(f'<polygon points="{pts}" fill="#f4efe6" stroke="#b9a98a" stroke-width="1.5"/>')
            out.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="11" fill="#7a6a4f" text-anchor="middle" dominant-baseline="middle">{cat}</text>')
        style = {"wall": ("#1f2937", 7), "door": ("#d97706", 5), "window": ("#0ea5e9", 5), "opening": ("#9ca3af", 3)}
        for kind, a, b, length in segs:
            col, w = style[kind]
            (x1, y1), (x2, y2) = P(a), P(b)
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="{w}" stroke-linecap="round"/>')
            if kind == "wall":
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                dx, dy = x2 - x1, y2 - y1
                n = math.hypot(dx, dy) or 1
                ox, oy = -dy / n * 16, dx / n * 16
                out.append(f'<text x="{mx + ox:.1f}" y="{my + oy:.1f}" font-size="13" font-weight="600" fill="#111827" '
                           f'text-anchor="middle" dominant-baseline="middle">{length:.2f} m</text>')
        st = self.stats()
        info = []
        if st["area"]:
            info.append(f"{st['area']} m²")
        if st["size"]:
            info.append(f"{st['size'][0]} × {st['size'][1]} m")
        if st["height"]:
            info.append(f"h {st['height']} m")
        out.append(f'<text x="{M:.0f}" y="{M / 2:.0f}" font-size="15" font-weight="700" fill="#111827">GimbalScan · {" · ".join(info)}</text>')
        out.append("</svg>")
        return "\n".join(out)
