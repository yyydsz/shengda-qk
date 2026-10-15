"""pytest 公共夹具：临时工作区配置、mock 教务服务、已登录适配器。

全部离线运行：mock 服务监听 127.0.0.1 的随机端口，不访问任何校园网地址。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterator

import pytest

from qk.adapters.cas import CasLoginAdapter
from qk.adapters.jwgl import JwglCourseAdapter
from qk.config import AppConfig, load_config
from qk.mockserver import MockConfig, MockCourse, MockServer, start_mock_server
from qk.net import QkSession

#: mock 服务的接口路径（与 qk/mockserver.py 的路由一致）
MOCK_PATHS = {
    "course_list_path": "/jwglxt/xsxk/xkList",
    "select_path": "/jwglxt/xsxk/xkSelect",
    "drop_path": "/jwglxt/xsxk/xkDrop",
    "selected_list_path": "/jwglxt/xsxk/selectedList",
}

MOCK_STUDENT_ID = "20230001"
MOCK_PASSWORD = "mock-pass"


@pytest.fixture
def mock_server() -> Iterator[MockServer]:
    """默认课程表的 mock 教务服务（含容量满、已选、退课失败等分支）。

    每个用例新建一个服务实例，因此天然隔离；不要在此处调用 ``state.reset()``
    ——服务启动后再清空状态会把已签发的票据一起抹掉。
    """
    server = start_mock_server(MockConfig.default(student_id=MOCK_STUDENT_ID, password=MOCK_PASSWORD))
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def make_mock_server() -> Iterator[Callable[..., MockServer]]:
    """按需构造 mock 服务（可自定义课程与异常分支）。"""
    servers: list[MockServer] = []

    def factory(config: MockConfig | None = None, **kwargs) -> MockServer:
        server = start_mock_server(config, **kwargs)
        servers.append(server)
        return server

    try:
        yield factory
    finally:
        for server in servers:
            server.stop()


def _write_yaml(path: Path, base_url: str, *, extra: str = "", credentials: bool = True,
                jwgl_paths: dict[str, str] | None = None) -> None:
    creds = (
        f'credentials:\n  student_id: "{MOCK_STUDENT_ID}"\n  password: "{MOCK_PASSWORD}"\n'
        if credentials
        else ""
    )
    paths = {**MOCK_PATHS, **(jwgl_paths or {})}
    path.write_text(
        f"""
{creds}http:
  interval: 0.0
  timeout: 5.0
  max_retries: 2
  concurrency: 4
cas:
  login_url: "{base_url.rstrip('/')}/lyuapServer/login"
  service: "{base_url}"
  logout_url: "{base_url.rstrip('/')}/lyuapServer/logout"
  test_login_path: "/"
jwgl:
  base_url: "{base_url}"
  index_path: "/"
  course_list_path: "{paths['course_list_path']}"
  select_path: "{paths['select_path']}"
  drop_path: "{paths['drop_path']}"
  selected_list_path: "{paths['selected_list_path']}"
  platform: "custom"
grab:
  retry_interval: 0.0
  max_attempts: 5
  stop_on_success: true
{extra}
""",
        encoding="utf-8",
    )


@pytest.fixture
def workspace() -> Iterator[Path]:
    """临时工作区目录（自动清理，不污染真实仓库）。"""
    path = Path(tempfile.mkdtemp(prefix="qk-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def config_factory(workspace: Path) -> Callable[..., AppConfig]:
    """生成指向 mock 服务的配置。

    每次调用写入**独立文件**：早期版本按 base_url 做哈希命名，同名会覆盖，
    导致用例之间拿到对方的配置（例如 TTL 被换成另一个用例的值）。
    """

    def factory(base_url: str, *, extra: str = "", credentials: bool = True,
                environ: dict | None = None, jwgl_paths: dict[str, str] | None = None) -> AppConfig:
        config_dir = Path(tempfile.mkdtemp(prefix="qk-cfg-", dir=workspace))
        config_path = config_dir / "config.yaml"
        _write_yaml(config_path, base_url, extra=extra, credentials=credentials, jwgl_paths=jwgl_paths)
        return load_config(config_path, environ=environ or {}, workspace=workspace)

    return factory


@pytest.fixture
def mock_app(mock_server: MockServer, config_factory: Callable[..., AppConfig]) -> tuple[AppConfig, MockServer]:
    """已指向 mock 服务的配置 + 该 mock 服务。"""
    return config_factory(mock_server.base_url), mock_server


@pytest.fixture
def logged_in(mock_app: tuple[AppConfig, MockServer]) -> tuple[AppConfig, MockServer, QkSession]:
    """完成登录的会话（CAS 适配器已跑通一次完整流程）。"""
    config, server = mock_app
    session = QkSession.from_config(config)
    info = CasLoginAdapter(config, session).login()
    assert info.ok, f"mock 登录失败：{info.message}"
    return config, server, session


@pytest.fixture
def course_adapter(logged_in) -> JwglCourseAdapter:
    """指向 mock 服务的选课适配器（已登录）。"""
    config, _server, session = logged_in
    return JwglCourseAdapter(config, session)


def make_course(teaching_class_id: str = "JXB900", **overrides) -> MockCourse:
    """构造一条 mock 课程，便于测试里临时定制。"""
    payload = {
        "course_code": "90000001",
        "course_name": "测试课程",
        "teacher": "测试老师",
        "credits": 1.0,
        "capacity": 10,
        "enrolled": 0,
        "time_text": "周一 1-2 节",
    }
    payload.update(overrides)
    return MockCourse(teaching_class_id=teaching_class_id, **payload)
