"""登录会话缓存：扫码/登录一次后落盘，供定时抢课复用。

设计要点：

* 落盘内容只包含 **Cookie 名值对 + 必要票据 + 时间信息**，不含账号密码；
* 默认路径 ``qk_state.json`` 已在 ``.gitignore`` 中排除，不会入库；
* 对外提供 :meth:`CachedSession.redacted`，只暴露 Cookie **名字**、保存时间与有效期，
  日志与终端一律通过它输出，避免 Cookie 值泄漏；
* 复用时不改动 ``qk/net.py``：优先把 Cookie 注入底层 requests 会话的 CookieJar，
  取不到时退化为一个只加 ``Cookie`` 头的传输包装（同样满足 Transport 协议）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

STATE_VERSION = 1


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CachedSession:
    """一份缓存下来的登录会话。"""

    cookies: dict[str, str] = field(default_factory=dict)
    saved_at: str = ""
    max_age_hours: float = 8.0
    student_id: str = ""
    service_url: str = ""
    #: 额外票据（如 CAS TGT）；属于敏感内容，只在内存中使用
    tickets: dict[str, str] = field(default_factory=dict)
    source: str = ""

    # ---------------------------------------------------------------- 时间
    @property
    def saved_datetime(self) -> datetime | None:
        try:
            return datetime.strptime(self.saved_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    @property
    def expires_at(self) -> datetime | None:
        saved = self.saved_datetime
        if saved is None:
            return None
        return saved + timedelta(hours=self.max_age_hours)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """是否已过期（时间无法解析时按过期处理）。"""
        expires = self.expires_at
        if expires is None:
            return True
        return (now or _now()) >= expires

    def remaining_seconds(self, *, now: datetime | None = None) -> float:
        expires = self.expires_at
        if expires is None:
            return 0.0
        return max(0.0, (expires - (now or _now())).total_seconds())

    # ---------------------------------------------------------------- 视图
    @property
    def complete(self) -> bool:
        return bool(self.cookies)

    def redacted(self) -> dict[str, Any]:
        """只暴露 Cookie 名字与时间信息，绝不输出 Cookie 值。"""
        return {
            "cookie_names": sorted(self.cookies.keys()),
            "ticket_names": sorted(self.tickets.keys()),
            "saved_at": self.saved_at,
            "expires_at": _iso(self.expires_at) if self.expires_at else "",
            "remaining_minutes": round(self.remaining_seconds() / 60, 1),
            "max_age_hours": self.max_age_hours,
            "student_id": self.student_id,
            "service_url": self.service_url,
            "source": self.source,
            "expired": self.is_expired(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "cookies": dict(self.cookies),
            "tickets": dict(self.tickets),
            "saved_at": self.saved_at,
            "max_age_hours": self.max_age_hours,
            "student_id": self.student_id,
            "service_url": self.service_url,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CachedSession":
        cookies = data.get("cookies")
        tickets = data.get("tickets")
        return cls(
            cookies={str(k): str(v) for k, v in (cookies or {}).items()},
            tickets={str(k): str(v) for k, v in (tickets or {}).items()},
            saved_at=str(data.get("saved_at", "")),
            max_age_hours=float(data.get("max_age_hours", 8.0) or 0.0),
            student_id=str(data.get("student_id", "")),
            service_url=str(data.get("service_url", "")),
            source=str(data.get("source", "")),
        )


class SessionStore:
    """读写 :class:`CachedSession`。"""

    def __init__(self, path: str | os.PathLike[str] = "qk_state.json",
                 max_age_hours: float = 8.0) -> None:
        self.path = Path(path)
        self.max_age_hours = max_age_hours

    # ---------------------------------------------------------------- 读写
    def save(self, cookies: Mapping[str, str], *, student_id: str = "", service_url: str = "",
             tickets: Mapping[str, str] | None = None, source: str = "",
             now: datetime | None = None) -> CachedSession:
        """把会话写入磁盘（原子替换：先写临时文件再替换，避免半个文件）。"""
        cached = CachedSession(
            cookies={str(k): str(v) for k, v in dict(cookies).items()},
            saved_at=_iso(now or _now()),
            max_age_hours=self.max_age_hours,
            student_id=student_id,
            service_url=service_url,
            tickets={str(k): str(v) for k, v in dict(tickets or {}).items()},
            source=source,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(cached.to_dict(), stream, ensure_ascii=False, indent=2)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return cached

    def load(self) -> CachedSession | None:
        """读取缓存；文件不存在或格式损坏时返回 ``None``（并把它当作“没有会话”处理）。"""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, Mapping):
            return None
        return CachedSession.from_dict(data)

    def clear(self) -> bool:
        """删除缓存文件；返回是否真的删掉了。"""
        if not self.path.exists():
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    # ---------------------------------------------------------------- 复用
    def load_usable(self, *, now: datetime | None = None) -> tuple[CachedSession | None, str]:
        """读取并判定可用性，返回 ``(会话, 说明)``；会话为 ``None`` 时说明原因。"""
        cached = self.load()
        if cached is None:
            return None, "没有找到缓存的登录会话"
        if not cached.complete:
            return None, "缓存的登录会话里没有任何 Cookie"
        if cached.is_expired(now=now):
            return None, (
                f"缓存的登录会话已过期（保存于 {cached.saved_at}，有效期 {cached.max_age_hours} 小时）"
            )
        return cached, f"复用 {cached.saved_at} 保存的会话，剩余约 {cached.remaining_seconds() / 60:.0f} 分钟"


class CookieTransport:
    """给任意传输加上固定的 ``Cookie`` 头（满足 :class:`qk.net.Transport` 协议）。

    仅当拿不到底层 requests 会话、无法直接写入 CookieJar 时才使用。
    """

    def __init__(self, inner: Any, cookies: Mapping[str, str]) -> None:
        self._inner = inner
        self._cookies = {str(k): str(v) for k, v in dict(cookies).items()}

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", None) or {})
        if self._cookies and not any(name.lower() == "cookie" for name in headers):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return self._inner.send(method, url, headers=headers, **kwargs)

    def cookies(self) -> dict[str, str]:
        inner_cookies = {}
        getter = getattr(self._inner, "cookies", None)
        if callable(getter):
            try:
                inner_cookies = dict(getter())
            except Exception:  # noqa: BLE001 - 传输实现各异，取不到就用注入值
                inner_cookies = {}
        return {**self._cookies, **inner_cookies}

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()


def apply_session(session: Any, cached: CachedSession) -> str:
    """把缓存会话注入到 ``QkSession``；返回实际上采用的注入方式说明。"""
    transport = getattr(session, "transport", None)
    if transport is None or not cached.cookies:
        return "未注入（没有可用的会话或传输）"

    inner = getattr(transport, "_session", None)  # RequestsTransport 内部的 requests.Session
    jar = getattr(inner, "cookies", None)
    if jar is not None and hasattr(jar, "set"):
        for name, value in cached.cookies.items():
            try:
                jar.set(name, value, path="/")
            except Exception:  # noqa: BLE001 - 个别 Cookie 属性异常时跳过，不阻断
                continue
        return f"已注入 {len(cached.cookies)} 个 Cookie 到底层会话"

    session.transport = CookieTransport(transport, cached.cookies)
    return f"已通过 Cookie 头注入 {len(cached.cookies)} 个 Cookie"


def looks_like_cookie_value(text: str, cookies: Mapping[str, str]) -> list[str]:
    """自检辅助：在文本里查找是否出现了 Cookie 值（返回命中的 Cookie 名）。"""
    hits: list[str] = []
    for name, value in cookies.items():
        if value and value in text:
            hits.append(name)
    return hits


def redact_state_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """只读地把缓存文件渲染成可安全打印的视图（用于 ``session --info`` 的自检）。"""
    store = SessionStore(path)
    cached = store.load()
    if cached is None:
        return {"path": str(path), "exists": Path(path).exists(), "session": None}
    return {"path": str(path), "exists": True, "session": cached.redacted()}


__all__ = [
    "CachedSession",
    "CookieTransport",
    "SessionStore",
    "apply_session",
    "looks_like_cookie_value",
    "redact_state_file",
]
