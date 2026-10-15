"""领域数据模型：跨适配层与 CLI 共用的稳定数据结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------

#: 敏感键名模式：命中即打码。
#: 判定只作用于**叶子标量**（见 :func:`redact`）：容器键（``credentials`` /
#: ``session`` / 响应头里的 ``Set-Cookie``）先递归展开，否则整棵子树会被替换成
#: ``***``，真正的敏感值反而被掩盖。因此这里既列 ``session``，也列
#: ``JSESSIONID`` 这类叶子键名。
SENSITIVE_KEY_PATTERN = re.compile(
    r"("
    r"pass(word|wd)?"          # password / passwd / pass
    r"|pwd"
    r"|secret"
    r"|token"
    r"|ticket"
    r"|cookie"
    r"|auth"
    r"|castgc"                 # CAS 登录票据 Cookie
    r"|session"                # session / session_id / JSESSIONID（大小写不敏感）
    r"|phpsessid"
    r"|credentials?"
    r")",
    re.IGNORECASE,
)

MASK = "***"


def is_sensitive_key(key: str) -> bool:
    """判断键名是否敏感（password / token / cookie 等）。"""
    return bool(SENSITIVE_KEY_PATTERN.search(str(key)))


def mask_value(value: Any) -> str:
    """把敏感值脱敏为 ``***``，空值保持为空串。"""
    if value is None or value == "":
        return ""
    return MASK


def redact(mapping: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """递归脱敏字典，供日志与终端输出使用。

    处理顺序很关键：**先判断容器再判断键名**。

    * 值是字典或列表 → 继续递归（``credentials`` / ``session`` 这类容器键因此保持可读）；
    * 值是标量且键名敏感 → 替换为 ``***``（``password`` / ``token`` / ``JSESSIONID`` 等）。
    """
    if _depth > 8:  # 防御异常结构
        return {"_": "<max-depth>"}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        key_str = str(key)
        if isinstance(value, Mapping):
            out[key_str] = redact(value, _depth=_depth + 1)
        elif isinstance(value, (list, tuple)):
            out[key_str] = [
                redact(item, _depth=_depth + 1) if isinstance(item, Mapping) else item
                for item in value
            ]
        elif is_sensitive_key(key_str):
            out[key_str] = mask_value(value)
        else:
            out[key_str] = value
    return out


def mask_url(url: str) -> str:
    """隐藏 URL 中的 ticket / token 等查询参数（CAS 回跳地址常带 ticket）。"""
    if not url or "?" not in url:
        return url
    head, _, query = url.partition("?")
    safe_parts = []
    for chunk in query.split("&"):
        name, sep, _value = chunk.partition("=")
        if sep and is_sensitive_key(name):
            safe_parts.append(f"{name}={MASK}")
        else:
            safe_parts.append(chunk)
    return f"{head}?" + "&".join(safe_parts)


# --------------------------------------------------------------------------
# HTTP 结果
# --------------------------------------------------------------------------


@dataclass
class HttpResult:
    """一次 HTTP 交互的结果（适配层只依赖本结构，不直接依赖 requests）。"""

    url: str
    status: int
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    attempts: int = 1
    elapsed: float = 0.0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """尝试解析 JSON，失败返回 None。"""
        import json

        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"HttpResult(url={mask_url(self.url)!r}, status={self.status}, attempts={self.attempts})"


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """登录后的会话信息。"""

    ok: bool
    student_id: str = ""
    name: str = ""
    cookies: Mapping[str, str] = field(default_factory=dict)
    service_url: str = ""
    message: str = ""

    def redacted(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "student_id": self.student_id,
            "name": self.name,
            "cookie_names": sorted(self.cookies.keys()),
            "service_url": mask_url(self.service_url),
            "message": self.message,
        }


# --------------------------------------------------------------------------
# 课程与选课结果
# --------------------------------------------------------------------------


@dataclass
class CandidateCourse:
    """教务系统返回的一条教学班（可选课程）。"""

    teaching_class_id: str
    course_code: str = ""
    course_name: str = ""
    teacher: str = ""
    credits: float | None = None
    capacity: int | None = None
    enrolled: int | None = None
    campus: str = ""
    time_text: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int | None:
        if self.capacity is None or self.enrolled is None:
            return None
        return self.capacity - self.enrolled

    @property
    def is_full(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def redacted(self) -> dict[str, Any]:
        return {
            "teaching_class_id": self.teaching_class_id,
            "course_code": self.course_code,
            "course_name": self.course_name,
            "teacher": self.teacher,
            "credits": self.credits,
            "capacity": self.capacity,
            "enrolled": self.enrolled,
            "remaining": self.remaining,
            "campus": self.campus,
            "time_text": self.time_text,
        }


class Outcome(str, Enum):
    """选课/退课结果分类。"""

    SUCCESS = "SUCCESS"
    DRY_RUN = "DRY_RUN"
    FULL = "FULL"
    ALREADY_SELECTED = "ALREADY_SELECTED"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    DROP_FAILED = "DROP_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_success(self) -> bool:
        return self in (Outcome.SUCCESS, Outcome.DRY_RUN, Outcome.ALREADY_SELECTED)

    @property
    def should_retry(self) -> bool:
        """这类结果值得继续重试（容量满/被限流/未知错误均可再试）。"""
        return self in (Outcome.FULL, Outcome.RATE_LIMITED, Outcome.UNKNOWN)


OUTCOME_TEXT: dict[Outcome, str] = {
    Outcome.SUCCESS: "选课成功",
    Outcome.DRY_RUN: "演练模式：未真正提交",
    Outcome.FULL: "容量已满",
    Outcome.ALREADY_SELECTED: "已经选过（自动跳过）",
    Outcome.CONFLICT: "时间冲突",
    Outcome.NOT_FOUND: "未找到该教学班",
    Outcome.DROP_FAILED: "退课失败",
    Outcome.AUTH_REQUIRED: "会话失效，需要重新登录",
    Outcome.RATE_LIMITED: "被服务器限流，稍后重试",
    Outcome.UNKNOWN: "未知结果（请查看原始响应）",
}


@dataclass
class SelectionResult:
    """一次选课/退课尝试的结果。"""

    outcome: Outcome
    course: CandidateCourse | None = None
    message: str = ""
    attempts: int = 1
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome.is_success

    def text(self) -> str:
        name = ""
        if self.course is not None:
            name = f"{self.course.course_name or self.course.course_code}#{self.course.teaching_class_id}"
        base = OUTCOME_TEXT.get(self.outcome, self.outcome.value)
        return f"{base}{('：' + name) if name else ''}{('（' + self.message + '）') if self.message else ''}"

    def redacted(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "course": self.course.redacted() if self.course else None,
            "message": self.message,
            "attempts": self.attempts,
        }


#: 高置信度凭据模式：命中即视为“疑似真实凭据”。
#: 只收录具有固定形态的会话票据（CAS / 教务系统 Cookie），避免把源码里的
#: 变量名当成凭据。
CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("CAS 会话票据", r"MOD_AUTH_CAS\s*[=:]\s*[0-9A-Za-z._~+/=-]{16,}"),
    ("CAS 登录票据", r"CASTGC\s*[=:]\s*[0-9A-Za-z._~+/=-]{16,}"),
    ("JSESSIONID", r"JSESSIONID\s*[=:]\s*[0-9A-Za-z._-]{12,}"),
    ("PHPSESSID", r"PHPSESSID\s*[=:]\s*[0-9A-Za-z._-]{12,}"),
    ("CLIENTKEY", r"CLIENTKEY\s*[=:]\s*[0-9A-Za-z._-]{12,}"),
    ("引用赋值的凭据", r"(?:password|passwd|pwd|token|secret)\s*[:=]\s*['\"][^'\"]{6,}['\"]"),
)

#: 弱提示：明文形态的键值对（无引号），仅在值形如真实密钥时才判定
_WEAK_ASSIGNMENT = re.compile(
    r"(?P<key>password|passwd|pwd|token|secret|session[_-]?id|jsessionid|cookie)\s*[:=]\s*"
    r"(?P<value>[^\s'\"<>]{6,120})",
    re.IGNORECASE,
)

#: 形如 ``LOCAL_STORAGE_TOKEN`` 的常量名、全大写标识符，不当成凭据值
_IDENTIFIER_LIKE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: 占位/示例值，不算凭据
_PLACEHOLDER_VALUES = frozenset(
    {"***", "your_password", "yourpassword", "changeme", "example", "test", "none", "null", "true", "false"}
)


def _looks_like_secret_value(value: str) -> bool:
    """判断一个值是否像真实密钥（而非变量名、占位符或示例文本）。"""
    candidate = value.strip().strip("'\"`;,)+")
    if len(candidate) < 6 or candidate.lower() in _PLACEHOLDER_VALUES:
        return False
    if _IDENTIFIER_LIKE.match(candidate):  # SOME_TOKEN / PASSWORD_FIELD
        return False
    has_digit = any(char.isdigit() for char in candidate)
    has_alpha = any(char.isalpha() for char in candidate)
    has_symbol = any(not char.isalnum() for char in candidate)
    # 纯字母要够长（避免 session / cookie 这类单词），含数字或符号则 8 位以上
    if has_digit or has_symbol:
        return len(candidate) >= 8
    return has_alpha and len(candidate) >= 16


def looks_like_credential(text: str) -> bool:
    """粗略判断一段文本是否包含疑似真实凭据（供交付前只读自检使用）。"""
    if not text:
        return False
    for _name, pattern in CREDENTIAL_PATTERNS:
        if re.search(pattern, text):
            return True
    for match in _WEAK_ASSIGNMENT.finditer(text):
        if _looks_like_secret_value(match.group("value")):
            return True
    return False


def find_credentials(texts: Iterable[tuple[str, str]]) -> list[str]:
    """在 ``(来源名, 文本)`` 列表里查找疑似凭据，返回 `来源:行号（类型）` 说明。"""
    hits: list[str] = []
    for source, text in texts:
        if not text:
            continue
        for name, pattern in CREDENTIAL_PATTERNS:
            match = re.search(pattern, text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{source}:{line}（{name}）")
                break
        else:
            for match in _WEAK_ASSIGNMENT.finditer(text):
                if _looks_like_secret_value(match.group("value")):
                    line = text.count("\n", 0, match.start()) + 1
                    hits.append(f"{source}:{line}（疑似 {match.group('key')} 赋值）")
                    break
    return hits
