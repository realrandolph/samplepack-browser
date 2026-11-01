#!/usr/bin/env python3
"""System tray application for Samplepack Browser on Linux Mint Cinnamon.

Uses pystray with the native GTK backend and Cinnamon popup patch (matching
realrandolph/mill) to display a reliable tray icon, auto-open the browser,
and provide a right-click menu with 'Shutdown'.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

# Force pystray to use GTK on Linux Mint / Cinnamon
if "PYSTRAY_BACKEND" not in os.environ:
    os.environ["PYSTRAY_BACKEND"] = "gtk"

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as exc:
    print(f"Error loading pystray or Pillow: {exc}", file=sys.stderr)
    print("Run launch.sh to ensure the virtual environment is set up.", file=sys.stderr)
    sys.exit(1)

from server import PACK_DIR, SamplepackHandler, SamplepackServer
import catalog_db

APP_TITLE = "Samplepack Browser"
DEFAULT_PORT = 12012


def _install_gtk_popup_patch(icon_class) -> bool:
    """Pass GTK's popup-menu button and timestamp through to Gtk.Menu.popup on Cinnamon."""
    if icon_class.__module__ != "pystray._gtk":
        return False
    original_popup = getattr(icon_class, "_on_status_icon_popup_menu", None)
    if original_popup is None or getattr(icon_class, "_mill_gtk_popup_patch", False):
        return False
    gtk = original_popup.__globals__.get("Gtk")
    if gtk is None:
        return False

    def _on_status_icon_popup_menu(icon, _status_icon, button, activate_time):
        if icon._menu_handle:
            icon._menu_handle.popup(
                None,
                None,
                gtk.StatusIcon.position_menu,
                icon._status_icon,
                button,
                activate_time,
            )

    icon_class._on_status_icon_popup_menu = _on_status_icon_popup_menu
    icon_class._mill_gtk_popup_patch = True
    return True


_install_gtk_popup_patch(pystray.Icon)


def create_tray_icon_image(size: int = 64) -> Image.Image:
    """Generate a crisp, high-contrast dark circular badge with a neon-acid waveform."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark background disc with subtle bright boundary
    draw.ellipse((3, 3, size - 4, size - 4), fill=(14, 18, 17, 245), outline="#3d4946", width=2)
    # Bright neon waveform bars
    bars = [(16, 16), (24, 34), (32, 24), (40, 40), (48, 18)]
    for bx, bh in bars:
        draw.line((bx, 32 - bh // 2, bx, 32 + bh // 2), fill="#d8ff3e", width=3)
    return img


def is_server_already_running(port: int) -> bool:
    """Check if our samplepack server is already serving on this port."""
    try:
        url = f"http://127.0.0.1:{port}/api/catalog"
        req = urllib.request.Request(url, headers={"User-Agent": "SamplepackTrayCheck"})
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify(summary: str, body: str, icon_name: str = "audio-speakers") -> None:
    """Send a desktop notification via notify-send if available."""
    try:
        subprocess.run(
            ["notify-send", "-a", APP_TITLE, "-i", icon_name, summary, body],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def open_browser(url: str) -> None:
    """Open URL in default web browser using xdg-open or webbrowser module."""
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)


def open_folder(path: Path) -> None:
    """Open folder in default file manager (Nemo)."""
    try:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def copy_to_clipboard(text: str) -> None:
    """Copy text to system clipboard via xclip or xsel if available."""
    for tool in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
        try:
            p = subprocess.Popen(tool, stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"), timeout=1.0)
            notify("Copied URL to Clipboard", text, "edit-copy")
            return
        except Exception:
            continue


class SamplepackTrayController:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{self.port}/"
        self.httpd: SamplepackServer | None = None
        self.server_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None
        self._shutdown_lock = threading.Lock()
        self._is_shutting_down = False

    def start_server(self) -> bool:
        catalog_db.migrate_legacy_files()
        catalog_db.generate_indexes()
        if is_server_already_running(self.port):
            notify(
                "Samplepack Browser already active",
                f"Opening existing browser at {self.url}",
                "audio-speakers",
            )
            open_browser(self.url)
            return False

        try:
            self.httpd = SamplepackServer(("127.0.0.1", self.port), SamplepackHandler)
        except OSError as err:
            if err.errno == 98:  # Address already in use
                # Try finding next port
                for p in range(self.port + 1, self.port + 10):
                    try:
                        self.httpd = SamplepackServer(("127.0.0.1", p), SamplepackHandler)
                        self.port = p
                        self.url = f"http://127.0.0.1:{self.port}/"
                        break
                    except OSError:
                        continue
            if not self.httpd:
                notify("Startup Error", f"Could not bind to port {self.port}", "dialog-error")
                return False

        # Link web UI /api/shutdown to tray shutdown
        self.httpd.shutdown_callback = self.shutdown

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, name="http-server", daemon=True)
        self.server_thread.start()
        return True

    def build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open in Browser", lambda icon, item: open_browser(self.url), default=True),
            pystray.MenuItem("Copy URL", lambda icon, item: copy_to_clipboard(self.url)),
            pystray.MenuItem("Open Samplepack Folder", lambda icon, item: open_folder(PACK_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Shutdown", lambda icon, item: self.shutdown()),
        )

    def run(self) -> int:
        if not self.start_server():
            return 0

        self.icon = pystray.Icon(
            "SamplepackBrowser",
            create_tray_icon_image(64),
            f"{APP_TITLE} (Port {self.port})",
            menu=self.build_menu(),
        )

        notify(
            "Samplepack Browser is running",
            f"Active at {self.url}\nRight-click the panel icon for menu or Shutdown.",
            "audio-speakers",
        )
        open_browser(self.url)

        # Signals
        def _handle_signal(sig, frame):
            self.shutdown()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        try:
            self.icon.run()
        except KeyboardInterrupt:
            self.shutdown()

        return 0

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._is_shutting_down:
                return
            self._is_shutting_down = True

        notify("Samplepack Browser stopped", "The server has been shut down.", "process-stop")

        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass

        if self.httpd:
            try:
                threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            except Exception:
                pass


def main() -> int:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    controller = SamplepackTrayController(port=port)
    return controller.run()


if __name__ == "__main__":
    sys.exit(main())
