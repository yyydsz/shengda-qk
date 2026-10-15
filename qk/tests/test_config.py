"""配置层测试：加载、环境变量覆盖、凭据脱敏、缺凭据报错。

全部离线运行，不访问网络，也不依赖真实凭据。
"""

from __future__ import annotations

import json

import pytest

from qk.config import (
    AppConfig,
    ConfigError,
    CourseTarget,
    Credentials,
    GrabConfig,
    apply_flat_credentials,
    deep_merge,
    env_overrides,
    find_default_config,
    is_within,
    iter_secret_values,
    load_config,
    parse_dotenv,
)
from qk.models import is_sensitive_key, mask_url, redact


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def write_file(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_deep_merge_nested():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    merged = deep_merge(base, {"b": {"c": 9}, "e": 5})
    assert merged == {"a": 1, "b": {"c": 9, "d": 3}, "e": 5}
    # 原字典不被修改
    assert base["b"]["c"] == 2


def test_deep_merge_ignores_none():
    assert deep_merge({"a": 1}, {"a": None}) == {"a": 1}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A=1\nB=2\n", {"A": "1", "B": "2"}),
        ("# 注释\n\nA = 1 \nexport B=two\n", {"A": "1", "B": "two"}),
        ("C='quoted'\nD=\"dq\"\n", {"C": "quoted", "D": "dq"}),
        ("NOEQ\n", {}),
    ],
)
def test_parse_dotenv(text, expected):
    assert parse_dotenv(text) == expected


def test_env_overrides_nested_and_flat():
    env = {
        "QK_STUDENT_ID": "20230001",
        "QK_HTTP__INTERVAL": "0.5",
        "QK_CAS__LOGIN_URL": "https://example.invalid/login",
        "OTHER": "ignored",
        "QK_EMPTY": "",
    }
    tree = env_overrides(env)
    assert tree["http"]["interval"] == "0.5"
    assert tree["cas"]["login_url"] == "https://example.invalid/login"
    assert "other" not in tree
    assert "empty" not in tree
    # 扁平凭据写法不进入嵌套树，而是由 apply_flat_credentials 归并到 credentials
    assert "student_id" not in tree
    data: dict = {}
    apply_flat_credentials(data, env)
    assert data["credentials"]["student_id"] == "20230001"


def test_is_within(tmp_path):
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    assert is_within(inner, tmp_path)
    assert not is_within(tmp_path.parent, tmp_path)


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def test_defaults_without_config_file(tmp_path):
    config = load_config(workspace=tmp_path, environ={})
    assert isinstance(config, AppConfig)
    assert config.http.interval == 0.35
    assert config.http.timeout == 10.0
    assert config.jwgl.base_url.startswith("https://")
    assert config.cas.login_url.startswith("https://cas.shengda.edu.cn")
    assert config.grab.targets == []
    # 即使没有配置文件也必须能通过结构校验
    config.validate()
    assert config.sources == ["默认值"]


def test_load_yaml_config(tmp_path):
    write_file(
        tmp_path,
        "config.yaml",
        """
http:
  interval: 1.25
  max_retries: 5
  headers:
    X-Test: "yes"
cas:
  login_url: "https://cas.example.invalid/login"
jwgl:
  base_url: "https://jw.example.invalid/"
grab:
  start_at: "2026-10-13 12:00:00"
  targets:
    - course_name: "大学体育Ⅲ"
      teacher: "张老师"
      priority: 0
""",
    )
    config = load_config(workspace=tmp_path, environ={})
    assert config.http.interval == 1.25
    assert config.http.max_retries == 5
    assert config.http.headers["X-Test"] == "yes"
    # 默认 UA 仍然保留，自定义头是叠加而不是整体替换
    assert "User-Agent" in config.http.headers
    assert config.grab.targets[0].course_name == "大学体育Ⅲ"
    assert config.grab.targets[0].teacher == "张老师"
    assert "配置文件" in config.sources[1]


def test_default_config_discovery(tmp_path):
    assert find_default_config(tmp_path) == ""
    write_file(tmp_path, "config.yml", "http:\n  interval: 0.5\n")
    assert find_default_config(tmp_path).endswith("config.yml")


def test_explicit_config_path_missing(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml", environ={})


def test_env_overrides_file(tmp_path):
    write_file(
        tmp_path,
        "config.yaml",
        "http:\n  interval: 1.0\ncredentials:\n  student_id: \"from-file\"\n",
    )
    env = {"QK_HTTP__INTERVAL": "2.5", "QK_STUDENT_ID": "20239999"}
    config = load_config(workspace=tmp_path, environ=env)
    assert config.http.interval == 2.5
    # 环境变量优先级最高，覆盖配置文件里的学号
    assert config.creds.student_id == "20239999"
    assert "环境变量 QK_*" in config.sources


def test_env_overrides_dotenv(tmp_path):
    """三层优先级：默认值 < 配置文件 < .env < 环境变量。"""
    write_file(tmp_path, "config.yaml", "credentials:\n  student_id: \"from-file\"\n")
    env_file = write_file(tmp_path, "creds.env", "QK_STUDENT_ID=from-dotenv\nQK_PASSWORD=pw\n")
    config = load_config(workspace=tmp_path, environ={"QK_STUDENT_ID": "from-environ"}, dotenv_path=env_file)
    assert config.creds.student_id == "from-environ"
    assert config.creds.password == "pw"

    config_no_env = load_config(workspace=tmp_path, environ={}, dotenv_path=env_file)
    assert config_no_env.creds.student_id == "from-dotenv"


def test_dotenv_does_not_override_config_file(tmp_path):
    write_file(tmp_path, "config.yaml", "credentials:\n  student_id: \"from-file\"\n")
    env_file = write_file(tmp_path, "creds.env", "QK_STUDENT_ID=from-dotenv\n")
    config = load_config(workspace=tmp_path, environ={}, dotenv_path=env_file)
    # 优先级：默认值 < 配置文件 < .env < 环境变量
    assert config.creds.student_id == "from-dotenv"


def test_boolean_and_int_coercion_from_env(tmp_path):
    env = {
        "QK_HTTP__VERIFY_TLS": "false",
        "QK_HTTP__MAX_RETRIES": "7",
        "QK_HTTP__TIMEOUT": "3.5",
    }
    config = load_config(workspace=tmp_path, environ=env)
    assert config.http.verify_tls is False
    assert config.http.max_retries == 7
    assert config.http.timeout == 3.5


def test_invalid_type_from_env_raises(tmp_path):
    with pytest.raises(ConfigError, match="需要整数"):
        load_config(workspace=tmp_path, environ={"QK_HTTP__MAX_RETRIES": "abc"})


def test_list_coercion_from_env(tmp_path):
    env = {"QK_CAS__SUCCESS_KEYWORDS": "选课,退出登录"}
    config = load_config(workspace=tmp_path, environ=env)
    assert config.cas.success_keywords == ["选课", "退出登录"]


def test_unknown_key_rejected(tmp_path):
    write_file(tmp_path, "config.yaml", "http:\n  interval_seconds: 1\n")
    with pytest.raises(ConfigError, match="无法识别"):
        load_config(workspace=tmp_path, environ={})


def test_unknown_top_level_key_rejected(tmp_path):
    write_file(tmp_path, "config.yaml", "htttp:\n  interval: 1\n")
    with pytest.raises(ConfigError, match="无法识别"):
        load_config(workspace=tmp_path, environ={})


def test_dotenv_credentials(tmp_path):
    env_file = write_file(tmp_path, "creds.env", "QK_STUDENT_ID=20231234\nQK_PASSWORD=secret-pass\n")
    config = load_config(workspace=tmp_path, environ={}, dotenv_path=env_file)
    assert config.creds.student_id == "20231234"
    assert config.creds.password == "secret-pass"
    assert config.creds.complete


def test_dotenv_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="dotenv 文件不存在"):
        load_config(workspace=tmp_path, environ={}, dotenv_path=tmp_path / "nope.env")


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, message",
    [
        ("http:\n  interval: -1\n", "interval"),
        ("http:\n  timeout: 0\n", "timeout"),
        ("http:\n  concurrency: 0\n", "concurrency"),
        ("grab:\n  max_attempts: 0\n", "max_attempts"),
        ("cas:\n  login_url: \"ftp://x\"\n", "login_url"),
        ("jwgl:\n  base_url: \"not-a-url\"\n", "base_url"),
        ("grab:\n  targets:\n    - priority: 1\n", "course_name"),
    ],
)
def test_validate_rejects_bad_values(tmp_path, payload, message):
    write_file(tmp_path, "config.yaml", payload)
    with pytest.raises(ConfigError, match=message):
        load_config(workspace=tmp_path, environ={})


def test_require_credentials_missing_student_id():
    config = AppConfig(creds=Credentials(password="x"))
    with pytest.raises(ConfigError, match="缺少学号"):
        config.require_credentials()


def test_require_credentials_missing_password():
    config = AppConfig(creds=Credentials(student_id="20230001"))
    with pytest.raises(ConfigError, match="缺少密码"):
        config.require_credentials()


def test_require_credentials_ok():
    config = AppConfig(creds=Credentials(student_id="1", password="2"))
    assert config.require_credentials().complete


# --------------------------------------------------------------------------
# 脱敏（C4）
# --------------------------------------------------------------------------


def test_redacted_hides_password_and_token(tmp_path):
    env = {
        "QK_STUDENT_ID": "20230001",
        "QK_PASSWORD": "sup3r-secret-pass",
        "QK_CAPTCHA_CODE": "8888",
    }
    config = load_config(workspace=tmp_path, environ=env)
    view = config.redacted()
    dumped = json.dumps(view, ensure_ascii=False)
    assert "sup3r-secret-pass" not in dumped
    assert "8888" not in dumped
    assert view["creds"]["password"] == "***"
    assert view["creds"]["student_id"] == "20230001"  # 学号本身不是秘密


def test_iter_secret_values_yields_only_non_empty():
    config = AppConfig(creds=Credentials(student_id="1", password="p", captcha_code=""))
    secrets = dict(iter_secret_values(config))
    assert secrets == {"credentials.password": "p"}


def test_secrets_never_in_repr(tmp_path):
    env = {"QK_PASSWORD": "another-secret"}
    config = load_config(workspace=tmp_path, environ=env)
    assert "another-secret" not in repr(config.redacted())
    assert "another-secret" not in str(config.redacted())


def test_redact_masks_nested_sensitive_keys():
    data = {
        "credentials": {"password": "p", "student_id": "1"},
        "session": {"token": "t", "cookie": "c", "name": "n"},
        "plain": "visible",
        "nested": [{"ticket": "tk"}],
    }
    masked = redact(data)
    # 容器键保持可读，叶子敏感键打码
    assert set(masked) == {"credentials", "session", "plain", "nested"}
    assert masked["credentials"]["password"] == "***"
    assert masked["credentials"]["student_id"] == "1"
    assert masked["session"]["token"] == "***"
    assert masked["session"]["cookie"] == "***"
    assert masked["session"]["name"] == "n"
    assert masked["plain"] == "visible"
    assert masked["nested"] == [{"ticket": "***"}]


@pytest.mark.parametrize(
    "key, sensitive",
    [
        ("JSESSIONID", True),
        ("jsessionid", True),
        ("session_id", True),
        ("sessionId", True),
        ("MOD_AUTH_CAS", True),
        ("CASTGC", True),
        ("PHPSESSID", True),
        ("Set-Cookie", True),
        ("Cookie", True),
        ("password", True),
        ("passwd", True),
        ("pwd", True),
        ("token", True),
        ("accessToken", True),
        ("csrf_token", True),
        ("secret", True),
        ("ticket", True),
        ("authorization", True),
        ("credentials", True),
        ("student_id", False),
        ("course_name", False),
        ("interval", False),
        ("attempts", False),
    ],
)
def test_is_sensitive_key(key, sensitive):
    assert is_sensitive_key(key) is sensitive


def test_redact_masks_leaf_session_cookie_keys():
    """回归：容器先递归修好后，JSESSIONID 这类叶子键仍必须打码。"""
    masked = redact({"JSESSIONID": "ABCDEF0123456789", "MOD_AUTH_CAS": "xyz987654321"})
    assert masked["JSESSIONID"] == "***"
    assert masked["MOD_AUTH_CAS"] == "***"
    # 容器本身可读
    masked_nested = redact({"cookies": {"JSESSIONID": "ABCDEF0123456789"}})
    assert masked_nested["cookies"]["JSESSIONID"] == "***"


def test_redact_keeps_cookie_names_viewable():
    """cookie_names 这类“只有名字没有值”的输出不应被整段吞掉。"""
    masked = redact({"cookie_names": ["JSESSIONID", "MOD_AUTH_CAS"]})
    assert masked["cookie_names"] == ["JSESSIONID", "MOD_AUTH_CAS"]


def test_redact_handles_response_headers():
    view = redact({"headers": {"Set-Cookie": "JSESSIONID=SECRETVALUE123", "Content-Type": "text/html"}})
    assert view["headers"]["Set-Cookie"] == "***"
    assert view["headers"]["Content-Type"] == "text/html"


def test_mask_url_hides_ticket():
    url = "https://my.shengda.edu.cn/?ticket=ST-12345-abc&x=1"
    masked = mask_url(url)
    assert "ST-12345-abc" not in masked
    assert "ticket=***" in masked
    assert "x=1" in masked
    assert mask_url("https://my.shengda.edu.cn/") == "https://my.shengda.edu.cn/"


def test_describe_targets_readable():
    config = AppConfig(
        grab=GrabConfig(
            targets=[CourseTarget(course_name="大学体育Ⅲ", teacher="张老师", priority=0)]
        )
    )
    lines = config.describe_targets()
    assert len(lines) == 1
    assert "大学体育Ⅲ" in lines[0] and "张老师" in lines[0]


def test_describe_targets_empty():
    assert "未配置任何抢课目标" in AppConfig().describe_targets()[0]
