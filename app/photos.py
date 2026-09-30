"""生成演示用现场照片（纯标准库合成 PNG），以及照片保存路径工具。"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

PHOTO_DIR = Path(__file__).resolve().parent.parent / "data" / "photos"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def make_sample_png(filename: str, base=(225, 228, 231), band=(43, 108, 163),
                    warn=False, w=480, h=300) -> str:
    """生成一张纯色底 + 色带的占位照片，返回相对 data/photos 的文件名。"""
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    rows = bytearray()
    for y in range(h):
        row = bytearray([0])  # filter type 0
        in_band = h * 0.58 <= y <= h * 0.74
        for x in range(w):
            if in_band:
                r, g, b = band
            elif warn and h * 0.2 <= y <= h * 0.3 and x >= w * 0.1:
                r, g, b = (232, 119, 34)
            else:
                r, g, b = base
            # 简单斜向纹理，避免照片看起来完全平
            if (x + y) % 64 < 2:
                r, g, b = max(r - 14, 0), max(g - 14, 0), max(b - 14, 0)
            row += bytes((r, g, b))
        rows += row
    raw = zlib.compress(bytes(rows), 9)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _png_chunk(b"IDAT", raw)
           + _png_chunk(b"IEND", b""))
    path = PHOTO_DIR / filename
    path.write_bytes(png)
    return filename
