"""CAS 账号密码登录用的 RSA 加密：用**浏览器真实密文**做回归。

测试向量怎么来的（可复现）
--------------------------
1. 打开 `https://cas.shengda.edu.cn/lyuapServer/login?service=…`；
2. 用 jsreverse 往 `#userName` / `#password` / `#captcha` 填值
   （密码填 `zzz-not-a-real-password`，账号与验证码随便填）；
3. 点「登 录」，在 Network 里导出 `POST /lyuapServer/v1/tickets` 的请求体；
4. 取其中 `password=` 后面的十六进制串作为期望值。

这条向量是从**真实前端**产生的，因此只要本实现与它一致，
就说明移植正确 —— 不需要任何凭据。
"""

from __future__ import annotations

from qk.adapters.cas_rsa import block_size, encrypt_password

#: 浏览器在登录页真实加密 `PLAIN` 得到的密文（256 位十六进制，无空格）
BROWSER_CIPHERTEXT = (
    "502d19f461b666ff202cac4714a3a0a1abd52ce387483e450480ea8383b20e0b"
    "8d0f105e7877474aaf42dbdf807d5d0cae2a43f8a2aaab4e119ed4f3fce8c392"
    "2df75dd510bbbecbba07c715b422742a364fc60e78a2df852252b2f941a2a024"
    "2dd876cebf463f1212fad0087118d932b17501ded0d9e531dd598eac70051a65"
)
PLAIN = "zzz-not-a-real-password"


def test_matches_browser_ciphertext() -> None:
    """核心：移植实现必须与浏览器逐字符一致（否则密码登录一定被拒）。"""
    assert encrypt_password(PLAIN) == BROWSER_CIPHERTEXT


def test_output_is_single_unspaced_block() -> None:
    """真实请求体里是一整串、无空格；且长度等于模数的十六进制宽度。"""
    out = encrypt_password(PLAIN)
    assert " " not in out, "服务端要的是无空格拼接"
    assert len(out) == 256, f"2048 位模数应得 256 个 hex 字符，实际 {len(out)}"
    assert out == out.lower()
    assert all(c in "0123456789abcdef" for c in out)


def test_reserved_underscore_variant_matches_js_style() -> None:
    """保留原 JS 的「空格连接」形式，便于与前端调试输出对照。

    单块密码时两种写法应当相同（只有一个 part）。
    """
    assert encrypt_password(PLAIN, separator=" ") == BROWSER_CIPHERTEXT


def test_empty_password_is_empty() -> None:
    """空密码不加密（返回空串），避免把填充字节当成密码发出去。"""
    assert encrypt_password("") == ""


def test_block_size_follows_js_semantics() -> None:
    """一块能放多少字符：按原 JS 的算法是 ``2 * top_index`` = **126**。

    容易被想当然成「2048 位 = 256 字节 = 一次 256 个字符」，但前端不是这么算的：
    它把模数按 4 位十六进制切成 64 段（低位在前），取**最高非零段的下标** 63，
    再乘 2 得到 126。照抄这个值才能与前端产生同样的分块边界 ——
    分块边界一变，密文就完全不同，服务端会拒。
    """
    assert block_size() == 126


def test_long_password_spans_blocks() -> None:
    """超过一块时按块拼接（不丢字符、不抛异常）。"""
    long_password = "a" * (block_size() + 3)
    out = encrypt_password(long_password)
    assert out
    assert " " not in out
    # 两块 → 512 个 hex 字符
    assert len(out) == 512
    # 单块与两块的密文必须不同（防止分块被忽略）
    assert out != encrypt_password("a" * block_size())


def test_different_passwords_give_different_ciphertext() -> None:
    """不同明文必须产生不同密文（防止误用固定值）。"""
    assert encrypt_password("password-a") != encrypt_password("password-b")
