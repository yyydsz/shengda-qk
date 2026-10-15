"""账号密码 + 图片验证码登录（``loginType=""`` 那条路径）。

契约全部来自 2026-10-15 在真实登录页的抓包：

* ``GET /lyuapServer/kaptcha?_t=<ms>&uid=``
  → ``{"kaptchaType":"1","uid":"<32hex>","content":"data:image/png;base64,…"}``
* ``POST /lyuapServer/v1/tickets``（表单编码）字段：
  ``username`` / ``password``（**RSA 密文**）/ ``service`` / ``loginType``（**空串**）
  / ``id``（验证码 uid）/ ``code``（用户填的答案）
* 失败时**仍返回 HTTP 200**，原因在 ``data.code``：实测见过 ``NOUSER``、``CODEFALSE``。

这些用例跑在 ``qk/mockserver.py`` 上（离线），mock 的失败响应已按真实结构对齐。
"""

from __future__ import annotations

import pytest

from qk.adapters.base import AuthError
from qk.adapters.cas import CasLoginAdapter
from qk.adapters.cas_rsa import encrypt_password
from qk.net import QkSession


def _adapter(config) -> CasLoginAdapter:
    return CasLoginAdapter(config, QkSession.from_config(config))


# --------------------------------------------------------------------------
# 取验证码
# --------------------------------------------------------------------------


def test_fetch_captcha_returns_uid_and_png(mock_server, config_factory) -> None:
    """验证码接口要给出 uid 与**可解码的 PNG**。"""
    config = config_factory(mock_server.base_url)
    uid, image = _adapter(config).fetch_captcha()

    assert len(uid) == 32, f"真实 uid 是 32 位十六进制，实际 {uid!r}"
    assert all(c in "0123456789abcdef" for c in uid)
    assert image.startswith(b"\x89PNG\r\n\x1a\n"), "返回的应当是 PNG 原始字节"
    assert len(image) > 50


def test_fetch_captcha_uid_changes_each_call(mock_server, config_factory) -> None:
    """每次取图都是新的 uid —— 服务端靠 uid 找回这张图的答案。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    first, _ = adapter.fetch_captcha()
    second, _ = adapter.fetch_captcha()
    assert first != second


def test_fetch_captcha_raises_on_garbage(mock_server, config_factory, monkeypatch) -> None:
    """响应里没有 content 时要给出可读错误，而不是抛 KeyError。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)

    class _Resp:
        status = 200

        @staticmethod
        def json():
            return {"kaptchaType": "1", "uid": "x", "unexpected": True}

    monkeypatch.setattr(adapter.session, "get", lambda *a, **k: _Resp())
    with pytest.raises(AuthError) as excinfo:
        adapter.fetch_captcha()
    assert "content" in str(excinfo.value)


# --------------------------------------------------------------------------
# 登录成功
# --------------------------------------------------------------------------


def test_password_login_success_with_correct_captcha(mock_server, config_factory) -> None:
    """账号 + 密码 + 正确验证码 → 建会话成功。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"

    uid, image = adapter.fetch_captcha()
    assert image  # 取到图才有 uid 可用

    info = adapter.login_with_password(
        mock_server.config.student_id, mock_server.config.password, "24", captcha_uid=uid)
    assert info.ok is True


def test_password_login_sends_rsa_ciphertext_and_empty_login_type(
    mock_server, config_factory, monkeypatch
) -> None:
    """提交体必须：密码是 RSA 密文、loginType 是**空串**、带 id 与 code。

    这三条正是「密码登录」与「扫码建会话」的区别 —— 少任何一条都会失败。
    """
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"
    uid, _ = adapter.fetch_captcha()

    captured: dict[str, str] = {}
    original_post = adapter.session.post

    def spy(url, **kwargs):
        if "/v1/tickets" in str(url):
            captured.update(kwargs.get("data") or {})
        return original_post(url, **kwargs)

    monkeypatch.setattr(adapter.session, "post", spy)
    adapter.login_with_password("20230001", "mock-pass", "24", captcha_uid=uid)

    assert captured.get("loginType") == "", "密码登录必须发空串，不能用扫码的 \"3\""
    assert captured.get("id") == uid
    assert captured.get("code") == "24"
    assert captured.get("username") == "20230001"
    assert captured.get("password") == encrypt_password("mock-pass"), "密码必须是 RSA 密文"


# --------------------------------------------------------------------------
# 失败原因（服务端用 data.code 说明，HTTP 仍是 200）
# --------------------------------------------------------------------------


def test_wrong_captcha_reports_codefalse(mock_server, config_factory) -> None:
    """验证码填错 → CODEFALSE，且提示里要告诉用户怎么恢复。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"
    uid, _ = adapter.fetch_captcha()

    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_password(
            mock_server.config.student_id, mock_server.config.password, "99", captcha_uid=uid)
    message = str(excinfo.value)
    assert "CODEFALSE" in message
    assert "验证码" in message, "应给出可操作提示，而不是只回显错误码"


def test_missing_captcha_uid_reports_codefalse(mock_server, config_factory) -> None:
    """不带 uid（或 uid 已作废）也归为验证码错误。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_password(
            mock_server.config.student_id, mock_server.config.password, "24", captcha_uid="")
    assert "CODEFALSE" in str(excinfo.value)


def test_unknown_user_reports_nouser(mock_server, config_factory) -> None:
    """账号不存在 → NOUSER，提示要能指向「核对学号」。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"
    uid, _ = adapter.fetch_captcha()

    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_password("99999999999", "whatever", "24", captcha_uid=uid)
    message = str(excinfo.value)
    assert "NOUSER" in message
    assert "账号" in message


def test_wrong_password_reports_raw_code(mock_server, config_factory) -> None:
    """密码错误：真实原因码未实测到，因此**原样回显**让用户能看到并反馈。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"
    uid, _ = adapter.fetch_captcha()

    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_password(
            mock_server.config.student_id, "definitely-not-the-password", "24", captcha_uid=uid)
    assert "PWDERR" in str(excinfo.value)


def test_captcha_answer_is_single_use(mock_server, config_factory) -> None:
    """同一个 uid 用过即作废：换图后旧 uid 不能再用（防重放）。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    mock_server.state.kaptcha_expected = "24"
    uid, _ = adapter.fetch_captcha()

    adapter.login_with_password(
        mock_server.config.student_id, mock_server.config.password, "24", captcha_uid=uid)

    # 同一个 uid 再来一次 → 答案已被消费掉
    with pytest.raises(AuthError) as excinfo:
        adapter.login_with_password(
            mock_server.config.student_id, mock_server.config.password, "24", captcha_uid=uid)
    assert "CODEFALSE" in str(excinfo.value)


def test_empty_credentials_rejected_before_network(mock_server, config_factory) -> None:
    """账号或密码为空时本地就拒绝，不发请求。"""
    config = config_factory(mock_server.base_url)
    adapter = _adapter(config)
    for user, password in (("", "x"), ("20230001", "")):
        with pytest.raises(AuthError):
            adapter.login_with_password(user, password, "24", captcha_uid="whatever")
