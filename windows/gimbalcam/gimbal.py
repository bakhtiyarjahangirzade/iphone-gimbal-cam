"""Zhiyun Crane M2S control over Bluetooth LE (protocol reverse-engineered, see README)."""
import asyncio
import threading

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "0000fee9-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_UUID = "d44bc439-abfd-45a2-b575-925416129601"

AXIS_TILT = 0x01
AXIS_PAN = 0x02
AXIS_ROLL = 0x03
CENTER = 2048

MODES = {"PF": 0x00, "L": 0x01, "F": 0x02, "POV": 0x03, "GO": 0x04}


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Gimbal:
    """Thread-safe facade; BLE runs on its own asyncio loop thread.

    Velocities are in [-1, 1]. pan > 0 = right, tilt > 0 = up, roll > 0 = clockwise.
    """

    def __init__(self, on_status=None, on_info=None):
        self.on_status = on_status or (lambda s: None)
        self.on_info = on_info or (lambda d: None)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client = None
        self.connected = False
        self.seq = 0
        self.vel = {AXIS_PAN: 0.0, AXIS_TILT: 0.0, AXIS_ROLL: 0.0}
        self._idle = {AXIS_PAN: 99, AXIS_TILT: 99, AXIS_ROLL: 99}
        self._task = None

    # ---------- public API ----------

    def connect(self):
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def disconnect(self):
        asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)

    def set_velocity(self, pan=None, tilt=None, roll=None):
        for axis, v in ((AXIS_PAN, pan), (AXIS_TILT, tilt), (AXIS_ROLL, roll)):
            if v is not None:
                self.vel[axis] = max(-1.0, min(1.0, float(v)))

    def stop(self):
        self.set_velocity(0, 0, 0)

    def set_mode(self, name):
        if name in MODES:
            self._send_soon(self._packet(0x27, bytes([0x80, MODES[name], 0x00])))

    def query(self, cmd):
        self._send_soon(self._packet(cmd, b"\x00\x00\x00"))

    # ---------- internals ----------

    def _packet(self, cmd: int, data: bytes) -> bytes:
        self.seq = (self.seq + 1) & 0xFF or 1
        payload = bytes([0x18, 0x12, self.seq, 0x01, cmd]) + data
        crc = crc16_xmodem(payload)
        return b"\x24\x3c" + len(payload).to_bytes(2, "little") + payload + crc.to_bytes(2, "little")

    def _joystick(self, axis: int, v: float) -> bytes:
        value = max(0, min(4095, round(CENTER + v * 2047)))
        return self._packet(axis, bytes([0x10, value & 0xFF, value >> 8]))

    def _send_soon(self, pkt):
        if self.connected:
            asyncio.run_coroutine_threadsafe(self._write(pkt), self.loop)

    async def _write(self, pkt):
        try:
            await self.client.write_gatt_char(WRITE_UUID, pkt, response=False)
        except Exception as e:  # noqa: BLE001
            self.on_status(f"Gimbal yazma hatası: {e}")

    async def _connect(self):
        if self.connected:
            return
        self.on_status("Gimbal aranıyor…")

        def match(d, adv):
            name = (d.name or adv.local_name or "").upper()
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            return name.startswith("CRANE") or name.startswith("ZHIYUN") or SERVICE_UUID in uuids

        dev = await BleakScanner.find_device_by_filter(match, timeout=12)
        if not dev:
            self.on_status("Gimbal bulunamadı. Açık mı, telefonun Bluetooth'u / ZY Cami kapalı mı?")
            return
        self.on_status(f"Bağlanıyor: {dev.name}")
        self.client = BleakClient(dev, disconnected_callback=self._on_disconnect)
        try:
            await self.client.connect(timeout=15)
            await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        except Exception as e:  # noqa: BLE001
            self.on_status(f"Gimbal bağlantı hatası: {e}")
            return
        self.connected = True
        self.on_status(f"Gimbal bağlı: {dev.name}")
        self.query(0x04)  # firmware
        self._task = asyncio.create_task(self._send_loop())

    async def _disconnect(self):
        self.stop()
        await asyncio.sleep(0.2)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _on_disconnect(self, _client):
        self.connected = False
        self.on_status("Gimbal bağlantısı koptu")

    def _on_notify(self, _handle, data: bytearray):
        if len(data) >= 12 and data[0] == 0x24 and data[1] == 0x3E and data[4:6] == b"\x18\x12":
            cmd = data[8]
            val = int.from_bytes(data[10:12], "little")
            if cmd == 0x04:
                self.on_info({"firmware": val / 100})

    async def _send_loop(self):
        while self.connected:
            for axis in (AXIS_PAN, AXIS_TILT, AXIS_ROLL):
                v = self.vel[axis]
                if abs(v) > 0.01:
                    self._idle[axis] = 0
                    await self._write(self._joystick(axis, v))
                elif self._idle[axis] < 3:
                    self._idle[axis] += 1
                    await self._write(self._joystick(axis, 0.0))
            await asyncio.sleep(0.05)
