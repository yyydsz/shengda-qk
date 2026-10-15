"""登录层与选课层的抽象接口。

平台差异（CAS 统一身份认证 / 教务选课接口的具体形态）只能出现在本包的
子模块里；CLI、抢课编排与测试只依赖这里的抽象，因此换学校/换平台时只需
新增一个适配器，不必改动核心逻辑。

新增适配器的步骤：

1. 实现 :class:`LoginAdapter`（返回 :class:`~qk.models.SessionInfo`）；
2. 实现 :class:`CourseAdapter`（查询候选课程 + 选课/退课）；
3. 在 ``qk/cli.py`` 的工厂函数里按 ``jwgl.platform`` 选择你的实现；
4. 写一份离线单测，用假传输驱动它。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..models import CandidateCourse, SelectionResult, SessionInfo


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class AdapterError(Exception):
    """适配器层错误基类：消息应当能直接展示给用户。"""


class AuthError(AdapterError):
    """账号或密码错误、认证被拒绝。"""


class CaptchaRequiredError(AdapterError):
    """需要验证码，且当前无法自动处理。"""

    def __init__(self, message: str, *, captcha_url: str = "") -> None:
        super().__init__(message)
        self.captcha_url = captcha_url


class SessionExpiredError(AdapterError):
    """会话过期，需要重新登录。"""


class SelectionError(AdapterError):
    """选课/退课请求本身失败（参数被拒绝、接口报错）。"""


class CourseNotFoundError(AdapterError):
    """没有匹配到任何教学班。"""


class PlatformUnsupportedError(AdapterError):
    """当前平台标识没有对应适配器。"""


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


@runtime_checkable
class LoginAdapter(Protocol):
    """登录层：负责建立可用会话，并判断会话是否仍然有效。"""

    name: str

    def login(self, *, dry_run: bool = False) -> SessionInfo:
        """登录并返回会话信息；失败抛 :class:`AuthError` 等。"""
        ...

    def is_session_alive(self) -> bool:
        """探测当前 Cookie 是否仍然有效。"""
        ...

    def logout(self) -> None:
        """尽力登出（失败不抛异常）。"""
        ...


@runtime_checkable
class CourseAdapter(Protocol):
    """选课层：查询候选教学班、选课、退课、列出已选。"""

    name: str

    def fetch_candidates(self, keyword: str = "") -> Sequence[CandidateCourse]:
        """拉取可选教学班列表（``keyword`` 为空表示全部）。"""
        ...

    def select(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交选课。``dry_run=True`` 时不得发出任何写请求。"""
        ...

    def drop(self, course: CandidateCourse, *, dry_run: bool = False) -> SelectionResult:
        """提交退课。``dry_run=True`` 时不得发出任何写请求。"""
        ...

    def fetch_selected(self) -> Sequence[CandidateCourse]:
        """已选课程列表。"""
        ...


# --------------------------------------------------------------------------
# 结果解析辅助
# --------------------------------------------------------------------------

#: 把服务器返回文本里的关键词映射到统一结果分类（顺序敏感：先匹配先返回）
MESSAGE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("已经选", "已选", "重复选课", "已修读"), "ALREADY_SELECTED"),
    (("人数已满", "已满", "容量已满", "无余量", "名额已满"), "FULL"),
    (("时间冲突", "冲突"), "CONFLICT"),
    (("未登录", "登录超时", "会话失效", "重新登录", "无权限"), "AUTH_REQUIRED"),
    (("操作过于频繁", "请求过于频繁", "限流", "稍后再试"), "RATE_LIMITED"),
    (("不存在", "未找到", "已停开"), "NOT_FOUND"),
    (("成功", "已选中", "选课完成"), "SUCCESS"),
)


def classify_message(text: str) -> str:
    """把服务器提示文本归类为 :class:`~qk.models.Outcome` 的取值名。

    适配器给出更精确的判断时应当优先使用自己的判断，本函数只作为通用兜底。
    """
    if not text:
        return "UNKNOWN"
    normalized = text.replace(" ", "")
    for keywords, outcome_name in MESSAGE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return outcome_name
    return "UNKNOWN"


def extract_message(payload: Any) -> str:
    """从 JSON/HTML 返回里取出人类可读的提示信息。"""
    if isinstance(payload, Mapping):
        for key in ("message", "msg", "info", "reason", "error", "提示"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            nested = extract_message(value)
            if nested:
                return nested
    if isinstance(payload, str):
        return payload.strip()[:200]
    return ""


def course_attr_of(course: CandidateCourse) -> str:
    """取课程的「授课形式/属性」原文（不同平台字段名不同，取到即用，取不到返回空串）。

    Kingosoft 放在 ``raw["course_attr"]``，形如 ``经济管理类/通识面授课``；
    后半段是**授课形式**（面授课／网络课等），这是排除过滤主要依据的字段。
    """
    raw = getattr(course, "raw", None) or {}
    if not isinstance(raw, Mapping):
        return ""
    for key in ("course_attr", "course_category", "courseAttr", "kcsx", "lb"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def filter_excluded_attrs(courses: Iterable[CandidateCourse],
                          patterns: Iterable[str]) -> list[CandidateCourse]:
    """按课程属性排除课程：**任一** pattern 作为子串命中即剔除（大小写不敏感）。

    典型用法是 ``patterns=["通识面授课"]``，把面授课全部滤掉、只看网络课/慕课等其它形式。
    属性取不到（空串）的课程**不会被排除**，避免因字段缺失误删有效课程。
    """
    needles = [str(p).strip().lower() for p in (patterns or []) if str(p).strip()]
    if not needles:
        return list(courses)
    kept: list[CandidateCourse] = []
    for course in courses:
        attr = course_attr_of(course).lower()
        if attr and any(needle in attr for needle in needles):
            continue
        kept.append(course)
    return kept


__all__ = [
    "AdapterError",
    "AuthError",
    "CaptchaRequiredError",
    "CourseAdapter",
    "CourseNotFoundError",
    "LoginAdapter",
    "MESSAGE_KEYWORDS",
    "PlatformUnsupportedError",
    "SelectionError",
    "SessionExpiredError",
    "classify_message",
    "course_attr_of",
    "extract_message",
    "filter_excluded_attrs",
]
