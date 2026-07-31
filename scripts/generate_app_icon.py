#!/usr/bin/env python3
from __future__ import annotations

import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICONSET = ASSETS / "app-icon.iconset"
SIZE = 1024


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[index] * (1 - t) + b[index] * t) for index in range(3))


def blend_pixel(pixels: bytearray, width: int, x: int, y: int, color: tuple[int, int, int, int], alpha_scale: float = 1.0) -> None:
    if x < 0 or y < 0 or x >= width:
        return
    index = (y * width + x) * 4
    if index < 0 or index + 3 >= len(pixels):
        return
    sr, sg, sb, sa = color
    sa = clamp((sa / 255.0) * alpha_scale)
    if sa <= 0:
        return
    dr, dg, db, da = pixels[index], pixels[index + 1], pixels[index + 2], pixels[index + 3] / 255.0
    out_a = sa + da * (1 - sa)
    if out_a <= 0:
        return
    pixels[index] = round((sr * sa + dr * da * (1 - sa)) / out_a)
    pixels[index + 1] = round((sg * sa + dg * da * (1 - sa)) / out_a)
    pixels[index + 2] = round((sb * sa + db * da * (1 - sa)) / out_a)
    pixels[index + 3] = round(out_a * 255)


def rounded_rect_distance(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radius: float) -> float:
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    hx = (x1 - x0) / 2 - radius
    hy = (y1 - y0) / 2 - radius
    qx = abs(x - cx) - hx
    qy = abs(y - cy) - hy
    outside = math.hypot(max(qx, 0), max(qy, 0))
    inside = min(max(qx, qy), 0)
    return outside + inside - radius


def draw_rounded_rect(
    pixels: bytearray,
    width: int,
    height: int,
    box: tuple[float, float, float, float],
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    x0, y0, x1, y1 = box
    for y in range(max(0, math.floor(y0 - 2)), min(height, math.ceil(y1 + 2))):
        for x in range(max(0, math.floor(x0 - 2)), min(width, math.ceil(x1 + 2))):
            distance = rounded_rect_distance(x + 0.5, y + 0.5, x0, y0, x1, y1, radius)
            alpha = clamp(0.5 - distance)
            blend_pixel(pixels, width, x, y, color, alpha)


def draw_rounded_rect_gradient(
    pixels: bytearray,
    width: int,
    height: int,
    box: tuple[float, float, float, float],
    radius: float,
    start: str,
    end: str,
) -> None:
    x0, y0, x1, y1 = box
    start_rgb = hex_rgb(start)
    end_rgb = hex_rgb(end)
    for y in range(max(0, math.floor(y0 - 2)), min(height, math.ceil(y1 + 2))):
        for x in range(max(0, math.floor(x0 - 2)), min(width, math.ceil(x1 + 2))):
            distance = rounded_rect_distance(x + 0.5, y + 0.5, x0, y0, x1, y1, radius)
            alpha = clamp(0.5 - distance)
            if alpha <= 0:
                continue
            t = clamp(((x - x0) / (x1 - x0) * 0.42) + ((y - y0) / (y1 - y0) * 0.58))
            r, g, b = mix(start_rgb, end_rgb, t)
            blend_pixel(pixels, width, x, y, (r, g, b, 255), alpha)


def draw_ellipse(
    pixels: bytearray,
    width: int,
    height: int,
    box: tuple[float, float, float, float],
    color: tuple[int, int, int, int],
) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    rx = (x1 - x0) / 2
    ry = (y1 - y0) / 2
    edge_scale = min(rx, ry)
    for y in range(max(0, math.floor(y0 - 2)), min(height, math.ceil(y1 + 2))):
        for x in range(max(0, math.floor(x0 - 2)), min(width, math.ceil(x1 + 2))):
            dx = (x + 0.5 - cx) / rx
            dy = (y + 0.5 - cy) / ry
            distance = (math.hypot(dx, dy) - 1) * edge_scale
            alpha = clamp(0.5 - distance)
            blend_pixel(pixels, width, x, y, color, alpha)


def point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i, point in enumerate(points):
        xi, yi = point
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            x_at_y = (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi
            if x < x_at_y:
                inside = not inside
        j = i
    return inside


def distance_to_segment(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = x - ax, y - ay
    length_sq = vx * vx + vy * vy
    if length_sq == 0:
        return math.hypot(x - ax, y - ay)
    t = clamp((wx * vx + wy * vy) / length_sq)
    px, py = ax + t * vx, ay + t * vy
    return math.hypot(x - px, y - py)


def draw_polygon(
    pixels: bytearray,
    width: int,
    height: int,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
) -> None:
    min_x = max(0, math.floor(min(point[0] for point in points) - 2))
    max_x = min(width, math.ceil(max(point[0] for point in points) + 2))
    min_y = max(0, math.floor(min(point[1] for point in points) - 2))
    max_y = min(height, math.ceil(max(point[1] for point in points) + 2))
    for y in range(min_y, max_y):
        for x in range(min_x, max_x):
            px, py = x + 0.5, y + 0.5
            inside = point_in_polygon(px, py, points)
            edge = min(
                distance_to_segment(px, py, *points[index], *points[(index + 1) % len(points)])
                for index in range(len(points))
            )
            alpha = 1.0 if inside else clamp(0.5 - edge)
            blend_pixel(pixels, width, x, y, color, alpha)


def draw_icon(size: int) -> bytearray:
    pixels = bytearray(size * size * 4)
    scale = size / SIZE

    def sbox(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        return tuple(value * scale for value in values)

    def spoints(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * scale, y * scale) for x, y in points]

    def scolor(hex_value: str, alpha: int = 255) -> tuple[int, int, int, int]:
        return (*hex_rgb(hex_value), alpha)

    draw_rounded_rect(pixels, size, size, sbox((112, 122, 912, 934)), 204 * scale, (1, 15, 38, 46))
    draw_rounded_rect_gradient(pixels, size, size, sbox((94, 78, 930, 914)), 206 * scale, "#146EA8", "#062F69")
    draw_ellipse(pixels, size, size, sbox((146, 98, 878, 850)), (30, 128, 190, 34))

    draw_polygon(pixels, size, size, spoints([(324, 166), (184, 398), (398, 394)]), scolor("#0A4A88"))
    draw_polygon(pixels, size, size, spoints([(700, 166), (840, 398), (626, 394)]), scolor("#0A4A88"))
    draw_polygon(pixels, size, size, spoints([(334, 198), (226, 386), (386, 364)]), scolor("#FFFFFF"))
    draw_polygon(pixels, size, size, spoints([(690, 198), (798, 386), (638, 364)]), scolor("#FFFFFF"))
    draw_polygon(pixels, size, size, spoints([(338, 246), (266, 364), (362, 348)]), scolor("#F7C8CA", 218))
    draw_polygon(pixels, size, size, spoints([(686, 246), (758, 364), (662, 348)]), scolor("#F7C8CA", 218))
    draw_ellipse(pixels, size, size, sbox((250, 258, 774, 628)), scolor("#FFFFFF"))
    draw_ellipse(pixels, size, size, sbox((374, 438, 650, 654)), scolor("#FFFDF6"))
    draw_ellipse(pixels, size, size, sbox((372, 398, 438, 468)), scolor("#063773"))
    draw_ellipse(pixels, size, size, sbox((586, 398, 652, 468)), scolor("#063773"))
    draw_ellipse(pixels, size, size, sbox((454, 474, 570, 570)), scolor("#171E27"))

    draw_ellipse(pixels, size, size, sbox((236, 500, 788, 664)), scolor("#0A4A88"))
    draw_rounded_rect(pixels, size, size, sbox((236, 582, 788, 804)), 68 * scale, scolor("#0A4A88"))
    draw_ellipse(pixels, size, size, sbox((270, 524, 754, 632)), scolor("#FFFFFF"))
    draw_rounded_rect(pixels, size, size, sbox((282, 606, 742, 772)), 56 * scale, scolor("#E6EDF5"))
    draw_ellipse(pixels, size, size, sbox((282, 674, 742, 820)), scolor("#D5E0EC"))
    draw_rounded_rect(pixels, size, size, sbox((244, 606, 780, 658)), 26 * scale, scolor("#0A4A88"))
    draw_rounded_rect(pixels, size, size, sbox((244, 718, 780, 770)), 26 * scale, scolor("#0A4A88"))
    draw_rounded_rect(pixels, size, size, sbox((382, 672, 666, 724)), 26 * scale, scolor("#FFFFFF", 245))
    draw_polygon(pixels, size, size, spoints([(328, 698), (392, 654), (392, 742)]), scolor("#FFFFFF", 245))

    draw_ellipse(pixels, size, size, sbox((326, 178, 760, 846)), (255, 255, 255, 10))
    return pixels


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png_bytes(width: int, height: int, pixels: bytearray) -> bytes:
    rows = []
    stride = width * 4
    for y in range(height):
        rows.append(b"\x00" + bytes(pixels[y * stride : (y + 1) * stride]))
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)),
            png_chunk(b"IEND", b""),
        ]
    )


def save_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    path.write_bytes(png_bytes(width, height, pixels))


def save_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header_size = 6
    entry_size = 16
    offset = header_size + entry_size * len(images)
    entries = []
    payloads = []
    for size, payload in images:
        width_byte = 0 if size >= 256 else size
        height_byte = 0 if size >= 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                width_byte,
                height_byte,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        payloads.append(payload)
        offset += len(payload)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(payloads))


def downsample(source: bytearray, source_size: int, target_size: int) -> bytearray:
    if source_size == target_size:
        return bytearray(source)
    factor = source_size // target_size
    target = bytearray(target_size * target_size * 4)
    for y in range(target_size):
        for x in range(target_size):
            sums = [0, 0, 0, 0]
            for yy in range(factor):
                for xx in range(factor):
                    index = ((y * factor + yy) * source_size + (x * factor + xx)) * 4
                    for channel in range(4):
                        sums[channel] += source[index + channel]
            count = factor * factor
            target_index = (y * target_size + x) * 4
            for channel in range(4):
                target[target_index + channel] = round(sums[channel] / count)
    return target


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    ICONSET.mkdir(exist_ok=True)
    master = draw_icon(SIZE)
    save_png(ASSETS / "app-icon.png", SIZE, SIZE, master)

    icon_sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for filename, target_size in icon_sizes.items():
        save_png(ICONSET / filename, target_size, target_size, downsample(master, SIZE, target_size))

    ico_images = []
    for target_size in (16, 24, 32, 48, 64, 128, 256):
        pixels = downsample(master, SIZE, target_size)
        ico_images.append((target_size, png_bytes(target_size, target_size, pixels)))
    save_ico(ASSETS / "app-icon.ico", ico_images)

    if sys.platform == "darwin":
        subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ASSETS / "app-icon.icns")], check=True)


if __name__ == "__main__":
    main()
