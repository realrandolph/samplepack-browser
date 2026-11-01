#!/usr/bin/env python3
"""System tray application for Samplepack Browser.

Uses pystray with its native GTK backend and Cinnamon popup patch on Linux,
and pystray's native Win32 backend on Windows. Provides a tray icon, opens the
browser, and offers 'Create Desktop Shortcut' and 'Shutdown' in its menu.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

# Force pystray to use GTK on Linux Mint / Cinnamon. Windows uses pystray's
# native Win32 backend, so the environment override and GTK patch stay off.
_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS and "PYSTRAY_BACKEND" not in os.environ:
    os.environ["PYSTRAY_BACKEND"] = "gtk"

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as exc:
    print(f"Error loading pystray or Pillow: {exc}", file=sys.stderr)
    print("Run launch.sh to ensure the virtual environment is set up.", file=sys.stderr)
    sys.exit(1)

from server import (
    PACK_DIR, SAMPLES_DIR, SamplepackHandler, SamplepackServer,
    create_desktop_shortcut as make_desktop_shortcut,
)
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


if not _IS_WINDOWS:
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


def _powershell_quote(value: str) -> str:
    """Quote one PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _windows_toast_classic(summary: str, body: str) -> bool:
    """Show a Windows toast through the WinRT notification APIs via PowerShell."""
    script = ";".join([
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
        "$texts = $template.GetElementsByTagName('text')",
        f"$texts.Item(0).AppendChild($template.CreateTextNode({_powershell_quote(summary)})) | Out-Null",
        f"$texts.Item(1).AppendChild($template.CreateTextNode({_powershell_quote(body)})) | Out-Null",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template)",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"{_powershell_quote(APP_TITLE)}).Show($toast)",
    ])
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return completed.returncode == 0


def notify(summary: str, body: str, icon_name: str = "audio-speakers",
           icon: pystray.Icon | None = None) -> None:
    """Send a desktop notification using the platform's native mechanism."""
    if sys.platform == "win32":
        if icon is not None:
            try:
                icon.notify(body, summary)
                return
            except Exception:
                pass
        _windows_toast_classic(summary, body)
        return
    try:
        subprocess.run(
            ["notify-send", "-a", APP_TITLE, "-i", icon_name, summary, body],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def open_browser(url: str) -> None:
    """Open a URL in the default browser on the current platform."""
    if sys.platform == "win32":
        try:
            if webbrowser.open(url):
                return
        except Exception:
            pass
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except Exception:
            pass
        return
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)


def open_folder(path: Path) -> None:
    """Open a folder in the platform file manager."""
    if sys.platform == "win32":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        except Exception:
            pass
        try:
            subprocess.Popen(["explorer", str(path)])
        except Exception:
            pass
        return
    try:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def copy_to_clipboard(text: str, icon: pystray.Icon | None = None) -> None:
    """Copy text to the system clipboard using the platform's own tools."""
    if sys.platform == "win32":
        quoted = _powershell_quote(text)
        commands = [
            (["powershell", "-NoProfile", "-NonInteractive", "-Command",
              f"Set-Clipboard -Value {quoted}"], None),
            (["clip"], text.encode("utf-8")),
        ]
        for command, payload in commands:
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE if payload else None)
                process.communicate(payload, timeout=3.0)
            except Exception:
                continue
            if process.returncode == 0:
                notify("Copied URL to Clipboard", text, "edit-copy", icon=icon)
                return
        return

    for tool in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
        try:
            p = subprocess.Popen(tool, stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"), timeout=1.0)
            notify("Copied URL to Clipboard", text, "edit-copy", icon=icon)
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
                icon=self.icon,
            )
            open_browser(self.url)
            return False

        try:
            self.httpd = SamplepackServer(("127.0.0.1", self.port), SamplepackHandler)
        except OSError as err:
            # Winsock reports WSAEADDRINUSE (10048) where POSIX reports EADDRINUSE.
            if err.errno in (errno.EADDRINUSE, 10048):
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
                notify("Startup Error", f"Could not bind to port {self.port}", "dialog-error",
                       icon=self.icon)
                return False

        # Link web UI /api/shutdown to tray shutdown
        self.httpd.shutdown_callback = self.shutdown

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, name="http-server", daemon=True)
        self.server_thread.start()
        return True

    def create_desktop_shortcut(self) -> None:
        try:
            result = make_desktop_shortcut(url=self.url)
        except Exception as exc:
            notify("Desktop shortcut failed", str(exc), "dialog-error", icon=self.icon)
        else:
            notify("Desktop shortcut created", result["path"], "application-x-executable",
                   icon=self.icon)

    def build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open in Browser", lambda icon, item: open_browser(self.url), default=True),
            pystray.MenuItem("Copy URL", lambda icon, item: copy_to_clipboard(self.url, icon=self.icon)),
            pystray.MenuItem("Open Samplepack Folder", lambda icon, item: open_folder(SAMPLES_DIR)),
            pystray.MenuItem("Create Desktop Shortcut", lambda icon, item: self.create_desktop_shortcut()),
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
            icon=self.icon,
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

        notify("Samplepack Browser stopped", "The server has been shut down.", "process-stop",
               icon=self.icon)

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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Samplepack Browser tray application")
    parser.add_argument("port", nargs="?", type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--port", dest="port_option", type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-tray", "--cli", dest="cli", action="store_true",
                        help="Run the HTTP server in the foreground without the tray")
    args = parser.parse_args(argv)
    port = args.port_option if args.port_option is not None else (args.port if args.port is not None else DEFAULT_PORT)
    if not 1 <= port <= 65535:
        print(f"invalid port: {port}", file=sys.stderr)
        return 2
    if args.cli:
        import server
        return server.main(["--port", str(port)])

    controller = SamplepackTrayController(port=port)
    return controller.run()


if __name__ == "__main__":
    sys.exit(main())
