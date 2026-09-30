from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass

from PIL import ImageGrab


@dataclass(frozen=True)
class ScreenRegion:
    """物理屏幕坐标区域。"""

    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScreenRegion | None":
        if not data:
            return None
        try:
            region = cls(
                int(data["x"]),
                int(data["y"]),
                int(data["width"]),
                int(data["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if region.width <= 0 or region.height <= 0:
            return None
        return region


def enable_per_monitor_dpi_awareness() -> None:
    """启用高 DPI 感知，减少框选坐标与截图像素错位。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def virtual_screen_bounds() -> ScreenRegion:
    """返回 Windows 虚拟桌面边界，兼容多显示器与负坐标。"""
    user32 = ctypes.windll.user32
    return ScreenRegion(
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


def capture_region_png(region: ScreenRegion) -> bytes:
    """截取区域并返回 PNG 字节。"""
    image = ImageGrab.grab(
        bbox=(
            region.x,
            region.y,
            region.x + region.width,
            region.y + region.height,
        ),
        all_screens=True,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
