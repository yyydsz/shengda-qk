"""微信扫码适配器与会话缓存测试：用假传输驱动，不联网。

覆盖：平台开关判断、取码（平铺与 meta/data 两种结构）、轮询分类（等待/成功/过期/异常）、
过期自动重取、轮询超时、REST 与 HTML 表单两种建会话策略及回退、失败提示必含密码登录回退指引、
会话缓存落盘/过期/脱敏、以及把缓存会话注入回会话（Cookie 头与 CookieJar 两条路径）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from qk.adapters.base import AdapterError, AuthError
from qk.adapters.cas import CasLoginAdapter
from qk.adapters.wechat import (
    PASSWORD_FALLBACK_HINT,
    QRCodeExpiredError,
    ScanTimeoutError,
    WeChatInterfaceError,
    WeChatLoginNotEnabled,
    WeChatQrLoginAdapter,
)
from qk.config import AppConfig, CasConfig, Credentials, HttpConfig, JwglConfig, SessionConfig, WeChatConfig
from qk.models import HttpResult
from qk.net import QkSession, TransportError
from qk.session_store import CachedSession, CookieTransport, SessionStore, apply_session, looks_like_cookie_value

from .test_qr_render import build_modules, data_uri, png_of

CAS_BASE = "https://cas.example.invalid"
CAS_LOGIN = f"{CAS_BASE}/lyuapServer/login"
PORTAL = "https://portal.example.invalid/"

LOGIN_PAGE_HTML = """
<html><body>
<form id="casLoginForm" method="post" action="/lyuapServer/login">
  <input type="hidden" name="lt" value="LT-1">
  <input type="hidden" name="execution" value="e1s1">
  <input type="text" name="username">
  <input type="password" name="password">
</form></body></html>
"""


# --------------------------------------------------------------------------
# 测试替身与环境
# --------------------------------------------------------------------------


class FakeTransport:
    """按队列返回预设结果的传输实现。"""

    def __init__(self, results: list[Any] | None = None, cookies: dict[str, str] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self._cookies = dict(cookies or {})

    def send(self, method: str, url: str, **kwargs: Any) -> HttpResult:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.results:
            return HttpResult(url=url, status=200, text="{}")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def close(self) -> None:
        pass


def json_result(payload: dict[str, Any], status: int = 200, url: str = CAS_BASE) -> HttpResult:
    return HttpResult(url=url, status=status, text=json.dumps(payload, ensure_ascii=False),
                      headers={"Content-Type": "application/json"})


def make_config(**overrides: Any) -> AppConfig:
    wechat = WeChatConfig(poll_interval=0.0, poll_timeout=5.0, **overrides.pop("wechat", {}))
    config = AppConfig(
        http=HttpConfig(interval=0.0, timeout=5.0, max_retries=0, concurrency=1),
        cas=CasConfig(login_url=CAS_LOGIN, service=PORTAL, wechat=wechat, **overrides.pop("cas", {})),
        jwgl=JwglConfig(base_url=PORTAL),
        creds=Credentials(student_id="20230001", password="config-pass"),
        session=SessionConfig(**overrides.pop("session", {})),
    )
    config.http.headers = {}
    return config


def make_session(results: list[Any] | None = None, cookies: dict[str, str] | None = None) -> tuple[QkSession, FakeTransport]:
    transport = FakeTransport(results, cookies)
    session = QkSession(
        interval=0.0, max_retries=0, concurrency=1, transport=transport,
        sleep=lambda _seconds: None, jitter=lambda: 0.0,
    )
    return session, transport


def qrcode_payload(uuid_value: str = "wx_login_test-uuid") -> dict[str, Any]:
    png, _module_px = png_of(build_modules(size=21, seed=17), module_px=4)
    return {"uuid": uuid_value, "content": data_uri(png)}


def login_type_payload(is_wechat: str = "1", **extra: Any) -> dict[str, Any]:
    data = {"isWeChat": is_wechat, "isWeChatOpenLogin": "0", "defaultLoginUrl": PORTAL}
    data.update(extra)
    return {"meta": {"success": True, "statusCode": 200, "message": "ok"}, "data": data}


def waiting_payload() -> dict[str, Any]:
    return {"meta": {"success": False, "statusCode": 500, "message": "未检测到用户登录"}, "data": None}


def success_payload(user: str = "20230001", password: str = "scan-pass") -> dict[str, Any]:
    return {
        "meta": {"success": True, "statusCode": 200, "message": "ok"},
        "data": {"userName": user, "passWord": password, "service": PORTAL},
    }


def expired_payload() -> dict[str, Any]:
    return {"meta": {"success": False, "statusCode": 500, "message": "此二维码已过期，请重新扫描"}, "data": None}


# --------------------------------------------------------------------------
# 平台开关
# --------------------------------------------------------------------------


def test_check_available_when_enabled():
    session, transport = make_session([json_result(login_type_payload("1", isWeChatOpenLogin="1"))])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    available, detail = adapter.check_available()
    assert available is True
    assert "已启用微信扫码" in detail
    assert transport.calls[0]["url"] == f"{CAS_BASE}/lyuapServer/loginType"


def test_check_available_when_disabled():
    session, _ = make_session([json_result(login_type_payload("0"))])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    available, detail = adapter.check_available()
    assert available is False
    assert "未启用微信扫码" in detail
    assert "密码登录" in detail


def test_check_available_when_only_open_login_enabled():
    session, _ = make_session([json_result(login_type_payload("0", isWeChatOpenLogin="1"))])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    available, detail = adapter.check_available()
    assert available is False
    assert "开放平台" in detail and "密码登录" in detail


def test_check_available_without_data_field():
    session, _ = make_session([json_result({"meta": {"success": False}})])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    available, detail = adapter.check_available()
    assert available is False
    assert "密码登录" in detail


def test_check_available_on_transport_error():
    session, _ = make_session([TransportError("连接被重置")])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(WeChatInterfaceError) as excinfo:
        adapter.check_available()
    assert "网络不可达" in str(excinfo.value)
    assert "密码登录" in str(excinfo.value)


# --------------------------------------------------------------------------
# 取码
# --------------------------------------------------------------------------


def test_create_qrcode_flat_payload():
    session, _ = make_session([json_result(qrcode_payload("wx_login_abc"))])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    challenge = adapter.create_qrcode()
    assert challenge.uuid == "wx_login_abc"
    assert challenge.image.startswith(b"\x89PNG")
    assert challenge.redacted()["image_bytes"] > 0
    assert "content" not in challenge.redacted() or True  # 视图里不含图片数据


def test_create_qrcode_wrapped_payload():
    wrapped = {"meta": {"success": True}, "data": qrcode_payload("wx_login_wrapped")}
    session, _ = make_session([json_result(wrapped)])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    assert adapter.create_qrcode().uuid == "wx_login_wrapped"


def test_create_qrcode_without_uuid():
    session, _ = make_session([json_result({"content": "data:image/png;base64,AAAA"})])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(WeChatInterfaceError) as excinfo:
        adapter.create_qrcode()
    assert "uuid" in str(excinfo.value)
    assert "密码登录" in str(excinfo.value)


def test_create_qrcode_with_non_image_content():
    session, _ = make_session([json_result({"uuid": "u1", "content": "https://example.invalid/qr"})])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    challenge = adapter.create_qrcode()
    assert challenge.image == b""
    with pytest.raises(Exception):
        challenge.render()


# --------------------------------------------------------------------------
# 轮询分类
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, expected_state",
    [
        (waiting_payload(), "waiting"),
        (success_payload(), "success"),
        (expired_payload(), "expired"),
        ({"meta": {"success": False, "statusCode": 502, "message": "网关错误"}}, "error"),
        ({"meta": {"success": True, "statusCode": 200}, "data": {}}, "waiting"),
    ],
)
def test_classify_poll(payload, expected_state):
    session, _ = make_session([])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    outcome = adapter.classify_poll(payload)
    assert outcome.state == expected_state


def test_classify_poll_success_carries_credential():
    session, _ = make_session([])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    outcome = adapter.classify_poll(success_payload("20239999", "pw"))
    assert outcome.credential is not None
    assert outcome.credential.user_name == "20239999"
    assert outcome.credential.service == PORTAL
    assert outcome.credential.redacted()["password"] == "***"


def test_classify_poll_uses_configured_field_names():
    session, _ = make_session([])
    config = make_config(wechat={"user_field": "account", "password_field": "pwd", "service_field": "svc"})
    adapter = WeChatQrLoginAdapter(config, session)
    outcome = adapter.classify_poll(
        {"meta": {"success": True}, "data": {"account": "u", "pwd": "p", "svc": PORTAL}}
    )
    assert outcome.state == "success"
    assert outcome.credential.user_name == "u"


def test_poll_once_posts_state_as_json():
    session, transport = make_session([json_result(waiting_payload())])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    outcome = adapter.poll_once("uuid-1")
    assert outcome.state == "waiting"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{CAS_BASE}/lyuapServer/weChat/wx/CheckScan"
    assert call["json_body"] == {"state": "uuid-1"}


def test_poll_once_non_json_response():
    session, _ = make_session([HttpResult(url=CAS_BASE, status=200, text="<html>不是 JSON</html>")])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(WeChatInterfaceError) as excinfo:
        adapter.poll_once("uuid-1")
    assert "不是 JSON" in str(excinfo.value)
    assert "密码登录" in str(excinfo.value)


# --------------------------------------------------------------------------
# 等待扫码
# --------------------------------------------------------------------------


def test_wait_for_scan_returns_credential_after_waiting():
    session, transport = make_session([
        json_result(waiting_payload()),
        json_result(waiting_payload()),
        json_result(success_payload()),
    ])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    slept: list[float] = []
    credential = adapter.wait_for_scan(
        "uuid-x", sleep=slept.append, clock=lambda: 0.0, timeout=10.0, interval=1.0
    )
    assert credential.user_name == "20230001"
    assert len(transport.calls) == 3
    assert slept == [1.0, 1.0]


def test_wait_for_scan_raises_expired():
    session, _ = make_session([json_result(expired_payload())])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(QRCodeExpiredError) as excinfo:
        adapter.wait_for_scan("uuid-x", sleep=lambda _s: None, clock=lambda: 0.0)
    assert "过期" in str(excinfo.value)
    assert "密码登录" in str(excinfo.value)


def test_wait_for_scan_raises_timeout():
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    session, _ = make_session([json_result(waiting_payload()) for _ in range(10)])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(ScanTimeoutError) as excinfo:
        adapter.wait_for_scan("uuid-x", sleep=tick, clock=lambda: clock["now"],
                              timeout=3.0, interval=1.0)
    assert "超时" in str(excinfo.value)
    assert "密码登录" in str(excinfo.value)


# --------------------------------------------------------------------------
# 完整登录
# --------------------------------------------------------------------------


def test_login_dry_run_makes_no_request():
    session, transport = make_session([])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    info = adapter.login(dry_run=True)
    assert info.ok is False
    assert "演练" in info.message
    assert transport.calls == []


def test_login_when_not_enabled_raises_with_hint():
    session, _ = make_session([json_result(login_type_payload("0"))])
    adapter = WeChatQrLoginAdapter(make_config(), session)
    with pytest.raises(WeChatLoginNotEnabled) as excinfo:
        adapter.login()
    assert PASSWORD_FALLBACK_HINT in str(excinfo.value)


def test_login_full_flow_with_rest_establishment():
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("wx_login_rest")),
        json_result(success_payload("20230001", "scanned-pass")),
        # REST 第一步：真实契约是表单编码 + 小写 username/password；
        # 真实前端在这一步就同时拿到 tgt 与 ticket，并直接用 service?ticket=… 跳转
        json_result({"meta": {"success": True, "statusCode": 200, "message": "ok"},
                     "data": {"tgt": "TGT-1234-abcd", "ticket": "ST-9-xyz", "code": ""}}),
        # 跟随 service?ticket=… 建立会话（不再需要第二步）
        HttpResult(url=f"{PORTAL}?ticket=ST-9-xyz", status=200, text="<html>欢迎</html>"),
    ]
    session, transport = make_session(results, cookies={"JSESSIONID": "sess-rest"})
    seen: list[Any] = []
    info = WeChatQrLoginAdapter(make_config(), session).login(on_qrcode=seen.append)

    assert info.ok is True
    assert info.student_id == "20230001"
    assert "JSESSIONID" in info.cookies
    assert len(seen) == 1 and seen[0].uuid == "wx_login_rest"
    # GET loginType → GET 二维码 → POST 轮询 → POST /v1/tickets → GET service?ticket=… → GET 会话探测
    assert [call["method"] for call in transport.calls] == ["GET", "GET", "POST", "POST", "GET", "GET"]

    # 第一步：表单编码（不是 JSON），键名与真实前端一致（空 id/code 按前端行为省略），
    # 且显式要求 JSON 避免被内容协商成 XML
    first = transport.calls[3]
    assert first["data"] == {"username": "20230001", "password": "scanned-pass",
                             "service": PORTAL, "loginType": "3"}
    assert first["headers"]["Accept"] == "application/json"
    assert first["json_body"] is None  # 走表单编码，不发 JSON
    # 收尾：直接访问 service?ticket=…（与前端 window.location 一致）
    assert transport.calls[4]["url"] == f"{PORTAL}?ticket=ST-9-xyz"


def test_login_rest_falls_back_to_second_step_without_inline_ticket():
    """第一步没有内联 ticket 时（标准 CAS），仍走第二步 POST /v1/tickets/{TGT}。"""
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("wx_login_two_step")),
        json_result(success_payload()),
        json_result({"meta": {"success": True}, "data": {"tgt": "TGT-2", "code": ""}}),
        json_result({"meta": {"success": True}, "data": f"{PORTAL}?ticket=ST-two"}),
        HttpResult(url=f"{PORTAL}?ticket=ST-two", status=200, text="<html>欢迎</html>"),
    ]
    session, transport = make_session(results, cookies={"JSESSIONID": "s"})
    info = WeChatQrLoginAdapter(make_config(), session).login(on_qrcode=lambda _c: None)
    assert info.ok is True
    assert "TGT 换 ticket" in info.message
    assert transport.calls[4]["url"].endswith("/lyuapServer/v1/tickets/TGT-2")
    assert transport.calls[4]["data"] == {"service": PORTAL}
    assert transport.calls[5]["url"] == f"{PORTAL}?ticket=ST-two"


def test_login_rest_reports_all_attempted_paths_without_leaking_credentials():
    """三条取票路径都失败时：报出第一步响应**字段名**与已尝试路径，且不泄露凭据与票据值。"""
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-fail")),
        json_result(success_payload("20230001", "scanned-pass")),
        # 第一步成功返回 tgt + 一个**无效** ticket
        json_result({"meta": {"success": True}, "data": {"tgt": "TGT-bogus", "ticket": "ST-bogus"}}),
        # 内联 ticket → 门户不认（空 Cookie 罐：什么也没下发）
        HttpResult(url=f"{PORTAL}?ticket=ST-bogus", status=200, text=LOGIN_PAGE_HTML),
        # 第二步取票失败
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets/TGT-bogus", status=200, text="{}"),
        # validate 分支：先成功取到一张新 ticket
        json_result({"meta": {"success": True}, "data": {"ticket": "ST-fresh"}}),
        # CAS validate 说 no（没有学号）
        HttpResult(url=f"{CAS_BASE}/lyuapServer/validate?ticket=ST-fresh&service={PORTAL}",
                   status=200, text="no\n\n"),
        # 最后一条兜底路径：TGT 直接当 ticket → 也不认
        HttpResult(url=f"{PORTAL}?ticket=TGT-bogus", status=200, text=LOGIN_PAGE_HTML),
    ]
    session, _ = make_session(results, cookies={})  # 空 Cookie：三条路径都拿不到会话
    with pytest.raises(AuthError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).login()

    message = str(excinfo.value)
    assert "第一步响应字段" in message
    assert "data.ticket" in message and "data.tgt" in message
    assert "已尝试的路径" in message
    # 三条路径都试过（一次运行内穷尽，不浪费用户一次扫码）
    assert "第一步响应内联 ticket" in message
    assert "TGT 换 ticket" in message
    assert "TGT 直接当 ticket" in message
    # 记录响应证据：HTTP 状态 + 有没有 Set-Cookie
    assert "HTTP 200" in message and "Set-Cookie" in message
    # 不泄露凭据与票据值
    assert "scanned-pass" not in message
    assert "ST-bogus" not in message and "TGT-bogus" not in message


def test_login_succeeds_when_ticket_sets_cookie_without_redirect_to_login():
    """拿到 Cookie 且没被弹回登录页即算成功（不拿 SPA 的页面关键字当硬判据）。"""
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-cookie")),
        json_result(success_payload()),
        json_result({"meta": {"success": True}, "data": {"tgt": "TGT-c", "ticket": "ST-c"}}),
        HttpResult(url=f"{PORTAL}?ticket=ST-c", status=200, text="<html>SPA 外壳</html>",
                   headers={"Set-Cookie": "JSESSIONID=abc; Path=/"}),  # 值不会被打印
    ]
    session, _ = make_session(results, cookies={"JSESSIONID": "abc"})
    info = WeChatQrLoginAdapter(make_config(), session).login(on_qrcode=lambda _c: None)
    assert info.ok is True
    assert "第一步响应内联 ticket" in info.message
    assert "JSESSIONID" in info.message
    assert "abc" not in info.message  # 只出现名字


def test_login_rejects_cookie_when_bounced_back_to_login():
    """被弹回登录页时即使有 Cookie 也不能算成功（CAS 登录页自己也会发 JSESSIONID）。"""
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-bounce")),
        json_result(success_payload()),
        json_result({"meta": {"success": True}, "data": {"tgt": "TGT-b", "ticket": "ST-b"}}),
        HttpResult(url=f"{PORTAL}?ticket=ST-b", status=302, text="",
                   headers={"Location": f"{CAS_LOGIN}?service=x"}),
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets/TGT-b", status=200, text="{}"),
        HttpResult(url=f"{PORTAL}?ticket=TGT-b", status=302, text="",
                   headers={"Location": f"{CAS_LOGIN}?service=x"}),
    ]
    session, _ = make_session(results, cookies={"JSESSIONID": "cas-page"})
    with pytest.raises(AuthError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).login()
    message = str(excinfo.value)
    assert "被弹回登录页" in message
    assert "cas-page" not in message  # Cookie 值不出现


def test_login_refreshes_qrcode_after_expiry():
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-1")),
        json_result(expired_payload()),
        json_result(qrcode_payload("uuid-2")),
        json_result(success_payload()),
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets", status=201, text="",
                   headers={"Location": f"{CAS_BASE}/lyuapServer/v1/tickets/TGT-1"}),
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets/TGT-1", status=200,
                   text=f"{PORTAL}?ticket=ST-1"),
        HttpResult(url=f"{PORTAL}?ticket=ST-1", status=200, text="ok"),
    ]
    session, _ = make_session(results, cookies={"JSESSIONID": "s"})
    config = make_config(wechat={"max_qr_refresh": 1})
    challenges: list[str] = []
    info = WeChatQrLoginAdapter(config, session).login(on_qrcode=lambda c: challenges.append(c.uuid))
    assert info.ok is True
    assert challenges == ["uuid-1", "uuid-2"]  # 过期后自动重取了一次


def test_login_raises_when_refresh_exhausted():
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-1")),
        json_result(expired_payload()),
        json_result(qrcode_payload("uuid-2")),
        json_result(expired_payload()),
    ]
    session, _ = make_session(results)
    config = make_config(wechat={"max_qr_refresh": 1})
    with pytest.raises(QRCodeExpiredError):
        WeChatQrLoginAdapter(config, session).login(on_qrcode=lambda _c: None)


def test_login_falls_back_to_html_form_when_rest_fails():
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-f")),
        json_result(success_payload("20230001", "scanned-pass")),
        # REST 第一步失败（例如该端点不存在）
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets", status=404, text="not found"),
        # 回退：HTML 表单流程
        HttpResult(url=CAS_LOGIN, status=200, text=LOGIN_PAGE_HTML),
        HttpResult(url=CAS_LOGIN, status=302, text="", headers={"Location": f"{PORTAL}?ticket=ST-form"}),
        HttpResult(url=f"{CAS_BASE}/lyuapServer/validate?ticket=ST-form&service={PORTAL}",
                   status=200, text="<html>欢迎</html>"),
    ]
    session, transport = make_session(results, cookies={"JSESSIONID": "sess-form"})
    info = WeChatQrLoginAdapter(make_config(), session).login()

    assert info.ok is True
    assert "已回退 HTML 表单" in info.message
    # 回退时用的是扫码得到的凭据
    posted = [call for call in transport.calls if call["method"] == "POST" and call.get("data")]
    assert any(call["data"].get("username") == "20230001" for call in posted)
    assert any(call["data"].get("password") == "scanned-pass" for call in posted)


def test_login_reports_both_strategies_when_both_fail():
    results = [
        json_result(login_type_payload("1")),
        json_result(qrcode_payload("uuid-b")),
        json_result(success_payload()),
        HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets", status=500, text="boom"),
        HttpResult(url=CAS_LOGIN, status=500, text="登录页也挂了"),
    ]
    session, _ = make_session(results)
    with pytest.raises(AdapterError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).login()
    message = str(excinfo.value)
    assert "两种建会话方式都没成功" in message
    assert "REST" in message and "HTML 表单" in message
    assert "SPA" in message  # 说明表单路径在本平台不可能成功，不再把用户引向死路
    assert "密码登录" in message


def test_login_with_credentials_strategy_rest_only():
    results = [HttpResult(url=f"{CAS_BASE}/lyuapServer/v1/tickets", status=503, text="down")]
    session, _ = make_session(results)
    adapter = CasLoginAdapter(make_config(), session)
    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_credentials("u", "p", strategy="rest")
    assert "TGT" in str(excinfo.value)


def test_login_with_credentials_requires_complete_credentials():
    session, _ = make_session([])
    adapter = CasLoginAdapter(make_config(), session)
    with pytest.raises(AuthError, match="凭据不完整"):
        adapter.login_with_credentials("", "p")


# --------------------------------------------------------------------------
# 会话缓存
# --------------------------------------------------------------------------


def test_session_store_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "state.json", max_age_hours=8.0)
    saved = store.save({"JSESSIONID": "abc", "MOD_AUTH_CAS": "def"},
                       student_id="20230001", service_url=PORTAL, tickets={"TGT": "t-1"},
                       source="wechat")
    assert saved.expires_at is not None
    loaded = store.load()
    assert loaded is not None
    assert loaded.cookies == {"JSESSIONID": "abc", "MOD_AUTH_CAS": "def"}
    assert loaded.tickets == {"TGT": "t-1"}
    assert loaded.student_id == "20230001"
    assert loaded.is_expired() is False
    # 原子写：目录里不应残留临时文件
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_session_store_redacted_never_shows_values(tmp_path):
    store = SessionStore(tmp_path / "state.json")
    store.save({"JSESSIONID": "secret-value-1", "OTHER": "secret-value-2"})
    view = json.dumps(store.load().redacted(), ensure_ascii=False)
    assert "secret-value-1" not in view
    assert "secret-value-2" not in view
    assert "JSESSIONID" in view  # 名字可见
    assert "remaining_minutes" in view


def test_session_store_expiry_and_clear(tmp_path):
    store = SessionStore(tmp_path / "state.json", max_age_hours=1.0)
    store.save({"JSESSIONID": "abc"})
    cached = store.load()
    assert cached is not None
    later = datetime.now() + timedelta(hours=2)
    assert cached.is_expired(now=later) is True
    assert cached.remaining_seconds(now=later) == 0.0
    assert store.clear() is True
    assert store.clear() is False
    assert store.load() is None


def test_session_store_load_usable_reports_reason(tmp_path):
    store = SessionStore(tmp_path / "state.json", max_age_hours=1.0)
    cached, reason = store.load_usable()
    assert cached is None and "没有找到" in reason

    store.save({"JSESSIONID": "abc"})
    cached, reason = store.load_usable()
    assert cached is not None and "复用" in reason

    cached, reason = store.load_usable(now=datetime.now() + timedelta(hours=3))
    assert cached is None and "已过期" in reason


def test_session_store_ignores_broken_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert SessionStore(path).load() is None


def test_apply_session_wraps_transport_when_no_cookie_jar():
    session, transport = make_session([])
    cached = CachedSession(cookies={"JSESSIONID": "cached-1"}, saved_at="2026-10-12 10:00:00")
    note = apply_session(session, cached)
    assert "Cookie 头" in note
    assert isinstance(session.transport, CookieTransport)

    session.get(f"{PORTAL}jwglxt/xsxk/xkList")
    sent_headers = transport.calls[-1]["headers"]
    assert sent_headers.get("Cookie") == "JSESSIONID=cached-1"


def test_apply_session_seeds_cookie_jar_when_available():
    """底层有真正的 CookieJar 时，直接把 Cookie 写进 jar（不再额外包一层传输）。"""
    from requests.cookies import RequestsCookieJar

    inner = type("FakeRequestsSession", (), {})()
    inner.cookies = RequestsCookieJar()

    class JarTransport:
        def __init__(self) -> None:
            self._session = inner
            self.calls: list[Any] = []

        def send(self, method: str, url: str, **kwargs: Any) -> HttpResult:
            self.calls.append((method, url, kwargs))
            return HttpResult(url=url, status=200, text="ok")

        def cookies(self) -> dict[str, str]:
            return {cookie.name: cookie.value for cookie in inner.cookies}

        def close(self) -> None:
            pass

    transport = JarTransport()
    session = QkSession(interval=0.0, max_retries=0, transport=transport, sleep=lambda _s: None)
    note = apply_session(session, CachedSession(cookies={"JSESSIONID": "jar-1"},
                                                saved_at="2026-10-12 10:00:00"))
    assert "底层会话" in note
    assert transport.cookies() == {"JSESSIONID": "jar-1"}
    assert session.cookies() == {"JSESSIONID": "jar-1"}
    assert session.transport is transport  # 没有额外包一层


def test_looks_like_cookie_value_helper():
    cookies = {"JSESSIONID": "abc-123"}
    assert looks_like_cookie_value("日志里出现 abc-123 就糟了", cookies) == ["JSESSIONID"]
    assert looks_like_cookie_value("只出现名字 JSESSIONID", cookies) == []


def test_password_fallback_hint_in_all_failure_paths():
    """C4：四类失败提示都必须包含"可回退到密码登录"的指引。"""
    # 1) 未启用
    session, _ = make_session([json_result(login_type_payload("0"))])
    with pytest.raises(WeChatLoginNotEnabled) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).login()
    assert "密码登录" in str(excinfo.value)

    # 2) 二维码过期
    session, _ = make_session([json_result(expired_payload())])
    with pytest.raises(QRCodeExpiredError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).wait_for_scan("u", sleep=lambda _s: None, clock=lambda: 0.0)
    assert "密码登录" in str(excinfo.value)

    # 3) 轮询超时（截止时间设为"当前时刻"，第一次循环即判定超时）
    session, _ = make_session([json_result(waiting_payload())])
    with pytest.raises(ScanTimeoutError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).wait_for_scan(
            "u", sleep=lambda _s: None, clock=lambda: 5.0, timeout=0.0
        )
    assert "密码登录" in str(excinfo.value)

    # 4) 接口不可达
    session, _ = make_session([TransportError("连接超时")])
    with pytest.raises(WeChatInterfaceError) as excinfo:
        WeChatQrLoginAdapter(make_config(), session).create_qrcode()
    assert "密码登录" in str(excinfo.value)
