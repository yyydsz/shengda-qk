"""微信扫码的 CLI 层测试：在 mock 上真跑 ``python -m qk``（走 ``qk.__main__.main``）。

覆盖：
* 成功路径（终端画出二维码 → 等待 → 成功并打印学号与 Cookie 名）
* 会话缓存（--save-session 落盘、session --info 只显示名字、session --clear 清除、grab --use-session 复用抢中）
* 四类失败（未启用 / 二维码过期 / 轮询超时 / 接口不可达）：退出码 3 且都含「可回退到密码登录」指引
* 既有行为不回归（login --dry-run 仍只打印演练计划；--help 含新子命令）
"""

from __future__ import annotations

import json
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from qk.__main__ import main
from qk.mockserver import MockConfig, MockServer, start_mock_server

PORTAL_USER = "20230001"
PORTAL_PASSWORD = "mock-pass"

CONFIG_TEMPLATE = """credentials:
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
  logout_url: "http://127.0.0.1:{port}/lyuapServer/logout"
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
class Cli:
    """一次测试用的环境：mock 服务 + 配置目录。"""

    server: MockServer
    directory: Path
    port: int

    def write_config(self, name: str = "config", *, poll_timeout: float = 5.0,
                     max_age_hours: float = 8.0) -> str:
        path = self.directory / f"{name}.yaml"
        path.write_text(
            CONFIG_TEMPLATE.format(
                student_id=PORTAL_USER,
                password=PORTAL_PASSWORD,
                port=self.port,
                state=str(self.directory / f"{name}-state.json").replace("\\", "/"),
                poll_timeout=poll_timeout,
                max_age_hours=max_age_hours,
            ),
            encoding="utf-8",
        )
        return str(path)

    @property
    def state_path(self) -> Path:
        return self.directory / "config-state.json"


@pytest.fixture
def cli() -> Iterator[Cli]:
    """默认 mock（第一次轮询等待、第二次成功）。"""
    server = start_mock_server(MockConfig.default(student_id=PORTAL_USER, password=PORTAL_PASSWORD,
                                                  qr_wait_rounds=1))
    directory = Path(tempfile.mkdtemp(prefix="qk-wx-cli-"))
    try:
        yield Cli(server=server, directory=directory, port=server.server_address[1])
    finally:
        server.stop()


def make_env(directory: Path, config: MockConfig) -> Cli:
    server = start_mock_server(config)
    return Cli(server=server, directory=directory, port=server.server_address[1])


def free_port() -> int:
    """拿一个几乎必然无人监听的端口（用于“接口不可达”场景）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ==========================================================================
# 成功路径
# ==========================================================================


def test_cli_login_wechat_success(cli: Cli, capsys):
    config = cli.write_config()

    assert main(["login", "--wechat", "--config", config]) == 0
    captured = capsys.readouterr()
    out = captured.out

    # 终端画出二维码：块字符 + 尺寸说明
    assert "█" in out or "▀" in out
    assert "模块" in out and "渲染方式" in out
    assert "请用微信「扫一扫」" in out
    # 学号与会话 Cookie 名（只显示名字）
    assert "登录结果：成功" in out
    assert f"学号：{PORTAL_USER}" in out
    assert "会话 Cookie：JSESSIONID（仅显示名字，值不输出）" in out
    # 不泄露密码与 Cookie 值
    assert PORTAL_PASSWORD not in out and PORTAL_PASSWORD not in captured.err
    for cookie_value in cli.server.state.sessions:
        assert cookie_value not in out


def test_cli_login_wechat_ascii_mode_and_qr_file(cli: Cli, capsys):
    config = cli.write_config()
    qr_path = cli.directory / "qr.png"

    assert main(["login", "--wechat", "--config", config,
                 "--qr-mode", "ascii", "--qr-file", str(qr_path), "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "##" in out
    assert "二维码图片已保存" in out
    assert qr_path.exists() and qr_path.read_bytes().startswith(b"\x89PNG")


def test_cli_login_dry_run_unchanged(cli: Cli, capsys):
    """C6：``login --dry-run`` 仍然只打印演练计划，不发任何请求。"""
    config = cli.write_config()

    assert main(["login", "--wechat", "--dry-run", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "没有发起任何网络请求" in out
    assert cli.server.state.login_requests == 0
    assert cli.server.state.qr_polls == {}


# ==========================================================================
# 会话缓存与复用
# ==========================================================================


def test_cli_save_session_info_and_clear(cli: Cli, capsys):
    config = cli.write_config()

    assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "会话已保存到" in out
    assert cli.state_path.exists()

    # 缓存文件里有值（复用所必需），但 CLI 输出里不能出现值
    raw = cli.state_path.read_text(encoding="utf-8")
    values = list(json.loads(raw)["cookies"].values())
    assert values and all(value not in out for value in values)

    assert main(["session", "--info", "--config", config]) == 0
    info_out = capsys.readouterr().out
    assert "Cookie 名：JSESSIONID" in info_out
    assert "值不显示" in info_out
    for value in values:
        assert value not in info_out

    assert main(["session", "--clear", "--config", config]) == 0
    assert "已删除会话缓存" in capsys.readouterr().out
    assert not cli.state_path.exists()

    # 清除后再 --info：明确提示不存在并给出扫码指引
    assert main(["session", "--info", "--config", config]) == 0
    missing_out = capsys.readouterr().out
    assert "没有找到会话缓存" in missing_out
    assert "login --wechat --save-session" in missing_out


def test_cli_grab_uses_saved_session(cli: Cli, capsys):
    """C2：扫码一次后，``grab --use-session`` 不再扫码就能抢课。"""
    config = cli.write_config()
    assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
    capsys.readouterr()

    qr_polls_before = dict(cli.server.state.qr_polls)
    assert main(["grab", "--use-session", "--config", config]) == 0
    out = capsys.readouterr().out

    assert "已复用缓存会话" in out
    assert "SUCCESS" in out
    assert cli.server.state.is_selected("JXB001") is True
    # 复用过程中不应产生任何新的扫码轮询
    assert cli.server.state.qr_polls == qr_polls_before


def test_cli_use_session_without_cache(cli: Cli, capsys):
    config = cli.write_config()

    assert main(["grab", "--use-session", "--config", config]) == 3
    out = capsys.readouterr().out
    assert "无法复用缓存会话" in out
    assert "没有找到缓存的登录会话" in out
    assert "login --wechat --save-session" in out


def test_cli_use_session_when_expired(cli: Cli, capsys):
    config = cli.write_config(max_age_hours=0.0)
    assert main(["login", "--wechat", "--save-session", "--config", config]) == 0
    capsys.readouterr()

    assert main(["grab", "--use-session", "--config", config]) == 3
    out = capsys.readouterr().out
    assert "已过期" in out
    assert "login --wechat --save-session" in out


# ==========================================================================
# 四类失败（C4：退出码 3 + 回退指引）
# ==========================================================================


def test_cli_wechat_not_enabled(cli: Cli, capsys):
    env = make_env(cli.directory, MockConfig.default(login_type_is_wechat="0"))
    try:
        config = env.write_config("notenabled")
        assert main(["login", "--wechat", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "未启用微信扫码" in err
        assert "密码登录" in err
    finally:
        env.server.stop()


def test_cli_qrcode_expired_then_refresh_fails(cli: Cli, capsys):
    # qr_expire_rounds=1：每次取码后第一次轮询就报过期；配置里 max_qr_refresh=1 → 重取一次仍失败
    env = make_env(cli.directory, MockConfig.default(qr_expire_rounds=1))
    try:
        config = env.write_config("expired")
        assert main(["login", "--wechat", "--config", config]) == 3
        captured = capsys.readouterr()
        assert "已过期" in captured.err
        assert "密码登录" in captured.err
        # 自动重取过一次二维码
        assert "已重新取码" in captured.out
        assert len(env.server.state.qr_polls) == 2
    finally:
        env.server.stop()


def test_cli_scan_timeout(cli: Cli, capsys):
    env = make_env(cli.directory, MockConfig.default(qr_never_success=True))
    try:
        config = env.write_config("timeout", poll_timeout=1.0)
        assert main(["login", "--wechat", "--timeout", "1", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "超时" in err
        assert "密码登录" in err
    finally:
        env.server.stop()


def test_cli_interface_unreachable(cli: Cli, capsys):
    port = free_port()
    config = cli.write_config()
    # 把配置指向一个没人监听的端口
    Path(config).write_text(
        Path(config).read_text(encoding="utf-8").replace(f"127.0.0.1:{cli.port}", f"127.0.0.1:{port}"),
        encoding="utf-8",
    )

    assert main(["login", "--wechat", "--config", config]) == 3
    err = capsys.readouterr().err
    assert "网络不可达" in err
    assert "校园网" in err
    assert "密码登录" in err


def test_cli_help_lists_new_options(capsys):
    """C6：``--help`` 行为不变且能看到新能力（help 退出码 0）。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for expected in ("run", "login", "list", "grab", "session"):
        assert expected in out
    assert "--use-session" in out
