"""微信扫码登录的**独立**端到端复验。

与 `test_wechat_cli.py` 的区别：这里不复用它的夹具与配置模板，自起 mock、自写配置，
直接驱动命令行入口 `qk.__main__.main(...)`，把「取码 → 终端渲染 → 轮询 → 建会话 →
会话落盘 → 复用抢课」整条链路当成一个黑盒跑一遍，并覆盖异常分支。

两条独立断言（对应 C5 的口径）：
1. **日志/终端**输出里不出现任何 Cookie **值**（只允许出现名字与有效期）；
2. **缓存文件的脱敏视图**里不出现任何 Cookie 值——注意缓存文件本身必须保存名值对，
   否则 C2 的「扫码一次可复用」无法实现，因此这里校验的是 `SessionStore` 暴露给
   外部展示的视图，而不是文件原文。
"""

from __future__ import annotations

import json
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from qk.__main__ import main
from qk.mockserver import MockConfig, MockServer, start_mock_server
from qk.session_store import SessionStore

STUDENT_ID = "20230001"
PASSWORD = "mock-pass"

CONFIG = """\
credentials:
  student_id: "{student_id}"
  password: "{password}"
http:
  interval: 0.0
  timeout: 5.0
  max_retries: 1
  concurrency: 1
cas:
  login_url: "http://127.0.0.1:{port}/lyuapServer/login"
  service: "http://127.0.0.1:{port}/"
  test_login_path: "/"
  wechat:
    poll_interval: 0.1
    poll_timeout: {poll_timeout}
session:
  path: "{state}"
  max_age_hours: {max_age_hours}
jwgl:
  base_url: "http://127.0.0.1:{port}/"
  index_path: "/"
  course_list_path: "/jwglxt/xsxk/xkList"
  select_path: "/jwglxt/xsxk/xkSelect"
  drop_path: "/jwglxt/xsxk/xkDrop"
  selected_list_path: "/jwglxt/xsxk/selectedList"
  platform: "custom"
grab:
  retry_interval: 0.0
  max_attempts: 2
  stop_on_success: true
  targets:
    - course_name: "大学体育Ⅲ"
"""


@dataclass
class Env:
    """一次独立复验用的环境。"""

    server: MockServer
    directory: Path

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def config(self, name: str = "cfg", *, poll_timeout: float = 5.0,
               max_age_hours: float = 8.0) -> str:
        path = self.directory / f"{name}.yaml"
        path.write_text(
            CONFIG.format(
                student_id=STUDENT_ID,
                password=PASSWORD,
                port=self.port,
                state=str(self.directory / f"{name}-state.json").replace("\\", "/"),
                poll_timeout=poll_timeout,
                max_age_hours=max_age_hours,
            ),
            encoding="utf-8",
        )
        return str(path)

    def state_file(self, name: str = "cfg") -> Path:
        return self.directory / f"{name}-state.json"

    def cookie_values(self) -> list[str]:
        """mock 侧当前所有会话令牌（用来断言它们不出现在任何输出里）。"""
        return list(self.server.state.sessions)


def start(mock_config: MockConfig, directory: Path) -> Env:
    return Env(server=start_mock_server(mock_config), directory=directory)


def default_mock(**overrides) -> MockConfig:
    return MockConfig.default(student_id=STUDENT_ID, password=PASSWORD, qr_wait_rounds=1, **overrides)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------
# 全链
# --------------------------------------------------------------------------


def test_e2e_scan_then_reuse_grabs_course(capsys):
    """取码 → 终端渲染 → 轮询成功 → 落盘 → 复用抢中（一条命令链，黑盒跑）。"""
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config()

        # 1) 扫码登录并保存会话
        assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
        captured = capsys.readouterr()
        output = captured.out + captured.err

        # 终端确实画出了二维码：块字符 + 尺寸说明 + 扫码指引
        assert any(char in output for char in ("█", "▀", "▄", "##"))
        assert "二维码：" in output and "模块" in output
        assert "列 x" in output and "行" in output
        assert "请用微信「扫一扫」" in output
        assert "waiting" in output and "success" in output
        # 学号与会话 Cookie 名（只显示名字）
        assert "登录结果：成功" in output
        assert f"学号：{STUDENT_ID}" in output
        assert "会话 Cookie：JSESSIONID（仅显示名字，值不输出）" in output
        assert "会话已保存到" in output

        # 断言①：日志/终端里不出现任何 Cookie 值
        for value in env.cookie_values():
            assert value not in output

        # 断言②：缓存文件的**脱敏视图**里不出现 Cookie 值
        store = SessionStore(env.state_file())
        cached = store.load()
        assert cached is not None and cached.cookies
        redacted_view = json.dumps(cached.redacted(), ensure_ascii=False)
        for value in cached.cookies.values():
            assert value not in redacted_view
        assert "JSESSIONID" in redacted_view  # 名字可见
        assert "saved_at" in redacted_view and "expires_at" in redacted_view

        # 2) 复用会话抢课：不应再产生扫码轮询
        polls_before = dict(env.server.state.qr_polls)
        assert main(["grab", "--use-session", "--config", config]) == 0
        grab_out = capsys.readouterr().out
        assert "已复用缓存会话" in grab_out
        assert "SUCCESS" in grab_out
        assert env.server.state.is_selected("JXB001") is True
        assert env.server.state.qr_polls == polls_before
    finally:
        env.server.stop()


def test_e2e_session_info_and_clear(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config()
        assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
        capsys.readouterr()

        assert main(["session", "--info", "--config", config]) == 0
        info = capsys.readouterr().out
        assert "Cookie 名：JSESSIONID" in info and "值不显示" in info
        for value in env.cookie_values():
            assert value not in info

        assert main(["session", "--clear", "--config", config]) == 0
        assert "已删除会话缓存" in capsys.readouterr().out
        assert not env.state_file().exists()
    finally:
        env.server.stop()


def test_e2e_dry_run_still_offline(capsys):
    """演练模式不得触网：mock 侧不应有任何登录/扫码记录。"""
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config()
        assert main(["login", "--wechat", "--dry-run", "--config", config]) == 0
        out = capsys.readouterr().out
        assert "[dry-run]" in out and "没有发起任何网络请求" in out
        assert env.server.state.qr_polls == {}
        assert env.server.state.login_requests == 0
    finally:
        env.server.stop()


# --------------------------------------------------------------------------
# 异常分支
# --------------------------------------------------------------------------


def test_e2e_not_enabled_guides_to_password(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(MockConfig.default(login_type_is_wechat="0"), directory)
    try:
        config = env.config("notenabled")
        assert main(["login", "--wechat", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "未启用微信扫码" in err and "密码登录" in err
    finally:
        env.server.stop()


def test_e2e_expired_qrcode_refetches_once(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(qr_expire_rounds=1), directory)
    try:
        config = env.config("expired")
        assert main(["login", "--wechat", "--config", config]) == 3
        captured = capsys.readouterr()
        assert "已重新取码" in captured.out
        assert len(env.server.state.qr_polls) == 2  # 自动重取一次
        assert "已过期" in captured.err and "密码登录" in captured.err
    finally:
        env.server.stop()


def test_e2e_scan_timeout(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(qr_never_success=True), directory)
    try:
        config = env.config("timeout", poll_timeout=1.0)
        assert main(["login", "--wechat", "--timeout", "1", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "超时" in err and "密码登录" in err
    finally:
        env.server.stop()


def test_e2e_unreachable_interface(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config("unreachable")
        Path(config).write_text(
            Path(config).read_text(encoding="utf-8").replace(f"127.0.0.1:{env.port}", f"127.0.0.1:{free_port()}"),
            encoding="utf-8",
        )
        assert main(["login", "--wechat", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "网络不可达" in err and "校园网" in err and "密码登录" in err
    finally:
        env.server.stop()


def test_e2e_reuse_after_expiry_asks_to_scan_again(capsys):
    """缓存过期后复用：明确提示需要重新扫码，而不是静默失败。"""
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config("expired-state", max_age_hours=0.0)
        assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
        capsys.readouterr()

        assert main(["grab", "--use-session", "--config", config]) == 3
        out = capsys.readouterr().out
        assert "已过期" in out
        assert "login --wechat --save-session" in out
    finally:
        env.server.stop()


def test_e2e_reuse_without_any_cache(capsys):
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = env.config("nocache")
        assert main(["grab", "--use-session", "--config", config]) == 3
        out = capsys.readouterr().out
        assert "没有找到缓存的登录会话" in out
        assert "login --wechat --save-session" in out
    finally:
        env.server.stop()


# --------------------------------------------------------------------------
# 与真实形态一致性（离线断言）
# --------------------------------------------------------------------------


def test_e2e_tries_both_ticket_paths_in_one_run(capsys):
    """一次运行内两条取票路径都要试完：内联 ticket 无效时自动改走第二步。

    这是真机联调的直接教训——一次失败就浪费用户一次扫码，因此不能"二选一"。
    """
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(rest_inline_ticket_invalid=True), directory)
    try:
        config = env.config()
        assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
        out = capsys.readouterr().out
        assert "登录结果：成功" in out
        # 真正生效的是第二步（TGT 换 ticket）
        assert "TGT 换 ticket" in out
        assert env.state_file().exists()
    finally:
        env.server.stop()


def test_e2e_mock_endpoints_match_measured_shapes(capsys):
    """mock 的三个端点与真实实测形态一致：平铺 {uuid,content}、data URI、等待态 500。"""
    import requests

    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        base = f"http://127.0.0.1:{env.port}"
        accept = {"Accept": "application/json"}  # 真实平台按 Accept 做内容协商
        login_type = requests.get(f"{base}/lyuapServer/loginType", headers=accept, timeout=5).json()
        assert login_type["meta"]["success"] is True
        assert login_type["data"]["isWeChat"] == "1"

        qr = requests.get(f"{base}/lyuapServer/weChat/CreateQRcode", headers=accept, timeout=5).json()
        assert set(qr) == {"uuid", "content"}  # 平铺，不套 meta/data
        assert qr["uuid"].startswith("wx_login_")
        assert qr["content"].startswith("data:image/png;base64,")

        waiting = requests.post(f"{base}/lyuapServer/weChat/wx/CheckScan", headers=accept,
                                json={"state": qr["uuid"]}, timeout=5).json()
        assert waiting["meta"]["success"] is False
        assert waiting["meta"]["statusCode"] == 500
    finally:
        env.server.stop()


def test_e2e_mock_does_content_negotiation_like_real_system(capsys):
    """回归护栏：不声明 ``Accept: application/json`` 时，mock 与真实系统一样返回 XML。

    真实系统按 Accept 头做内容协商（这是真机联调踩到的坑：CLI 默认带浏览器式 Accept，
    于是接口返回 ``<WebResponse>`` 而不是 JSON）。适配器必须显式声明 JSON，本用例保证
    这条行为差异不会在离线测试里被抹平。
    """
    import requests

    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        base = f"http://127.0.0.1:{env.port}"
        browser_accept = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

        xml = requests.get(f"{base}/lyuapServer/loginType", headers=browser_accept, timeout=5)
        assert xml.text.lstrip().startswith("<WebResponse>")
        assert "<isWeChat>1</isWeChat>" in xml.text
        assert xml.headers["Content-Type"].startswith("application/xml")

        # 同一次调用的 JSON 版本（适配器走的就是这条）
        js = requests.get(f"{base}/lyuapServer/loginType", headers={"Accept": "application/json"},
                          timeout=5).json()
        assert js["data"]["isWeChat"] == "1"

        # 取码与轮询同理
        raw_qr = requests.get(f"{base}/lyuapServer/weChat/CreateQRcode", headers=browser_accept,
                              timeout=5)
        assert raw_qr.text.lstrip().startswith("<WebResponse>")
        json_qr = requests.get(f"{base}/lyuapServer/weChat/CreateQRcode",
                               headers={"Accept": "application/json"}, timeout=5).json()
        assert json_qr["uuid"].startswith("wx_login_")
    finally:
        env.server.stop()


def test_e2e_adapter_always_asks_for_json(capsys):
    """适配器对扫码接口必须带 ``Accept: application/json``（否则真实系统返回 XML）。"""
    from qk.adapters.wechat import JSON_ACCEPT_HEADER, WeChatQrLoginAdapter
    from qk.config import load_config

    assert JSON_ACCEPT_HEADER == {"Accept": "application/json"}

    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-wx-"))
    env = start(default_mock(), directory)
    try:
        config = load_config(workspace=env.directory)
        # 模拟 CLI 的默认浏览器式请求头（真机联调时的现场）
        config.cas.login_url = f"http://127.0.0.1:{env.port}/lyuapServer/login"
        config.cas.service = f"http://127.0.0.1:{env.port}/"
        config.http.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        from qk.net import QkSession

        adapter = WeChatQrLoginAdapter(config, QkSession.from_config(config))
        available, detail = adapter.check_available()
        assert available is True, detail
        challenge = adapter.create_qrcode()
        assert challenge.uuid.startswith("wx_login_")
        credential = adapter.wait_for_scan(challenge.uuid, sleep=lambda _s: None)
        assert credential.user_name == STUDENT_ID
    finally:
        env.server.stop()
