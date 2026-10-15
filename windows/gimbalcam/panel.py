"""Local control panel: http://127.0.0.1:<port>/ (opened as an Edge app window).

WebSocket /ws:
    server -> panel   text: JSON state   |   binary: JPEG preview frame
    panel  -> server  text: JSON command (see Core.command)
"""
import asyncio
import json
import time
from pathlib import Path

from aiohttp import WSMsgType, web

WEB_DIR = Path(__file__).resolve().parent / "web"


def make_app(core, on_quit):
    app = web.Application()
    clients = set()
    app["clients"] = clients
    app["last_seen"] = time.monotonic()

    async def index(request):
        return web.FileResponse(WEB_DIR / "panel.html", headers={"Cache-Control": "no-store"})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        clients.add(ws)
        loop = asyncio.get_running_loop()
        new_jpeg = asyncio.Event()

        def notify():
            loop.call_soon_threadsafe(new_jpeg.set)
        core.jpeg_listeners.append(notify)

        async def push():
            last_version, last_state_t, last_seq = -1, 0.0, -1
            while not ws.closed:
                try:
                    await asyncio.wait_for(new_jpeg.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                new_jpeg.clear()
                now = time.monotonic()
                if core.version != last_version or now - last_state_t > 0.5:
                    last_version, last_state_t = core.version, now
                    await ws.send_str(json.dumps(core.state()))
                if core.jpeg is not None and core.jpeg_seq != last_seq and core.phone["connected"]:
                    last_seq = core.jpeg_seq
                    if ws._writer is not None and ws._writer.transport is not None \
                            and ws._writer.transport.get_write_buffer_size() > 1_000_000:
                        continue  # panel is behind: skip frame, keep it real-time
                    await ws.send_bytes(core.jpeg)

        pusher = asyncio.create_task(push())
        try:
            async for msg in ws:
                app["last_seen"] = time.monotonic()
                if msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    if cmd.get("cmd") == "quit":
                        on_quit()
                    else:
                        await loop.run_in_executor(None, core.command, cmd)
        finally:
            pusher.cancel()
            clients.discard(ws)
            app["last_seen"] = time.monotonic()
            try:
                core.jpeg_listeners.remove(notify)
            except ValueError:
                pass
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    return app
