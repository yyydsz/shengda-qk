"""网络层测试：限速、指数退避重试、并发上限、Cookie 访问、统计。

用假传输（FakeTransport）与假时钟（FakeClock）驱动，不联网、不真的等待。
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from qk.config import HttpConfig
from qk.models import HttpResult, mask_url
from qk.net import RETRY_STATUS, QkSession, TransportError


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeClock:
    """可推进的假时钟：sleep 会同时推进时间，因此限速行为可被断言。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


class FakeTransport:
    """按队列返回预设结果的传输实现，并记录每次调用。"""

    def __init__(self, results: list[Any] | None = None, cookies: Mapping[str, str] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self._cookies = dict(cookies or {})
        self.closed = False

    def send(self, method: str, url: str, **kwargs: Any) -> HttpResult:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.results:
            return HttpResult(url=url, status=200, text="ok")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def close(self) -> None:
        self.closed = True


def make_session(results=None, *, interval=0.35, max_retries=3, backoff=0.6, concurrency=2, transport=None):
    clock = FakeClock()
    fake = transport if transport is not None else FakeTransport(results)
    session = QkSession(
        interval=interval,
        max_retries=max_retries,
        backoff=backoff,
        concurrency=concurrency,
        transport=fake,
        sleep=clock.sleep,
        clock=clock.monotonic,
        jitter=lambda: 0.0,
    )
    return session, fake, clock


# --------------------------------------------------------------------------
# 限速
# --------------------------------------------------------------------------


def test_first_request_does_not_wait():
    session, _fake, clock = make_session(interval=0.5)
    session.get("https://example.invalid/a")
    assert clock.slept == []


def test_consecutive_requests_are_spaced_by_interval():
    session, fake, clock = make_session(interval=0.5)
    session.get("https://example.invalid/a")
    session.get("https://example.invalid/b")
    session.get("https://example.invalid/c")
    assert len(fake.calls) == 3
    # 后两次各等待 0.5s，总计 1.0s
    assert clock.slept == pytest.approx([0.5, 0.5])
    assert session.stats.throttled_waits == pytest.approx(1.0)


def test_zero_interval_never_waits():
    session, _fake, clock = make_session(interval=0)
    for _ in range(3):
        session.get("https://example.invalid/x")
    assert clock.slept == []


def test_slow_request_does_not_add_extra_wait():
    """如果请求本身已经超过 interval，就不应再额外等待。"""

    class SlowTransport(FakeTransport):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__()
            self.clock = clock

        def send(self, method: str, url: str, **kwargs: Any) -> HttpResult:
            self.clock.now += 1.0  # 模拟请求耗时 1s
            return super().send(method, url, **kwargs)

    clock = FakeClock()
    transport = SlowTransport(clock)
    session = QkSession(
        interval=0.5,
        transport=transport,
        sleep=clock.sleep,
        clock=clock.monotonic,
        jitter=lambda: 0.0,
    )
    session.get("https://example.invalid/a")
    session.get("https://example.invalid/b")
    assert clock.slept == []


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------


def test_retry_on_429_then_success():
    results = [
        HttpResult(url="u", status=429, text="too many requests"),
        HttpResult(url="u", status=200, text="ok"),
    ]
    session, fake, clock = make_session(results, max_retries=3)
    result = session.get("https://example.invalid/a")
    assert result.status == 200
    assert result.attempts == 2
    assert len(fake.calls) == 2
    assert session.stats.requests == 2
    assert session.stats.retries == 1
    # 第一次重试退避 = 0.6 * 2^0 * (0.5 + 0) = 0.3
    assert session.stats.backoff_waits == pytest.approx(0.3)


def test_retry_backoff_is_exponential():
    results = [HttpResult(url="u", status=503) for _ in range(3)] + [HttpResult(url="u", status=200)]
    session, _fake, _clock = make_session(results, max_retries=5, backoff=1.0)
    result = session.get("https://example.invalid/a")
    assert result.attempts == 4
    # 三次退避：0.5、1.0、2.0（jitter=0 → 系数 0.5）
    assert session.stats.backoff_waits == pytest.approx(0.5 + 1.0 + 2.0)


def test_retries_exhausted_returns_last_retryable_response():
    results = [HttpResult(url="u", status=500, text="boom") for _ in range(3)]
    session, fake, _clock = make_session(results, max_retries=2)
    result = session.get("https://example.invalid/a")
    assert result.status == 500
    assert result.attempts == 3
    assert len(fake.calls) == 3


def test_non_retryable_status_is_returned_immediately():
    results = [HttpResult(url="u", status=404, text="not found")]
    session, fake, clock = make_session(results, max_retries=3)
    result = session.get("https://example.invalid/a")
    assert result.status == 404
    assert len(fake.calls) == 1
    assert clock.slept == []
    assert session.stats.retries == 0


def test_transport_error_is_retried_then_raised():
    results = [TransportError("连接被重置"), TransportError("连接被重置")]
    session, fake, _clock = make_session(results, max_retries=1)
    with pytest.raises(TransportError, match="已重试 1 次"):
        session.get("https://example.invalid/a")
    assert len(fake.calls) == 2


def test_transport_error_then_success():
    results = [TransportError("超时"), HttpResult(url="u", status=200, text="ok")]
    session, _fake, _clock = make_session(results, max_retries=2)
    assert session.get("https://example.invalid/a").status == 200


def test_custom_retry_status_set():
    results = [HttpResult(url="u", status=418), HttpResult(url="u", status=200)]
    session, _fake, _clock = make_session(results, max_retries=2)
    result = session.request("GET", "https://example.invalid/a", retry_status=[418])
    assert result.status == 200
    assert result.attempts == 2
    assert 418 not in RETRY_STATUS  # 默认不重试 418


def test_retries_can_be_disabled_per_call():
    results = [HttpResult(url="u", status=500), HttpResult(url="u", status=200)]
    session, _fake, _clock = make_session(results, max_retries=3)
    result = session.request("GET", "https://example.invalid/a", retries=0)
    assert result.status == 500
    assert result.attempts == 1


# --------------------------------------------------------------------------
# 其它
# --------------------------------------------------------------------------


def test_post_sends_form_data():
    session, fake, _clock = make_session()
    session.post("https://example.invalid/login", data={"username": "u", "password": "p"})
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["data"] == {"username": "u", "password": "p"}


def test_headers_are_merged():
    session, fake, _clock = make_session()
    session.headers = {"User-Agent": "qk-test", "Accept-Language": "zh-CN"}
    session.get("https://example.invalid/a", headers={"X-Extra": "1"})
    sent = fake.calls[0]["headers"]
    assert sent["User-Agent"] == "qk-test"
    assert sent["X-Extra"] == "1"


def test_cookies_are_readable_but_not_logged():
    session, _fake, _clock = make_session()
    session.transport = FakeTransport(cookies={"JSESSIONID": "abc123", "MOD_AUTH_CAS": "def456"})
    assert session.cookie_names() == ["JSESSIONID", "MOD_AUTH_CAS"]
    assert session.cookies()["JSESSIONID"] == "abc123"
    # 统计信息里不含 Cookie 值
    assert "abc123" not in str(session.stats.redacted())


def test_context_manager_closes_transport():
    session, fake, _clock = make_session()
    with session:
        session.get("https://example.invalid/a")
    assert fake.closed is True


def test_from_config_builds_session_with_defaults():
    http = HttpConfig(interval=0.75, timeout=3.0, max_retries=1, concurrency=1)
    http.headers = {"User-Agent": "ua"}
    session = QkSession.from_config(type("C", (), {"http": http})())
    assert session.interval == 0.75
    assert session.timeout == 3.0
    assert session.max_retries == 1
    assert session.headers["User-Agent"] == "ua"
    assert isinstance(session.transport, object)


def test_stats_redacted_shape():
    session, _fake, _clock = make_session()
    session.get("https://example.invalid/a")
    stats = session.stats.redacted()
    assert set(stats) == {"requests", "retries", "throttled_waits", "backoff_waits"}
    assert stats["requests"] == 1


def test_url_with_ticket_is_masked_in_messages():
    session, _fake, _clock = make_session([TransportError("boom")], max_retries=0)
    with pytest.raises(TransportError) as excinfo:
        session.get("https://cas.shengda.edu.cn/login?ticket=ST-999-secret")
    assert "ST-999-secret" not in str(excinfo.value)
    assert mask_url("https://x/?ticket=ST-1") .endswith("ticket=***")
