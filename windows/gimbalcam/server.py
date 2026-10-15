"""HTTPS + WebSocket server the iPhone's Safari connects to (after scanning the QR code).

Phone -> PC WebSocket messages
    text   JSON: hello / recording / zoom / rec_begin {name} / rec_end {name,size}
    binary [0x01][flags][int64 ts µs][H.264 Annex-B]   live 720p preview
    binary [0x03][bytes]                              recording file chunk (MP4)
PC -> phone: text JSON commands {"cmd": "record"|"zoom"|"quality"|"keyframe"|"status", ...}
"""
import asyncio
import datetime
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
from pathlib import Path

import av
from aiohttp import WSMsgType, web

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GimbalCam"
RECORDINGS_DIR = Path.home() / "Videos" / "GimbalCam"
WEB_DIR = Path(__file__).resolve().parent / "web"
PORT = 8443


# ---------------- network helpers ----------------

def lan_ips():
    """Best-guess LAN IPv4 addresses, preferred first."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def ensure_cert(ips):
    """Self-signed certificate covering the LAN IPs (regenerated if IPs change)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    APP_DIR.mkdir(parents=True, exist_ok=True)
    cert_p, key_p, ips_p = APP_DIR / "cert.pem", APP_DIR / "key.pem", APP_DIR / "cert_ips.json"
    try:
        if cert_p.exists() and key_p.exists() and set(json.loads(ips_p.read_text())) >= set(ips):
            return cert_p, key_p
    except (OSError, ValueError):
        pass

    all_ips = sorted(set(ips) | {"127.0.0.1"})
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GimbalCam")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in all_ips]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ips_p.write_text(json.dumps(all_ips))
    return cert_p, key_p


# ---------------- server ----------------

class PhoneServer:
    """Callbacks are invoked from the server thread:
        on_frame(av.VideoFrame), on_status(str), on_json(dict), on_file(event, info), on_connection(bool)
    """

    def __init__(self, token, on_frame, on_status, on_json, on_file, on_connection):
        self.token = token
        self.on_frame = on_frame
        self.on_status = on_status
        self.on_json = on_json
        self.on_file = on_file
        self.on_connection = on_connection
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.codec = None
        self.ips = lan_ips()
        self.port = PORT
        self.stats_bytes = 0
        self.rec_fh = None
        self.rec_info = {}

    @property
    def url(self):
        return f"https://{self.ips[0]}:{self.port}/?t={self.token}"

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            cert, key = ensure_cert(self.ips)
            sslctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            sslctx.load_cert_chain(str(cert), str(key))

            app = web.Application(client_max_size=64 * 1024 * 1024)
            app.router.add_get("/", self._index)
            app.router.add_get("/ws", self._ws)
            runner = web.AppRunner(app, access_log=None)
            self.loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "0.0.0.0", self.port, ssl_context=sslctx)
            self.loop.run_until_complete(site.start())
            self.on_status("QR kodu iPhone Kamera ile tarat")
        except OSError as e:
            self.on_status(f"Sunucu başlatılamadı (port {self.port}): {e}")
            return
        self.loop.run_forever()

    async def _index(self, request):
        return web.FileResponse(WEB_DIR / "phone.html", headers={"Cache-Control": "no-store"})

    async def _ws(self, request):
        if not secrets.compare_digest(request.query.get("t", ""), self.token):
            return web.Response(status=403, text="Geçersiz QR. Bilgisayardaki QR kodu yeniden tarat.")
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=10)
        await ws.prepare(request)

        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        self.ws = ws
        self.codec = None
        self.on_connection(True)
        self.on_status(f"Telefon bağlı ({request.remote})")
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
                self.on_connection(False)
                self.on_status("Telefon bağlantısı koptu — sayfa açıksa kendiliğinden bağlanır")
        return ws

    def _on_binary(self, data: bytes):
        kind = data[0]
        if kind == 1:
            self.stats_bytes += len(data)
            codec_name = "vp8" if data[1] & 2 else "h264"
            if self.codec is None or self.codec.name != codec_name:
                self.codec = av.CodecContext.create(codec_name, "r")
            try:
                for frame in self.codec.decode(av.Packet(data[10:])):
                    self.on_frame(frame)
            except av.error.InvalidDataError:
                self.send_json({"cmd": "keyframe"})
        elif kind == 3 and self.rec_fh:
            self.rec_fh.write(memoryview(data)[1:])
            self.rec_info["received"] += len(data) - 1
            self.on_file("progress", dict(self.rec_info))

    def _on_text(self, d):
        t = d.get("type")
        if t == "rec_begin":
            if self.rec_fh and self.rec_info.get("name") == d.get("name"):
                return  # resumed after reconnect
            self._close_rec()
            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            name = os.path.basename(d.get("name") or "kayit.mp4")
            path = RECORDINGS_DIR / name
            self.rec_fh = open(path, "wb")
            self.rec_info = {"name": name, "path": str(path), "received": 0}
            self.on_file("begin", dict(self.rec_info))
        elif t == "rec_end":
            if self.rec_fh:
                self._close_rec()
                self.on_file("done", dict(self.rec_info))
        else:
            self.on_json(d)

    def _close_rec(self):
        if self.rec_fh:
            try:
                self.rec_fh.close()
            except OSError:
                pass
        self.rec_fh = None

    def send_json(self, obj):
        ws = self.ws
        if ws is None or ws.closed:
            return False
        asyncio.run_coroutine_threadsafe(ws.send_str(json.dumps(obj)), self.loop)
        return True

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed


def new_token():
    return secrets.token_urlsafe(9)
