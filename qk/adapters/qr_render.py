"""把服务端返回的二维码图片画到终端——**只用 Python 标准库**。

服务端（升达统一认证）实测返回 ``data:image/png;base64,...``，PNG 规格为
220x220、8bit、RGBA、非隔行、单 IDAT，所以这里实现一个刚好够用的 PNG 解码器：

1. 解析 IHDR / PLTE / IDAT / IEND 四类块；
2. ``zlib`` 解压 IDAT，按行做反过滤（PNG 的 5 种过滤器）；
3. 支持 8bit 的灰度(0)/RGB(2)/调色板(3)/灰度+Alpha(4)/RGBA(6)，隔行与非 8bit 明确报错；
4. 转成亮度后取阈值得到"深色/浅色"矩阵，再从左上角定位符（7 个模块宽）推算模块尺寸，
   把二维码重采样成"每模块一个字符"；
5. 用半块字符（▀/▄/█/空格）按 2 个模块行 = 1 行字符输出，纵横比正确、扫码最稳；
   默认用 ANSI 反色（白底黑模块），这样在深色主题终端里极性也正确。
"""

from __future__ import annotations

import base64
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DATA_URI_PATTERN = re.compile(r"^data:(?P<mime>[\w./+-]+)?;?(?P<encoding>base64)?,(?P<payload>.*)$", re.DOTALL)

#: ANSI：白底 + 黑字，保证二维码极性在深色/浅色终端下都正确
ANSI_RESET = "\x1b[0m"
ANSI_QR = "\x1b[47m\x1b[30m"

#: 画二维码用的半块字符（需要终端编码支持 Unicode 制表/方块字符）
BLOCK_CHARACTERS = "█▀▄"

#: 只用实心块时的字符（cp936/GBK 能打印 █ 与 ▄，但**打印不了 ▀**，实测确认）
SOLID_BLOCK = "█"

BLOCK_FULL = "█"
BLOCK_UPPER = "▀"
BLOCK_LOWER = "▄"
BLOCK_EMPTY = " "

#: 退化为纯 ASCII 时使用的笔画
ASCII_DARK = "##"
ASCII_LIGHT = "  "

#: 渲染模式
MODE_AUTO = "auto"
MODE_HALF = "half"      # ▀/▄/█：1 字符/模块、2 模块行/行，纵横比正确
MODE_SOLID = "solid"    # █：1 字符/模块、1 模块/行（窄，但纵向被拉伸 2 倍）
MODE_ASCII = "ascii"    # ##：2 字符/模块，纯 ASCII（宽一倍）


@dataclass
class StreamCapabilities:
    """当前输出流能打印哪些字符（按真实 encode 探测，不看编码名）。"""

    encoding: str
    half_blocks: bool
    solid_block: bool

    def redacted(self) -> dict[str, Any]:
        return {"encoding": self.encoding, "half_blocks": self.half_blocks,
                "solid_block": self.solid_block}


def _encodable(text: str, encoding: str) -> bool:
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def stream_capabilities(stream: object | None = None) -> StreamCapabilities:
    """探测输出流对各档字符的支持情况。

    中文 Windows 控制台通常是 cp936/GBK：它能打印 ``█`` 与 ``▄``，但**不能**打印
    ``▀``。因此不能"要么半块、要么双宽 ASCII"二选一——那样 cp936 下会退化成
    宽一倍的图案（61 模块 → 130 列），多数终端会折行，反而扫不出来。
    """
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return StreamCapabilities(
        encoding=encoding,
        half_blocks=_encodable(BLOCK_CHARACTERS, encoding),
        solid_block=_encodable(SOLID_BLOCK, encoding),
    )


def supports_block_characters(stream: object | None = None) -> bool:
    """输出流能否画半块字符（``█▀▄``）。"""
    return stream_capabilities(stream).half_blocks


def choose_mode(modules: int, *, quiet_zone: int = 2, width: int = 0,
                caps: StreamCapabilities | None = None) -> str:
    """按终端能力与宽度挑渲染模式。"""
    caps = caps or stream_capabilities()
    if caps.half_blocks:
        return MODE_HALF
    if caps.solid_block:
        # 实心块只有 1 字符/模块宽：能塞进终端就用它，避免双宽图案折行
        return MODE_SOLID
    return MODE_ASCII


class QRImageError(Exception):
    """二维码图片无法解析或无法渲染（消息可直接展示给用户）。"""


@dataclass
class Bitmap:
    """解码后的黑白位图。``dark[y][x] is True`` 表示深色（二维码模块）。"""

    width: int
    height: int
    dark: list[list[bool]] = field(default_factory=list)

    def count_dark(self) -> int:
        return sum(1 for row in self.dark for value in row if value)


@dataclass
class RenderResult:
    """渲染结果，便于测试断言与 CLI 输出。"""

    lines: list[str]
    modules: int
    module_px: float
    columns: int
    rows: int
    color: bool
    confidence: float = 0.0
    warning: str = ""

    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------


def parse_data_uri(uri: str) -> tuple[bytes, str]:
    """解析 ``data:image/png;base64,...``；返回 (字节, mime)。"""
    if not isinstance(uri, str) or not uri.startswith("data:"):
        raise QRImageError(
            "服务端没有返回图片形式的二维码（期望 data:image/png;base64,...）。"
            "请在配置里指定正确的二维码字段名（cas.wechat.content_field）。"
        )
    match = DATA_URI_PATTERN.match(uri)
    if not match:
        raise QRImageError("二维码 data URI 格式无法识别。")
    mime = (match.group("mime") or "").lower()
    payload = match.group("payload") or ""
    if match.group("encoding") != "base64":
        raise QRImageError(f"暂不支持非 base64 的 data URI（mime={mime or '未知'}）。")
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise QRImageError(f"二维码 base64 解码失败：{exc}") from exc
    if not data:
        raise QRImageError("二维码 base64 内容为空。")
    return data, mime


def decode_png(data: bytes) -> Bitmap:
    """把 PNG 字节解码成黑白位图。"""
    if not data.startswith(PNG_SIGNATURE):
        raise QRImageError("返回的图片不是 PNG 格式（缺少 PNG 文件头）。")

    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    palette: list[tuple[int, int, int]] = []
    idat = bytearray()

    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        chunk_type = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        offset += 12 + length  # 长度 + 类型 + 数据 + CRC

        if chunk_type == b"IHDR":
            if len(payload) < 13:
                raise QRImageError("PNG 的 IHDR 块长度不足。")
            header = struct.unpack(">IIBBBBB", payload[:13])
        elif chunk_type == b"PLTE":
            palette = [
                (payload[i], payload[i + 1], payload[i + 2])
                for i in range(0, len(payload) - 2, 3)
            ]
        elif chunk_type == b"IDAT":
            idat.extend(payload)
        elif chunk_type == b"IEND":
            break

    if header is None:
        raise QRImageError("PNG 缺少 IHDR 块。")
    width, height, bit_depth, color_type, compression, filter_method, interlace = header
    if interlace != 0:
        raise QRImageError("暂不支持隔行（Adam7）PNG，请改用密码登录或反馈该图片。")
    if bit_depth != 8:
        raise QRImageError(f"暂不支持 {bit_depth} 位深的 PNG（只支持 8 位）。")
    if compression != 0 or filter_method != 0:
        raise QRImageError("PNG 使用了不支持的压缩/过滤方式。")
    if not idat:
        raise QRImageError("PNG 缺少 IDAT 图像数据。")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise QRImageError(f"暂不支持该 PNG 色彩类型：{color_type}。")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise QRImageError(f"PNG 数据解压失败：{exc}") from exc

    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise QRImageError(f"PNG 像素数据不完整（期望 {expected} 字节，实际 {len(raw)}）。")

    # 反过滤：逐行处理，filter 类型 0..4
    previous = bytearray(stride)
    luminances: list[list[int]] = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        line = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        _unfilter(filter_type, line, previous, channels)
        luminances.append(_to_luminance(line, channels, color_type, palette))
        previous = line

    return _threshold(luminances)


def _unfilter(filter_type: int, line: bytearray, previous: bytearray, bpp: int) -> None:
    """就地还原一行像素（PNG 规范 9.2）。"""
    length = len(line)
    if filter_type == 0:
        return
    if filter_type == 1:  # Sub
        for index in range(bpp, length):
            line[index] = (line[index] + line[index - bpp]) & 0xFF
        return
    if filter_type == 2:  # Up
        for index in range(length):
            line[index] = (line[index] + previous[index]) & 0xFF
        return
    if filter_type == 3:  # Average
        for index in range(length):
            left = line[index - bpp] if index >= bpp else 0
            line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        return
    if filter_type == 4:  # Paeth
        for index in range(length):
            left = line[index - bpp] if index >= bpp else 0
            up = previous[index]
            up_left = previous[index - bpp] if index >= bpp else 0
            line[index] = (line[index] + _paeth(left, up, up_left)) & 0xFF
        return
    raise QRImageError(f"PNG 使用了未知的过滤器类型：{filter_type}")


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


def _to_luminance(line: bytes | bytearray, channels: int, color_type: int,
                  palette: list[tuple[int, int, int]]) -> list[int]:
    values: list[int] = []
    if color_type == 3:  # 调色板
        for index in line:
            if palette and index < len(palette):
                red, green, blue = palette[index]
                values.append((red * 299 + green * 587 + blue * 114) // 1000)
            else:
                values.append(255 if index else 0)
        return values
    for index in range(0, len(line), channels):
        chunk = line[index:index + channels]
        if color_type == 0:
            values.append(chunk[0])
        elif color_type == 4:
            values.append(_over_white(chunk[0], chunk[1]))
        elif color_type == 2:
            values.append((chunk[0] * 299 + chunk[1] * 587 + chunk[2] * 114) // 1000)
        else:  # RGBA
            luminance = (chunk[0] * 299 + chunk[1] * 587 + chunk[2] * 114) // 1000
            values.append(_over_white(luminance, chunk[3]))
    return values


def _over_white(value: int, alpha: int) -> int:
    """把带透明度的像素合成到白底上。"""
    return (value * alpha + 255 * (255 - alpha)) // 255


def _threshold(luminances: list[list[int]]) -> Bitmap:
    flat = [value for row in luminances for value in row]
    if not flat:
        raise QRImageError("PNG 没有任何像素。")
    low, high = min(flat), max(flat)
    if high - low < 16:  # 近乎纯色，无法区分模块
        raise QRImageError("二维码图片几乎是纯色，无法识别出模块。")
    threshold = (low + high) // 2
    dark = [[value < threshold for value in row] for row in luminances]
    return Bitmap(width=len(luminances[0]), height=len(luminances), dark=dark)


# --------------------------------------------------------------------------
# 重采样与渲染
# --------------------------------------------------------------------------


def _dark_bbox(bitmap: Bitmap) -> tuple[int, int, int, int]:
    left, top = bitmap.width, bitmap.height
    right = bottom = -1
    for y, row in enumerate(bitmap.dark):
        for x, value in enumerate(row):
            if not value:
                continue
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)
    if right < left or bottom < top:
        raise QRImageError("二维码图片里没有找到深色模块（图可能为空）。")
    return left, top, right, bottom


def _estimate_module_px(bitmap: Bitmap, bbox: tuple[int, int, int, int]) -> float:
    """用左上角定位符估算模块边长：定位符最上面一行的深色连续段正好是 7 个模块。

    返回**浮点**值：真实图片里模块边长常是非整数（实测 220px / 61 模块 ≈ 3.6px），
    取整会让误差沿行列方向累积，导致远角模块采样错位。
    """
    left, top, right, _bottom = bbox
    run = 0
    for x in range(left, right + 1):
        if bitmap.dark[top][x]:
            run += 1
        elif run:
            break
    if run <= 0:
        raise QRImageError("无法从定位符估算二维码模块尺寸。")
    return run / 7.0


def _nearest_qr_version(modules: float) -> int:
    """把推算出的模块数对齐到合法的二维码版本尺寸（21 + 4k）。"""
    candidates = [21 + 4 * k for k in range(0, 20)]
    best = min(candidates, key=lambda value: abs(value - modules))
    return best if abs(best - modules) <= 2 else int(round(modules))


def _qr_extent(bitmap: Bitmap, bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    """用定位符的几何关系量出二维码的横向/纵向跨度（像素）。

    不能直接用"最暗像素包围盒"：二维码最右列与最下行是数据模块，可能是浅色，
    这样会把跨度量小、模块数算少。可靠做法是利用第 0 行与第 0 列：

    * 二维码第 0 行 = 左上定位符顶边（7 个深色模块）+ … + 右上定位符顶边（7 个深色模块），
      所以该行**最后一个深色像素**就是二维码右边缘；
    * 第 0 列同理，最后一个深色像素是左下定位符的底边 = 二维码下边缘。
    """
    left, top, right, bottom = bbox
    last_dark_x = max((x for x in range(left, right + 1) if bitmap.dark[top][x]), default=left)
    last_dark_y = max((y for y in range(top, bottom + 1) if bitmap.dark[y][left]), default=top)
    return last_dark_x - left + 1, last_dark_y - top + 1


def _sample_with(bitmap: Bitmap, left: int, top: int, modules: int,
                 module_px: float) -> tuple[list[list[bool]], float]:
    """按给定模块数/模块边长采样，返回 (矩阵, 置信度)。

    每个模块取它**整个格子**的多数值（浮点边界 → 整数像素区间），而不是只看中心
    几个像素：模块边长非整数时（真实图片 220/61≈3.6px），整格多数对边界误差更稳。
    """
    matrix: list[list[bool]] = []
    agreements: list[float] = []
    for my in range(modules):
        y0 = top + my * module_px
        y1 = top + (my + 1) * module_px
        row: list[bool] = []
        for mx in range(modules):
            x0 = left + mx * module_px
            x1 = left + (mx + 1) * module_px
            value = _center_value(bitmap, x0, y0, x1, y1)
            row.append(value)
            agreements.append(_block_purity(bitmap, x0, y0, x1, y1, value))
        matrix.append(row)
    confidence = sum(agreements) / len(agreements) if agreements else 0.0
    return matrix, confidence


def _center_value(bitmap: Bitmap, x0: float, y0: float, x1: float, y1: float) -> bool:
    """取模块**中心小块**的多数值。

    中心离模块边界最远：模块边长非整数（如 3.6px）时，边界像素归属存在量化歧义，
    以整格多数取值会被相邻模块"污染"，中心取值则稳定得多。
    """
    center_x = int((x0 + x1) / 2)
    center_y = int((y0 + y1) / 2)
    patch = max(0, int((x1 - x0) / 4) - 1)
    dark = 0
    total = 0
    for y in range(center_y - patch, center_y + patch + 1):
        if not (0 <= y < bitmap.height):
            continue
        row = bitmap.dark[y]
        for x in range(center_x - patch, center_x + patch + 1):
            if not (0 <= x < bitmap.width):
                continue
            total += 1
            if row[x]:
                dark += 1
    return dark * 2 >= max(total, 1)


def _block_purity(bitmap: Bitmap, x0: float, y0: float, x1: float, y1: float,
                  value: bool) -> float:
    """整格像素与判定值一致的比例，用作"图是否清晰"的诊断（不参与判定）。"""
    x_start = max(0, int(x0))
    x_end = min(bitmap.width, max(int(x1), int(x0) + 1))
    y_start = max(0, int(y0))
    y_end = min(bitmap.height, max(int(y1), int(y0) + 1))
    hits = 0
    total = 0
    for y in range(y_start, y_end):
        row = bitmap.dark[y]
        for x in range(x_start, x_end):
            total += 1
            if row[x] == value:
                hits += 1
    return hits / total if total else 0.0


def _finder_score(matrix: list[list[bool]]) -> float:
    """按二维码结构给候选模块数打分：三个定位符必须是 7x7 同心方，周围一圈应为浅色。

    这是比"块内纯度高"可靠得多的判据——模块边长只有 3~4 像素时，块跨越真实模块
    边界会让"纯度"变低，反而可能让错误的粗糙候选得分更高。
    """
    size = len(matrix)
    if size < 21:
        return 0.0
    hits = 0
    checks = 0
    for origin_x, origin_y in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                core = 2 <= x <= 4 and 2 <= y <= 4
                expected = edge or core
                checks += 1
                if matrix[origin_y + y][origin_x + x] == expected:
                    hits += 1
        # 分隔线：定位符相邻的内侧一行/一列应为浅色
        for offset in range(-1, 8):
            for x, y in ((origin_x + offset, origin_y + 7), (origin_x + 7, origin_y + offset)):
                if 0 <= x < size and 0 <= y < size:
                    checks += 1
                    if not matrix[y][x]:
                        hits += 1
    return hits / checks if checks else 0.0


def sample_modules(bitmap: Bitmap) -> tuple[list[list[bool]], int, float, float]:
    """重采样成模块矩阵；返回 (矩阵, 模块数, 模块像素边长, 采样置信度)。

    模块数不能只靠"跨度 ÷ 模块边长"取整（像素量化会让边长估偏），也不该用采样
    纯度挑（见 :func:`_finder_score` 的说明）。做法是：对跨度附近的每个合法尺寸
    （21+4k）都真采样一次，用**定位符结构吻合度**挑最优，再用"离粗估多近"做并列微调。

    置信度 = 每个模块取其整格像素与判定结果一致的平均比例，仅作为"图是否清晰"的
    诊断与告警依据（低于约 0.85 提示可能扫不出来）。
    """
    bbox = _dark_bbox(bitmap)
    left, top, _right, _bottom = bbox
    rough_px = _estimate_module_px(bitmap, bbox)
    extent_x, extent_y = _qr_extent(bitmap, bbox)
    if extent_x <= 0 or extent_y <= 0:
        raise QRImageError("无法从定位符量出二维码跨度。")

    base = extent_x / rough_px
    candidates = [21 + 4 * k for k in range(0, 20) if abs((21 + 4 * k) - base) <= 4]
    if not candidates:
        candidates = [21 + 4 * k for k in range(0, 20) if abs((21 + 4 * k) - base) <= 8]
    if not candidates:  # 实在对不上任何合法尺寸，就用推算值本身
        candidates = [max(21, int(round(base)))]

    best: tuple[float, int, float, list[list[bool]], float] | None = None
    for modules in candidates:
        module_px = (extent_x + extent_y) / (2.0 * modules)
        if module_px < 1.0:
            continue
        matrix, confidence = _sample_with(bitmap, left, top, modules, module_px)
        structure = _finder_score(matrix)
        # 结构吻合度优先；并列时取更接近粗估（也就是模块边长更自洽）的那一档
        score = structure - 0.0005 * abs(modules - base)
        if best is None or score > best[0]:
            best = (score, modules, module_px, matrix, confidence)

    if best is None:
        raise QRImageError("无法推算二维码模块尺寸（图片过小或被裁剪）。")
    _score, modules, module_px, matrix, confidence = best
    return matrix, modules, module_px, confidence


def _majority(bitmap: Bitmap, center_x: int, center_y: int, radius: int) -> tuple[bool, float]:
    """取中心小块里的多数值，并返回"与多数一致"的像素占比。"""
    dark = 0
    total = 0
    for y in range(center_y - radius, center_y + radius + 1):
        if not (0 <= y < bitmap.height):
            continue
        for x in range(center_x - radius, center_x + radius + 1):
            if not (0 <= x < bitmap.width):
                continue
            total += 1
            if bitmap.dark[y][x]:
                dark += 1
    if total == 0:
        return False, 0.0
    is_dark = dark * 2 >= total
    agreement = (dark if is_dark else total - dark) / total
    return is_dark, agreement


def render_matrix(matrix: list[list[bool]], *, quiet_zone: int = 2, color: bool = True,
                  invert: bool = False, width: int = 0, mode: str = MODE_AUTO,
                  caps: StreamCapabilities | None = None) -> RenderResult:
    """把模块矩阵渲染成终端行。

    :param mode: ``auto``（按终端能力挑）/ ``half``（▀▄█，纵横比正确）/ ``solid``（█，窄但纵向拉伸）/
        ``ascii``（##，纯 ASCII、宽一倍）
    :param width: 终端可用列数（0 表示不校验）
    """
    modules = len(matrix)
    if mode == MODE_AUTO:
        mode = choose_mode(modules, quiet_zone=quiet_zone, width=width, caps=caps)
    if mode == MODE_HALF and not (caps or stream_capabilities()).half_blocks:
        mode = MODE_SOLID

    lines: list[str] = []
    warning = ""

    if mode == MODE_HALF:
        pad = BLOCK_EMPTY * quiet_zone
        for row_index in range(0, modules, 2):
            body: list[str] = []
            for mx in range(modules):
                upper = _ink(matrix[row_index][mx], invert)
                lower = _ink(matrix[row_index + 1][mx], invert) if row_index + 1 < modules else False
                if upper and lower:
                    body.append(BLOCK_FULL)
                elif upper:
                    body.append(BLOCK_UPPER)
                elif lower:
                    body.append(BLOCK_LOWER)
                else:
                    body.append(BLOCK_EMPTY)
            chunk = f"{pad}{''.join(body)}{pad}"
            lines.append(f"{ANSI_QR}{chunk}{ANSI_RESET}" if color else chunk)
        columns = modules + quiet_zone * 2
        rows = (modules + 1) // 2

    elif mode == MODE_SOLID:
        quiet = BLOCK_EMPTY * quiet_zone
        for row_index in range(modules):
            body = "".join(
                (BLOCK_FULL if _ink(value, invert) else BLOCK_EMPTY) for value in matrix[row_index]
            )
            lines.append(f"{quiet}{body}{quiet}")
        columns = modules + quiet_zone * 2
        rows = modules
        warning = (
            "当前终端不支持半块字符（▀），已改用实心块绘制：图案会纵向拉伸约 2 倍。"
            "若手机扫不出来，请把终端窗口调大、或改用 --qr-file 保存图片后扫码"
        )

    else:  # MODE_ASCII
        quiet = ASCII_LIGHT * quiet_zone
        for row_index in range(modules):
            body = "".join(
                (ASCII_DARK if _ink(value, invert) else ASCII_LIGHT) for value in matrix[row_index]
            )
            lines.append(f"{quiet}{body}{quiet}")
        columns = (modules + quiet_zone * 2) * 2
        rows = modules
        warning = "当前终端不支持方块字符，已退化为 ASCII 绘制；若扫不出来请放大窗口或改用 --qr-file 保存图片"

    if width and columns > width:
        hint = (
            f"终端宽度可能不足：二维码需要约 {columns} 列，当前终端 {width} 列。"
            "请放大窗口，或用 --qr-file 保存图片后用手机相册扫码。"
        )
        warning = f"{warning}；{hint}" if warning else hint

    return RenderResult(
        lines=lines, modules=modules, module_px=0.0, columns=columns, rows=rows,
        color=(mode == MODE_HALF and color), warning=warning,
    )


def _ink(dark: bool, invert: bool) -> bool:
    """在"墨色=深色模块"的假设下决定该格子是否落墨。"""
    return (not dark) if invert else dark


def render_png(data: bytes, *, quiet_zone: int = 2, color: bool = True, invert: bool = False,
               width: int = 0, mode: str = MODE_AUTO,
               caps: StreamCapabilities | None = None) -> RenderResult:
    """PNG 字节 → 终端渲染结果。

    ``mode='auto'``（默认）时按当前终端能打印的字符自动选择渲染方式。
    """
    bitmap = decode_png(data)
    matrix, modules, module_px, confidence = sample_modules(bitmap)
    result = render_matrix(matrix, quiet_zone=quiet_zone, color=color, invert=invert,
                           width=width, mode=mode, caps=caps)
    result.module_px = module_px
    result.confidence = confidence
    if confidence < 0.85:
        hint = (
            f"二维码采样置信度偏低（{confidence:.2f}），图像可能被缩放或模糊；"
            "若扫不出来可改用 --qr-file 保存原图后扫码。"
        )
        result.warning = f"{result.warning}；{hint}" if result.warning else hint
    return result


def render_data_uri(uri: str, **options: object) -> RenderResult:
    """``data:image/png;base64,...`` → 终端渲染结果。"""
    data, mime = parse_data_uri(uri)
    if mime and "png" not in mime:
        raise QRImageError(f"二维码图片格式为 {mime}，当前只支持 PNG。")
    return render_png(data, **options)  # type: ignore[arg-type]


__all__ = [
    "ANSI_QR",
    "ANSI_RESET",
    "ASCII_DARK",
    "ASCII_LIGHT",
    "BLOCK_CHARACTERS",
    "Bitmap",
    "MODE_ASCII",
    "MODE_AUTO",
    "MODE_HALF",
    "MODE_SOLID",
    "QRImageError",
    "RenderResult",
    "SOLID_BLOCK",
    "StreamCapabilities",
    "choose_mode",
    "decode_png",
    "parse_data_uri",
    "render_data_uri",
    "render_matrix",
    "render_png",
    "sample_modules",
    "stream_capabilities",
    "supports_block_characters",
]
