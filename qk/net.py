"""HTTP 会话封装：超时、限速、指数退避重试、并发上限、Cookie 管理。

设计要点：

* 传输层可替换（:class:`Transport` 协议）。默认用 requests；测试注入假传输，
  因此限速与重试逻辑无需联网即可验证。
* ``sleep`` 可注入，测试里不会真的等待。
* 任何日志/异常信息都经过脱敏，Cookie 与 ticket 不会明文外泄。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import HttpResult, mask_url

#: 出现这些状态码时会重试
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(Exception):
    """传输层异常（连接失败、超时等），统一由 QkSession 决定是否重试。"""


class Transport(Protocol):
    """最小传输接口：只要能发一个请求并拿到状态码/正文/响应头即可。"""

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout: float = 10.0,
        allow_redirects: bool = True,
    ) -> HttpResult:
        ...

    def cookies(self) -> dict[str, str]:
        ...

    def close(self) -> None:
        ...


# --------------------------------------------------------------------------
# 默认传输实现（requests）
# --------------------------------------------------------------------------


class RequestsTransport:
    """基于 requests.Session 的传输实现，自动保持 Cookie。"""

    def __init__(self, *, verify_tls: bool = True, headers: Mapping[str, str] | None = None) -> None:
        try:
            import requests  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境已装 requests
            raise TransportError(
                "缺少 requests 库，请先执行：python -m pip install -r requirements.txt"
            ) from exc
        self._requests = requests
        self._session = requests.Session()
        self._session.verify = verify_tls
        if headers:
            self._session.headers.update(dict(headers))

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout: float = 10.0,
        allow_redirects: bool = True,
    ) -> HttpResult:
        started = time.monotonic()
        try:
            response = self._session.request(
                method.upper(),
                url,
                headers=dict(headers or {}),
                params=dict(params or {}) or None,
                data=dict(data or {}) or None,
                json=json_body,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
        except self._requests.exceptions.RequestException as exc:
            raise TransportError(f"{type(exc).__name__}: {mask_url(str(exc))}") from exc
        return HttpResult(
            url=str(response.url),
            status=int(response.status_code),
            text=response.text,
            headers={k: v for k, v in response.headers.items()},
            elapsed=time.monotonic() - started,
        )

    def cookies(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self._session.cookies}

    def close(self) -> None:
        self._session.close()


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


@dataclass
class SessionStats:
    """本次运行的请求统计，便于在日志里给出“我到底发了多少请求”。"""

    requests: int = 0
    retries: int = 0
    throttled_waits: float = 0.0
    backoff_waits: float = 0.0

    def redacted(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "throttled_waits": round(self.throttled_waits, 3),
            "backoff_waits": round(self.backoff_waits, 3),
        }


@dataclass
class QkSession:
    """带限速与重试的 HTTP 会话。

    :param transport: 传输实现；默认按需创建 :class:`RequestsTransport`
    :param interval: 相邻两次请求的最小间隔（秒），用于避免把服务器打爆
    :param max_retries: 单个请求的最大重试次数（不含首次）
    :param backoff: 退避基数，第 n 次重试等待 ``backoff * 2**(n-1)`` 秒并叠加抖动
    :param concurrency: 同时进行的请求数上限
    """

    timeout: float = 10.0
    interval: float = 0.35
    max_retries: int = 3
    backoff: float = 0.6
    concurrency: int = 2
    headers: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    transport: Transport | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    stats: SessionStats = field(default_factory=SessionStats)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_request_at: float | None = field(default=None, repr=False)
    _semaphore: threading.BoundedSemaphore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = RequestsTransport(verify_tls=self.verify_tls, headers=self.headers)
        if self.concurrency and self.concurrency > 0:
            self._semaphore = threading.BoundedSemaphore(self.concurrency)

    # ------------------------------------------------------------------ 内部
    def _throttle(self) -> None:
        """保证请求之间至少间隔 ``interval`` 秒（线程安全）。

        用 ``None`` 作为“还没发过请求”的哨兵：早期版本用 0.0 做真值判断，
        在时钟从 0 开始的环境（例如测试用的假时钟）里会误判为“已发过请求”。
        """
        if self.interval <= 0:
            return
        with self._lock:
            now = self.clock()
            if self._last_request_at is not None:
                wait = self.interval - (now - self._last_request_at)
                if wait > 0:
                    self.stats.throttled_waits += wait
                    self.sleep(wait)
            self._last_request_at = self.clock()

    def _backoff_wait(self, attempt: int) -> float:
        """第 ``attempt`` 次重试前的等待秒数（指数 + 抖动）。"""
        base = self.backoff * (2 ** max(0, attempt - 1))
        return base * (0.5 + self.jitter() * 0.5)

    def _should_retry_status(self, status: int) -> bool:
        return status in RETRY_STATUS

    # ------------------------------------------------------------------ 公开
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        allow_redirects: bool = True,
        retries: int | None = None,
        retry_status: Sequence[int] | None = None,
    ) -> HttpResult:
        """发一次请求；网络错误与可重试状态码会按退避策略重试。"""
        assert self.transport is not None
        max_retries = self.max_retries if retries is None else retries
        retry_set = frozenset(retry_status) if retry_status is not None else RETRY_STATUS
        merged_headers = {**self.headers, **dict(headers or {})}
        last_error: str = ""
        acquired = False
        if self._semaphore is not None:
            self._semaphore.acquire()
            acquired = True
        try:
            for attempt in range(1, max_retries + 2):
                self._throttle()
                self.stats.requests += 1
                try:
                    result = self.transport.send(
                        method,
                        url,
                        headers=merged_headers,
                        params=params,
                        data=data,
                        json_body=json_body,
                        timeout=self.timeout,
                        allow_redirects=allow_redirects,
                    )
                except TransportError as exc:
                    last_error = str(exc)
                    if attempt > max_retries:
                        raise TransportError(
                            f"请求失败（已重试 {max_retries} 次）：{mask_url(url)}：{last_error}"
                        ) from exc
                else:
                    result.attempts = attempt
                    if result.status not in retry_set or attempt > max_retries:
                        return result
                    last_error = f"HTTP {result.status}"
                wait = self._backoff_wait(attempt)
                self.stats.retries += 1
                self.stats.backoff_waits += wait
                self.sleep(wait)
            raise TransportError(f"请求失败：{mask_url(url)}：{last_error}")  # pragma: no cover
        finally:
            if acquired and self._semaphore is not None:
                self._semaphore.release()

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **kwargs: Any) -> HttpResult:
        return self.request("GET", url, params=params, **kwargs)

    def post(self, url: str, *, data: Mapping[str, Any] | None = None, **kwargs: Any) -> HttpResult:
        return self.request("POST", url, data=data, **kwargs)

    def cookies(self) -> dict[str, str]:
        assert self.transport is not None
        return self.transport.cookies()

    def cookie_names(self) -> list[str]:
        return sorted(self.cookies().keys())

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

    def __enter__(self) -> "QkSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 工厂
    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "QkSession":
        """按 :class:`~qk.config.HttpConfig` 构建会话。"""
        http = getattr(config, "http", config)
        params: dict[str, Any] = {
            "timeout": http.timeout,
            "interval": http.interval,
            "max_retries": http.max_retries,
            "backoff": http.backoff,
            "concurrency": http.concurrency,
            "headers": dict(getattr(http, "headers", {}) or {}),
            "verify_tls": getattr(http, "verify_tls", True),
        }
        params.update(overrides)
        return cls(**params)
