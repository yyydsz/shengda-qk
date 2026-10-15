"""真实验证：从升达 CAS 取一张图片验证码并保存，确认整条链路可用。

只发一个只读 GET，不需要任何凭据。图片写到系统临时目录，不落在仓库里。

用法::

    python tools\\fetch_captcha_check.py            # 取一张，打印信息
    python tools\\fetch_captcha_check.py --open     # 取完用系统看图器打开
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.cas import CasLoginAdapter  # noqa: E402
from qk.config import load_config  # noqa: E402
from qk.net import QkSession  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="取一张真实验证码做连通性检查")
    ap.add_argument("--open", action="store_true", help="取完用系统看图器打开图片")
    args = ap.parse_args()

    config = load_config(ROOT / "config.yaml", environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    adapter = CasLoginAdapter(config, session)

    print(f"CAS 基址：{adapter.cas_base()}")
    print(f"验证码端点：{config.cas.captcha_path}")
    uid, image = adapter.fetch_captcha()
    print("\n✓ 取到验证码")
    print(f"   uid    = {uid}")
    print(f"   PNG 字节 = {len(image)}")
    print(f"   文件头 = {image[:8].hex()}（89504e470d0a1a0a = PNG）")

    out = Path(tempfile.gettempdir()) / "qk_captcha_sample.png"
    out.write_bytes(image)
    print(f"\n   已保存到 {out} —— 打开看看是不是能读出算式")
    if args.open:
        import os  # noqa: PLC0415

        os.startfile(out)  # type: ignore[attr-defined]  # noqa: S606 - Windows 专用

    # 再取一张，确认 uid 每次都不同（服务端每张图独立校验）
    uid2, image2 = adapter.fetch_captcha()
    same_uid = uid2 == uid
    print(f"\n第二次：uid 与第一次{'相同 ✗（可疑）' if same_uid else '不同 ✓'}")
    print(f"        图片与第一次{'相同' if image2 == image else '不同 ✓'}")

    # 对照：不填验证码直接登录，应当被服务端以 CODEFALSE 拒绝
    print("\n=== 对照：不填验证码直接登录（预期被拒）===")
    try:
        adapter.login_with_password("00000000000", "dummy-placeholder", "",
                                   captcha_uid="")
        print("   未被拒绝（意外）")
    except Exception as exc:  # noqa: BLE001
        print(f"   {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
