from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage import FileAccessMode, StorageFile


async def _recognize_file(path: Path) -> str:
    """使用 Windows.Media.Ocr 识别一个本地图片文件。"""
    storage_file = await StorageFile.get_file_from_path_async(str(path.resolve()))
    stream = await storage_file.open_async(FileAccessMode.READ)
    try:
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("Windows 没有可用的 OCR 语言包。")
        result = await engine.recognize_async(bitmap)
        lines = [line.text.strip() for line in result.lines if line.text.strip()]
        return "\n".join(lines)
    finally:
        stream.close()


def recognize_png(png_bytes: bytes) -> str:
    """识别 PNG 字节；OCR 模型完全使用 Windows 自带资源。"""
    if not png_bytes:
        raise ValueError("OCR 输入图片为空。")

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="quick_knowledge_ocr_", suffix=".png", delete=False) as handle:
            handle.write(png_bytes)
            temp_path = Path(handle.name)
        return asyncio.run(_recognize_file(temp_path))
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                # OCR 已完成；临时文件清理失败不应伪装成查询失败。
                pass
