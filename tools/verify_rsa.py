"""对照浏览器真实密文，验证 ``qk.adapters.cas_rsa`` 的 RSA 移植是否正确。

用途
----
``qk/tests/test_cas_rsa.py`` 已把这条向量固化成回归测试。本脚本是**手动排查版**：
当怀疑「密码加密不对」时，打印期望值与实际值，一眼看出差异位置。

如何自己产生一条新的测试向量
----------------------------
1. 打开 `https://cas.shengda.edu.cn/lyuapServer/login?service=…`；
2. 用 jsreverse（或 F12）往 ``#userName`` / ``#password`` / ``#captcha`` 填值 ——
   密码填一个**假的**字符串（例如 `zzz-not-a-real-password`）；
3. 点「登 录」，在 Network 里导出 ``POST /lyuapServer/v1/tickets`` 的请求体；
4. 把 ``password=`` 后面那串十六进制贴到下面 ``BROWSER_CIPHERTEXT``，
   把明文贴到 ``PLAIN``，运行本脚本 —— 一致即可放心。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.cas_rsa import block_size, encrypt_password  # noqa: E402

#: 浏览器在登录页真实加密下面这串明文得到的密文（256 位十六进制，无空格）
BROWSER_CIPHERTEXT = (
    "502d19f461b666ff202cac4714a3a0a1abd52ce387483e450480ea8383b20e0b"
    "8d0f105e7877474aaf42dbdf807d5d0cae2a43f8a2aaab4e119ed4f3fce8c392"
    "2df75dd510bbbecbba07c715b422742a364fc60e78a2df852252b2f941a2a024"
    "2dd876cebf463f1212fad0087118d932b17501ded0d9e531dd598eac70051a65"
)
PLAIN = "zzz-not-a-real-password"


def main() -> int:
    print(f"明文            : {PLAIN!r}（{len(PLAIN)} 字符）")
    print(f"分块大小        : {block_size()} 字符/块（原 JS 用 2*top_index 推算，非 256）")
    print(f"浏览器密文长度  : {len(BROWSER_CIPHERTEXT)} hex")

    mine = encrypt_password(PLAIN)
    print(f"本实现密文长度  : {len(mine)} hex")
    print()
    print(f"浏览器 : {BROWSER_CIPHERTEXT}")
    print(f"本实现 : {mine}")
    print()

    if mine == BROWSER_CIPHERTEXT:
        print("✓ 完全一致 —— RSA 移植正确")
        return 0
    print("✗ 不一致：")
    for i, (a, b) in enumerate(zip(mine, BROWSER_CIPHERTEXT)):
        if a != b:
            print(f"   首个差异在第 {i} 个字符：本实现={a!r} 浏览器={b!r}")
            break
    else:
        print("   前缀相同但长度不同 —— 检查分块处理")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
