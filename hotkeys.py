from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Dict, Tuple


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


def parse_hotkey(text: str) -> Tuple[int, int]:
    """把 Ctrl+Alt+1 这类字符串解析为 Win32 RegisterHotKey 参数。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("快捷键不能为空。")

    modifiers = MOD_NOREPEAT
    key_name = parts[-1]
    for part in parts[:-1]:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part in {"win", "windows"}:
            modifiers |= MOD_WIN
        else:
            raise ValueError(f"不支持的快捷键修饰键：{part}")

    if len(key_name) == 1 and key_name.isalnum():
        vk = ord(key_name.upper())
    elif key_name.startswith("f") and key_name[1:].isdigit():
        n = int(key_name[1:])
        if not 1 <= n <= 24:
            raise ValueError(f"不支持的功能键：{key_name}")
        vk = 0x70 + n - 1
    else:
        mapping = {
            "space": 0x20,
            "tab": 0x09,
            "enter": 0x0D,
            "backspace": 0x08,
            "esc": 0x1B,
            "escape": 0x1B,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "insert": 0x2D,
            "delete": 0x2E,
            "home": 0x24,
            "end": 0x23,
            "pageup": 0x21,
            "pagedown": 0x22,
            ";": 0xBA,
            "=": 0xBB,
            ",": 0xBC,
            "-": 0xBD,
            ".": 0xBE,
            "/": 0xBF,
            "`": 0xC0,
            "[": 0xDB,
            "\\": 0xDC,
            "]": 0xDD,
            "'": 0xDE,
        }
        if key_name not in mapping:
            raise ValueError(f"不支持的快捷键主键：{key_name}")
        vk = mapping[key_name]
    return modifiers, vk


class GlobalHotkeyManager:
    """在独立线程注册全局快捷键，并把事件转交给 UI。"""

    def __init__(
        self,
        hotkeys: Dict[str, str],
        on_trigger: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.hotkeys = hotkeys
        self.on_trigger = on_trigger
        self.on_error = on_error
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """启动快捷键监听线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="GlobalHotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止监听线程，并等待 Win32 热键完成注销。"""
        self._stop.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = None

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        id_to_name: Dict[int, str] = {}
        registered = []
        try:
            for index, (name, text) in enumerate(self.hotkeys.items(), start=101):
                try:
                    modifiers, vk = parse_hotkey(text)
                except Exception as exc:  # noqa: BLE001
                    self.on_error(f"快捷键 {text} 解析失败：{exc}")
                    continue
                ok = user32.RegisterHotKey(None, index, modifiers, vk)
                if not ok:
                    self.on_error(f"快捷键 {text} 注册失败，可能已被其他软件占用。")
                    continue
                id_to_name[index] = name
                registered.append(index)

            msg = MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    name = id_to_name.get(int(msg.wParam))
                    if name:
                        self.on_trigger(name)
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
