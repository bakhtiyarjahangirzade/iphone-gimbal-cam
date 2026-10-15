"""GimbalCam launcher: starts the core, opens the control panel as an Edge app
window, and exits cleanly when that window is closed."""
import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser

from . import panel
from .core import APP_DIR, Core

PANEL_PORT = 8765


def _port_in_use(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_edge():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("google-chrome")


def open_panel_window(url):
    """Open the panel as a standalone app window. Returns the Popen or None."""
    browser = _find_edge()
    if not browser:
        webbrowser.open(url)
        return None
    profile = APP_DIR / "panel-window"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={profile}",
        "--window-size=1480,920", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,msEdgeSidebarV2", "--disable-extensions",
    ])


def main():
    url = f"http://127.0.0.1:{PANEL_PORT}/"

    # Already running? Just show its window.
    if _port_in_use(PANEL_PORT):
        open_panel_window(url)
        return

    core = Core()
    quit_event = threading.Event()
    panel_app = panel.make_app(core, on_quit=quit_event.set)
    core.server.extra_sites.append((panel_app, "127.0.0.1", PANEL_PORT))
    core.start()

    proc = open_panel_window(url)
    started = time.monotonic()
    ever_connected = False

    # Exit when: the user pressed "Quit", or the window is closed
    # (browser process gone AND no panel connected for a few seconds).
    while not quit_event.is_set():
        time.sleep(0.5)
        clients = panel_app["clients"]
        if clients:
            ever_connected = True
            continue
        idle = time.monotonic() - panel_app["last_seen"]
        browser_gone = proc is None or proc.poll() is not None
        if ever_connected and browser_gone and idle > 3:
            break
        if ever_connected and proc is None and idle > 15:
            break
        if not ever_connected and time.monotonic() - started > 120:
            break

    try:
        core.shutdown()
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        os._exit(0)  # make sure no background thread keeps the process alive


if __name__ == "__main__":
    main()
