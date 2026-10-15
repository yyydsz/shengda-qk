"""离线 mock 教务服务：让“通用登录逻辑”在无校园网的环境下也能被完整验证。

它同时扮演两个角色（与真实链路一致）：

1. **统一身份认证（CAS）**：``GET /lyuapServer/login`` 返回登录页，
   ``POST /lyuapServer/login`` 校验账号密码后 302 回跳 ``service`` 并带上 ``ticket``，
   ``GET /lyuapServer/validate`` 校验票据；成功后签发 ``JSESSIONID`` 会话 Cookie。
2. **教务系统**：``GET /jwglxt/xsxk/xkList`` 课程列表、
   ``POST /jwglxt/xsxk/xkSelect`` 选课、``POST /jwglxt/xsxk/xkDrop`` 退课、
   ``GET /jwglxt/xsxk/selectedList`` 已选列表。没有有效会话时返回 401。

可参数化的异常分支（用于验证抢课编排的重试/跳过/重登逻辑）：

* ``full``：容量已满，永远选不上
* ``full_then_free``：前 ``free_after`` 次尝试报“人数已满”，之后放行（验证重试）
* ``already_selected``：一开始就在已选列表里（验证“已选自动跳过”）
* ``drop_fail``：退课接口返回失败（验证退课失败分支）
* 会话过期：``session_ttl`` 次请求后失效（验证自动重新登录）
* 验证码缺失/存在：``captcha_required`` 控制登录页是否带验证码

本模块**只用于本地自测**，不会被真实环境使用：请只在 ``jwgl.base_url`` 指向
``http://127.0.0.1:<port>/`` 时启动它。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field, replace
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from .adapters.jwgl import decode_kingo_params, kingo_md5

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class MockCourse:
    """mock 里的一门教学班。"""

    teaching_class_id: str
    course_code: str
    course_name: str
    teacher: str = ""
    credits: float = 2.0
    capacity: int = 50
    enrolled: int = 0
    time_text: str = ""
    #: general / full / full_then_free / already_selected / drop_fail
    behaviour: str = "general"
    #: full_then_free 用：第几次尝试之后放行
    free_after: int = 2

    def to_json(self) -> dict[str, Any]:
        return {
            "jxb_id": self.teaching_class_id,
            "kch": self.course_code,
            "kcmc": self.course_name,
            "jsxm": self.teacher,
            "xf": self.credits,
            "capacity": self.capacity,
            "selected": self.enrolled,
            "syrs": max(self.capacity - self.enrolled, 0),
            "time": self.time_text,
        }


@dataclass
class MockConfig:
    """mock 服务的可调参数。"""
    student_id: str = "20230001"
    password: str = "mock-pass"
    #: 服务基址（用于 CAS 回跳），None 表示按请求 Host 推断
    base_url: str | None = None
    #: 会话在多少次请求之后失效（0 表示不失效）
    session_ttl: int = 0
    #: 置为 True 后，**下一次**带会话的请求会被判定为失效（供测试精确制造过期）
    expire_next_request: bool = False
    #: 置为 True 后，第一次提交选课时会话失效（供 CLI 演示“会话过期→自动重登”）
    expire_on_first_select: bool = False
    #: 登录页是否要求验证码
    captcha_required: bool = False
    #: 正确的验证码；None 表示任意 4 位数字均可
    captcha_code: str | None = None
    courses: list[MockCourse] = field(default_factory=list)
    #: 已选列表（教学班号）
    selected: list[str] = field(default_factory=list)
    #: 登录请求计数与选课尝试计数，便于断言
    login_calls: int = 0
    select_attempts: dict[str, int] = field(default_factory=dict)

    # ---------------------------------------------------------------- 微信扫码
    #: ``isWeChat`` / ``isWeChatOpenLogin`` 的返回值（"1" / "0"）
    login_type_is_wechat: str = "1"
    login_type_is_wechat_open: str = "0"
    #: 前 N 次 CheckScan 返回"等待扫码"
    qr_wait_rounds: int = 1
    #: ``>0`` 时第 N 次 CheckScan 起返回"二维码已过期"
    qr_expire_rounds: int = 0
    #: ``True`` 时永远返回等待（用于验证轮询超时）
    qr_never_success: bool = False
    #: mock 生成的二维码图案（模块数与模块像素边长）
    qr_modules: int = 21
    qr_module_px: int = 4
    #: 扫码成功后返回的凭据
    scan_user: str = ""
    scan_password: str = ""
    #: CAS REST 两步建会话是否可用（False 时返回 404，用于验证 HTML 表单回退）
    rest_tickets_enabled: bool = True
    #: True 时第一步返回的 ticket 是**无效**的（用于验证"一次运行内改走第二步"）
    rest_inline_ticket_invalid: bool = False
    # ---------------------------------------------------------------- 青果 Kingosoft
    #: 写操作加密用的临时 DES 密钥与服务端时间戳（真实系统由 /frame/homepage 下发）
    kingosoft_temp_key: str = "mockkey123456"
    kingosoft_timestamp: str = "1700000000000"
    #: True 时：没先取菜单上下文就访问选课页，会返回真实系统那句「错误:无效访问请求」
    kingosoft_require_menu_context: bool = True
    #: True 时：写操作必须带加密体（params/token/timestamp），否则按真实系统语义拒绝
    kingosoft_require_encryption: bool = True
    #: 选课上下文（学年/学期/课程范围/轮次）与课程表 id，默认取实测值
    kingosoft_year: str = "2026"
    kingosoft_term: str = "0"
    kingosoft_kcfw: str = "zxggrx"
    kingosoft_lcid: str = "35732179068578980275991"
    kingosoft_table_id: str = "2568"

    @classmethod
    def default(cls, **overrides: Any) -> "MockConfig":
        """一份覆盖全部异常分支的默认课程表。"""
        courses = [
            MockCourse("JXB001", "10000001", "大学体育Ⅲ", "张老师", 1.0, 40, 12, "周一 3-4 节"),
            MockCourse("JXB002", "10000002", "大学英语Ⅱ", "李老师", 2.0, 60, 60, "周二 1-2 节", "full"),
            MockCourse(
                "JXB003", "10000003", "通识选修：音乐鉴赏", "王老师", 1.5, 30, 30,
                "周三 5-6 节", "full_then_free", free_after=2,
            ),
            MockCourse(
                "JXB004", "10000004", "程序设计基础", "赵老师", 3.0, 50, 20,
                "周四 3-4 节", "drop_ok",
            ),
            MockCourse(
                "JXB005", "10000005", "创新创业基础", "钱老师", 1.0, 45, 5,
                "周五 7-8 节", "drop_fail",
            ),
        ]
        overrides.setdefault("courses", courses)
        overrides.setdefault("selected", ["JXB004"])
        config = cls(**overrides)
        # selected 是类级默认列表，必须拷一份给实例，否则用例之间会互相污染
        config.selected = list(config.selected)
        if not config.scan_user:
            config.scan_user = config.student_id
        if not config.scan_password:
            config.scan_password = config.password
        return config


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------


class MockState:
    """线程安全的 mock 状态：会话、票据、课程与已选。"""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        #: 原始课程表副本（reset 时用来还原，避免用例之间互相污染）
        self.initial_courses = [replace(course) for course in config.courses]
        self.initial_selected = list(config.selected)
        self.lock = threading.Lock()
        self.sessions: dict[str, int] = {}  # jsessionid -> 剩余请求次数（0 表示无限）
        self.tickets: dict[str, str] = {}  # ticket -> student_id
        self.ticket_services: dict[str, str] = {}  # ticket -> 登录时携带的 service 地址
        self.credentials: list[tuple[str, str]] = []
        self.login_requests = 0
        self.select_attempts: dict[str, int] = {}
        self.extra_enrolled: dict[str, int] = {}
        self.select_expired = False  # expire_on_first_select 的一次性标记
        # 微信扫码：uuid -> 已轮询次数
        self.qr_polls: dict[str, int] = {}
        # CAS REST：TGT -> 用户名（ST 复用上面的 tickets/ticket_services）
        self.tgt_users: dict[str, str] = {}
        #: 图片验证码：uid -> 正确答案。真实系统里 `GET /lyuapServer/kaptcha` 返回
        #: 一张画着算术题的 PNG 和一个 uid，登录时要把 uid（字段 id）与答案（code）
        #: 一起提交 —— 服务端靠 uid 找到这张图对应的答案。
        self.kaptcha_answers: dict[str, str] = {}
        #: 当前该填的验证码答案（mock 可指定；None 表示随机一道两位数以内的算式）
        self.kaptcha_expected: str | None = None
        # 青果 Kingosoft：菜单上下文（真实系统里缺它就会报「无效访问请求」）
        self.kingosoft_menu_context: str = ""
        # Kingosoft 写操作的解密后的最后一条参数（供用例断言）
        self.kingosoft_last_write: str = ""

    def reset(self) -> None:
        """把状态还原到服务启动时的样子（供测试用例之间隔离使用）。"""
        with self.lock:
            self.config.courses = [replace(course) for course in self.initial_courses]
            self.config.selected = list(self.initial_selected)
            self.sessions.clear()
            self.tickets.clear()
            self.ticket_services.clear()
            self.credentials.clear()
            self.login_requests = 0
            self.select_attempts.clear()
            self.extra_enrolled.clear()
            self.select_expired = False
            self.qr_polls.clear()
            self.tgt_users.clear()
            self.kaptcha_answers.clear()
            self.kingosoft_menu_context = ""
            self.kingosoft_last_write = ""

    # ------------------------------------------------------------- 会话
    def issue_session(self) -> str:
        token = secrets.token_hex(8).upper()
        ttl = self.config.session_ttl
        with self.lock:
            self.sessions[token] = ttl
        return token

    def touch_session(self, token: str) -> bool:
        """消耗一次会话寿命；返回会话是否仍然有效。"""
        with self.lock:
            if token not in self.sessions:
                return False
            ttl = self.sessions[token]
            if ttl == 0:
                return True
            ttl -= 1
            if ttl <= 0:
                del self.sessions[token]
                return False
            self.sessions[token] = ttl
            return True

    def invalidate(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    # ------------------------------------------------------------- 课程
    def find_course(self, teaching_class_id: str) -> MockCourse | None:
        for course in self.config.courses:
            if course.teaching_class_id == teaching_class_id:
                return course
        return None

    def enrolled_count(self, course: MockCourse) -> int:
        with self.lock:
            return course.enrolled + self.extra_enrolled.get(course.teaching_class_id, 0)

    def is_selected(self, teaching_class_id: str) -> bool:
        with self.lock:
            return teaching_class_id in self.config.selected

    def mark_selected(self, teaching_class_id: str) -> None:
        with self.lock:
            if teaching_class_id not in self.config.selected:
                self.config.selected.append(teaching_class_id)

    def mark_dropped(self, teaching_class_id: str) -> None:
        with self.lock:
            if teaching_class_id in self.config.selected:
                self.config.selected.remove(teaching_class_id)

    def note_attempt(self, teaching_class_id: str) -> int:
        with self.lock:
            self.select_attempts[teaching_class_id] = self.select_attempts.get(teaching_class_id, 0) + 1
            return self.select_attempts[teaching_class_id]


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def build_qr_png(modules: int = 21, module_px: int = 4, quiet: int = 2, seed: int = 7) -> bytes:
    """生成一张"像二维码"的 PNG（三个定位符 + 分隔线 + 伪随机数据区）。

    用纯标准库写出真实的 8bit RGBA、非隔行 PNG，形态与真实服务端一致
    （实测真实图为 220x220 / 8bit / RGBA / 单 IDAT / 非隔行），因此客户端的
    解码与采样逻辑可以在 mock 上被完整验证。
    """
    size = modules
    grid = [[False] * size for _ in range(size)]
    state = seed * 2654435761 % (2**31)
    for y in range(size):
        for x in range(size):
            state = (1103515245 * state + 12345) % (2**31)
            grid[y][x] = (state >> 16) % 2 == 0
    for origin_x, origin_y in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                core = 2 <= x <= 4 and 2 <= y <= 4
                grid[origin_y + y][origin_x + x] = edge or core
        for offset in range(-1, 8):
            for x, y in ((origin_x + offset, origin_y + 7), (origin_x + offset, origin_y - 1),
                         (origin_x + 7, origin_y + offset), (origin_x - 1, origin_y + offset)):
                if 0 <= x < size and 0 <= y < size:
                    grid[y][x] = False

    side = (size + quiet * 2) * module_px
    rows: list[bytes] = []
    for y in range(side):
        module_y = y // module_px - quiet
        row = bytearray()
        for x in range(side):
            module_x = x // module_px - quiet
            inside = 0 <= module_x < size and 0 <= module_y < size
            dark = inside and grid[module_y][module_x]
            level = 0 if dark else 255
            row.extend((level, level, level, 255))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + row for row in rows)  # 每行 filter=0
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def qr_data_uri(modules: int = 21, module_px: int = 4, quiet: int = 2, seed: int = 7) -> str:
    """二维码 PNG 的 ``data:image/png;base64,...`` 形式（与真实响应一致）。"""
    png = build_qr_png(modules=modules, module_px=module_px, quiet=quiet, seed=seed)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


#: Kingosoft：缺菜单上下文时真实系统返回的页面（实测 1098 字节，标题即这句）
KINGOSOFT_INVALID_ACCESS = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
    '<html><head><base href="/"><title>错误:无效访问请求</title></head>'
    "<body>错误:无效访问请求</body></html>"
)

#: Kingosoft：选课页（真实 31481 字节；这里保留可识别的表单骨架）
KINGOSOFT_XK_PAGE = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
    '<html><head><title></title><meta http-equiv="Content-Type" content="text/html; charset=GBK" /></head>'
    '<body><form method="post" id="ActionForm" target="frmReport">'
    '<input type="hidden" id="xktype" name="xktype" value="2"/>'
    '<input type="hidden" id="xh" name="xh"/><input type="hidden" id="xn" name="xn"/>'
    '</form></body></html>'
)

#: 真实课程表表头（列 id 与实测一致，含隐藏列）
KINGOSOFT_TABLE_HEADERS: tuple[tuple[str, str], ...] = (
    ("head_kc", "课程"), ("head_xf", "学分"), ("head_zxs", "总学时"), ("head_lb", "课程类别"),
    ("head_kcsx", "课程属性"), ("head_skbh", "上课班号"), ("head_skbjmc", "上课班级名称"),
    ("head_xkfs", "选课方式"), ("head_rkjs", "任课教师"), ("head_xk_points", "投入选课币"),
    ("head_is_buy_book", "购买教材"), ("head_is_cx", "重修"), ("head_xk_status", "选课状态"),
    ("head_operation", "操作"), ("head_kcdm", " "), ("head_kclb1", " "), ("head_kclb2", " "),
    ("head_khfs", " "), ("head_kclb3", " "), ("head_is_yxtj", " "),
    ("head_skbzdm", "上课班组代码"), ("head_skbjdm", "上课班级代码"),
)


def to_web_response_xml(payload: Any) -> str:
    """把 meta/data 结构渲染成平台的 XML 形态（真实系统默认返回 XML）。

    这是**实测行为**：真实 ``/lyuapServer/*`` 接口按 ``Accept`` 头做内容协商，
    没有显式要求 ``application/json`` 时会返回
    ``<WebResponse><meta><success>true</success>…``。mock 复刻这一点，
    这样「忘了声明 Accept」在本项目里能被离线用例抓住。
    """
    def render(value: Any) -> str:
        if isinstance(value, Mapping):
            return "".join(f"<{key}>{render(item)}</{key}>" for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return "".join(f"<item>{render(item)}</item>" for item in value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return escape(str(value))

    return "<WebResponse>" + render(payload) + "</WebResponse>"


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>统一身份认证（mock）</title></head>
<body>
<h1>统一身份认证（mock 服务）</h1>
<form id="casLoginForm" method="post" action="/lyuapServer/login">
  <input type="hidden" name="lt" value="{lt}">
  <input type="hidden" name="execution" value="{execution}">
  <input type="hidden" name="_eventId" value="submit">
  <input type="hidden" name="service" value="{service}">
  <label>学号 <input type="text" name="username" id="username"></label>
  <label>密码 <input type="password" name="password" id="password"></label>
  {captcha_block}
  <button type="submit">登录</button>
</form>
</body></html>
"""

CAPTCHA_BLOCK = """<label>验证码 <input type="text" name="captcha" id="captcha"></label>
<img id="captchaImg" src="/lyuapServer/captcha" alt="验证码">"""

#: mock 图片验证码用的最小合法 PNG（1×1 透明）。
#: 测试只要求「能被 QPixmap/base64 正常解码」，不要求能认出算式；
#: 真实系统给的是一张画着算术题（如 ``6 * 4 =``）的 100×25 PNG。
_MOCK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PORTAL_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>一站式服务门户（mock）</title></head>
<body>
<div id="app">登录成功，欢迎 {student_id}</div>
<a href="/jwglxt/xsxk/xkList">选课</a>
<a href="/lyuapServer/logout">退出</a>
</body></html>
"""


class MockHandler(BaseHTTPRequestHandler):
    """把 CAS 与教务接口实现成最小的 HTTP 服务。"""

    server_version = "qk-mockserver/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- 工具
    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def config(self) -> MockConfig:
        return self.state.config

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 基类签名
        if getattr(self.server, "quiet", True):  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def base_url(self) -> str:
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"  # type: ignore[attr-defined]
        return f"http://{host}"

    def _send(self, status: int, body: str = "", *, content_type: str = "application/json; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _redirect(self, location: str, *, headers: dict[str, str] | None = None) -> None:
        self._send(302, "", headers={"Location": location, **(headers or {})})

    def _session_token(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        for chunk in cookie_header.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name in ("JSESSIONID", "MOD_AUTH_CAS"):
                return value
        return ""

    def _authenticated(self) -> bool:
        if self.config.expire_next_request:
            # 一次性开关：让“下一次请求”恰好撞上会话失效
            with self.state.lock:
                self.config.expire_next_request = False
                self.state.sessions.clear()
            return False
        token = self._session_token()
        return bool(token) and self.state.touch_session(token)

    def _require_auth(self) -> bool:
        if self._authenticated():
            return True
        self._json(401, {"code": "AUTH_REQUIRED", "message": "未登录或会话已失效，请重新登录"})
        return False

    def _body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                data = json.loads(raw)
                return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except ValueError:
                return {}
        # keep_blank_values=True 很关键：账号密码登录靠**空串** loginType 区分
        # 「密码登录」与「扫码建会话」（前者空串、后者 "3"）。默认的 parse_qs
        # 会把 `loginType=` 这种空值参数整个丢掉，mock 就永远匹配不上密码登录路径。
        return {key: values[0] for key, values in
                parse_qs(raw, keep_blank_values=True).items()}

    # ------------------------------------------------------------- 青果 Kingosoft
    def handle_kingosoft_temp(self, method: str) -> None:
        """服务器下发写操作加密用的临时密钥 / 时间戳（真实系统同一接口）。"""
        if method == "getTempDeskey":
            return self._send(200, self.config.kingosoft_temp_key,
                              content_type="text/plain; charset=utf-8")
        if method == "getTempNowtime":
            return self._send(200, self.config.kingosoft_timestamp,
                              content_type="text/plain; charset=utf-8")
        return self._send(404, "unknown method", content_type="text/plain; charset=utf-8")

    def handle_kingosoft_menu(self, query: str) -> None:
        """菜单上下文：真实系统里必须先调用它，否则页面报「错误:无效访问请求」。"""
        menucode = {key: values[0] for key, values in parse_qs(query).items()}.get("menucode", "")
        with self.state.lock:
            self.state.kingosoft_menu_context = menucode or "S2020202"
        self._send(200, json.dumps({
            "linkfile": "/student/wsxk.zx.html",
            "menuName": "选课(按开课计划)",
            "menuCode": menucode or "S2020202",
            "menuList": [],
            "breadcrumbsInfo": "<span>学生服务</span>",
        }, ensure_ascii=False), content_type="application/json;charset=UTF-8")

    def handle_kingosoft_xk_page(self) -> None:
        """选课页：没有菜单上下文时按真实系统返回「无效访问请求」。"""
        with self.state.lock:
            ready = bool(self.state.kingosoft_menu_context)
        if self.config.kingosoft_require_menu_context and not ready:
            return self._send(200, KINGOSOFT_INVALID_ACCESS,
                              content_type="text/html;charset=UTF-8")
        self._send(200, KINGOSOFT_XK_PAGE, content_type="text/html;charset=UTF-8")

    def handle_kingosoft_time_range(self, xktype: str) -> None:
        """选课上下文：真实响应是 ``{"status":…,"result":"<内层 JSON 字符串>"}``。"""
        config = self.config
        inner = {
            "xn": config.kingosoft_year, "xqM": config.kingosoft_term,
            "xqName": "第一学期", "xnxqDesc": f"{config.kingosoft_year}学年第一学期",
            "kcfw": config.kingosoft_kcfw, "kcfwmc": "主修(公共任选)",
            "lcid": config.kingosoft_lcid, "lcmc": "通识选修课（正选）",
            "xh": config.student_id, "nj": "2024", "zydm": "0501", "yxbdm": "05",
            "kxkc": "0", "isXjls": "0", "sfbd": "0", "xxkckzfs": "0", "yxkzyfxxk": "0",
            "qssj": "2026-10-12 17:00", "jssj": "2026-10-14 12:00", "isValidTimerange": "1",
            "lx": xktype or "2",
        }
        self._send(200, json.dumps({
            "status": "200", "message": "操作成功!",
            "result": json.dumps(inner, ensure_ascii=False),
        }, ensure_ascii=False), content_type="text/plain;charset=UTF-8")

    def handle_kingosoft_table(self, query: str, body: Mapping[str, str]) -> None:
        """课程列表：真实形态是 GBK 的 HTML 表格，列由**表头 id** 决定。"""
        params = {key: values[0] for key, values in parse_qs(query).items()}
        if params.get("tableId") not in (None, self.config.kingosoft_table_id):
            return self._send(404, KINGOSOFT_INVALID_ACCESS, content_type="text/html;charset=UTF-8")
        keyword = str(body.get("kcmc") or "").strip().lower()
        header = "".join(
            f"<td id='{cell_id}'>{title}</td>" for cell_id, title in KINGOSOFT_TABLE_HEADERS
        )
        rows = [f"<tr class='H' style='POSITION: relative'>{header}</tr>"]
        with self.state.lock:
            selected = set(self.config.selected)
        for course in self.config.courses:
            if keyword and keyword not in course.course_name.lower() and keyword not in course.course_code.lower():
                continue
            status = "已选" if course.teaching_class_id in selected else ""
            cells = {
                "head_kc": course.course_name,
                "head_xf": f"{course.credits:g}",
                "head_zxs": "32",
                "head_lb": "通识选修课",
                "head_kcsx": "选修",
                "head_skbh": course.teaching_class_id,
                "head_skbjmc": course.time_text or f"{course.course_name}班",
                "head_xkfs": "志愿",
                "head_rkjs": course.teacher,
                "head_xk_status": status,
                "head_kcdm": course.course_code,
                "head_skbzdm": f"{course.course_code[-2:] or '01'}",
                "head_skbjdm": course.teaching_class_id,
            }
            rows.append("<tr>" + "".join(
                f"<td>{cells.get(cell_id, '')}</td>" for cell_id, _ in KINGOSOFT_TABLE_HEADERS
            ) + "</tr>")
        html = (
            '<!DOCTYPE html><html><head><title>数据列表</title>'
            '<meta http-equiv="Content-Type" content="text/html; charset=GBK"></head>'
            f'<body><table>{"".join(rows)}</table></body></html>'
        )
        self._send(200, html, content_type="text/html;charset=GBK")

    def _kingosoft_decode(self, body: Mapping[str, str]) -> str:
        """校验并解密写操作参数；返回明文参数（校验失败返回空串）。"""
        encrypted = str(body.get("params") or "")
        token = str(body.get("token") or "")
        stamp = str(body.get("timestamp") or "")
        if not encrypted:
            return ""
        if self.config.kingosoft_require_encryption:
            if stamp != self.config.kingosoft_timestamp:
                return ""
            try:
                plain = decode_kingo_params(
                    f"params={encrypted}&timestamp={stamp}", self.config.kingosoft_temp_key
                )
            except (ValueError, IndexError):
                return ""
            if not plain:
                return ""
            expected = kingo_md5(kingo_md5(plain) + kingo_md5(stamp))
            if token != expected:
                return ""
        else:
            plain = encrypted
        with self.state.lock:
            self.state.kingosoft_last_write = plain
        return plain

    @staticmethod
    def _kingosoft_fields(plain: str) -> dict[str, str]:
        """解析明文参数；**键名去掉 ``electiveCourseForm.`` 前缀**。

        真实页面提交时字段名带该前缀（``electiveCourseForm.skbjdm`` 等），
        而 mock 只关心语义字段，所以在解析层统一剥掉前缀，
        这样带前缀与不带前缀的写法都能被识别。
        """
        fields: dict[str, str] = {}
        for part in plain.split("&"):
            key, _, value = part.partition("=")
            if not key:
                continue
            fields[key.replace("electiveCourseForm.", "")] = value
        return fields

    def handle_kingosoft_save(self, body: Mapping[str, str]) -> None:
        """提交选课：解密参数 → 记录选课结果（真实端点为 saveElectiveCourse.action）。"""
        plain = self._kingosoft_decode(body)
        if not plain:
            return self._send(200, json.dumps(
                {"status": "500", "message": "参数校验失败，请刷新页面后重试"},
                ensure_ascii=False), content_type="text/plain;charset=UTF-8")
        fields = self._kingosoft_fields(plain)
        kcdm, skbjdm = fields.get("kcdm", ""), fields.get("skbjdm", "")
        with self.state.lock:
            course = next((item for item in self.config.courses
                           if item.teaching_class_id == skbjdm and item.course_code == kcdm), None)
            if course is None:
                return self._send(200, json.dumps(
                    {"status": "500", "message": "未找到该上课班级，可能已关闭"},
                    ensure_ascii=False), content_type="text/plain;charset=UTF-8")
            if skbjdm in self.config.selected:
                return self._send(200, json.dumps(
                    {"status": "500", "message": "您已选过该课程"}, ensure_ascii=False),
                    content_type="text/plain;charset=UTF-8")
            self.config.selected.append(skbjdm)
        self._send(200, json.dumps({"status": "200", "message": "操作成功!", "result": True},
                                   ensure_ascii=False),
                   content_type="text/plain;charset=UTF-8")

    def handle_kingosoft_cancel(self, body: Mapping[str, str]) -> None:
        """退课：解密参数 → 从已选里移除（真实端点为 cancelElectiveCourse.action）。"""
        plain = self._kingosoft_decode(body)
        if not plain:
            return self._send(200, json.dumps(
                {"status": "500", "message": "参数校验失败，请刷新页面后重试"},
                ensure_ascii=False), content_type="text/plain;charset=UTF-8")
        skbjdm = self._kingosoft_fields(plain).get("skbjdm", "")
        with self.state.lock:
            if skbjdm not in self.config.selected:
                return self._send(200, json.dumps(
                    {"status": "500", "message": "该课程未选，无法退课"}, ensure_ascii=False),
                    content_type="text/plain;charset=UTF-8")
            self.config.selected.remove(skbjdm)
        self._send(200, json.dumps({"status": "200", "message": "操作成功!", "result": True},
                                   ensure_ascii=False),
                   content_type="text/plain;charset=UTF-8")

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}

        if path in ("/lyuapServer/login", "/lyuapServer/login/"):
            return self.handle_login_page(query.get("service", ""))
        if path == "/lyuapServer/loginType":
            return self.handle_login_type()
        if path == "/lyuapServer/weChat/CreateQRcode":
            return self.handle_create_qrcode()
        if path == "/lyuapServer/captcha":
            return self._send(200, "MOCK", content_type="image/png", headers={"X-Mock-Captcha": "1"})
        if path == "/lyuapServer/kaptcha":
            return self.handle_kaptcha()
        if path == "/lyuapServer/validate":
            return self.handle_validate(query.get("ticket", ""), query.get("service", ""))
        if path == "/lyuapServer/logout":
            self.state.invalidate(self._session_token())
            return self.handle_login_page(query.get("service", ""))
        if path == "/":
            return self.handle_portal(query)
        if path == "/jwglxt/xsxk/xkList":
            return self.handle_course_list(query.get("keyword", ""))
        if path == "/jwglxt/xsxk/selectedList":
            return self.handle_selected_list()
        # ---------------------------------------------------------- 青果 Kingosoft
        if path == "/frame/homepage":
            return self.handle_kingosoft_temp(query.get("method", ""))
        if path == "/jw/common/getWsxkTimeRange.action":
            return self.handle_kingosoft_time_range(query.get("xktype", "2"))
        if path == "/caslogin":
            # 教务系统的 CAS 回调：用 ticket 换教务会话（真实系统会下发 JSESSIONID）
            return self.handle_portal(query)
        if path == "/student/wsxk.zx.html":
            return self.handle_kingosoft_xk_page()
        return self._json(404, {"code": "NOT_FOUND", "message": f"未知路径 {path}"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()

        if path in ("/lyuapServer/login", "/lyuapServer/login/"):
            return self.handle_login_submit(body)
        if path == "/lyuapServer/weChat/wx/CheckScan":
            return self.handle_check_scan(body)
        if path == "/lyuapServer/v1/tickets":
            return self.handle_rest_tickets(body)
        if path.startswith("/lyuapServer/v1/tickets/"):
            return self.handle_rest_ticket_service(path.rsplit("/", 1)[-1], body)
        if path == "/jwglxt/xsxk/xkSelect":
            return self.handle_select(body)
        if path == "/jwglxt/xsxk/xkDrop":
            return self.handle_drop(body)
        # ---------------------------------------------------------- 青果 Kingosoft
        if path == "/frame/normalusemenu/getBreadcrumbsInfoNew.action":
            return self.handle_kingosoft_menu(parsed.query)
        if path == "/taglib/DataTable.jsp":
            return self.handle_kingosoft_table(parsed.query, body)
        if path == "/jw/common/saveElectiveCourse.action":
            return self.handle_kingosoft_save(body)
        if path == "/jw/common/cancelElectiveCourse.action":
            return self.handle_kingosoft_cancel(body)
        return self._json(404, {"code": "NOT_FOUND", "message": f"未知路径 {path}"})

    # ------------------------------------------------------------- 微信扫码
    def _wants_json(self) -> bool:
        """真实平台按 Accept 做内容协商：不显式要求 JSON 就返回 XML。"""
        return "application/json" in (self.headers.get("Accept") or "")

    def _negotiated(self, payload: Mapping[str, Any], status: int = 200) -> None:
        """按 Accept 头返回 JSON 或平台的 XML 形态。"""
        if self._wants_json():
            return self._json(status, payload)
        self._send(status, to_web_response_xml(payload),
                   content_type="application/xml; charset=utf-8")

    def handle_login_type(self) -> None:
        """平台登录方式配置；``isWeChat="1"`` 表示启用微信扫码（真实部署实测值）。"""
        config = self.config
        self._negotiated({
            "meta": {"success": True, "statusCode": 200, "message": "ok"},
            "data": {
                "isWeChat": config.login_type_is_wechat,
                "isWeChatOpenLogin": config.login_type_is_wechat_open,
                "isSMS": "0",
                "isApp": "0",
                "isDingDing": "0",
                "isMMDL": "0",
                "defaultLoginUrl": f"{self.base_url()}/",
                "casTitle": "统一身份认证平台（mock）",
            },
        })

    def handle_create_qrcode(self) -> None:
        """取登录二维码：与真实响应一致——**平铺** ``{uuid, content}``，content 是 PNG data URI。"""
        config = self.config
        uuid_value = f"wx_login_{secrets.token_hex(16)}"
        with self.state.lock:
            self.state.qr_polls[uuid_value] = 0
            seed = len(self.state.qr_polls)
        self._negotiated({
            "uuid": uuid_value,
            "content": qr_data_uri(modules=config.qr_modules, module_px=config.qr_module_px, seed=seed),
        })

    def handle_check_scan(self, body: dict[str, str]) -> None:
        """轮询扫码状态：等待若干次后成功 / 二维码过期 / 永不成功（三种都可参数化）。"""
        config = self.config
        uuid_value = body.get("state") or body.get("uuid") or ""
        if not uuid_value:
            return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                              "message": "缺少 state 参数"}, "data": None})
        with self.state.lock:
            if uuid_value not in self.state.qr_polls:
                return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                                  "message": "未检测到用户登录"}, "data": None})
            self.state.qr_polls[uuid_value] += 1
            polls = self.state.qr_polls[uuid_value]

        if config.qr_expire_rounds and polls >= config.qr_expire_rounds:
            return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                              "message": "此二维码已过期，请重新扫描"}, "data": None})
        if config.qr_never_success or polls <= config.qr_wait_rounds:
            return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                              "message": "未检测到用户登录"}, "data": None})

        service = body.get("service") or f"{self.base_url()}/"
        self._negotiated({
            "meta": {"success": True, "statusCode": 200, "message": "ok"},
            "data": {
                "userName": config.scan_user,
                "passWord": config.scan_password,
                "service": service,
                "type": "3",  # 真实前端扫码分支传的 loginType
            },
        })

    # ------------------------------------------------------------- CAS REST
    def handle_kaptcha(self) -> None:
        """图片验证码：返回 data URI 图 + uid（与真实响应同构）。

        真实契约（2026-10-15 在登录页实测）::

            GET /lyuapServer/kaptcha?_t=<毫秒时间戳>&uid=
            → {"kaptchaType":"1","uid":"<32位hex>","content":"data:image/png;base64,…"}

        每次调用产生**新的 uid**；登录时必须回传该 uid（字段 ``id``）与答案（``code``），
        服务端靠 uid 找回这张图对应的答案 —— 所以「换一张」之后旧答案立刻作废。
        这里用一张 1×1 的合法 PNG（测试只要求能被解码，不要求能认出算式）。
        """
        uid = secrets.token_hex(16)
        expected = self.state.kaptcha_expected
        if expected is None:
            expected = str(secrets.randbelow(10) * secrets.randbelow(10))
        with self.state.lock:
            self.state.kaptcha_answers[uid] = str(expected)
        content = "data:image/png;base64," + _MOCK_PNG_B64
        self._send(200, json.dumps({"kaptchaType": "1", "uid": uid, "content": content},
                                   ensure_ascii=False),
                   content_type="application/json;charset=UTF-8")

    def handle_rest_tickets(self, body: dict[str, str]) -> None:
        """REST 第一步：表单编码 ``username``/``password``/``service``/``loginType``。

        两种登录方式（2026-10-15 抓真实请求核对）：

        * ``loginType="3"``（扫码建会话）：``password`` 是**明文**；
        * ``loginType=""``（账号密码登录）：``password`` 是 **RSA 密文**，
          且必须带验证码 ``id``（uid）与 ``code``（答案）。

        失败时**照样返回 HTTP 200**，只在 ``data.code`` 里给原因 ——
        这与真实系统一致（实测 ``NOUSER`` / ``CODEFALSE``），
        客户端据此才能给出「账号不存在」「验证码错误」这类可操作提示。
        """
        config = self.config
        if not config.rest_tickets_enabled:
            return self._send(404, "not found", content_type="text/plain; charset=utf-8")
        user = body.get("username") or body.get("userName") or ""
        password = body.get("password") or body.get("passWord") or ""
        login_type = body.get("loginType")
        if login_type is None:
            login_type = "3"

        def reject(code: str) -> None:
            self._negotiated({
                "meta": {"success": True, "statusCode": 200, "message": "ok"},
                "data": {"code": code},
            })

        # ---- 账号密码登录：先验验证码，再验账号，最后验密码 ----
        if login_type == "":
            uid = (body.get("id") or "").strip()
            answer = (body.get("code") or "").strip()
            with self.state.lock:
                want = self.state.kaptcha_answers.pop(uid, None) if uid else None
            if want is None or answer != want:
                return reject("CODEFALSE")
            if user != config.student_id:
                return reject("NOUSER")
            # 密码是 RSA 密文，服务端没有私钥也没关系：加密是**确定性**的，
            # 用它自己的公钥重新加密一遍已知密码再比对即可。
            from .adapters.cas_rsa import encrypt_password  # noqa: PLC0415

            if password != encrypt_password(config.password):
                return reject("PWDERR")
            return self._issue_rest_session(user, body.get("service") or "")

        # ---- 扫码建会话：明文密码 ----
        accepted = {
            (config.student_id, config.password),
            (config.scan_user, config.scan_password),
        }
        if (user, password) not in accepted:
            return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                              "message": "请求操作失败!"}, "data": None})
        return self._issue_rest_session(user, body.get("service") or "")

    def _issue_rest_session(self, user: str, service: str) -> None:
        """两步 REST 建会话的第一步：发放 TGT（必要时同时内联 ticket）。"""
        config = self.config
        tgt = f"TGT-{secrets.token_hex(8)}"
        st = f"ST-{secrets.token_hex(8)}"
        service = service or f"{self.base_url()}/"
        with self.state.lock:
            self.state.tgt_users[tgt] = user
            if config.rest_inline_ticket_invalid:
                # 故意给一个没登记过的 ticket：门户会拒绝，客户端应当在同一轮里改走第二步
                st = f"ST-invalid-{secrets.token_hex(6)}"
            else:
                self.state.tickets[st] = user
                self.state.ticket_services[st] = service
        # 与真实前端一致：第一步响应里**同时**给出 tgt 与 ticket，
        # 前端直接用 `service?ticket=<ticket>` 跳转（第二步只在复用 TGT 时才用）
        self._negotiated({
            "meta": {"success": True, "statusCode": 200, "message": "ok"},
            "data": {"tgt": tgt, "ticket": st, "code": ""},
        })

    def handle_rest_ticket_service(self, tgt: str, body: dict[str, str]) -> None:
        """REST 第二步：用 TGT 换回带 ticket 的 service 地址（表单编码 ``service``）。"""
        with self.state.lock:
            user = self.state.tgt_users.get(tgt, "")
        if not user:
            return self._negotiated({"meta": {"success": False, "statusCode": 500,
                                              "message": "请求操作失败!"}, "data": None})
        service = body.get("service") or f"{self.base_url()}/"
        st = f"ST-{secrets.token_hex(8)}"
        with self.state.lock:
            self.state.tickets[st] = user
            self.state.ticket_services[st] = service
        joiner = "&" if "?" in service else "?"
        self._negotiated({
            "meta": {"success": True, "statusCode": 200, "message": "ok"},
            "data": f"{service}{joiner}ticket={st}",
        })

    # ------------------------------------------------------------- CAS
    def handle_login_page(self, service: str) -> None:
        with self.state.lock:
            self.state.login_requests += 1
            lt = f"LT-{secrets.token_hex(4)}"
        captcha_block = CAPTCHA_BLOCK if self.config.captcha_required else ""
        html = LOGIN_PAGE.format(
            lt=lt,
            execution="e1s1",
            service=service,
            captcha_block=captcha_block,
        )
        self._send(200, html, content_type="text/html; charset=utf-8")

    def handle_login_submit(self, body: dict[str, str]) -> None:
        service = body.get("service") or f"{self.base_url()}/"
        with self.state.lock:
            self.state.credentials.append((body.get("username", ""), body.get("password", "")))
            self.state.login_requests += 1

        if self.config.captcha_required:
            supplied = (body.get("captcha") or "").strip()
            expected = self.config.captcha_code
            if not supplied:
                return self._send(
                    200,
                    LOGIN_PAGE.format(lt="LT-retry", execution="e1s1", service=service,
                                      captcha_block=CAPTCHA_BLOCK),
                    content_type="text/html; charset=utf-8",
                )
            if expected and supplied != expected:
                return self._send(
                    200,
                    LOGIN_PAGE.format(lt="LT-retry", execution="e1s1", service=service,
                                      captcha_block=CAPTCHA_BLOCK),
                    content_type="text/html; charset=utf-8",
                )

        if body.get("username") != self.config.student_id or body.get("password") != self.config.password:
            error_page = LOGIN_PAGE.format(
                lt="LT-error", execution="e1s1", service=service,
                captcha_block=CAPTCHA_BLOCK if self.config.captcha_required else "",
            ).replace("</form>", '<p class="error">用户名或密码错误</p></form>')
            return self._send(200, error_page, content_type="text/html; charset=utf-8")

        ticket = f"ST-{secrets.token_hex(8)}"
        with self.state.lock:
            self.state.tickets[ticket] = self.config.student_id
            self.state.ticket_services[ticket] = service
        joiner = "&" if "?" in service else "?"
        self._redirect(f"{service}{joiner}ticket={ticket}")

    def handle_validate(self, ticket: str, service: str) -> None:
        """校验 ticket：成功时下发会话 Cookie，并按标准 CAS 回跳 service。"""
        with self.state.lock:
            student_id = self.state.tickets.pop(ticket, "")
            stored_service = self.state.ticket_services.pop(ticket, "")
        if not student_id:
            return self._send(200, "no\n\n", content_type="text/plain; charset=utf-8")

        token = self.state.issue_session()
        cookie = f"JSESSIONID={token}; Path=/; HttpOnly"
        target = service or stored_service
        if target:
            # 302 与 Set-Cookie 同时下发，客户端跟随重定向时即可带上会话
            joiner = "&" if "?" in target else "?"
            return self._redirect(f"{target}{joiner}ticket={ticket}", headers={"Set-Cookie": cookie})
        self._send(
            200,
            f"yes\n{student_id}\n",
            content_type="text/plain; charset=utf-8",
            headers={"Set-Cookie": cookie},
        )

    # ------------------------------------------------------------- 门户与教务
    def handle_portal(self, query: dict[str, str] | None = None) -> None:
        """门户页：带 ticket 时用它换会话（REST 两步建会话的最后一步也走这里）。"""
        query = query or {}
        ticket = query.get("ticket", "")
        if ticket:
            with self.state.lock:
                user = self.state.tickets.pop(ticket, "")
            if user:
                token = self.state.issue_session()
                self._send(
                    200,
                    PORTAL_PAGE.format(student_id=user),
                    content_type="text/html; charset=utf-8",
                    headers={"Set-Cookie": f"JSESSIONID={token}; Path=/; HttpOnly"},
                )
                return
        token = self._session_token()
        if token and self.state.touch_session(token):
            self._send(
                200,
                PORTAL_PAGE.format(student_id=self.config.student_id),
                content_type="text/html; charset=utf-8",
            )
            return
        service = f"{self.base_url()}/"
        self._redirect(f"/lyuapServer/login?{urlencode({'service': service}).replace('+', '%20')}")

    def handle_course_list(self, keyword: str) -> None:
        if not self._require_auth():
            return
        items = []
        for course in self.config.courses:
            if keyword and keyword not in course.course_name and keyword not in course.course_code:
                continue
            payload = course.to_json()
            payload["selected"] = self.state.enrolled_count(course)
            payload["syrs"] = max(course.capacity - payload["selected"], 0)
            items.append(payload)
        self._json(200, {"code": "0", "message": "查询成功", "items": items})

    def handle_selected_list(self) -> None:
        if not self._require_auth():
            return
        items = []
        for teaching_class_id in list(self.config.selected):
            course = self.state.find_course(teaching_class_id)
            if course is not None:
                items.append(course.to_json())
        self._json(200, {"code": "0", "message": "查询成功", "items": items})

    def handle_select(self, body: dict[str, str]) -> None:
        if self.config.expire_on_first_select and not self.state.select_expired:
            # 演示用：第一次选课时把会话作废，客户端应自动重登并重试
            with self.state.lock:
                self.state.select_expired = True
                self.state.sessions.clear()
        if not self._require_auth():
            return
        teaching_class_id = body.get("jxb_id") or body.get("teaching_class_id") or ""
        course = self.state.find_course(teaching_class_id)
        if course is None:
            return self._json(200, {"code": "NOT_FOUND", "message": "未找到该教学班"})

        attempt = self.state.note_attempt(teaching_class_id)

        # 行为分支优先于“容量是否已满”的硬判定，否则 full_then_free 永远轮不到放行
        if self.state.is_selected(teaching_class_id):
            return self._json(200, {"code": "ALREADY_SELECTED", "message": "你已经选过这门课"})
        if course.behaviour == "already_selected":
            return self._json(200, {"code": "ALREADY_SELECTED", "message": "你已经选过这门课"})
        if course.behaviour == "full":
            return self._json(200, {"code": "FULL", "message": "该教学班人数已满"})
        if course.behaviour == "full_then_free" and attempt <= course.free_after:
            return self._json(200, {"code": "FULL", "message": "该教学班人数已满"})
        if course.behaviour == "full_then_free" and attempt == course.free_after + 1:
            # 到点放行：模拟有人退课腾出一个名额，否则课程本身满额仍会被下面的硬判定拦住
            course.enrolled = max(course.enrolled - 1, 0)
        if self.state.enrolled_count(course) >= course.capacity:
            return self._json(200, {"code": "FULL", "message": "该教学班人数已满"})

        self.state.mark_selected(teaching_class_id)
        with self.state.lock:
            self.state.extra_enrolled[teaching_class_id] = self.state.extra_enrolled.get(teaching_class_id, 0) + 1
        self._json(200, {"code": "SUCCESS", "message": "选课成功"})

    def handle_drop(self, body: dict[str, str]) -> None:
        if not self._require_auth():
            return
        teaching_class_id = body.get("jxb_id") or body.get("teaching_class_id") or ""
        course = self.state.find_course(teaching_class_id)
        if course is None:
            return self._json(200, {"code": "NOT_FOUND", "message": "未找到该教学班"})
        if not self.state.is_selected(teaching_class_id):
            return self._json(200, {"code": "NOT_SELECTED", "message": "你还没有选这门课"})
        if course.behaviour == "drop_fail":
            return self._json(200, {"code": "DROP_FAIL", "message": "退课失败：不在退课时间内"})
        self.state.mark_dropped(teaching_class_id)
        with self.state.lock:
            # 退课只撤销“本次会话额外占用”的名额，不凭空改变初始已选人数
            current = self.state.extra_enrolled.get(teaching_class_id, 0)
            self.state.extra_enrolled[teaching_class_id] = max(current - 1, 0)
        self._json(200, {"code": "SUCCESS", "message": "退课成功"})


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


class MockServer(ThreadingHTTPServer):
    """带状态与可配置行为的 mock 服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: MockConfig, *, quiet: bool = True) -> None:
        super().__init__(address, MockHandler)
        self.state = MockState(config)
        self.quiet = quiet

    @property
    def config(self) -> MockConfig:
        return self.state.config

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/"

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="qk-mock", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def start_mock_server(config: MockConfig | None = None, *, host: str = "127.0.0.1", port: int = 0,
                      quiet: bool = True) -> MockServer:
    """启动 mock 服务（port=0 时由系统分配端口）。返回已启动的服务器对象。"""
    server = MockServer((host, port), config or MockConfig.default(), quiet=quiet)
    server.start_background()
    return server


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qk.mock",
        description="启动离线 mock 教务服务（自测用，切勿指向真实环境）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--student-id", default="20230001", help="mock 账号")
    parser.add_argument("--password", default="mock-pass", help="mock 密码")
    parser.add_argument("--session-ttl", type=int, default=0, help="会话在多少次请求后失效（0=不失效）")
    parser.add_argument("--captcha-required", action="store_true", help="登录页要求验证码")
    parser.add_argument("--captcha-code", default=None, help="正确的验证码；缺省表示任意 4 位数字")
    parser.add_argument("--expire-on-select", action="store_true",
                        help="第一次提交选课时让会话失效，用于演示“会话过期→自动重登”")
    parser.add_argument("--login-type-is-wechat", default="1", choices=("0", "1"),
                        help="平台是否启用微信扫码（isWeChat）")
    parser.add_argument("--qr-wait-rounds", type=int, default=1,
                        help="前几次轮询返回“等待扫码”")
    parser.add_argument("--qr-expire-rounds", type=int, default=0,
                        help="第几次轮询起返回“二维码已过期”（0=不过期）")
    parser.add_argument("--qr-never-success", action="store_true",
                        help="永远返回等待，用于验证轮询超时")
    parser.add_argument("--qr-modules", type=int, default=21, help="mock 二维码的模块数")
    parser.add_argument("--qr-module-px", type=int, default=4, help="mock 二维码每模块像素边长")
    parser.add_argument("--no-rest-tickets", action="store_true",
                        help="禁用 CAS REST 两步建会话（用于验证回退 HTML 表单）")
    parser.add_argument("--quiet", action="store_true", help="不打印每个请求")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = MockConfig.default(
        student_id=args.student_id,
        password=args.password,
        session_ttl=args.session_ttl,
        captcha_required=args.captcha_required,
        captcha_code=args.captcha_code,
        expire_on_first_select=args.expire_on_select,
        login_type_is_wechat=args.login_type_is_wechat,
        qr_wait_rounds=args.qr_wait_rounds,
        qr_expire_rounds=args.qr_expire_rounds,
        qr_never_success=args.qr_never_success,
        qr_modules=args.qr_modules,
        qr_module_px=args.qr_module_px,
        rest_tickets_enabled=not args.no_rest_tickets,
    )
    server = MockServer((args.host, args.port), config, quiet=args.quiet)
    print(f"mock 教务服务已启动：{server.base_url}")
    print(f"  账号 {config.student_id} / 密码 {config.password}")
    print("  端点：/lyuapServer/login、/jwglxt/xsxk/xkList、/jwglxt/xsxk/xkSelect、"
          "/jwglxt/xsxk/xkDrop、/jwglxt/xsxk/selectedList")
    print("  Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
