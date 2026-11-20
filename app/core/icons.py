"""Generates the PNG app icons.

iOS ignores SVG for `apple-touch-icon`, and pulling in Pillow just for two
static images is not worth it, so the icon is rasterised here with nothing but
`zlib`. Rendered once during the image build and cached on disk afterwards.
"""

import struct
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"
SIZES = (180, 512)
SUPERSAMPLE = 2

BG_TOP = (0x22, 0x22, 0x22)
BG_BOTTOM = (0x0A, 0x0A, 0x0A)
GREEN = (0x1D, 0xB9, 0x54)
GREEN_LIGHT = (0x4A, 0xE0, 0x7A)
GREEN_MID = (0x1E, 0xD7, 0x60)

RINGS = ((178.0, 6.0, 0.16), (138.0, 4.0, 0.24), (98.0, 2.6, 0.34))
PLAY = ((228.0, 174.0), (228.0, 338.0), (378.0, 256.0))
GLOW_CENTER = (256.0, 210.0)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[float, float, float]:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _in_triangle(x: float, y: float) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = PLAY
    denom = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if denom == 0:
        return False
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denom
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denom
    c = 1.0 - a - b
    return a >= 0 and b >= 0 and c >= 0


def _sample(x: float, y: float) -> tuple[float, float, float]:
    """Colour at a point in the 512x512 design space."""
    diagonal = (x + y) / 1024.0
    colour = _lerp(BG_TOP, BG_BOTTOM, diagonal)

    glow_dist = ((x - GLOW_CENTER[0]) ** 2 + (y - GLOW_CENTER[1]) ** 2) ** 0.5
    glow = max(0.0, 1.0 - glow_dist / 290.0) ** 2
    colour = _lerp(colour, GREEN, glow * 0.28)

    distance = ((x - 256.0) ** 2 + (y - 256.0) ** 2) ** 0.5
    for radius, width, alpha in RINGS:
        if abs(distance - radius) <= width:
            colour = _lerp(colour, GREEN_MID, alpha)

    if _in_triangle(x, y):
        colour = _lerp(GREEN_LIGHT, GREEN, (y - 174.0) / 164.0)

    return colour


def _render(size: int) -> bytes:
    """RGB pixel rows, supersampled for smooth edges."""
    scale = 512.0 / (size * SUPERSAMPLE)
    samples = SUPERSAMPLE * SUPERSAMPLE
    rows = bytearray()

    for py in range(size):
        rows.append(0)  # PNG filter type "none"
        row = bytearray()
        for px in range(size):
            red = green = blue = 0.0
            for sy in range(SUPERSAMPLE):
                y = ((py * SUPERSAMPLE) + sy + 0.5) * scale
                for sx in range(SUPERSAMPLE):
                    x = ((px * SUPERSAMPLE) + sx + 0.5) * scale
                    r, g, b = _sample(x, y)
                    red += r
                    green += g
                    blue += b
            row += bytes(
                (
                    min(255, max(0, round(red / samples))),
                    min(255, max(0, round(green / samples))),
                    min(255, max(0, round(blue / samples))),
                )
            )
        rows += row

    return bytes(rows)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _png(size: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def ensure_icons(force: bool = False) -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        target = ICON_DIR / f"icon-{size}.png"
        if target.exists() and not force:
            continue
        target.write_bytes(_png(size, _render(size)))


if __name__ == "__main__":
    ensure_icons(force=True)
    for icon_size in SIZES:
        print(f"geschrieben: {ICON_DIR / f'icon-{icon_size}.png'}")
