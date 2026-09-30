from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from api_client import ApiConfig, DeepSeekClient, TokenUsage
from hotkeys import GlobalHotkeyManager, parse_hotkey
from ocr_utils import recognize_png
from screen_utils import (
    ScreenRegion,
    capture_region_png,
    enable_per_monitor_dpi_awareness,
    virtual_screen_bounds,
)


ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
CONTEXT_PATH = ROOT / "context.txt"
USAGE_LOG_PATH = ROOT / "usage_log.jsonl"


def load_json(path: Path, default: dict) -> dict:
    """读取 JSON；损坏时明确报错，不静默覆盖原文件。"""
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法读取 {path.name}：{exc}") from exc


def write_json(path: Path, data: dict) -> None:
    """以 UTF-8 写入 JSON。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_text_file(path: Path) -> str:
    """读取常见中英文文本编码。"""
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", raw, 0, min(1, len(raw)), f"无法识别文件编码：{path}")


class OutputBubble:
    """可拖动、始终置顶的独立输出组件。"""

    def __init__(self, app: "QuickKnowledgeApp") -> None:
        self.app = app
        cfg = app.config.get("ui", {})
        self.width = int(cfg.get("output_width", 440))
        self.height = int(cfg.get("output_height", 190))
        self._drag_origin = None
        self._stream_started = False

        self.window = tk.Toplevel(app.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="#1e1f22")

        top = tk.Frame(self.window, bg="#2b2d31", height=30)
        top.pack(fill="x")
        top.pack_propagate(False)
        self.title_label = tk.Label(
            top,
            text="快速知识查询",
            bg="#2b2d31",
            fg="#f2f3f5",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        )
        self.title_label.pack(side="left", fill="both", expand=True, padx=(10, 4))
        copy_btn = tk.Button(
            top,
            text="复制",
            command=self.copy_text,
            relief="flat",
            bd=0,
            bg="#2b2d31",
            fg="#d8d9dc",
            activebackground="#3b3d43",
            activeforeground="white",
            font=("Microsoft YaHei UI", 8),
        )
        copy_btn.pack(side="right", padx=3)
        hide_btn = tk.Button(
            top,
            text="×",
            command=self.window.withdraw,
            relief="flat",
            bd=0,
            bg="#2b2d31",
            fg="#d8d9dc",
            activebackground="#d83c3e",
            activeforeground="white",
            font=("Microsoft YaHei UI", 10),
            width=3,
        )
        hide_btn.pack(side="right")

        self.text = tk.Text(
            self.window,
            wrap="word",
            bd=0,
            relief="flat",
            bg="#1e1f22",
            fg="#f2f3f5",
            insertbackground="white",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=10,
            spacing1=2,
            spacing3=2,
        )
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")

        for widget in (top, self.title_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

    def _initial_position(self) -> tuple[int, int]:
        pos = self.app.state.get("output_position") or {}
        if "x" in pos and "y" in pos:
            return int(pos["x"]), int(pos["y"])
        bounds = virtual_screen_bounds()
        return bounds.x + bounds.width - self.width - 24, bounds.y + 24

    def show_waiting(self) -> None:
        """显示悬浮窗并进入等待流式内容状态。"""
        x, y = self._initial_position()
        self.window.geometry(f"{self.width}x{self.height}{x:+d}{y:+d}")
        self._replace_text("正在查询…")
        self._stream_started = False
        self.window.deiconify()
        self.window.lift()

    def append_delta(self, delta: str) -> None:
        """追加流式文本。"""
        if not self._stream_started:
            self._replace_text("")
            self._stream_started = True
        self.text.configure(state="normal")
        self.text.insert("end", delta)
        self.text.see("end")
        self.text.configure(state="disabled")

    def show_error(self, error: str) -> None:
        """把明确错误展示在悬浮窗中。"""
        self._replace_text("错误：" + error)
        self.window.deiconify()
        self.window.lift()

    def _replace_text(self, value: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.configure(state="disabled")

    def copy_text(self) -> None:
        value = self.text.get("1.0", "end-1c")
        self.window.clipboard_clear()
        self.window.clipboard_append(value)

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y())

    def _drag_move(self, event) -> None:
        if not self._drag_origin:
            return
        sx, sy, wx, wy = self._drag_origin
        nx = wx + event.x_root - sx
        ny = wy + event.y_root - sy
        self.window.geometry(f"{nx:+d}{ny:+d}")

    def _drag_end(self, _event) -> None:
        self._drag_origin = None
        self.app.state["output_position"] = {
            "x": self.window.winfo_x(),
            "y": self.window.winfo_y(),
        }
        self.app.save_state()


class InputPopup:
    """快捷键 1 使用的小型文字输入框。"""

    def __init__(self, app: "QuickKnowledgeApp") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("快速询问")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)
        self.window.geometry(self._geometry_near_pointer())

        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)
        self.entry = ttk.Entry(frame, width=54, font=("Microsoft YaHei UI", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(frame, text="询问", command=self.submit, width=8).pack(side="left", padx=(8, 0))
        self.entry.bind("<Return>", lambda _e: self.submit())
        self.entry.bind("<Escape>", lambda _e: self.window.destroy())
        self.entry.focus_force()

    def _geometry_near_pointer(self) -> str:
        self.app.root.update_idletasks()
        x = self.app.root.winfo_pointerx()
        y = self.app.root.winfo_pointery()
        return f"500x62{x - 250:+d}{y - 31:+d}"

    def submit(self) -> None:
        question = self.entry.get().strip()
        if not question:
            return
        self.window.destroy()
        self.app.ask_text(question)


class ApiKeyDialog:
    """让用户在应用内填写本次运行使用的 DeepSeek API Key。"""

    def __init__(self, app: "QuickKnowledgeApp") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("设置 API Key")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)
        self.window.transient(app.root)

        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="DeepSeek API Key", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")

        env_name = app.api.config.api_key_env
        env_key = app.api.get_environment_api_key()
        hint = (
            f"已从环境变量 {env_name} 自动填入。"
            if env_key
            else f"未检测到环境变量 {env_name}，请手动填写。"
        )
        ttk.Label(frame, text=hint).pack(anchor="w", pady=(6, 8))

        self.entry = ttk.Entry(frame, width=54, show="•", font=("Microsoft YaHei UI", 10))
        self.entry.pack(fill="x")
        current_key = app.api.get_api_key()
        if current_key:
            self.entry.insert(0, current_key)
        self.entry.selection_range(0, "end")

        self.show_value = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="显示 API Key",
            variable=self.show_value,
            command=self._toggle_visibility,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, text="API Key 仅保存在本次运行的内存中，不写入配置文件或磁盘。", foreground="#666666").pack(
            anchor="w", pady=(6, 10)
        )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="使用此 Key", command=self.submit).pack(side="right", padx=(0, 6))

        self.entry.bind("<Return>", lambda _e: self.submit())
        self.entry.bind("<Escape>", lambda _e: self.window.destroy())
        self.window.grab_set()
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        self.entry.focus_force()

    def _toggle_visibility(self) -> None:
        self.entry.configure(show="" if self.show_value.get() else "•")

    def submit(self) -> None:
        self.app.apply_api_key(self.entry.get())
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class HotkeyRecorder:
    """点击“修改”后录制一次键盘组合，并把标准化结果回传给主界面。"""

    MODIFIER_KEYSYMS = {
        "Control_L": "Ctrl",
        "Control_R": "Ctrl",
        "Alt_L": "Alt",
        "Alt_R": "Alt",
        "Shift_L": "Shift",
        "Shift_R": "Shift",
        "Win_L": "Win",
        "Win_R": "Win",
        "Super_L": "Win",
        "Super_R": "Win",
    }
    KEY_NAMES = {
        "Return": "Enter",
        "space": "Space",
        "Tab": "Tab",
        "BackSpace": "Backspace",
        "Delete": "Delete",
        "Insert": "Insert",
        "Home": "Home",
        "End": "End",
        "Prior": "PageUp",
        "Next": "PageDown",
        "Left": "Left",
        "Right": "Right",
        "Up": "Up",
        "Down": "Down",
        "semicolon": ";",
        "equal": "=",
        "comma": ",",
        "minus": "-",
        "period": ".",
        "slash": "/",
        "grave": "`",
        "bracketleft": "[",
        "backslash": "\\",
        "bracketright": "]",
        "apostrophe": "'",
    }
    MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")

    def __init__(self, app: "QuickKnowledgeApp", action: str, action_label: str) -> None:
        self.app = app
        self.action = action
        self.action_label = action_label
        self.modifiers: set[str] = set()
        self.finished = False

        self.window = tk.Toplevel(app.root)
        self.window.title("录制快捷键")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"正在修改：{action_label}",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        self.hint = ttk.Label(frame, text="请直接按下新的快捷键组合，例如 Ctrl + Shift + Q")
        self.hint.pack(anchor="w", pady=(10, 12))
        ttk.Button(frame, text="取消", command=self.cancel).pack(anchor="e")

        self.window.bind("<KeyPress>", self._on_key_press)
        self.window.bind("<KeyRelease>", self._on_key_release)
        self.window.grab_set()
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        self.window.focus_force()

    def _on_key_press(self, event) -> str:
        keysym = event.keysym
        modifier = self.MODIFIER_KEYSYMS.get(keysym)
        if modifier:
            self.modifiers.add(modifier)
            self._refresh_hint()
            return "break"

        if keysym == "Escape" and not self.modifiers:
            self.cancel()
            return "break"

        key_name = self._normalize_key(keysym, event.char)
        if not key_name:
            self.hint.configure(text=f"暂不支持这个按键：{keysym}，请换一个。")
            return "break"

        parts = [name for name in self.MODIFIER_ORDER if name in self.modifiers]
        parts.append(key_name)
        hotkey = "+".join(parts)
        try:
            parse_hotkey(hotkey)
        except ValueError as exc:
            self.hint.configure(text=str(exc))
            return "break"

        self.finished = True
        self._close()
        self.app.apply_recorded_hotkey(self.action, hotkey)
        return "break"

    def _on_key_release(self, event) -> str:
        modifier = self.MODIFIER_KEYSYMS.get(event.keysym)
        if modifier:
            self.modifiers.discard(modifier)
            self._refresh_hint()
        return "break"

    def _refresh_hint(self) -> None:
        active = [name for name in self.MODIFIER_ORDER if name in self.modifiers]
        suffix = " + ".join(active)
        self.hint.configure(text=(suffix + " + …") if suffix else "请按下新的快捷键组合")

    def _normalize_key(self, keysym: str, char: str) -> str | None:
        if len(keysym) == 1 and keysym.isalnum():
            return keysym.upper()
        if keysym.startswith("F") and keysym[1:].isdigit():
            return keysym.upper()
        if keysym in self.KEY_NAMES:
            return self.KEY_NAMES[keysym]
        if char and char in ";=,-./`[]\\'":
            return char
        return None

    def cancel(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._close()
        self.app.cancel_hotkey_recording()

    def _close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class SelectionOverlay:
    """全虚拟桌面截图框选层。"""

    def __init__(self, app: "QuickKnowledgeApp", on_selected) -> None:
        self.app = app
        self.on_selected = on_selected
        self.bounds = virtual_screen_bounds()
        self.start = None
        self.rect_id = None

        self.window = tk.Toplevel(app.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.30)
        self.window.configure(bg="black")
        self.window.geometry(
            f"{self.bounds.width}x{self.bounds.height}{self.bounds.x:+d}{self.bounds.y:+d}"
        )

        self.canvas = tk.Canvas(self.window, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            24,
            24,
            anchor="nw",
            text="拖动框选截图区域 · Esc 取消",
            fill="white",
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("<Escape>", lambda _e: self.cancel())
        self.window.focus_force()
        self.window.grab_set()

    def _press(self, event) -> None:
        self.start = (event.x, event.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#62d4ff",
            width=4,
        )

    def _motion(self, event) -> None:
        if self.start and self.rect_id:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], event.x, event.y)

    def _release(self, event) -> None:
        if not self.start:
            self.cancel()
            return
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width < 8 or height < 8:
            self.cancel()
            return
        region = ScreenRegion(
            self.bounds.x + int(left),
            self.bounds.y + int(top),
            int(width),
            int(height),
        )
        self._finish(region)

    def cancel(self) -> None:
        self._finish(None)

    def _finish(self, region: ScreenRegion | None) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        # 给系统一点时间让半透明覆盖层完全消失，再执行截图。
        self.app.root.after(120, lambda: self.on_selected(region))


class QuickKnowledgeApp:
    """快速知识查询系统主应用。"""

    def __init__(self) -> None:
        enable_per_monitor_dpi_awareness()
        self.config = load_json(CONFIG_PATH, {})
        self.state = load_json(STATE_PATH, {})
        self.context_value = CONTEXT_PATH.read_text(encoding="utf-8") if CONTEXT_PATH.exists() else ""
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.query_lock = threading.Lock()
        self.hotkey_manager: GlobalHotkeyManager | None = None
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        self._exiting = False
        self._recording_hotkey = False

        api_cfg = ApiConfig(
            api_base=self.config.get("api_base", "https://api.deepseek.com"),
            api_key_env=self.config.get("api_key_env", "DEEPSEEK_API_KEY"),
            model=self.config.get("model", "deepseek-flash"),
            system_prompt=self.config.get(
                "system_prompt",
                "你是快速知识查询助手。优先依据已装载的知识补充回答。回答尽量控制在100个汉字以内，只保留最有用的信息；若过度压缩会导致明显错误或歧义，可以适当超过。不要复述问题，不展开无关背景，不展示推理过程。",
            ),
            screenshot_prompt=self.config.get("screenshot_prompt", "请回答截图中的问题。"),
            max_output_tokens=int(self.config.get("max_output_tokens", 400)),
        )
        self.api = DeepSeekClient(api_cfg)

        self.root = tk.Tk()
        self.root.title("快速知识查询")
        ui_cfg = self.config.get("ui", {})
        width = int(ui_cfg.get("main_width", 700))
        height = int(ui_cfg.get("main_height", 590))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(460, 340)
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

        self.output = OutputBubble(self)
        self._build_main_ui()

        self._start_hotkey_manager()
        self.root.after(30, self._pump_ui_queue)
        self.root.after(150, self._refresh_status)
        self.root.after(350, self._prompt_api_key_if_missing)

    def _build_main_ui(self) -> None:
        # 整个设置页放进 Canvas：窗口再矮也能纵向滚动到全部按钮。
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.main_canvas = canvas

        outer = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def sync_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)
            wrap = max(250, event.width - 42)
            self.status_label.configure(wraplength=wrap)
            self.context_hint.configure(wraplength=wrap)

        def on_mousewheel(event) -> None:
            # 鼠标位于内部文本框时，让文本框自己滚动；其他位置滚动整个页面。
            if event.widget in (self.context_text, self.usage_text):
                return
            if event.delta:
                canvas.yview_scroll(-1 * int(event.delta / 120), "units")

        outer.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_width)
        self.root.bind("<MouseWheel>", on_mousewheel, add="+")

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="快速知识查询", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left", anchor="w")
        ttk.Button(title_row, text="设置 API Key", command=self.show_api_key_dialog).pack(side="right")
        self.api_label = ttk.Label(outer, text="")
        self.api_label.pack(anchor="w", pady=(2, 0))

        self.context_hint = ttk.Label(
            outer,
            text="固定知识上下文（查询时实际发送为 role:'user', message:'知识补充：A'）",
            font=("Microsoft YaHei UI", 9),
            justify="left",
        )
        self.context_hint.pack(anchor="w", fill="x", pady=(10, 5))

        # 固定高度，避免文本框抢光窗口空间；内容多时文本框本身可滚动。
        self.context_text = scrolledtext.ScrolledText(
            outer,
            wrap="word",
            height=7,
            font=("Microsoft YaHei UI", 9),
        )
        self.context_text.pack(fill="x")
        if self.context_value:
            self.context_text.insert("1.0", self.context_value)

        context_box = ttk.LabelFrame(outer, text="上下文操作", padding=(8, 6))
        context_box.pack(fill="x", pady=(7, 4))
        context_actions = (
            ("装载上下文", self.load_context_files),
            ("装载文件夹", self.load_context_folder),
            ("应用当前文本", self.apply_context_text),
            ("清空上下文", self.clear_context),
        )
        for index, (text, command) in enumerate(context_actions):
            row, column = divmod(index, 2)
            ttk.Button(context_box, text=text, command=command).grid(
                row=row, column=column, sticky="ew", padx=3, pady=3
            )
        context_box.columnconfigure(0, weight=1)
        context_box.columnconfigure(1, weight=1)
        self.context_status = ttk.Label(context_box, text="")
        self.context_status.grid(row=2, column=0, columnspan=2, sticky="w", padx=3, pady=(3, 0))

        fixed_box = ttk.LabelFrame(outer, text="固定截图区域", padding=(8, 6))
        fixed_box.pack(fill="x", pady=(6, 4))
        ttk.Button(fixed_box, text="设定固定截图区域", command=self.select_fixed_region).grid(
            row=0, column=0, sticky="ew", padx=3, pady=3
        )
        ttk.Button(fixed_box, text="清除固定区域", command=self.clear_fixed_region).grid(
            row=0, column=1, sticky="ew", padx=3, pady=3
        )
        fixed_box.columnconfigure(0, weight=1)
        fixed_box.columnconfigure(1, weight=1)
        self.fixed_region_label = ttk.Label(fixed_box, text="")
        self.fixed_region_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=3, pady=(3, 0))

        ocr_box = ttk.LabelFrame(outer, text="截图处理", padding=(8, 6))
        ocr_box.pack(fill="x", pady=(6, 4))
        ocr_cfg = self.config.setdefault("ocr", {})
        saved_mode = str(ocr_cfg.get("mode", "")).strip()
        if saved_mode not in {"prefer_ocr", "image_only"}:
            saved_mode = "prefer_ocr" if bool(ocr_cfg.get("enabled", True)) else "image_only"
        self.ocr_mode_var = tk.StringVar(value=saved_mode)
        ttk.Radiobutton(
            ocr_box,
            text="优先 OCR",
            value="prefer_ocr",
            variable=self.ocr_mode_var,
            command=self.apply_ocr_mode,
        ).grid(row=0, column=0, sticky="w", padx=3, pady=3)
        ttk.Radiobutton(
            ocr_box,
            text="不使用 OCR（始终发送原图）",
            value="image_only",
            variable=self.ocr_mode_var,
            command=self.apply_ocr_mode,
        ).grid(row=1, column=0, sticky="w", padx=3, pady=3)
        ocr_box.columnconfigure(0, weight=1)
        self.ocr_mode_hint = ttk.Label(ocr_box, text="", justify="left")
        self.ocr_mode_hint.grid(row=2, column=0, sticky="w", padx=3, pady=(3, 0))
        self._refresh_ocr_mode_hint()

        hotkey_box = ttk.LabelFrame(outer, text="快捷键", padding=(8, 5))
        hotkey_box.pack(fill="x", pady=(6, 4))
        hotkeys = self.config.setdefault("hotkeys", {})
        hotkey_rows = (
            ("text", "文字快问", "Ctrl+Alt+1"),
            ("screen_select", "框选截图", "Ctrl+Alt+2"),
            ("screen_fixed", "固定区域", "Ctrl+Alt+3"),
        )
        for row, (action, label, default) in enumerate(hotkey_rows):
            hotkeys.setdefault(action, default)
            ttk.Label(hotkey_box, text=label).grid(row=row, column=0, sticky="w", padx=(3, 5), pady=3)
            var = tk.StringVar(value=hotkeys[action])
            self.hotkey_vars[action] = var
            ttk.Label(hotkey_box, textvariable=var).grid(row=row, column=1, sticky="w", padx=5, pady=3)
            ttk.Button(
                hotkey_box,
                text="修改快捷键",
                command=lambda a=action, text=label: self.record_hotkey(a, text),
            ).grid(row=row, column=2, sticky="e", padx=(5, 3), pady=3)
        hotkey_box.columnconfigure(1, weight=1)

        action_box = ttk.LabelFrame(outer, text="快速操作", padding=(8, 6))
        action_box.pack(fill="x", pady=(6, 4))
        actions = (
            ("文字快问", self.show_input_popup),
            ("截图快问", self.select_and_ask),
            ("询问固定区", self.ask_fixed_region),
            ("显示输出框", self.output.show_waiting),
            ("退出", self.exit_app),
        )
        for index, (text, command) in enumerate(actions):
            row, column = divmod(index, 3)
            ttk.Button(action_box, text=text, command=command).grid(
                row=row, column=column, sticky="ew", padx=3, pady=3
            )
        for column in range(3):
            action_box.columnconfigure(column, weight=1)

        usage_box = ttk.LabelFrame(outer, text="Token 日志", padding=(8, 6))
        usage_box.pack(fill="x", pady=(6, 4))
        usage_top = ttk.Frame(usage_box)
        usage_top.pack(fill="x")
        ttk.Label(usage_top, text="每条回复：输入 / 缓存命中输入 / 输出 token").pack(side="left")
        ttk.Button(usage_top, text="清空日志", command=self.clear_usage_log).pack(side="right")
        self.usage_text = scrolledtext.ScrolledText(
            usage_box,
            wrap="none",
            height=6,
            font=("Consolas", 9),
            state="disabled",
        )
        self.usage_text.pack(fill="x", pady=(5, 0))
        self._load_usage_log()

        self.status_label = ttk.Label(outer, text="", anchor="w", justify="left")
        self.status_label.pack(fill="x", pady=(7, 4))
        self._update_context_status()
        self._update_fixed_region_label()
        self.root.after_idle(sync_scrollregion)

    def _format_usage_line(self, record: dict) -> str:
        """把一条 token 记录格式化成主界面的一行。"""
        stamp = str(record.get("time", ""))
        mode = str(record.get("mode", "查询"))
        return (
            f"{stamp}  {mode:<10}  "
            f"输入 {int(record.get('input_tokens', 0)):,}  |  "
            f"缓存命中 {int(record.get('cached_input_tokens', 0)):,}  |  "
            f"输出 {int(record.get('output_tokens', 0)):,}"
        )

    def _append_usage_line(self, line: str) -> None:
        self.usage_text.configure(state="normal")
        self.usage_text.insert("end", line + "\n")
        self.usage_text.see("end")
        self.usage_text.configure(state="disabled")

    def _load_usage_log(self) -> None:
        """启动时加载最近 200 条 token 日志。"""
        if not USAGE_LOG_PATH.exists():
            return
        try:
            lines = USAGE_LOG_PATH.read_text(encoding="utf-8").splitlines()[-200:]
            for raw in lines:
                if raw.strip():
                    self._append_usage_line(self._format_usage_line(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001
            self._append_usage_line(f"日志读取失败：{exc}")

    def _record_usage(self, usage: TokenUsage, mode: str) -> None:
        """持久化并显示单次回复的 token 用量。"""
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
        }
        self._append_usage_line(self._format_usage_line(record))
        try:
            with USAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._append_usage_line(f"日志文件写入失败：{exc}")

    def clear_usage_log(self) -> None:
        self.usage_text.configure(state="normal")
        self.usage_text.delete("1.0", "end")
        self.usage_text.configure(state="disabled")
        try:
            USAGE_LOG_PATH.unlink(missing_ok=True)
        except OSError as exc:
            self._append_usage_line(f"日志文件删除失败：{exc}")

    def _get_ocr_mode(self) -> str:
        """返回当前截图处理模式，并兼容旧版 enabled 布尔配置。"""
        ocr_cfg = self.config.get("ocr", {})
        mode = str(ocr_cfg.get("mode", "")).strip()
        if mode in {"prefer_ocr", "image_only"}:
            return mode
        return "prefer_ocr" if bool(ocr_cfg.get("enabled", True)) else "image_only"

    def _refresh_ocr_mode_hint(self) -> None:
        """刷新主界面 OCR 模式说明。"""
        if not hasattr(self, "ocr_mode_hint"):
            return
        if self._get_ocr_mode() == "prefer_ocr":
            text = "优先 OCR：文字足够时只发送 OCR 文本；识别失败或文字过少时回退原图。"
        else:
            text = "不使用 OCR：截图始终直接发送原图，适合图表、公式、界面等需要视觉信息的内容。"
        self.ocr_mode_hint.configure(text=text)

    def apply_ocr_mode(self) -> None:
        """保存截图处理模式并立即生效。"""
        mode = self.ocr_mode_var.get()
        if mode not in {"prefer_ocr", "image_only"}:
            raise ValueError(f"不支持的 OCR 模式：{mode}")
        ocr_cfg = self.config.setdefault("ocr", {})
        ocr_cfg["mode"] = mode
        ocr_cfg["enabled"] = mode == "prefer_ocr"
        write_json(CONFIG_PATH, self.config)
        self._refresh_ocr_mode_hint()
        label = "优先 OCR" if mode == "prefer_ocr" else "不使用 OCR（始终发送原图）"
        self.status_label.configure(text=f"截图处理已切换为：{label}。")

    def show_api_key_dialog(self) -> None:
        """打开 API Key 输入窗口；环境变量存在时会自动预填。"""
        ApiKeyDialog(self)

    def apply_api_key(self, api_key: str) -> None:
        """应用仅本次进程使用的 API Key，并刷新状态。"""
        self.api.set_api_key(api_key)
        self._refresh_status()
        self.status_label.configure(
            text="API Key 已在本次运行中生效。" if self.api.has_api_key() else "API Key 已清空；查询前请重新填写。"
        )

    def _prompt_api_key_if_missing(self) -> None:
        """首次启动若没有环境变量或运行时 Key，则自动弹出填写窗口。"""
        if not self.api.has_api_key() and not self._exiting:
            self.show_api_key_dialog()

    def save_state(self) -> None:
        write_json(STATE_PATH, self.state)

    def _update_context_status(self) -> None:
        self.context_status.configure(text=f"已装载 {len(self.context_value):,} 字符")

    def _update_fixed_region_label(self) -> None:
        region = ScreenRegion.from_dict(self.state.get("fixed_region"))
        if region:
            self.fixed_region_label.configure(
                text=f"固定区：({region.x}, {region.y}) {region.width}×{region.height}"
            )
        else:
            self.fixed_region_label.configure(text="尚未设定固定区域")

    def _refresh_status(self) -> None:
        if self.api.has_api_key():
            self.api_label.configure(text="DeepSeek V4.1 Flash · API 就绪")
        else:
            self.api_label.configure(text="DeepSeek V4.1 Flash · 缺少 API Key")
        self.status_label.configure(text="软件运行中；关闭主窗口将完全退出。")

    def _start_hotkey_manager(self) -> None:
        """按当前配置重新注册全局快捷键。"""
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        self.hotkey_manager = GlobalHotkeyManager(
            dict(self.config.get("hotkeys", {})),
            lambda name: self.ui_queue.put(("hotkey", name)),
            lambda message: self.ui_queue.put(("hotkey_error", message)),
        )
        self.hotkey_manager.start()

    def record_hotkey(self, action: str, action_label: str) -> None:
        """暂停全局热键，进入一次键盘录制，避免旧快捷键在录制过程中被触发。"""
        if self._recording_hotkey:
            return
        self._recording_hotkey = True
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        self.status_label.configure(text="快捷键录制中：直接按下你想使用的新组合键。")
        HotkeyRecorder(self, action, action_label)

    def apply_recorded_hotkey(self, action: str, hotkey: str) -> None:
        """保存新快捷键并立即重新注册，无需重启应用。"""
        hotkeys = self.config.setdefault("hotkeys", {})
        for other_action, other_hotkey in hotkeys.items():
            if other_action != action and other_hotkey.lower() == hotkey.lower():
                self._recording_hotkey = False
                self._start_hotkey_manager()
                messagebox.showerror("快捷键重复", f"{hotkey} 已被另一个功能使用，请换一个。")
                return
        hotkeys[action] = hotkey
        write_json(CONFIG_PATH, self.config)
        if action in self.hotkey_vars:
            self.hotkey_vars[action].set(hotkey)
        self._recording_hotkey = False
        self._start_hotkey_manager()
        self.status_label.configure(text=f"快捷键已修改为 {hotkey}，已立即生效。")

    def cancel_hotkey_recording(self) -> None:
        self._recording_hotkey = False
        self._start_hotkey_manager()
        self.status_label.configure(text="已取消修改快捷键。")

    def load_context_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择要装载的知识文件")
        if not paths:
            return
        self._load_paths([Path(p) for p in paths])

    def load_context_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择知识文件夹")
        if not folder:
            return
        allowed = {s.lower() for s in self.config.get("context_file_extensions", [])}
        files = [p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in allowed]
        files.sort(key=lambda p: str(p).lower())
        if not files:
            messagebox.showwarning("没有文件", "该文件夹中没有找到配置允许的文本文件。")
            return
        self._load_paths(files)

    def _load_paths(self, paths: list[Path]) -> None:
        parts = []
        errors = []
        for path in paths:
            try:
                text = read_text_file(path)
                parts.append(f"===== 文件：{path.name} =====\n{text}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
        if not parts:
            messagebox.showerror("装载失败", "\n".join(errors[:8]) or "没有可装载内容。")
            return
        value = "\n\n".join(parts)
        self.context_text.delete("1.0", "end")
        self.context_text.insert("1.0", value)
        self._set_context(value)
        if errors:
            messagebox.showwarning("部分文件未装载", "\n".join(errors[:8]))

    def apply_context_text(self) -> None:
        self._set_context(self.context_text.get("1.0", "end-1c"))

    def _set_context(self, value: str) -> None:
        self.context_value = value
        CONTEXT_PATH.write_text(value, encoding="utf-8")
        self._update_context_status()
        self.status_label.configure(text=f"上下文已装载：{len(value):,} 字符。下一次询问开始生效。")

    def clear_context(self) -> None:
        self.context_text.delete("1.0", "end")
        self._set_context("")

    def show_input_popup(self) -> None:
        InputPopup(self)

    def select_and_ask(self) -> None:
        self.status_label.configure(text="请拖动框选要询问的截图区域。")
        SelectionOverlay(self, self._after_select_for_query)

    def _after_select_for_query(self, region: ScreenRegion | None) -> None:
        if not region:
            self.status_label.configure(text="已取消截图询问。")
            return
        self._ask_region(region)

    def select_fixed_region(self) -> None:
        self.status_label.configure(text="请拖动框选以后反复使用的固定截图区域。")
        SelectionOverlay(self, self._after_select_fixed)

    def _after_select_fixed(self, region: ScreenRegion | None) -> None:
        if not region:
            self.status_label.configure(text="已取消固定区域设置。")
            return
        self.state["fixed_region"] = region.to_dict()
        self.save_state()
        self._update_fixed_region_label()
        self.status_label.configure(text="固定截图区域已保存。")

    def clear_fixed_region(self) -> None:
        self.state.pop("fixed_region", None)
        self.save_state()
        self._update_fixed_region_label()

    def ask_fixed_region(self) -> None:
        region = ScreenRegion.from_dict(self.state.get("fixed_region"))
        if not region:
            self.status_label.configure(text="尚未设置固定区域：请先框选一次，框选完成后会自动发送。")
            SelectionOverlay(self, self._after_select_fixed_and_ask)
            return
        self._ask_region(region)

    def _after_select_fixed_and_ask(self, region: ScreenRegion | None) -> None:
        if not region:
            return
        self.state["fixed_region"] = region.to_dict()
        self.save_state()
        self._update_fixed_region_label()
        self._ask_region(region)

    def _ask_region(self, region: ScreenRegion) -> None:
        try:
            image_png = capture_region_png(region)
        except Exception as exc:  # noqa: BLE001
            self.output.show_error(f"截图失败：{exc}")
            return
        self._start_query(question=None, image_png=image_png)

    def ask_text(self, question: str) -> None:
        self._start_query(question=question, image_png=None)

    def _start_query(self, *, question: str | None, image_png: bytes | None) -> None:
        if not self.query_lock.acquire(blocking=False):
            self.output.show_error("上一条询问仍在生成，请稍后再试。")
            return
        self.output.show_waiting()
        self.status_label.configure(text="正在调用 DeepSeek V4.1 Flash…")

        def worker() -> None:
            send_question = question
            send_image = image_png
            mode = "文字"

            if image_png is not None and self._get_ocr_mode() == "prefer_ocr":
                self.ui_queue.put(("ocr_status", "正在用 Windows 自带 OCR 识别截图…"))
                try:
                    ocr_text = recognize_png(image_png).strip()
                    min_chars = int(self.config.get("ocr", {}).get("min_non_whitespace_chars", 4))
                    compact_length = len("".join(ocr_text.split()))
                    if compact_length >= min_chars:
                        send_question = (
                            f"{self.api.config.screenshot_prompt}\n\n"
                            "以下是截图经 Windows OCR 识别出的文字，请直接依据这些文字回答；"
                            "OCR 可能存在少量错字，请结合上下文合理理解：\n\n"
                            f"{ocr_text}"
                        )
                        send_image = None
                        mode = "截图OCR"
                        self.ui_queue.put(("ocr_status", f"Windows OCR 已识别 {compact_length:,} 个非空白字符，只发送文字以节省图片 token。"))
                    else:
                        mode = "截图原图"
                        self.ui_queue.put(("ocr_status", "OCR 文字过少，已自动回退发送原图。"))
                except Exception as exc:  # noqa: BLE001
                    mode = "截图原图"
                    self.ui_queue.put(("ocr_status", f"Windows OCR 失败，已自动回退发送原图：{exc}"))
            elif image_png is not None:
                mode = "截图原图"

            self.api.stream_answer(
                context=self.context_value,
                question=send_question,
                image_png=send_image,
                on_delta=lambda text: self.ui_queue.put(("delta", text)),
                on_done=lambda usage: self.ui_queue.put(("done", (usage, mode))),
                on_error=lambda exc: self.ui_queue.put(("query_error", str(exc))),
            )

        threading.Thread(target=worker, name="DeepSeekQuery", daemon=True).start()

    def _pump_ui_queue(self) -> None:
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                if kind == "hotkey":
                    if data == "text":
                        self.show_input_popup()
                    elif data == "screen_select":
                        self.select_and_ask()
                    elif data == "screen_fixed":
                        self.ask_fixed_region()
                elif kind == "hotkey_error":
                    self.status_label.configure(text=data)
                elif kind == "delta":
                    self.output.append_delta(str(data))
                elif kind == "ocr_status":
                    self.status_label.configure(text=str(data))
                elif kind == "done":
                    usage, mode = data
                    self._record_usage(usage, str(mode))
                    if self.query_lock.locked():
                        self.query_lock.release()
                    self.status_label.configure(text="回答完成。下一次问题仍只使用固定知识上下文，不继承本次问答。")
                elif kind == "query_error":
                    if self.query_lock.locked():
                        self.query_lock.release()
                    self.output.show_error(str(data))
                    self.status_label.configure(text="查询失败，请查看悬浮输出框中的错误。")
        except queue.Empty:
            pass
        self.root.after(30, self._pump_ui_queue)

    def exit_app(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        try:
            self.root.quit()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    QuickKnowledgeApp().run()
