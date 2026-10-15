"""端到端复验：自起 mock 教务服务、自写配置、直接驱动 CLI 入口。

与 `test_adapters.py` / `test_grabber.py` 的区别：

* 那两个文件测的是**适配器与编排层**（直接构造对象）；
* 本文件测的是**用户真正敲的命令**——`qk.__main__.main([...])`，覆盖
  「参数解析 → 配置加载 → CAS 登录 → 选课/退课 → 结果归类 → 输出与退出码」整条链路，
  并且**不复用** conftest 里的夹具与服务实例，作为独立复验。

覆盖：登录、查询、定时抢课、抢中确认、重复选课自动跳过、退课成功、退课失败、
容量满重试、会话过期重登、验证码两情形、dry-run 不触网。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from qk.__main__ import main
from qk.mockserver import MockConfig, MockCourse, MockServer, start_mock_server

STUDENT_ID = "20230001"
PASSWORD = "mock-pass"

#: 与 qk/mockserver.py 路由一致的接口路径
PATHS = {
    "course_list_path": "/jwglxt/xsxk/xkList",
    "select_path": "/jwglxt/xsxk/xkSelect",
    "drop_path": "/jwglxt/xsxk/xkDrop",
    "selected_list_path": "/jwglxt/xsxk/selectedList",
}

CONFIG_TEMPLATE = """\
credentials:
  student_id: "{student_id}"
  password: "{password}"
http:
  interval: 0.0
  timeout: 5.0
  max_retries: 2
  concurrency: 4
cas:
  login_url: "{base}lyuapServer/login"
  service: "{base}"
  logout_url: "{base}lyuapServer/logout"
  test_login_path: "/"
jwgl:
  base_url: "{base}"
  index_path: "/"
  course_list_path: "{course_list_path}"
  select_path: "{select_path}"
  drop_path: "{drop_path}"
  selected_list_path: "{selected_list_path}"
  platform: "custom"
grab:
  retry_interval: {retry_interval}
  max_attempts: {max_attempts}
  stop_on_success: {stop_on_success}
  drop_if_selected: {drop_if_selected}
{start_at}{targets}
"""


@dataclass
class E2E:
    """一次端到端测试的环境：mock 服务 + 配置文件目录。"""

    server: MockServer
    directory: Path
    base_url: str

    def write_config(self, name: str, targets: list[dict], **options) -> str:
        """写一份指向本环境的配置，返回路径。"""
        lines = []
        for target in targets:
            lines.append("    - course_name: \"%s\"" % target.get("course_name", ""))
            if target.get("teacher"):
                lines.append("      teacher: \"%s\"" % target["teacher"])
            lines.append("      priority: %d" % target.get("priority", 0))
        targets_block = "  targets:\n" + "\n".join(lines) + "\n"

        path = self.directory / f"{name}.yaml"
        path.write_text(
            CONFIG_TEMPLATE.format(
                student_id=STUDENT_ID,
                password=PASSWORD,
                base=self.base_url,
                retry_interval=options.get("retry_interval", 0.0),
                max_attempts=options.get("max_attempts", 3),
                stop_on_success=str(options.get("stop_on_success", True)).lower(),
                drop_if_selected=str(options.get("drop_if_selected", False)).lower(),
                start_at=(
                    "  start_at: \"%s\"\n" % options["start_at"] if options.get("start_at") else ""
                ),
                targets=targets_block,
                **PATHS,
            ),
            encoding="utf-8",
        )
        return str(path)


@pytest.fixture
def e2e() -> Iterator[E2E]:
    """独立起一个 mock 服务（默认课程表，含满额/已选/退课失败分支）。"""
    server = start_mock_server(MockConfig.default(student_id=STUDENT_ID, password=PASSWORD))
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-"))
    try:
        yield E2E(server=server, directory=directory, base_url=server.base_url)
    finally:
        server.stop()


def write_custom_config(env: E2E, name: str, courses: list[MockCourse], selected: list[str],
                        targets: list[dict], **options) -> str:
    """用自定义课程表启一个**新** mock 服务并写配置（场景之间互不污染）。"""
    server = start_mock_server(
        MockConfig.default(student_id=STUDENT_ID, password=PASSWORD, courses=courses, selected=selected)
    )
    custom = E2E(server=server, directory=env.directory, base_url=server.base_url)
    return custom.write_config(name, targets, **options)


# ==========================================================================
# 全链
# ==========================================================================


def test_e2e_login_list_then_grab(e2e: E2E, capsys):
    """登录 → 查询候选课程 → 抢课抢中确认（一条命令链，走真实 CLI 入口）。"""
    config = e2e.write_config("full", [{"course_name": "大学体育Ⅲ"}])

    assert main(["login", "--config", config]) == 0
    login_out = capsys.readouterr().out
    assert "登录结果：成功" in login_out
    assert STUDENT_ID in login_out
    assert "JSESSIONID" in login_out
    assert "值不输出" in login_out
    assert PASSWORD not in login_out and PASSWORD not in capsys.readouterr().err

    assert main(["list", "--config", config]) == 0
    list_out = capsys.readouterr().out
    assert "大学体育Ⅲ" in list_out and "JXB001" in list_out
    assert "余量" in list_out

    assert main(["grab", "--config", config]) == 0
    grab_out = capsys.readouterr().out
    assert "选课成功" in grab_out
    assert "SUCCESS" in grab_out
    assert e2e.server.state.is_selected("JXB001") is True


def test_e2e_grab_json_report(e2e: E2E, capsys):
    """--json 输出可被解析，并按服务端真实结果如实上报。"""
    config = e2e.write_config("json", [{"course_name": "大学体育Ⅲ"}])

    assert main(["grab", "--config", config, "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{"):])
    assert report["logged_in"] is True
    assert report["relogins"] == 0
    assert report["success"] is True
    assert report["targets"][0]["status"] == "SUCCESS"
    assert report["targets"][0]["course"]["teaching_class_id"] == "JXB001"

    # 再跑一次：这次课程已在已选列表里，编排层应跳过而不是重复提交
    assert main(["grab", "--config", config, "--json"]) == 1
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{"):])
    assert report["targets"][0]["status"] == "SKIPPED_ALREADY_SELECTED"
    assert report["targets"][0]["attempts"] == 0
    assert report["success"] is False


def test_e2e_server_rejects_duplicate_select(e2e: E2E, capsys):
    """服务端在提交那一刻回“已选过”→ 归类 ALREADY_SELECTED，不重试。"""
    courses = [
        MockCourse("JXB911", "91100001", "重复选课测试", "李老师", 1.0, 50, 10,
                   "周二 1-2 节", "already_selected"),
    ]
    config = write_custom_config(
        e2e, "dupsel", courses, [], [{"course_name": "重复选课测试"}]
    )

    assert main(["grab", "--config", config]) == 1
    output = capsys.readouterr().out
    assert "ALREADY_SELECTED" in output
    assert "[第 1/3 次]" in output and "[第 2/3 次]" not in output  # 不重试


def test_e2e_dry_run_never_touches_network(e2e: E2E, capsys):
    """run --dry-run 不联网、不登录、不提交。"""
    config = e2e.write_config("dry", [{"course_name": "大学体育Ⅲ"}])

    assert main(["run", "--config", config, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "[dry-run]" in output
    assert "没有发起任何网络请求，也没有提交选课" in output
    assert e2e.server.state.login_requests == 0
    assert e2e.server.state.select_attempts == {}
    assert e2e.server.state.is_selected("JXB001") is False


def test_e2e_start_at_in_past_starts_immediately(e2e: E2E, capsys):
    """起抢时间已过 → 不等待，直接开抢。"""
    config = e2e.write_config(
        "start", [{"course_name": "大学体育Ⅲ"}], start_at="2020-01-01 00:00:00"
    )
    assert main(["grab", "--config", config]) == 0
    output = capsys.readouterr().out
    assert "等待起抢时间" not in output
    assert "SUCCESS" in output


# ==========================================================================
# 异常分支
# ==========================================================================


def test_e2e_full_capacity_retries_then_reports_full(e2e: E2E, capsys):
    """容量满 → 按 max_attempts 重试，最后如实报 FULL 且退出码非 0。"""
    config = e2e.write_config("fullcap", [{"course_name": "大学英语Ⅱ"}], max_attempts=2)

    assert main(["grab", "--config", config]) == 1
    output = capsys.readouterr().out
    assert "[第 1/2 次]" in output and "[第 2/2 次]" in output
    assert "FULL" in output
    assert "人数已满" in output
    assert e2e.server.state.select_attempts["JXB002"] == 2


def test_e2e_already_selected_is_skipped(e2e: E2E, capsys):
    """已在已选列表 → 自动跳过，不发起选课请求。"""
    config = e2e.write_config("sel", [{"course_name": "程序设计基础"}])

    assert main(["grab", "--config", config]) == 1
    output = capsys.readouterr().out
    assert "SKIPPED_ALREADY_SELECTED" in output
    assert "跳过该目标" in output
    assert e2e.server.state.select_attempts.get("JXB004") is None


def test_e2e_drop_then_reselect(e2e: E2E, capsys):
    """drop_if_selected=True：先退课再重新选上。"""
    config = e2e.write_config(
        "drop", [{"course_name": "程序设计基础"}], drop_if_selected=True
    )

    assert main(["grab", "--config", config]) == 0
    output = capsys.readouterr().out
    assert "已退课" in output
    assert "SUCCESS" in output
    assert e2e.server.state.is_selected("JXB004") is True


def test_e2e_drop_failure_is_reported(e2e: E2E, capsys):
    """退课接口拒绝（不在退课时间内）→ 报 DROP_FAILED 并保留原因。"""
    courses = [
        MockCourse("JXB910", "91000001", "退课被拒测试", "钱老师", 1.0, 45, 5,
                   "周五 7-8 节", "drop_fail"),
    ]
    config = write_custom_config(
        e2e, "dropfail", courses, ["JXB910"],
        [{"course_name": "退课被拒测试"}], drop_if_selected=True,
    )

    assert main(["grab", "--config", config]) == 1
    output = capsys.readouterr().out
    assert "DROP_FAILED" in output
    assert "退课失败" in output and "不在退课时间内" in output


def test_e2e_session_expiry_triggers_relogin(e2e: E2E, capsys):
    """第一次提交选课时会话失效 → 自动重登并抢中。"""
    server = start_mock_server(
        MockConfig.default(
            student_id=STUDENT_ID, password=PASSWORD, expire_on_first_select=True
        )
    )
    env = E2E(server=server, directory=e2e.directory, base_url=server.base_url)
    config = env.write_config("expire", [{"course_name": "大学体育Ⅲ"}])

    assert main(["grab", "--config", config]) == 0
    output = capsys.readouterr().out
    assert "会话失效" in output
    assert "重新登录（第 1 次）" in output
    assert "SUCCESS" in output
    assert server.state.is_selected("JXB001") is True


def test_e2e_captcha_missing_then_provided(capsys, monkeypatch):
    """验证码：缺失 → 明确报错退出码 3；提供正确验证码 → 登录成功。"""
    server = start_mock_server(
        MockConfig.default(
            student_id=STUDENT_ID, password=PASSWORD, captcha_required=True, captcha_code="8888"
        )
    )
    directory = Path(tempfile.mkdtemp(prefix="qk-e2e-captcha-"))
    env = E2E(server=server, directory=directory, base_url=server.base_url)
    config = env.write_config("captcha", [{"course_name": "大学体育Ⅲ"}])
    try:
        monkeypatch.delenv("QK_CAPTCHA_CODE", raising=False)
        assert main(["login", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "验证码" in err
        assert "captcha_code" in err

        monkeypatch.setenv("QK_CAPTCHA_CODE", "8888")
        assert main(["login", "--config", config]) == 0
        out = capsys.readouterr().out
        assert "登录结果：成功" in out

        monkeypatch.setenv("QK_CAPTCHA_CODE", "0000")
        assert main(["login", "--config", config]) == 3
        err = capsys.readouterr().err
        assert "验证码错误或已过期" in err
    finally:
        server.stop()


def test_e2e_missing_interface_path_is_explicit(e2e: E2E, capsys):
    """教务接口路径未确认时，明确报错并指出该填哪个配置项（不猜路径）。"""
    config = e2e.write_config("nopath", [{"course_name": "大学体育Ⅲ"}])
    # 抹掉课程列表路径：模拟“还没抓到真实接口”
    text = Path(config).read_text(encoding="utf-8").replace(
        f'course_list_path: "{PATHS["course_list_path"]}"', 'course_list_path: ""'
    )
    Path(config).write_text(text, encoding="utf-8")

    assert main(["grab", "--config", config]) == 1
    output = capsys.readouterr().out
    assert "ERROR" in output
    assert "jwgl.course_list_path" in output
    assert "不猜测路径" in output


def test_e2e_missing_credentials_exit_code(e2e: E2E, capsys):
    """缺凭据 → 退出码 2 且提示怎么补。"""
    config = e2e.write_config("nocreds", [{"course_name": "大学体育Ⅲ"}])
    text = Path(config).read_text(encoding="utf-8").replace(
        f'  password: "{PASSWORD}"', '  password: ""'
    )
    Path(config).write_text(text, encoding="utf-8")

    assert main(["grab", "--config", config]) == 2
    err = capsys.readouterr().err
    assert "缺少密码" in err
    assert "QK_PASSWORD" in err
