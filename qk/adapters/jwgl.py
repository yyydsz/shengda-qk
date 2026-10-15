"""教务（选课）接口适配器。

包含两套实现，由 ``jwgl.platform`` 选择（**默认 ``kingosoft``**，见 ``JwglConfig.platform``）：

* :class:`KingosoftCourseAdapter`（``platform="kingosoft"``，**默认**）——**本校真实教务系统**
  （青果 Kingosoft 高校教学综合管理服务平台，2026-10-13 在校园网实测）：CAS 换票建会话 →
  取菜单上下文（缺它会报「无效访问请求」）→ 表单 POST ``taglib/DataTable.jsp?tableId=2568``
  查询课程（GBK HTML 表格，按**表头 id** 定列）→ ``jw/common/saveElectiveCourse.action``
  提交选课、``jw/common/cancelElectiveCourse.action`` 退课（参数按平台前端的
  ``getEncParams`` 做 DES+base64+token 加密）。真实端点与字段见 README 的
  「教务系统（Kingosoft）选课接口现状」。
* :class:`JwglCourseAdapter`（``platform=auto|custom|qz``）——通用实现，路径来自
  ``jwgl.course_list_path`` / ``select_path`` / ``drop_path`` / ``selected_list_path``；
  **路径为空时抛 :class:`PlatformUnsupportedError` 并指出该填哪个配置项，不会乱发请求**，
  返回格式同时兼容 JSON 与 HTML 表格。

换平台只需实现同样接口的新适配器（见 ``qk/adapters/base.py`` 顶部的说明）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from ..config import AppConfig
from ..models import CandidateCourse, Outcome, SelectionResult
from ..net import QkSession, TransportError
from .base import (
    CourseAdapter,
    CourseNotFoundError,
    PlatformUnsupportedError,
    SelectionError,
    SessionExpiredError,
    classify_message,
    extract_message,
    filter_excluded_attrs,
)

#: 服务器返回码 → 统一结果分类
CODE_TO_OUTCOME: dict[str, Outcome] = {
    "0": Outcome.SUCCESS,
    "SUCCESS": Outcome.SUCCESS,
    "OK": Outcome.SUCCESS,
    "TRUE": Outcome.SUCCESS,
    "FULL": Outcome.FULL,
    "COURSE_FULL": Outcome.FULL,
    "ALREADY_SELECTED": Outcome.ALREADY_SELECTED,
    "REPEAT": Outcome.ALREADY_SELECTED,
    "NOT_FOUND": Outcome.NOT_FOUND,
    "AUTH_REQUIRED": Outcome.AUTH_REQUIRED,
    "NOT_LOGIN": Outcome.AUTH_REQUIRED,
    "CONFLICT": Outcome.CONFLICT,
    "TIME_CONFLICT": Outcome.CONFLICT,
    "RATE_LIMITED": Outcome.RATE_LIMITED,
    "DROP_FAIL": Outcome.DROP_FAILED,
    "DROP_FAILED": Outcome.DROP_FAILED,
    "NOT_SELECTED": Outcome.DROP_FAILED,
}

#: 课程条目里常见字段别名
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "teaching_class_id": ("jxb_id", "teaching_class_id", "jxbid", "classid", "jxbmc_id", "id"),
    "course_code": ("kch", "course_code", "kcdm", "courseCode", "kch_id"),
    "course_name": ("kcmc", "course_name", "kcmc_show", "name", "title"),
    "teacher": ("jsxm", "teacher", "jsmc", "teachername", "skjs"),
    "credits": ("xf", "credits", "credit", "xf_num"),
    "capacity": ("capacity", "zrs", "rl", "kxrs", "capacity_num"),
    "enrolled": ("selected", "yxrs", "enrolled", "bmrs", "selected_num"),
    "campus": ("xqmc", "campus", "campus_name"),
    "time_text": ("time", "sksj", "time_text", "sktime", "sksjms"),
}

#: HTML 表格解析：按 ``<tr>`` 切分，再取 ``<td>`` 内容
_TABLE_ROW = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL = re.compile(r"<t[dh][^>]*>(?P<cell>.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def _clean_html_text(value: str) -> str:
    text = _TAGS.sub("", value or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return " ".join(text.split())


def _pick(entry: Mapping[str, Any], field_name: str, default: Any = None) -> Any:
    """按别名表从条目里取值。"""
    for alias in FIELD_ALIASES.get(field_name, (field_name,)):
        if alias in entry and entry[alias] not in (None, ""):
            return entry[alias]
    return default


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def course_from_mapping(entry: Mapping[str, Any]) -> CandidateCourse:
    """把服务器返回的一条记录转成 :class:`CandidateCourse`。"""
    teaching_class_id = str(_pick(entry, "teaching_class_id", "") or "").strip()
    capacity = _to_int(_pick(entry, "capacity"))
    enrolled = _to_int(_pick(entry, "enrolled"))
    # 有些接口只给“剩余人数 syrs”，用它反推已选
    if enrolled is None and capacity is not None and "syrs" in entry:
        remaining = _to_int(entry.get("syrs"))
        if remaining is not None:
            enrolled = max(capacity - remaining, 0)
    return CandidateCourse(
        teaching_class_id=teaching_class_id,
        course_code=str(_pick(entry, "course_code", "") or "").strip(),
        course_name=str(_pick(entry, "course_name", "") or "").strip(),
        teacher=str(_pick(entry, "teacher", "") or "").strip(),
        credits=_to_float(_pick(entry, "credits")),
        capacity=capacity,
        enrolled=enrolled,
        campus=str(_pick(entry, "campus", "") or "").strip(),
        time_text=str(_pick(entry, "time_text", "") or "").strip(),
        raw=dict(entry),
    )


def parse_candidates(payload: Any) -> list[CandidateCourse]:
    """从 JSON 响应里解析课程列表；识别 ``items`` / ``rows`` / ``data`` 等常见包装。"""
    entries: Iterable[Any]
    if isinstance(payload, Mapping):
        items: Any = None
        for key in ("items", "rows", "data", "list", "records"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        entries = items if items is not None else []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    return [course_from_mapping(entry) for entry in entries if isinstance(entry, Mapping)]


def parse_candidates_html(html: str) -> list[CandidateCourse]:
    """从 HTML 表格解析课程列表（按表头列名定位，避免列顺序差异）。"""
    if not html:
        return []
    rows = _TABLE_ROW.findall(html)
    if not rows:
        return []
    parsed_rows = [[_clean_html_text(cell) for cell in _TABLE_CELL.findall(row)] for row in rows]
    parsed_rows = [row for row in parsed_rows if row]
    if not parsed_rows:
        return []

    header = parsed_rows[0]
    column_map: dict[str, int] = {}
    header_hints = {
        "teaching_class_id": ("教学班", "教学班号", "选课号", "课序号"),
        "course_code": ("课程号", "课程代码"),
        "course_name": ("课程名称", "课程名"),
        "teacher": ("教师", "任课教师", "上课教师"),
        "credits": ("学分",),
        "capacity": ("容量", "课容量", "限选人数"),
        "enrolled": ("已选", "已选人数", "选课人数"),
        "time_text": ("上课时间", "时间", "上课节次"),
        "campus": ("校区", "上课地点"),
    }
    for field_name, hints in header_hints.items():
        for index, title in enumerate(header):
            if any(hint in title for hint in hints):
                column_map[field_name] = index
                break

    courses: list[CandidateCourse] = []
    start = 1 if column_map else 0
    for row in parsed_rows[start:]:
        if len(row) < 2:
            continue
        entry: dict[str, Any] = {}
        if column_map:
            for field_name, index in column_map.items():
                if index < len(row):
                    entry[field_name] = row[index]
        else:  # 没有表头时按“教学班号/课程号/课程名/教师”常见顺序兜底
            for field_name, value in zip(
                ("teaching_class_id", "course_code", "course_name", "teacher"), row
            ):
                entry[field_name] = value
        course = course_from_mapping(entry)
        if course.teaching_class_id or course.course_name:
            courses.append(course)
    return courses


class JwglCourseAdapter(CourseAdapter):
    """教务选课接口实现（路径未确认时明确报错，不猜路径）。"""

    name = "jwgl"

    def __init__(self, config: AppConfig, session: QkSession) -> None:
        self.config = config
        self.session = session

    # ------------------------------------------------------------------ 工具
    def _url(self, path: str) -> str:
        base = self.config.jwgl.base_url.rstrip("/") + "/"
        return base + path.lstrip("/")

    def _require_path(self, attr: str, purpose: str) -> str:
        path = getattr(self.config.jwgl, attr, "")
        if not path:
            raise PlatformUnsupportedError(
                f"教务接口尚未确认：{purpose} 需要配置 jwgl.{attr}。\n"
                "  该路径只能在校园网下用浏览器开发者工具（Network 面板）确认后填入；"
                "脚本刻意不猜测路径，以免向真实服务器发送无意义的请求。\n"
                "  自测时可先把 jwgl.base_url 指向本项目的 mock 服务"
                "（python -m qk.mockserver）并使用上面的示例路径。"
            )
        return path

    def _request_json(self, method: str, path: str, *, purpose: str, **kwargs: Any) -> Mapping[str, Any]:
        url = self._url(path)
        try:
            result = self.session.request(method, url, **kwargs)
        except TransportError as exc:
            raise SelectionError(f"{purpose} 请求失败：{exc}") from exc
        if result.status in (401, 403):
            raise SessionExpiredError(f"{purpose} 返回 HTTP {result.status}，会话已失效，需要重新登录。")
        payload = result.json()
        if payload is None:
            return {
                "code": "HTML",
                "message": "",
                "_html": result.text,
                "_status": result.status,
                "_url": result.url,
            }
        if not isinstance(payload, Mapping):
            return {"code": "RAW", "message": "", "_payload": payload}
        return {**payload, "_status": result.status, "_url": result.url}

    def _ensure_ok(self, payload: Mapping[str, Any], purpose: str) -> Mapping[str, Any]:
        status = int(payload.get("_status", 200) or 200)
        if status in (401, 403):
            raise SessionExpiredError(f"{purpose} 返回 HTTP {status}，会话已失效，需要重新登录。")
        return payload

    def detect_platform(self) -> str:
        """按配置与探测结果给出当前平台标识。"""
        configured = (self.config.jwgl.platform or "auto").lower()
        if configured != "auto":
            return configured
        return "custom" if self.config.jwgl.course_list_path else "unknown"

    # ------------------------------------------------------------------ 查询
    def fetch_candidates(self, keyword: str = "") -> Sequence[CandidateCourse]:
        """拉取可选教学班列表。"""
        path = self._require_path("course_list_path", "查询候选课程")
        payload = self._request_json(
            "GET", path, purpose="查询候选课程",
            params={"keyword": keyword} if keyword else None,
        )
        self._ensure_ok(payload, "查询候选课程")
        html = payload.get("_html")
        courses = parse_candidates_html(html) if html else parse_candidates(payload)
        if keyword and html:
            lowered = keyword.lower()
            courses = [
                course
                for course in courses
                if lowered in course.course_name.lower() or lowered in course.course_code.lower()
            ]
        # 与青果适配器一致：按 jwgl.exclude_course_attrs 排除指定授课形式
        return filter_excluded_attrs(courses, self.config.jwgl.exclude_course_attrs)

    def fetch_selected(self) -> Sequence[CandidateCourse]:
        """已选课程列表；路径未配置时返回空列表（不阻塞抢课主流程）。"""
        if not self.config.jwgl.selected_list_path:
            return []
        payload = self._request_json("GET", self.config.jwgl.selected_list_path, purpose="查询已选课程")
        self._ensure_ok(payload, "查询已选课程")
        html = payload.get("_html")
        return parse_candidates_html(html) if html else parse_candidates(payload)

    # ------------------------------------------------------------------ 写入
    def _submit(self, path: str, course: CandidateCourse, *, purpose: str) -> SelectionResult:
        url = self._url(path)
        data = {"jxb_id": course.teaching_class_id, **(self.config.jwgl.extra_params or {})}
        try:
            result = self.session.post(url, data=data)
        except TransportError as exc:
            return SelectionResult(Outcome.UNKNOWN, course, message=f"{purpose} 请求失败：{exc}")

        if result.status in (401, 403):
            # 抛异常而不是返回结果：编排层据此触发“重新登录后重试”
            raise SessionExpiredError(
                f"{purpose} 返回 HTTP {result.status}，会话已失效，需要重新登录。"
            )

        payload = result.json()
        if payload is None:
            outcome = Outcome(  # 纯文本/HTML 页面：按关键词归类
                {"SUCCESS": "SUCCESS", "ALREADY_SELECTED": "ALREADY_SELECTED", "FULL": "FULL",
                 "AUTH_REQUIRED": "AUTH_REQUIRED", "RATE_LIMITED": "RATE_LIMITED",
                 "NOT_FOUND": "NOT_FOUND", "CONFLICT": "CONFLICT"}.get(
                    classify_message(result.text), "UNKNOWN"
                )
            )
            message = _clean_html_text(result.text)[:200]
            return SelectionResult(outcome, course, message=message, raw_text=result.text)

        if not isinstance(payload, Mapping):
            return SelectionResult(Outcome.UNKNOWN, course, message="响应格式无法识别", raw_text=result.text)

        code = str(payload.get("code", payload.get("result", "")) or "").upper()
        message = extract_message(payload) or str(payload.get("message", "") or "")
        if code in CODE_TO_OUTCOME:
            outcome = CODE_TO_OUTCOME[code]
        else:
            outcome = Outcome(classify_message(message or code))
        if outcome is Outcome.UNKNOWN and payload.get("success") is True:
            outcome = Outcome.SUCCESS
        return SelectionResult(outcome, course, message=message, raw_text=result.text)

    def select(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交选课；``dry_run=True`` 时不发任何写请求。"""
        if dry_run:
            return SelectionResult(Outcome.DRY_RUN, course, message="演练模式：未提交选课")
        if not course.teaching_class_id:
            raise CourseNotFoundError("该课程缺少教学班号（jxb_id），无法提交选课。")
        path = self._require_path("select_path", "提交选课")
        return self._submit(path, course, purpose="提交选课")

    def drop(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交退课；``dry_run=True`` 时不发任何写请求。"""
        if dry_run:
            return SelectionResult(Outcome.DRY_RUN, course, message="演练模式：未提交退课")
        if not course.teaching_class_id:
            raise CourseNotFoundError("该课程缺少教学班号（jxb_id），无法提交退课。")
        path = self._require_path("drop_path", "提交退课")
        return self._submit(path, course, purpose="提交退课")

    # ------------------------------------------------------------------ 工厂
    @classmethod
    def create(cls, config: AppConfig, session: QkSession | None = None) -> "JwglCourseAdapter":
        return cls(config, session or QkSession.from_config(config))


# ==========================================================================
# 写操作参数加密（移植平台自己的 /custom/js/jkingo.des.js + SetKingoEncypt.jsp）
#
# 前端实现（实测）：
#     token   = md5(md5(params) + md5(timestamp))
#     encoded = base64encode(utf16to8(strEnc(params, tempDeskey, null, null)))
#     body    = "params=" + encoded + "&token=" + token + "&timestamp=" + timestamp
# 其中 tempDeskey / timestamp 由服务端下发（/frame/homepage?method=getTempDeskey|getTempNowtime）。
# DES 常量表由该 JS 文件**程序化提取**，实现已用 node 跑平台原版 JS 生成的参考向量逐条校验
# （见 qk/tests/test_adapters.py::test_kingosoft_crypto_matches_platform_vectors）。
# ==========================================================================

# --- DES 常量表：由平台 /custom/js/jkingo.des.js 程序化提取 ---
_DES_P = (15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9, 1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24)
_DES_FP = (39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25, 32, 0, 40, 8, 48, 16, 56, 24)
_DES_PC2 = (13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3, 25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36, 46, 54, 29, 39, 50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31)
_DES_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_DES_SBOX = (
    (
        (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
        (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
        (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
        (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    ),
    (
        (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
        (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
        (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
        (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    ),
    (
        (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
        (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
        (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
        (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    ),
    (
        (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
        (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
        (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
        (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    ),
    (
        (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
        (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
        (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
        (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    ),
    (
        (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
        (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
        (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
        (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    ),
    (
        (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
        (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
        (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
        (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    ),
    (
        (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
        (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
        (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
        (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
    ),
)

_HEX_DIGITS = "0123456789ABCDEF"


def _str_to_bt(text: str) -> list[int]:
    """des.js ``strToBt``：取前 4 个字符，每字符 16 位（大端），不足补 0。"""
    bits = [0] * 64
    for i in range(min(len(text), 4)):
        code = ord(text[i])
        for j in range(16):
            bits[16 * i + j] = (code >> (15 - j)) & 1
    return bits


def _bt4_to_hex(bits: list[int]) -> str:
    value = 0
    for bit in bits:
        value = value * 2 + bit
    return _HEX_DIGITS[value]


def _bt64_to_hex(bits: list[int]) -> str:
    return "".join(_bt4_to_hex(bits[i * 4: i * 4 + 4]) for i in range(16))


def _get_key_bytes(key: str) -> list[list[int]]:
    blocks = [_str_to_bt(key[i * 4: i * 4 + 4]) for i in range(len(key) // 4)]
    if len(key) % 4:
        blocks.append(_str_to_bt(key[(len(key) // 4) * 4:]))
    return blocks


def _init_permute(data: list[int]) -> list[int]:
    out = [0] * 64
    for i in range(4):
        m, n = 1 + 2 * i, 2 * i
        for j in range(7, -1, -1):
            k = 7 - j
            out[i * 8 + k] = data[j * 8 + m]
            out[i * 8 + k + 32] = data[j * 8 + n]
    return out


def _expand_permute(right: list[int]) -> list[int]:
    out = [0] * 48
    for i in range(8):
        out[i * 6 + 0] = right[31] if i == 0 else right[i * 4 - 1]
        out[i * 6 + 1] = right[i * 4 + 0]
        out[i * 6 + 2] = right[i * 4 + 1]
        out[i * 6 + 3] = right[i * 4 + 2]
        out[i * 6 + 4] = right[i * 4 + 3]
        out[i * 6 + 5] = right[0] if i == 7 else right[i * 4 + 4]
    return out


def _sbox_permute(expand: list[int]) -> list[int]:
    out = [0] * 32
    for i in range(8):
        row = expand[i * 6] * 2 + expand[i * 6 + 5]
        col = (expand[i * 6 + 1] * 8 + expand[i * 6 + 2] * 4
               + expand[i * 6 + 3] * 2 + expand[i * 6 + 4])
        value = _DES_SBOX[i][row][col]
        for j in range(4):
            out[i * 4 + j] = (value >> (3 - j)) & 1
    return out


def _p_permute(bits: list[int]) -> list[int]:
    return [bits[_DES_P[i]] for i in range(32)]


def _finally_permute(bits: list[int]) -> list[int]:
    return [bits[_DES_FP[i]] for i in range(64)]


def _xor(one: list[int], two: list[int]) -> list[int]:
    return [a ^ b for a, b in zip(one, two)]


def _generate_keys(key_byte: list[int]) -> list[list[int]]:
    key = [0] * 56
    for i in range(7):
        k = 7
        for j in range(8):
            key[i * 8 + j] = key_byte[8 * k + i]
            k -= 1
    keys: list[list[int]] = []
    for i in range(16):
        for _ in range(_DES_SHIFTS[i]):
            left, right = key[0], key[28]
            for k in range(27):
                key[k] = key[k + 1]
            for k in range(27):
                key[28 + k] = key[29 + k]
            key[27] = left
            key[55] = right
        keys.append([key[_DES_PC2[m]] for m in range(48)])
    return keys


def _des_encrypt_block(data_byte: list[int], key_byte: list[int]) -> list[int]:
    keys = _generate_keys(key_byte)
    ip = _init_permute(data_byte)
    left, right = ip[:32], ip[32:]
    for i in range(16):
        temp = left
        left = right
        right = _xor(_p_permute(_sbox_permute(_xor(_expand_permute(right), keys[i]))), temp)
    return _finally_permute(right + left)


def _encrypt_with_keys(block: list[int], key_blocks: list[list[int]]) -> list[int]:
    """des.js 单密钥分支：同一个 64 位块按密钥分块**依次**加密。"""
    result = block
    for key_block in key_blocks:
        result = _des_encrypt_block(result, key_block)
    return result


def kingo_str_enc(data: str, first_key: str) -> str:
    """移植 ``strEnc(data, firstKey, null, null)``（本平台只用这一分支）。"""
    key_blocks = _get_key_bytes(first_key) if first_key else []
    length = len(data)
    if length <= 0 or not key_blocks:
        return ""
    if length < 4:
        return _bt64_to_hex(_encrypt_with_keys(_str_to_bt(data), key_blocks))
    out: list[str] = []
    iterator, remainder = divmod(length, 4)
    for i in range(iterator):
        out.append(_bt64_to_hex(_encrypt_with_keys(_str_to_bt(data[i * 4: i * 4 + 4]), key_blocks)))
    if remainder > 0:
        out.append(_bt64_to_hex(_encrypt_with_keys(_str_to_bt(data[iterator * 4:]), key_blocks)))
    return "".join(out)


def _hex_to_bt64(hex_text: str) -> list[int]:
    """des.js ``hexToBt64``：16 个十六进制字符 → 64 位。"""
    bits = [0] * 64
    for i in range(16):
        value = int(hex_text[i], 16)
        for j in range(4):
            bits[i * 4 + j] = (value >> (3 - j)) & 1
    return bits


def _hex_to_byte(hex_text: str) -> str:
    """des.js ``hexToByte``：每 4 个十六进制字符还原 1 个字符（供 ``strDec`` 用）。"""
    out: list[str] = []
    for i in range(len(hex_text) // 4):
        out.append(chr(int(hex_text[i * 4: i * 4 + 4], 16)))
    return "".join(out)


def _des_decrypt_block(data_byte: list[int], key_byte: list[int]) -> list[int]:
    keys = _generate_keys(key_byte)
    ip = _init_permute(data_byte)
    left, right = ip[:32], ip[32:]
    for i in range(15, -1, -1):
        temp = left
        left = right
        right = _xor(_p_permute(_sbox_permute(_xor(_expand_permute(right), keys[i]))), temp)
    return _finally_permute(right + left)


def _decrypt_with_keys(block: list[int], key_blocks: list[list[int]]) -> list[int]:
    """与加密相反的顺序逐块解密。"""
    result = block
    for key_block in reversed(key_blocks):
        result = _des_decrypt_block(result, key_block)
    return result


def kingo_str_dec(data: str, first_key: str) -> str:
    """移植 ``strDec(data, firstKey, null, null)``：入参是**十六进制字符串**。

    与 des.js 的差异：① 末尾去掉分组填充产生的 ``\\x00``；② 入参不是合法十六进制时
    返回空串而不是抛异常（服务端/中间层可能给出被 URL 解码破坏的密文，属于可预期输入）。
    仅用于自测（mock 校验客户端提交的加密参数）；生产路径只会加密、不会解密。
    """
    key_blocks = _get_key_bytes(first_key) if first_key else []
    length = len(data)
    if length <= 0 or not key_blocks:
        return ""
    try:
        if length < 16:
            plain = _hex_to_byte(_bt64_to_hex(_decrypt_with_keys(_hex_to_bt64(data), key_blocks)))
            return plain.rstrip("\x00")
        out: list[str] = []
        iterator, remainder = divmod(length, 16)
        for i in range(iterator):
            block = _decrypt_with_keys(_hex_to_bt64(data[i * 16: i * 16 + 16]), key_blocks)
            out.append(_hex_to_byte(_bt64_to_hex(block)))
        if remainder > 0:
            block = _decrypt_with_keys(_hex_to_bt64(data[iterator * 16:]), key_blocks)
            out.append(_hex_to_byte(_bt64_to_hex(block)))
        return "".join(out).rstrip("\x00")
    except (ValueError, IndexError):
        return ""


def decode_kingo_params(body: str, temp_key: str) -> str:
    """从 ``params=<b64>&token=…&timestamp=…`` 还原明文参数（供 mock 自测校验）。"""
    field = ""
    for part in (body or "").split("&"):
        if part.startswith("params="):
            field = part[len("params="):]
            break
    if not field:
        return ""
    padded = field + "=" * (-len(field) % 4)
    raw = base64.b64decode(padded).decode("latin-1")
    return kingo_utf8to16(kingo_str_dec(raw, temp_key))


def kingo_utf16to8(text: str) -> str:
    """base64.js 的 ``utf16to8``。"""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if 0x0001 <= code <= 0x007F:
            out.append(char)
        elif code > 0x07FF:
            out.append(chr(0xE0 | ((code >> 12) & 0x0F)))
            out.append(chr(0x80 | ((code >> 6) & 0x3F)))
            out.append(chr(0x80 | (code & 0x3F)))
        else:
            out.append(chr(0xC0 | ((code >> 6) & 0x1F)))
            out.append(chr(0x80 | (code & 0x3F)))
    return "".join(out)


def kingo_b64_encode(text: str) -> str:
    """base64.js 的 ``base64encode(utf16to8(text))``。"""
    return base64.b64encode(kingo_utf16to8(text).encode("latin-1")).decode("ascii")


def kingo_utf8to16(text: str) -> str:
    """base64.js 的 ``utf8to16``（``utf16to8`` 的逆）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        code = ord(text[i])
        if code < 0x80:
            out.append(text[i])
            i += 1
        elif code < 0xE0:
            out.append(chr(((code & 0x1F) << 6) | (ord(text[i + 1]) & 0x3F)))
            i += 2
        else:
            out.append(chr(((code & 0x0F) << 12) | ((ord(text[i + 1]) & 0x3F) << 6)
                           | (ord(text[i + 2]) & 0x3F)))
            i += 3
    return "".join(out)


def kingo_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def encode_params(params: str, temp_key: str, timestamp: str) -> str:
    """等价于前端 ``getEncParams``：返回 ``params=…&token=…&timestamp=…`` 的表单体。

    **注意**：base64 结果里含 ``+``/``/``/``=``，直接当原始 body 发送会被服务端按表单
    规则把 ``+`` 解成空格。调用方应转成 dict 交给 HTTP 库编码（见 :func:`split_encoded_body`）。
    """
    token = kingo_md5(kingo_md5(params) + kingo_md5(timestamp))
    return f"params={kingo_b64_encode(kingo_str_enc(params, temp_key))}&token={token}&timestamp={timestamp}"


def split_encoded_body(body: str) -> dict[str, str]:
    """把 :func:`encode_params` 的表单体拆成字段字典（交给 HTTP 库做正确编码）。"""
    fields: dict[str, str] = {}
    for part in body.split("&"):
        key, _, value = part.partition("=")
        if key:
            fields[key] = value
    return fields


# ==========================================================================
# 青果 Kingosoft（2026-10-13 真机实测契约，见 README「教务系统（Kingosoft）选课接口现状」）
# ==========================================================================

#: 真实课程表表头 id → 统一字段名（来自实测的 DataTable.jsp 响应）
KINGOSOFT_COLUMNS: dict[str, str] = {
    "head_kc": "course_name",
    "head_xf": "credits",
    "head_zxs": "total_hours",
    "head_lb": "course_category",
    "head_kcsx": "course_attr",
    "head_skbh": "class_no",
    "head_skbjmc": "class_name",
    "head_xkfs": "select_mode",
    "head_rkjs": "teacher",
    "head_xk_status": "select_status",
    "head_kcdm": "course_code",
    "head_skbzdm": "class_group_code",
    "head_skbjdm": "teaching_class_id",
    # 隐藏的类别码：弹窗内层班级列表与提交都要带（实测浏览器请求体
    # kclb1=03&kclb2=01&kclb3=13&khfs=02），缺了它们服务端会返回空班级列表。
    "head_kclb1": "kclb1",
    "head_kclb2": "kclb2",
    "head_kclb3": "kclb3",
    "head_khfs": "khfs",
    "head_is_yxtj": "is_yxtj",
}

_XK_HEADER_CELL = re.compile(r"<t[dh][^>]*id=['\"]?(head_[A-Za-z0-9_]+)", re.IGNORECASE)
_ROW_PATTERN = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
#: 数据行单元格的行内 id：``<td id='tr0_kcdm' …>`` → 捕获 ``kcdm``
_ROW_CELL_ID = re.compile(r"<t[dh][^>]*id=['\"]?tr\d+_([A-Za-z0-9_]+)", re.IGNORECASE)

# --------------------------------------------------------------------------
# 选课弹窗（wsxk.zx_promt.jsp）解析
#
# 真实流程（页面 JS 实测）：列表行点「选择」→ isSelectableSkbjdm.action 校验 →
# 打开弹窗 wsxk.zx_promt.jsp → 弹窗里列出**可选的上课班级**（每个带 skbjdm）→
# 选中一个班级后由 electiveCourseForm.skbjdm 提交。
# 列表行的 skbjdm 对未选课程是空的，所以班号**只能从弹窗里取**。
# --------------------------------------------------------------------------

#: 弹窗内层班级列表的 tableId（实测：弹窗 iframe 以它加载班级，表头含 head_skbjdm）
POPUP_CLASS_TABLE_ID = "6142"

#: 内层班级列表的行内 id：``<td id='tr0_skbjdm' …>`` → 捕获 ``skbjdm``
_CLASS_ROW_CELL_ID = re.compile(
    r"<t[dh][^>]*id=['\"]?tr\d+_(?P<col>[A-Za-z0-9_]+)['\"]?[^>]*>(?P<v>.*?)</t[dh]>",
    re.IGNORECASE | re.DOTALL,
)
_TAGS_ONLY = re.compile(r"<[^>]+>")


def parse_class_list(html: str) -> list[dict[str, str]]:
    """解析弹窗内层班级列表（``DataTable.jsp?tableId=6142``）的每个上课班级。

    真实结构（已抓原文核实）：表头带 ``head_skbjdm``/``head_skbzdm``/``head_skfs_m``
    （授课方式）等；数据行形如 ``<td id='tr0_skbjdm'>21241931-039</td>``，
    页面 JS 用 ``kutil.getValue4TD(tr, "skbjdm")`` 取班号。

    返回每行的 ``{列名: 文本}``（列名取行内 id 去掉 ``tr{N}_`` 前缀）。
    **``tbody`` 为空是正常情况**——表示该课程当前没有可选上课班级。
    """
    if not html:
        return []
    rows: list[dict[str, str]] = []
    for row_html in _ROW_PATTERN.findall(html):
        cells: dict[str, str] = {}
        for match in _CLASS_ROW_CELL_ID.finditer(row_html):
            text = _TAGS_ONLY.sub("", match.group("v"))
            cells[match.group("col")] = " ".join(text.replace("&nbsp;", " ").split())
        if cells:
            rows.append(cells)
    return rows


def parse_class_skbjdms(html: str) -> list[str]:
    """从内层班级列表里取出所有**可选上课班级代码**（去重保序）。

    取不到就返回空列表 —— 由上层的可操作提示接手，**绝不猜一个班号去提交**。
    """
    found: list[str] = []
    for row in parse_class_list(html):
        value = (row.get("skbjdm") or "").strip()
        if value and value not in ("-", "0") and value not in found:
            found.append(value)
    return found


def parse_kingosoft_table(html: str) -> list[CandidateCourse]:
    """解析 Kingosoft ``DataTable.jsp`` 的课程表：按**表头 id** 定列，避免列序差异。

    真实表头列：课程/学分/总学时/课程类别/课程属性/上课班号/上课班级名称/选课方式/
    任课教师/投入选课币/购买教材/重修/选课状态/操作，隐藏列 ``head_kcdm``(课程代码)/
    ``head_skbzdm``(上课班组代码)/``head_skbjdm``(上课班级代码)。
    **注意**：真实表里没有「容量/余量」列，因此 ``capacity``/``enrolled`` 保持 ``None``，
    不做任何猜测。

    数据行的单元格形如 ``<td id='tr0_kcdm' name='kcdm'>01261231</td>``——**行内 id 带
    ``tr{N}_`` 前缀**。优先按行内 id 取值（比按表头位置索引稳），取不到再退回位置映射。
    """
    if not html:
        return []
    rows = _ROW_PATTERN.findall(html)
    if not rows:
        return []
    header_ids = _XK_HEADER_CELL.findall(rows[0])
    if not header_ids:
        return []
    courses: list[CandidateCourse] = []
    for row in rows[1:]:
        raw_cells = _TABLE_CELL.findall(row)
        cells = [_clean_html_text(cell) for cell in raw_cells]
        if not cells or len(cells) < 2:
            continue
        # 行内 id：tr{N}_{列名} → 列名。真实响应里数据行每个 <td> 都带 id
        by_row_id: dict[str, str] = {}
        for index, cell_html in enumerate(raw_cells):
            found = _ROW_CELL_ID.search(cell_html)
            if found and index < len(cells):
                by_row_id[found.group(1)] = cells[index]
        entry: dict[str, Any] = {}
        for index, cell_id in enumerate(header_ids):
            field_name = KINGOSOFT_COLUMNS.get(cell_id)
            if not field_name:
                continue
            value = by_row_id.get(cell_id[5:]) if cell_id.startswith("head_") else None
            if value is None:
                value = cells[index] if index < len(cells) else ""
            entry[field_name] = value
        course = CandidateCourse(
            teaching_class_id=str(entry.get("teaching_class_id", "") or "").strip(),
            course_code=str(entry.get("course_code", "") or "").strip(),
            course_name=str(entry.get("course_name", "") or "").strip(),
            teacher=str(entry.get("teacher", "") or "").strip(),
            credits=_to_float(entry.get("credits")),
            capacity=None,   # 真实表无此列
            enrolled=None,   # 真实表无此列
            time_text=str(entry.get("class_name", "") or "").strip(),
            raw={**entry, "select_status": entry.get("select_status", "")},
        )
        if course.teaching_class_id or course.course_name:
            courses.append(course)
    return courses


def _double_encoded_json(text: str) -> Mapping[str, Any]:
    """Kingosoft 常见形态：``{"status":"200","result":"{...}"}``（result 是**字符串**里的 JSON）。

    解析失败时返回 ``{}`` 而**不抛异常**：Kingosoft 在会话失效/未登录时会返回一段
    ``<script>alert('温馨提示：凭证已失效，请重新登录!')…</script>`` 的 HTML，而不是 JSON。
    这里容错之后，由 :meth:`KingosoftCourseAdapter._single_json` 统一判定成
    :class:`SessionExpiredError`，避免把 ``JSONDecodeError`` 冒到顶层变成「未预期的错误」。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if isinstance(payload, Mapping) and isinstance(payload.get("result"), str):
        try:
            inner = json.loads(payload["result"])
        except (TypeError, ValueError):
            return payload
        if isinstance(inner, Mapping):
            return {**payload, "result": inner}
    return payload if isinstance(payload, Mapping) else {}


#: Kingosoft 会话失效 / 缺菜单上下文时返回的页面标志（实测 2026-10-14）：
#: 未带有效会话访问任一 ``.action`` 端点，都会返回 ``<script>alert('温馨提示：凭证已失效…')``；
#: 缺菜单上下文或 Referer 不对时返回「无效的访问请求，请使用正常的途径访问本系统！」。
#: 两种写法都要覆盖——实测弹窗内层列表报的是**带"的"**那种。
KINGOSOFT_SESSION_EXPIRED_HINTS: tuple[str, ...] = (
    "凭证已失效",
    "无效访问请求",
    "无效的访问请求",
)

#: 服务器**频率限制**的标志（实测 2026-10-14）。
#: 它同样返回 HTTP 200 + 一张错误页，但**与会话失效是两回事**：
#: 误判成「请重新登录」会让用户白白去扫码，实际只需要等待。
KINGOSOFT_RATE_LIMIT_HINTS: tuple[str, ...] = (
    "请求太过频繁",
    "请求过于频繁",
)


def looks_rate_limited(text: str) -> bool:
    """正文是否是 Kingosoft 的频率限制页。"""
    haystack = text or ""
    return any(hint in haystack for hint in KINGOSOFT_RATE_LIMIT_HINTS)


def looks_session_expired(text: str) -> bool:
    """正文是否是 Kingosoft 的会话失效标志。

    **先排除频控页**：实测频控页里也会出现「无效的访问请求」字样，
    若不做排除，调用方无论怎么排序都可能把它当会话失效，
    用户就会白白去重新扫码。
    """
    haystack = text or ""
    if looks_rate_limited(haystack):
        return False
    return any(hint in haystack for hint in KINGOSOFT_SESSION_EXPIRED_HINTS)


class KingosoftCourseAdapter(CourseAdapter):
    """青果 Kingosoft 选课适配器（真实契约实测于 2026-10-13）。

    流程：CAS 换票（``POST {cas}/v1/tickets/{CASTGC}`` → ``GET {service}?ticket=``）建教务会话
    → 取菜单上下文（否则页面报「无效访问请求」）→ 取选课上下文（学年/学期/轮次/课程范围）
    → 表单 POST ``taglib/DataTable.jsp?tableId=…`` 查询课程（GBK HTML 表格）
    → 提交/退课走 ``jw/common/*.action``，参数经 ``getEncParams`` 加密。
    """

    name = "kingosoft"

    def __init__(self, config: AppConfig, session: QkSession) -> None:
        self.config = config
        self.session = session
        self._ready = False
        self._context: dict[str, Any] = {}
        #: 最近一次服务器提示（如校验可选择性被拒的原因），供上层展示
        self.last_message = ""
        #: 最近打开的选课弹窗 URL。提交时的 ``Referer`` 必须用它 ——
        #: 浏览器抓包实测：saveElectiveCourse 的 Referer 是弹窗页而非选课页，
        #: 用错 Referer 会得到空响应。
        self._popup_url = ""

    # ------------------------------------------------------------ 会话
    def _cookie_value(self, name: str) -> str:
        cookies = getattr(getattr(getattr(self.session, "transport", None), "_session", None), "cookies", None)
        if cookies is None:
            return ""
        try:
            return next((item.value for item in cookies if item.name == name), "")
        except TypeError:  # pragma: no cover - 非标准 CookieJar
            return ""

    def ensure_session(self, *, force: bool = False) -> None:
        """建立教务会话（幂等）：换票 → caslogin → 菜单上下文。

        **只要拿得到 CASTGC 就一定换票**，不再用「有没有 JSESSIONID」来跳过。
        踩过的坑：扫码登录会带回**门户** ``my.shengda.edu.cn`` 的 JSESSIONID
        （与 CAS 的 ``cas.shengda.edu.cn``、教务的 ``jw.shengda.edu.cn:8090`` 都不是同一个），
        旧逻辑看到同名 Cookie 就跳过换票，于是拿门户会话去打教务，一路「凭证已失效」。

        **没有 CASTGC 时不立刻报错**：某些登录路径（如 mock 扫码）只留下 JSESSIONID，
        此时先按「已有会话」继续，由真实请求去判定；真的失效了，
        :meth:`_recover_session` / 调用方的重试逻辑会给出「请重新登录」的提示。
        """
        if self._ready and not force:
            return
        jwgl = self.config.jwgl
        base = self._base()
        service = jwgl.cas_service or f"{base}/caslogin"
        tgt = self._cookie_value("CASTGC")
        if not tgt:
            # 没有 CAS 票据：可能已经有可用的教务会话，交给真实请求判活
            self._ready = True
            self._menu_context()
            self._warmup_pages()
            return
        tickets_path = (self.config.cas.wechat.rest_tickets_path
                        or "/lyuapServer/v1/tickets").rstrip("/")
        cas_base = self._cas_base()
        created = self.session.post(
            f"{cas_base}{tickets_path}/{tgt}", data={"service": service},
            headers={"Accept": "application/json"}, allow_redirects=False,
        )
        ticket = re.search(r"(ST-[A-Za-z0-9._\-]+)", created.text or "")
        if not ticket:
            raise SessionExpiredError(
                f"换教务 ticket 失败（HTTP {created.status}）：CAS 会话可能已过期，"
                "请重新 `python -m qk login --wechat --save-session`。"
            )
        joiner = "&" if "?" in service else "?"
        self.session.get(f"{service}{joiner}ticket={ticket.group(1)}")
        self._menu_context()
        self._warmup_pages()
        self._ready = True

    def _menu_context(self) -> None:
        """菜单上下文：缺这一步，选课页/报表页会返回「错误:无效访问请求」。"""
        self.session.post(
            self._url(f"{self.config.jwgl.menu_context_path}"
                      f"?menucode={self.config.jwgl.menucode}"),
            data={}, headers={"Referer": self._url("/frame/homes.html")},
            allow_redirects=True,
        )

    def _warmup_pages(self) -> None:
        """按浏览器的真实顺序预访问几个页面，**把学生身份绑定到会话上**。

        2026-10-14 实测（决定性对照）：

        * 用本工具「换票 + 菜单上下文」建出来的会话提交选课 →
          ``{"status":"400","message":"当前选课操作的用户不是选课学生本人!"}``
        * 换成**浏览器的** ``JSESSIONID`` 提交同样参数 →
          不再报「不是本人」（变成限流/业务消息）

        两者 ``xh`` 都是同一个学号，差异只在会话。对比浏览器实际访问顺序，本工具
        少了首页 / 下拉字典 / **选课页** 这几步；其中最可疑的是选课页 ——
        服务端很可能在首次请求选课页时把当前学生写进会话。这里照浏览器补齐。
        """
        jwgl = self.config.jwgl
        steps = (
            ("/frame/homes.html", None),
            ("/frame/droplist/getDropLists.action", {}),
            ("/student/wsxk.zx.html", None),
        )
        for path, data in steps:
            try:
                if data is None:
                    self.session.get(self._url(path),
                                     headers={"Referer": self._url("/frame/homes.html")})
                else:
                    self.session.post(self._url(path), data=data,
                                      headers={"Referer": self._url("/frame/homes.html")})
            except TransportError:
                # 预热只是尽力而为：失败不阻断建会话，由真实请求判活
                continue

    def _recover_session(self, purpose: str) -> bool:
        """判定失效后重建一次教务会话（返回是否**真的**重建了）。

        没有 CAS 票据时无法重建（换票是唯一入口），此时返回 ``False``，
        让调用方给出「请重新登录」的可操作提示，而不是假装恢复成功。
        """
        if not self._cookie_value("CASTGC"):
            self._ready = False
            return False
        self._ready = False
        try:
            self.ensure_session(force=True)
        except (SessionExpiredError, TransportError):
            return False
        self._ready = True
        return True

    def _base(self) -> str:
        """教务系统基址：Kingosoft 专用字段优先，回退到 ``jwgl.base_url``。"""
        jwgl = self.config.jwgl
        return (jwgl.kingosoft_base_url or jwgl.base_url).rstrip("/")

    def _url(self, path: str) -> str:
        """把基址与路径拼成 URL（统一单斜杠，便于阅读与断言）。

        **更正（2026-10-14 晚）**：先前这里写着「拼出双斜杠导致服务端空响应」，
        那是**误判**。实测 ``f"{base}{path}"`` 在 ``_base()`` 已去尾斜杠、
        ``path`` 以 ``/`` 开头时，结果恰好只有一个斜杠；
        当时看到的 ``//jw.shengda...`` 只是调试输出把 ``https:`` 截断了。

        真正导致写请求「HTTP 200 + 空响应」的原因是 **Referer 用错页**：
        提交必须带选课弹窗 URL 作为 Referer（见 :meth:`_post_action`）。

        本方法保留的价值：调用点统一、绝对 URL 直通、可断言无多余斜杠。
        """
        base = self._base()
        if path.startswith(("http://", "https://")):
            return path
        return f"{base}/{path.lstrip('/')}"

    def _cas_base(self) -> str:
        parsed = urlparse(self.config.cas.login_url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""

    def _single_json(self, text: str, *, purpose: str) -> Mapping[str, Any]:
        """解析一次 Kingosoft 响应；会话失效时抛 :class:`SessionExpiredError`。

        未带有效会话时，Kingosoft 的 ``.action`` 端点会返回 **HTTP 200** 加一段
        ``<script>alert('温馨提示：凭证已失效，请重新登录!')…</script>`` 的 HTML。
        这类响应必须在**解析 JSON 之前**识别出来，否则 ``json.loads`` 会抛
        ``JSONDecodeError`` 并冒到顶层，用户只能看到「未预期的错误」而拿不到
        「请重新登录」的可操作提示。
        """
        if looks_rate_limited(text):
            # 频控与会话失效都会返回 HTTP 200 的错误页，必须**先**区分：
            # 误报成「请重新登录」会让用户白白去扫码，实际只需等一会儿。
            raise SelectionError(
                f"{purpose}被教务服务器限流（返回「请求太过频繁」）。"
                "会话仍然有效，请等待 1–2 分钟再重试，"
                "并把 grab.retry_interval 调大（如 3–5 秒）以免再次触发。"
            )
        if looks_session_expired(text):
            self._ready = False   # 下次调用重新走换票/菜单上下文
            raise SessionExpiredError(
                f"{purpose}时教务会话已失效（服务器返回「凭证已失效」）。"
                "请重新登录：python -m qk login --wechat --save-session"
            )
        payload = _double_encoded_json(text or "")
        if not payload:
            # 实测（2026-10-14）：短时间内反复提交后，服务端会**静默返回空响应**
            # （HTTP 200、无 content-type、正文 0 字节）——这是限流/拦截的形态。
            # 空响应**不等于**会话失效：拿它去让用户重新扫码是误导。
            if not (text or "").strip():
                raise SelectionError(
                    f"{purpose}收到**空响应**（HTTP 200 但正文 0 字节），"
                    "服务端多半在限流/拦截。会话通常仍然有效：请等待 2–5 分钟再试，"
                    "并把 grab.retry_interval 调到 3–5 秒以上。"
                    "若持续为空，再重新登录一次排除会话问题。"
                )
            self._ready = False
            raise SessionExpiredError(
                f"{purpose}返回的内容不是预期的 JSON（可能是未登录页面或会话已过期）。"
                "请重新登录：python -m qk login --wechat --save-session"
            )
        return payload

    # ------------------------------------------------------------ 选课上下文
    def context(self) -> dict[str, Any]:
        """取选课上下文（学年/学期/课程范围/轮次等），真实来源 ``getWsxkTimeRange.action``。"""
        self.ensure_session()
        if self._context:
            return self._context
        jwgl = self.config.jwgl
        result = self.session.get(
            self._url(f"{jwgl.time_range_path}?xktype={jwgl.xktype}"),
            headers={"Referer": self._url("/frame/homes.html")},
        )
        payload = self._single_json(result.text, purpose="取选课上下文")
        inner = payload.get("result")
        self._context = dict(inner) if isinstance(inner, Mapping) else {}
        return self._context

    def _form_params(self, keyword: str = "") -> dict[str, str]:
        context = self.context()
        jwgl = self.config.jwgl
        return {
            "xktype": str(jwgl.xktype),
            "xh": str(context.get("xh") or self.config.creds.student_id or ""),
            "xn": str(context.get("xn") or ""),
            "xq": str(context.get("xqM") or ""),
            "nj": str(context.get("nj") or ""),
            "zydm": str(context.get("zydm") or ""),
            "yxbdm": str(context.get("yxbdm") or ""),
            # 课程范围：服务器只认 ``kcfw``（2026-10-13 实测：只发 ``_kcfw`` 返回 0 行，
            # 发 ``kcfw`` 返回全部课程）。页面上 ``<select id="kcfw" name="kcfw">`` 有 name
            # 属性、自身会被表单提交，``_kcfw`` 只是 ``QryData()`` 里
            # ``j$("#_kcfw").val(j$("#kcfw").val())`` 同步的隐藏副本。
            # 这里照页面原样**两个都发**，避免依赖服务端读哪个键的实现细节。
            "kcfw": str(context.get("kcfw") or ""),
            "_kcfw": str(context.get("kcfw") or ""),
            "kcmc": keyword,
            "initQry": "0",
            "lcid": str(context.get("lcid") or ""),
            "items": "",
            "is_xjls": str(context.get("isXjls") or ""),
            "zysx": "",
            "sfbd": str(context.get("sfbd") or ""),
            "djs": str(context.get("djs") or ""),
            "kxkc": str(context.get("kxkc") or ""),
            "xxkckzfs": str(context.get("xxkckzfs") or ""),
            "yxkzyfxxk": str(context.get("yxkzyfxxk") or "0"),
            "yxsjct": str(context.get("yxsjct") or ""),
            "xfsx": str(context.get("xfsx") or ""),
            "mssx": str(context.get("mssx") or ""),
            "hidkcmc": "",
            "xwxmkc": "on",
            **(self.config.jwgl.extra_params or {}),
        }

    def _with_session_retry(self, operation: Callable[[], str], purpose: str) -> str:
        """执行 ``operation``（返回响应正文）；若显示会话失效则重建会话并**重试一次**。

        只重试一次，避免在服务器异常时反复换票加大压力。
        """
        text = operation()
        if not looks_session_expired(text):
            return text
        if not self._recover_session(purpose):
            raise SessionExpiredError(
                f"{purpose}时会话已失效，且自动重建未成功。请重新登录："
                "python -m qk login --wechat --save-session"
            )
        text = operation()
        if looks_session_expired(text):
            self._ready = False
            raise SessionExpiredError(
                f"{purpose}时会话已失效（已自动重建过一次仍未通过）。请重新登录："
                "python -m qk login --wechat --save-session"
            )
        return text

    # ------------------------------------------------------------ 查询
    def fetch_candidates(self, keyword: str = "") -> Sequence[CandidateCourse]:
        """查询候选课程（真实接口：表单 POST 到 ``taglib/DataTable.jsp``）。"""
        jwgl = self.config.jwgl
        table_id = jwgl.course_list_table_id or "2568"
        self.ensure_session()

        def _query() -> str:
            result = self.session.post(
                self._url(f"{jwgl.data_table_path}?tableId={table_id}&fre=1"),
                data=self._form_params(keyword),
                headers={"Referer": self._url("/student/wsxk.zx.html")},
            )
            if result.status in (401, 403):
                raise SessionExpiredError(
                    f"查询候选课程返回 HTTP {result.status}，教务会话已失效。"
                )
            return result.text or ""

        text = self._with_session_retry(_query, "查询候选课程")
        courses = parse_kingosoft_table(text)
        # 客户端过滤：按 jwgl.exclude_course_attrs 排除指定授课形式（如「通识面授课」）。
        # 服务器仍会返回全部课程，这里只裁剪展示/匹配用的列表。
        return filter_excluded_attrs(courses, self.config.jwgl.exclude_course_attrs)

    def fetch_selected(self) -> Sequence[CandidateCourse]:
        """已选课程列表：真实端点尚未确认（不猜路径、不伪造），返回空列表。"""
        return []

    # ------------------------------------------------------------ 写入
    def _secret(self, params: str) -> str:
        """按前端 ``getEncParams`` 生成写请求的表单体（密钥/时间戳由服务器下发）。"""
        jwgl = self.config.jwgl
        key = self.session.get(self._url(jwgl.temp_key_path)).text or ""
        stamp = self.session.get(self._url(jwgl.temp_time_path)).text or ""
        return encode_params(params, key.strip(), stamp.strip())

    def _post_action(self, path: str, params: str, course: CandidateCourse, *, purpose: str) -> SelectionResult:
        url = self._url(path)
        # 请求头照浏览器抓包（2026-10-14 从一次成功提交的 reqid 快照核对）：
        #   Referer 必须指向**选课弹窗**（用选课页会拿到空响应）
        #   Content-Type / X-Requested-With 与 jQuery doAjax 一致
        headers = {
            "Origin": self._base(),
            "Referer": self._popup_url or self._url("/student/wsxk.zx.html"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            body = split_encoded_body(self._secret(params))
            result = self.session.post(url, data=body, headers=headers)
        except TransportError as exc:
            return SelectionResult(Outcome.UNKNOWN, course, message=f"{purpose}请求失败：{exc}")
        if result.status in (401, 403):
            raise SessionExpiredError(f"{purpose}返回 HTTP {result.status}，教务会话已失效。")
        payload = self._single_json(result.text, purpose=purpose)
        status = str(payload.get("status", "") or "")
        message = str(payload.get("message", "") or "")
        if status == "200":
            outcome = Outcome.SUCCESS
        elif payload.get("result") in (True, "true") and not message:
            outcome = Outcome.SUCCESS
        else:
            outcome = Outcome(classify_message(message or status))
        return SelectionResult(outcome, course, message=message or status, raw_text=result.text or "")

    def _write_params(self, course: CandidateCourse, *, xktype: str | None = None) -> str:
        """按**浏览器真实提交明文**拼参数（2026-10-14 从一次成功提交抓包解密核对）。

        金标准来自浏览器里一次**真正选上课**的提交：把该请求的 ``params`` 用服务端下发的
        临时密钥解密后得到 34 个字段。要点：

        * 字段名**不带** ``electiveCourseForm.`` 前缀。页面 ``doPostSave`` 里的
          ``params.replaceAll("electiveCourseForm.", "")`` **确实生效** ——
          表单 DOM 上有前缀，但加密前被剥掉了。（先前照着 DOM 加前缀是错的。）
        * 班号写在 ``skbjdm``（值形如 ``01265731-002``）；``txt_skbjdm`` 提交时是**空**。
        * ``is_buy_book`` / ``is_cx`` / ``is_yxtj`` 是 ``0`` / ``0`` / ``1``（不是空串）。
        * ``njzy`` 里的竖线在明文里就是 ``%7C``（页面未解码）。
        * ``xf`` 形如 ``1.0``；``xfsx`` / ``mssx`` 字面量是 ``undefined``。
        """
        context = self.context()
        raw = course.raw or {}
        xf = f"{course.credits:.1f}" if course.credits is not None else ""
        nj = str(context.get("nj") or "")
        zydm = str(context.get("zydm") or "")
        pairs = {
            "xktype": str(xktype or self.config.jwgl.xktype),
            "lcid": str(context.get("lcid") or ""),
            "xxkckzfs": str(context.get("xxkckzfs") or "0"),
            "yxkzyfxxk": str(context.get("yxkzyfxxk") or "0"),
            "yxsjct": str(context.get("yxsjct") or "0"),
            "xn": str(context.get("xn") or ""),
            "xq": str(context.get("xqM") or ""),
            "xh": str(context.get("xh") or self.config.creds.student_id or ""),
            "nj": nj,
            "zydm": zydm,
            "kcdm": course.course_code,
            "kclb1": str(raw.get("kclb1", "") or ""),
            "kclb2": str(raw.get("kclb2", "") or ""),
            "kclb3": str(raw.get("kclb3", "") or ""),
            "khfs": str(raw.get("khfs", "") or ""),
            "xfsx": "undefined",
            "mssx": "undefined",
            # 班号写进 skbjdm（页面 doConfirm() 里 j$("#skbjdm").val(skbjdm) 后再 submit）
            "skbjdm": course.teaching_class_id,
            "skbzdm": str(raw.get("class_group_code", "") or ""),
            "xf": xf,
            "is_checkTime": "1",
            "zfx_m": "0",
            "outnumber": "0",
            "xsszxq": "1",
            "xqdm": "",
            "kcmc": "",
            "kcfw": str(context.get("kcfw") or ""),
            # 明文里就是 %7C（页面没解码）
            "njzy": f"{nj}%7C{zydm}" if nj or zydm else "",
            "txt_skbjdm": "",
            "xk_points": "0",
            # 弹窗里「购买教材 / 重修 / 上课班调剂」三个开关的提交值，
            # 实测默认是 0 / 0 / 1（上课班调剂默认勾选）
            "is_buy_book": str(raw.get("is_buy_book", "") or "0"),
            "is_cx": str(raw.get("is_cx", "") or "0"),
            "is_yxtj": str(raw.get("is_yxtj", "") or "1"),
            "menucode_current": "",
        }
        return "&".join(f"{key}={value}" for key, value in pairs.items())

    def _popup_params(self, course: CandidateCourse) -> str:
        """照页面 ``myShowSkbjdm()`` 拼选课弹窗的参数（明文，随后由 getEncParams 加密）。

        页面在 ``viewResume()`` 里先过 ``isSelectableSkbjdm.action`` 校验，
        通过后才打开弹窗；这里为保证流程一致也先校验。
        """
        context = self.context()
        xf = f"{course.credits:g}" if course.credits is not None else ""
        pairs = {
            "kcdm": course.course_code,
            "xh": str(context.get("xh") or self.config.creds.student_id or ""),
            "xn": str(context.get("xn") or ""),
            "xq": str(context.get("xqM") or ""),
            "checked": "false",
            "skbjdm": course.teaching_class_id or "",
            "nj": str(context.get("nj") or ""),
            "njzy": "",
            "zydm": str(context.get("zydm") or ""),
            "kclb1": "", "kclb2": "", "kclb3": "", "khfs": "",
            "xf": xf, "xfsx": "", "mssx": "",
            "xktype": str(self.config.jwgl.xktype),
            "lcid": str(context.get("lcid") or ""),
            "xxkckzfs": str(context.get("xxkckzfs") or ""),
            "yxsjct": str(context.get("yxsjct") or ""),
            "yxkzyfxxk": str(context.get("yxkzyfxxk") or "0"),
            "isbuybook": "", "iscx": "", "isyxtj": "",
            "kcfw": str(context.get("kcfw") or ""),
        }
        return "&".join(f"{key}={value}" for key, value in pairs.items())

    def ensure_selectable(self, course: CandidateCourse) -> bool:
        """对应页面 ``isSelectableSkbjdm.action``：问服务器这门课能否选。

        返回 ``True`` 表示允许选；``False`` 表示服务器明确拒绝（如已选过、不在范围），
        拒绝原因在 :attr:`last_message` 里。
        """
        self.ensure_session()
        context = self.context()
        query = (f"xn={context.get('xn')}&xq_m={context.get('xqM')}"
                 f"&xh={context.get('xh')}&kcdm={course.course_code}")
        result = self.session.get(
            self._url(f"/jw/common/isSelectableSkbjdm.action?{query}"),
            headers={"Referer": self._url("/student/wsxk.zx.html")},
        )
        if looks_session_expired(result.text):
            self._ready = False
            raise SessionExpiredError("校验可选择性时会话已失效，请重新登录。")
        payload = _double_encoded_json(result.text or "")
        self.last_message = str(payload.get("message", "") or "")
        return str(payload.get("status", "")) == "200"

    def resolve_teaching_class_ids(self, course: CandidateCourse) -> list[str]:
        """解析这门课**可选的上课班级代码**列表。

        这是自动选课的关键一步：主列表行里的 ``skbjdm`` 对未选课程是空的，
        班号只能从**弹窗内层班级列表**里取。

        真实流程（已抓原文核实）：
        1. 打开弹窗 ``student/report/wsxk.zx_promt.jsp``（参数经 getEncParams 加密）；
        2. 弹窗里的 iframe 用 ``taglib/DataTable.jsp?tableId=6142&fre=1&xn=..&xq_m=..&xh=..
           &kcdm=<课程号>&skbjdm=&xktype=2&kcfw=..&isyxkc=false&lcid=..`` 拉班级列表；
        3. 数据行形如 ``<td id='tr0_skbjdm'>21241931-039</td>``。

        **Referer 必须是弹窗 URL**：用选课页当 Referer 会被服务端判为
        「无效的访问请求」（实测），用弹窗 URL 才正常返回。

        只发**读**请求，不提交任何选课。``tbody`` 为空表示该课当前没有可选班级。
        """
        self.ensure_session()
        base = self._base()
        context = self.context()
        encoded = self._secret(self._popup_params(course))
        popup_url = self._url(f"/student/report/wsxk.zx_promt.jsp?{encoded}")
        # 记下来供提交时作 Referer —— 浏览器实测提交的 Referer 就是这个弹窗 URL
        self._popup_url = popup_url
        popup = self.session.get(popup_url,
                                 headers={"Referer": self._url("/student/wsxk.zx.html")})
        if looks_session_expired(popup.text):
            self._ready = False
            raise SessionExpiredError("打开选课弹窗时会话已失效，请重新登录。")

        query = (
            f"xn={context.get('xn')}&xq_m={context.get('xqM')}&xh={context.get('xh')}"
            f"&kcdm={course.course_code}&skbjdm=&xktype={self.config.jwgl.xktype}"
            f"&kcfw={context.get('kcfw') or ''}&isyxkc=false&lcid={context.get('lcid') or ''}"
        )
        # 请求体必须带上**课程类别码**（kclb1/kclb2/kclb3/khfs，来自主列表行的隐藏列）。
        # 实测教训：不带它们时服务端返回「表头正常但 tbody 为空」——
        # 看起来像「这门课没有可选班级」，实际是查询条件缺失。
        # 这些字段的真实值见 2026-10-14 浏览器抓包（reqid=217）：
        #   kclb1=03&kclb2=01&kclb3=13&khfs=02
        raw = course.raw or {}
        popup_field = "electiveCourseForm."
        body = {
            f"{popup_field}xktype": str(self.config.jwgl.xktype),
            f"{popup_field}lcid": str(context.get("lcid") or ""),
            "xxkckzfs": str(context.get("xxkckzfs") or ""),
            "yxkzyfxxk": str(context.get("yxkzyfxxk") or "0"),
            f"{popup_field}yxsjct": str(context.get("yxsjct") or ""),
            f"{popup_field}xn": str(context.get("xn") or ""),
            f"{popup_field}xq": str(context.get("xqM") or ""),
            f"{popup_field}xh": str(context.get("xh") or ""),
            f"{popup_field}nj": str(context.get("nj") or ""),
            f"{popup_field}zydm": str(context.get("zydm") or ""),
            f"{popup_field}kcdm": course.course_code,
            f"{popup_field}kclb1": str(raw.get("kclb1", "") or ""),
            f"{popup_field}kclb2": str(raw.get("kclb2", "") or ""),
            f"{popup_field}kclb3": str(raw.get("kclb3", "") or ""),
            f"{popup_field}khfs": str(raw.get("khfs", "") or ""),
            f"{popup_field}xfsx": "undefined",
            f"{popup_field}mssx": "undefined",
            f"{popup_field}skbjdm": "",
            f"{popup_field}skbzdm": "",
            f"{popup_field}xf": f"{course.credits:g}" if course.credits is not None else "",
            f"{popup_field}is_checkTime": "1",
            f"{popup_field}zfx_m": "0",
            f"{popup_field}outnumber": "0",
            "xsszxq": "1",
            "xqdm": "",
            "kcmc": "",
            f"{popup_field}kcfw": str(context.get("kcfw") or ""),
            f"{popup_field}njzy": f"{context.get('nj') or ''}|{context.get('zydm') or ''}",
            "txt_skbjdm": "",
            f"{popup_field}xk_points": "0",
            f"{popup_field}is_buy_book": "",
            f"{popup_field}is_cx": "",
            f"{popup_field}is_yxtj": "",
            "menucode_current": "",
        }
        inner = self.session.post(
            self._url(f"{self.config.jwgl.data_table_path}"
                      f"?tableId={POPUP_CLASS_TABLE_ID}&fre=1&{query}"),
            data=body,
            headers={"Referer": popup_url, "Origin": base},
        )
        if looks_session_expired(inner.text):
            self._ready = False
            raise SessionExpiredError("查询上课班级时会话已失效，请重新登录。")
        return parse_class_skbjdms(inner.text or "")

    def select(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交选课：真实端点 ``jw/common/saveElectiveCourse.action``（参数加密）。

        ``skbjdm`` 为空时（未选课程在列表里就是空的）会**先打开选课弹窗解析班号**，
        取不到则抛 :class:`CourseNotFoundError` 并说明原因，绝不猜一个班号提交。
        """
        if dry_run:
            return SelectionResult(Outcome.DRY_RUN, course, message="演练模式：未提交选课")
        if not course.teaching_class_id:
            course = self._with_resolved_class(course)
        path = self.config.jwgl.select_path or "/jw/common/saveElectiveCourse.action"
        return self._post_action(path, self._write_params(course), course, purpose="提交选课")

    def _with_resolved_class(self, course: CandidateCourse) -> CandidateCourse:
        """打开弹窗解析上课班级代码；解析不到就给出可操作错误。"""
        if not course.course_code:
            raise CourseNotFoundError(
                "该课程既没有上课班级代码（skbjdm）也没有课程代码（kcdm），无法提交选课。"
            )
        try:
            classes = self.resolve_teaching_class_ids(course)
        except TransportError as exc:
            raise SelectionError(f"打开选课弹窗失败：{exc}") from exc
        if not classes:
            raise CourseNotFoundError(
                f"课程 {course.course_code}（{course.course_name}）在选课弹窗里没有可用上课班级，"
                "可能已满员或不在可选范围。可先跑 `python -m qk list` 核对，"
                "或到选课页面手动确认。"
            )
        chosen = course.teaching_class_id if course.teaching_class_id in classes else classes[0]
        return replace(course, teaching_class_id=chosen)

    def _drop_params(self, course: CandidateCourse, *, xktype: str | None = None) -> str:
        """退课参数：与选课**完全不同**，只有 8 个字段。

        来自页面 ``cancelElectiveCourse()`` 的真实拼串（2026-10-14 从运行中的页面读取）::

            var params = "electiveCourseForm.xn=" + xn
                       + "&electiveCourseForm.xq=" + xq
                       + "&electiveCourseForm.xh=" + xh
                       + "&electiveCourseForm.kcdm=" + kcdm
                       + "&electiveCourseForm.skbjdm=" + skbjdm
                       + "&electiveCourseForm.skbzdm=" + skbzdm
                       + "&electiveCourseForm.lcid=" + lcid
                       + "&electiveCourseForm.xktype=2";
            params = new String(params).replaceAll("electiveCourseForm.", "");

        注意最后那句：**前缀在加密前被剥掉** —— 与选课提交的结论一致
        （先前把选课参数加上前缀是错的，根因即在此）。
        端点 ``jw/common/cancelElectiveCourse.action``。
        """
        context = self.context()
        raw = course.raw or {}
        pairs = {
            "xn": str(context.get("xn") or ""),
            "xq": str(context.get("xqM") or ""),
            "xh": str(context.get("xh") or self.config.creds.student_id or ""),
            "kcdm": course.course_code,
            "skbjdm": course.teaching_class_id,
            "skbzdm": str(raw.get("class_group_code", "") or ""),
            "lcid": str(context.get("lcid") or ""),
            "xktype": str(xktype or self.config.jwgl.xktype),
        }
        return "&".join(f"{key}={value}" for key, value in pairs.items())

    def drop(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交退课：真实端点 ``jw/common/cancelElectiveCourse.action``（参数加密）。"""
        if dry_run:
            return SelectionResult(Outcome.DRY_RUN, course, message="演练模式：未提交退课")
        if not course.teaching_class_id:
            raise CourseNotFoundError("该课程缺少上课班级代码（skbjdm），无法提交退课。")
        path = self.config.jwgl.drop_path or "/jw/common/cancelElectiveCourse.action"
        return self._post_action(path, self._drop_params(course), course, purpose="提交退课")

    # ------------------------------------------------------------ 工厂
    @classmethod
    def create(cls, config: AppConfig, session: QkSession | None = None) -> "KingosoftCourseAdapter":
        return cls(config, session or QkSession.from_config(config))


def build_course_adapter(config: AppConfig, session: QkSession | None = None) -> CourseAdapter:
    """按 ``jwgl.platform`` 选择选课适配器。"""
    platform = (config.jwgl.platform or "auto").lower()
    if platform == "kingosoft":
        return KingosoftCourseAdapter.create(config, session)
    if platform in ("auto", "custom", "qz", "jwgl"):
        return JwglCourseAdapter.create(config, session)
    raise PlatformUnsupportedError(
        f"未知的 jwgl.platform：{platform}。可选用值：auto / custom / qz / kingosoft；"
        "新增平台请参考 README「新增接口 / 新平台适配的接入方式」。"
    )


__all__ = [
    "CODE_TO_OUTCOME",
    "FIELD_ALIASES",
    "KINGOSOFT_COLUMNS",
    "KINGOSOFT_SESSION_EXPIRED_HINTS",
    "JwglCourseAdapter",
    "KingosoftCourseAdapter",
    "build_course_adapter",
    "course_from_mapping",
    "looks_session_expired",
    "parse_candidates",
    "parse_candidates_html",
    "parse_class_list",
    "parse_class_skbjdms",
    "parse_kingosoft_table",
]
