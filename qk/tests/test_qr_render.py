"""终端二维码渲染测试：PNG 解码、模块采样、字符输出与降级。

测试图由本文件内的**最小 PNG 编码器**现场生成（含各种过滤器与色彩类型），
因此不依赖任何二进制夹具、也不需要网络。
"""

from __future__ import annotations

import base64
import random
import struct
import zlib
from pathlib import Path

import pytest

from qk.adapters.qr_render import (
    ANSI_QR,
    ANSI_RESET,
    BLOCK_EMPTY,
    BLOCK_FULL,
    BLOCK_LOWER,
    BLOCK_UPPER,
    PNG_SIGNATURE,
    QRImageError,
    decode_png,
    parse_data_uri,
    render_data_uri,
    render_matrix,
    render_png,
    sample_modules,
)

# --------------------------------------------------------------------------
# 最小 PNG 编码器（仅测试用）
# --------------------------------------------------------------------------


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def make_png(width: int, height: int, rows: list[bytes], *, color_type: int = 6,
             bit_depth: int = 8, interlace: int = 0,
             extra_chunks: tuple[bytes, ...] = (), filter_type: int = 0,
             previous_rows: list[bytes] | None = None) -> bytes:
    """把已经打包好的像素行封装成 PNG（默认每行 filter=0）。"""
    raw = bytearray()
    for index, row in enumerate(rows):
        raw.append(filter_type)
        if filter_type == 0:
            raw.extend(row)
        elif filter_type == 1:  # Sub
            delta = bytearray(row)
            bpp = len(row) // width
            for position in range(len(row) - 1, bpp - 1, -1):
                delta[position] = (row[position] - row[position - bpp]) & 0xFF
            raw.extend(delta)
        elif filter_type == 2:  # Up
            previous = (previous_rows or rows[:1])[index - 1] if index else bytes(len(row))
            raw.extend((row[i] - previous[i]) & 0xFF for i in range(len(row)))
        else:  # pragma: no cover - 仅用于覆盖未知过滤器
            raw.extend(row)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace))
        + b"".join(extra_chunks)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


def build_modules(size: int = 21, seed: int = 7) -> list[list[bool]]:
    """造一个"像二维码"的模块矩阵：三个 7x7 定位符 + 伪随机数据区。

    定位符周围会留出一圈**浅色分隔线**——真实二维码一定有，采样算法也依赖它
    来量准模块边长。
    """
    rng = random.Random(seed)
    grid = [[rng.random() < 0.5 for _ in range(size)] for _ in range(size)]
    for origin_x, origin_y in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                core = 2 <= x <= 4 and 2 <= y <= 4
                grid[origin_y + y][origin_x + x] = edge or core
        # 分隔线：定位符内侧各留一行/一列浅色
        for offset in range(-1, 8):
            for x, y in (
                (origin_x + offset, origin_y + 7),
                (origin_x + offset, origin_y - 1),
                (origin_x + 7, origin_y + offset),
                (origin_x - 1, origin_y + offset),
            ):
                if 0 <= x < size and 0 <= y < size:
                    grid[y][x] = False
    return grid


def modules_to_rows(grid: list[list[bool]], *, module_px: int = 4, quiet: int = 2,
                    color_type: int = 6) -> tuple[int, int, list[bytes]]:
    """把模块矩阵转成像素行（黑=0，白=255）。"""
    size = len(grid)
    side = (size + quiet * 2) * module_px
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    rows: list[bytes] = []
    for y in range(side):
        row = bytearray()
        module_y = y // module_px - quiet
        for x in range(side):
            module_x = x // module_px - quiet
            inside = 0 <= module_x < size and 0 <= module_y < size
            dark = inside and grid[module_y][module_x]
            level = 0 if dark else 255
            if color_type == 0:
                row.append(level)
            elif color_type == 2:
                row.extend((level, level, level))
            elif color_type == 4:
                row.extend((level, 255))
            else:
                row.extend((level, level, level, 255))
        rows.append(bytes(row))
    return side, channels, rows


def png_of(grid: list[list[bool]], **kwargs) -> tuple[bytes, int]:
    module_px = kwargs.pop("module_px", 4)
    quiet = kwargs.pop("quiet", 2)
    color_type = kwargs.get("color_type", 6)
    side, _channels, rows = modules_to_rows(grid, module_px=module_px, quiet=quiet,
                                            color_type=color_type)
    return make_png(side, side, rows, **kwargs), module_px


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------


def test_decode_png_rgba_roundtrip():
    grid = build_modules()
    png, module_px = png_of(grid)
    bitmap = decode_png(png)
    assert bitmap.width == bitmap.height == (21 + 4) * module_px
    # 左上定位符左上角、右上定位符右上角都应是深色（静默区分别留 2 个模块）
    assert bitmap.dark[2 * module_px][2 * module_px] is True
    assert bitmap.dark[2 * module_px][(2 + 21) * module_px - 1] is True


@pytest.mark.parametrize("color_type", [0, 2, 4, 6])
def test_decode_png_supported_color_types(color_type):
    grid = build_modules()
    png, _ = png_of(grid, color_type=color_type)
    bitmap = decode_png(png)
    matrix, modules, module_px, confidence = sample_modules(bitmap)
    assert modules == 21
    assert confidence > 0.9


def test_decode_png_palette_image():
    grid = build_modules(size=21, seed=3)
    side, _channels, rows = modules_to_rows(grid, color_type=0)
    # 调色板：索引 0=黑，索引 1=白
    palette = _chunk(b"PLTE", bytes([0, 0, 0, 255, 255, 255]))
    indexed_rows = [bytes(0 if value == 0 else 1 for value in row) for row in rows]
    png = make_png(side, side, indexed_rows, color_type=3, extra_chunks=(palette,))
    bitmap = decode_png(png)
    matrix, modules, _px, confidence = sample_modules(bitmap)
    assert modules == 21 and confidence > 0.9


def test_decode_png_applies_sub_filter():
    grid = build_modules(size=21, seed=11)
    side, _channels, rows = modules_to_rows(grid)
    png = make_png(side, side, rows, filter_type=1)
    bitmap = decode_png(png)
    _matrix, modules, _px, confidence = sample_modules(bitmap)
    assert modules == 21 and confidence > 0.9


def test_decode_png_applies_up_filter():
    grid = build_modules(size=21, seed=13)
    side, _channels, rows = modules_to_rows(grid)
    png = make_png(side, side, rows, filter_type=2, previous_rows=rows)
    bitmap = decode_png(png)
    _matrix, modules, _px, confidence = sample_modules(bitmap)
    assert modules == 21 and confidence > 0.9


def test_decode_png_rejects_non_png():
    with pytest.raises(QRImageError, match="不是 PNG"):
        decode_png(b"GIF89a-not-a-png")


def test_decode_png_rejects_interlaced():
    grid = build_modules(size=21)
    side, _channels, rows = modules_to_rows(grid)
    png = make_png(side, side, rows, interlace=1)
    with pytest.raises(QRImageError, match="隔行"):
        decode_png(png)


def test_decode_png_rejects_16bit():
    grid = build_modules(size=21)
    side, _channels, rows = modules_to_rows(grid)
    png = make_png(side, side, rows, bit_depth=16)
    with pytest.raises(QRImageError, match="位深"):
        decode_png(png)


def test_decode_png_rejects_unsupported_color_type():
    side, _channels, rows = 12, 3, [bytes(36) for _ in range(12)]
    png = make_png(side, side, rows, color_type=7)
    with pytest.raises(QRImageError, match="色彩类型"):
        decode_png(png)


def test_decode_png_rejects_flat_image():
    side = 40
    rows = [bytes([255] * (side * 4)) for _ in range(side)]
    png = make_png(side, side, rows)
    with pytest.raises(QRImageError, match="纯色"):
        decode_png(png)


def test_decode_png_rejects_truncated_data():
    grid = build_modules(size=21)
    png, _ = png_of(grid)
    broken = png[: len(png) // 2] + _chunk(b"IEND", b"")
    with pytest.raises(QRImageError):
        decode_png(broken)


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [21, 25, 29])
@pytest.mark.parametrize("module_px", [3, 4, 6])
def test_sample_modules_recovers_size_and_grid(size, module_px):
    grid = build_modules(size=size, seed=size)
    png, _ = png_of(grid, module_px=module_px)
    bitmap = decode_png(png)
    matrix, modules, estimated_px, confidence = sample_modules(bitmap)
    assert modules == size
    assert estimated_px == pytest.approx(module_px, abs=0.35)
    assert confidence > 0.9
    assert matrix == grid  # 采样结果应与原始模块矩阵完全一致


def test_sample_modules_matches_original_on_uneven_scale():
    """模块边长非整数时（真实图片 220/61≈3.6px）也不能累积漂移。

    注意：这里人为把 61 个模块压进 220 像素，模块宽在 3/4 像素之间跳变，所以
    "整格纯度"（confidence）天然偏低——它不是判据，**零错位**才是。真实服务端
    图片的模块边长是整数（3px），confidence 达 0.994。
    """
    grid = build_modules(size=61, seed=5)
    side = 220
    pixels: list[bytearray] = [bytearray() for _ in range(side)]
    for y in range(side):
        module_y = int(y * 61 / side)
        row = pixels[y]
        for x in range(side):
            module_x = int(x * 61 / side)
            dark = grid[module_y][module_x]
            row.extend((0 if dark else 255,) * 3 + (255,))
    png = make_png(side, side, [bytes(row) for row in pixels])
    bitmap = decode_png(png)
    matrix, modules, module_px, _confidence = sample_modules(bitmap)
    assert modules == 61
    assert module_px == pytest.approx(side / 61, abs=0.1)
    mismatches = sum(
        1 for y in range(61) for x in range(61) if matrix[y][x] != grid[y][x]
    )
    assert mismatches == 0


def test_sample_modules_rejects_blank_bitmap():
    from qk.adapters.qr_render import Bitmap

    bitmap = Bitmap(width=4, height=4, dark=[[False] * 4 for _ in range(4)])
    with pytest.raises(QRImageError, match="没有找到深色模块"):
        sample_modules(bitmap)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def test_render_matrix_color_uses_half_blocks():
    grid = build_modules(size=21)
    result = render_matrix(grid, quiet_zone=2, color=True)
    assert result.modules == 21
    assert result.rows == 11  # 21 个模块行 → 11 行字符
    assert result.columns == 25  # 21 + 两侧各 2 个静默模块
    assert len(result.lines) == 11
    for line in result.lines:
        assert line.startswith(ANSI_QR) and line.endswith(ANSI_RESET)
    body = "".join(line.replace(ANSI_QR, "").replace(ANSI_RESET, "") for line in result.lines)
    assert BLOCK_FULL in body
    assert set(body) <= {BLOCK_FULL, BLOCK_UPPER, BLOCK_LOWER, BLOCK_EMPTY}
    assert not result.warning


def test_render_matrix_half_mode_without_color():
    """半块模式但不开 ANSI：宽度仍是 1 字符/模块，只是没有白底黑字。"""
    grid = build_modules(size=21)
    result = render_matrix(grid, quiet_zone=2, color=False)
    assert result.columns == 25  # 21 + 两侧各 2 个静默模块
    assert result.rows == 11     # 21 个模块行 → 11 行字符
    assert ANSI_QR not in result.text()
    assert BLOCK_FULL in result.text()
    assert not result.warning


def test_render_matrix_narrow_terminal_warning():
    grid = build_modules(size=61)
    result = render_matrix(grid, quiet_zone=2, color=True, width=40)
    assert "宽度可能不足" in result.warning


def test_render_matrix_invert_swaps_ink():
    grid = build_modules(size=21, seed=21)
    normal = render_matrix(grid, color=False)
    inverted = render_matrix(grid, color=False, invert=True)
    assert normal.lines != inverted.lines
    assert inverted.lines[0].strip() != ""


def test_render_png_end_to_end():
    grid = build_modules(size=25, seed=9)
    png, module_px = png_of(grid, module_px=6)
    result = render_png(png, quiet_zone=2, color=True, width=0)
    assert result.modules == 25
    assert result.module_px == pytest.approx(module_px, abs=0.4)
    assert result.confidence > 0.9
    assert len(result.lines) == 13
    assert not result.warning


# --------------------------------------------------------------------------
# data URI
# --------------------------------------------------------------------------


def test_parse_data_uri_ok():
    png, _ = png_of(build_modules(size=21))
    data, mime = parse_data_uri(data_uri(png))
    assert data == png
    assert mime == "image/png"


@pytest.mark.parametrize(
    "uri, message",
    [
        ("https://example.invalid/qr.png", "没有返回图片形式"),
        ("data:image/png,notbase64", "非 base64"),
        ("data:image/png;base64,", "为空"),
    ],
)
def test_parse_data_uri_errors(uri, message):
    with pytest.raises(QRImageError, match=message):
        parse_data_uri(uri)


def test_render_data_uri_rejects_other_mime():
    png, _ = png_of(build_modules(size=21))
    with pytest.raises(QRImageError, match="只支持 PNG"):
        render_data_uri("data:image/jpeg;base64," + base64.b64encode(png).decode())


def test_render_data_uri_ok():
    png, _ = png_of(build_modules(size=21))
    result = render_data_uri(data_uri(png))
    assert result.modules == 21
    assert len(result.lines) == 11


# --------------------------------------------------------------------------
# 终端编码能力与真实图回归
# --------------------------------------------------------------------------

REAL_QR_PATH = Path(__file__).parent / "data" / "real_qrcode_220.png"


class _FakeStream:
    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding


@pytest.mark.parametrize(
    "encoding, half, solid",
    [
        ("utf-8", True, True),
        ("UTF-8", True, True),
        ("cp437", True, True),      # 传统 DOS 码页：三档方块字符齐全
        ("cp936", False, True),     # 中文 Windows 控制台：能打 █ 与 ▄，但打不了 ▀
        ("gbk", False, True),
        ("ascii", False, False),
        ("latin-1", False, False),
        ("cp1252", False, False),
        (None, True, True),
    ],
)
def test_stream_capabilities_probe_per_encoding(encoding, half, solid):
    from qk.adapters.qr_render import stream_capabilities

    caps = stream_capabilities(_FakeStream(encoding))
    assert caps.half_blocks is half
    assert caps.solid_block is solid
    assert caps.redacted()["encoding"] == (encoding or "utf-8")


@pytest.mark.parametrize(
    "encoding, expected_mode",
    [
        ("utf-8", "half"),
        ("cp936", "solid"),   # 关键：中文控制台不能退化成双宽 ASCII
        ("gbk", "solid"),
        ("ascii", "ascii"),
    ],
)
def test_choose_mode_matches_terminal_capability(encoding, expected_mode):
    from qk.adapters.qr_render import choose_mode, stream_capabilities

    caps = stream_capabilities(_FakeStream(encoding))
    assert choose_mode(61, quiet_zone=2, width=200, caps=caps) == expected_mode


def test_solid_mode_is_narrow_and_warns_about_stretch():
    grid = build_modules(size=61, seed=2)
    result = render_matrix(grid, quiet_zone=2, mode="solid")
    assert result.columns == 65   # 61 + 两侧各 2：绝不会变成 130 列
    assert result.rows == 61
    assert "纵向拉伸" in result.warning
    assert BLOCK_FULL in result.text()
    assert "▀" not in result.text()


def test_auto_mode_uses_solid_on_cp936_stream():
    grid = build_modules(size=61, seed=3)
    from qk.adapters.qr_render import stream_capabilities

    caps = stream_capabilities(_FakeStream("cp936"))
    result = render_matrix(grid, quiet_zone=2, mode="auto", caps=caps)
    assert result.columns == 65
    assert "▀" not in result.text()


def test_ascii_mode_rendering_is_pure_ascii():
    grid = build_modules(size=21)
    result = render_matrix(grid, quiet_zone=2, mode="ascii")
    assert result.columns == (21 + 4) * 2
    assert result.rows == 21
    assert "ASCII" in result.warning
    text = result.text()
    assert text.isascii()  # 可以用任意编码打印，不会抛 UnicodeEncodeError
    assert "##" in text
    assert BLOCK_FULL not in text


def test_ascii_mode_can_be_forced_through_render_png():
    grid = build_modules(size=25, seed=4)
    png, _ = png_of(grid, module_px=4)
    plain = render_png(png, mode="ascii")
    assert plain.text().isascii()
    assert plain.modules == 25
    colored = render_png(png, mode="half")
    assert ANSI_QR in colored.lines[0]


def test_real_qrcode_regression():
    """真实服务端二维码的回归护栏（夹具取自 2026-10 升达统一认证实测）。

    期望：220x220 RGBA、61 模块、模块边长 3.000px、采样置信度 >0.95、
    三个定位符全部重建正确、渲染尺寸稳定。采样算法以后再改动时，这条会先报警。

    夹具缺失时**直接失败**而不是跳过——否则唯一的真实图回归会静默失效。
    """
    from qk.adapters.qr_render import _finder_score

    assert REAL_QR_PATH.exists(), (
        f"缺少真实二维码夹具：{REAL_QR_PATH}（应随仓库一起提供，不能删除）"
    )
    data = REAL_QR_PATH.read_bytes()
    bitmap = decode_png(data)
    assert (bitmap.width, bitmap.height) == (220, 220)

    matrix, modules, module_px, confidence = sample_modules(bitmap)
    assert modules == 61
    assert module_px == pytest.approx(3.0, abs=0.05)
    assert confidence > 0.95
    assert _finder_score(matrix) == pytest.approx(1.0, abs=0.02)

    ascii_result = render_png(data, quiet_zone=2, mode="ascii")
    assert (ascii_result.columns, ascii_result.rows) == ((61 + 4) * 2, 61)
    solid_result = render_png(data, quiet_zone=2, mode="solid")
    assert (solid_result.columns, solid_result.rows) == (65, 61)
    colored = render_png(data, quiet_zone=2, mode="half")
    assert (colored.columns, colored.rows) == (65, 31)
    assert colored.lines[0].startswith(ANSI_QR)
    assert not colored.warning
