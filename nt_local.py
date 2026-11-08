"""Local, read-only facts about this PC, shared by the launcher and the supervisor.

- Is NinjaTrader running, and is it sitting at its account login window?
- The launcher's config switch and heartbeat (written by launcher.py, read by sup_watch.py).

Nothing here talks to NinjaTrader or the add-on, and nothing here can start, stop or click anything. The window check
uses plain Win32 calls (no subprocess), so it cannot flash a console or steal focus from a fullscreen game.
"""
import json
import os
import subprocess
from datetime import datetime

DIR = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator")
LOGIN_TITLE = "Welcome"  # NinjaTrader 8.1 login window; it asks for the password on every launch (BUG-001)


def nt_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NinjaTrader.exe", "/NH"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except OSError:
        return False
    return "NinjaTrader.exe" in out


def login_window_open() -> bool:
    """True if a visible top-level window titled "Welcome" belongs to NinjaTrader.exe. False if anything goes wrong."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        found = []

        def exe_name(pid):
            h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                return os.path.basename(buf.value).lower() if ok else ""
            finally:
                kernel32.CloseHandle(h)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    title = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, title, n + 1)
                    if title.value.strip() == LOGIN_TITLE:
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if exe_name(pid.value) == "ninjatrader.exe":
                            found.append(hwnd)
                            return False
            return True

        user32.EnumWindows(visit, 0)
        return bool(found)
    except Exception:
        return False


def process_state() -> dict:
    """{"nt_running": bool, "login_window": bool}. The login check only runs when NinjaTrader is up."""
    running = nt_running()
    return {"nt_running": running, "login_window": running and login_window_open()}


# --- launcher files ----------------------------------------------------------------------------------------------

def launcher_config_path() -> str:
    return os.environ.get("NT_LAUNCHER_CONFIG") or os.path.join(DIR, "launcher.json")


def launcher_state_path() -> str:
    return os.environ.get("NT_LAUNCHER_STATE") or os.path.join(DIR, "launcher_state.json")


def launcher_heartbeat_path() -> str:
    return os.path.join(os.path.dirname(launcher_state_path()), "launcher_heartbeat.json")


def launcher_enabled() -> bool:
    """The launcher's master switch ("enabled" in launcher.json). A missing or unreadable file counts as off;
    a file that exists but cannot be parsed counts as on, because then the launcher is broken, not switched off."""
    path = launcher_config_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", False))
    except (OSError, ValueError, AttributeError):
        return True


def write_launcher_heartbeat(now: datetime, ok: bool, detail: str = "") -> None:
    path = launcher_heartbeat_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": now.isoformat(), "ok": ok, "detail": detail[:500]}, f)
    os.replace(tmp, path)


def read_launcher_heartbeat():
    """{"at": datetime, "ok": bool, "detail": str} or None if there is no readable heartbeat."""
    try:
        with open(launcher_heartbeat_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return {"at": datetime.fromisoformat(raw["at"]), "ok": bool(raw.get("ok")), "detail": raw.get("detail", "")}
    except (OSError, ValueError, KeyError, TypeError):
        return None
