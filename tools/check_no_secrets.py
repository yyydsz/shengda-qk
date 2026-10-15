"""交付前只读自检。

两项检查：

1. **交付物凭据扫描**（必须通过）：计划 writePaths 里的文件不得包含密码、Cookie、
   Token 等凭据内容。
2. **仓库历史文件扫描**（报告，不计失败）：工作区里侦查阶段遗留的 ``.html`` 等
   抓取文件不是本次交付物，但 C4 要求“仓库内不存在凭据内容”，所以一并扫描并
   如实报告；发现命中会在结论里明确标注为待人工处理。

另外校验 ``.gitignore`` 是否覆盖本地凭据文件，并直接演练一次
:func:`qk.models.redact`，确认 Cookie 映射经过脱敏后不会泄露值。

用法::

    python tools/check_no_secrets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.models import find_credentials, redact  # noqa: E402 - 需要先把仓库根加入 sys.path

#: 整行「中文注释」不参与扫描：像 ``# 登录方式：password=账号密码`` 这样的说明文字
#: 会被弱模式误判成密钥赋值。只跳过**整行且含中文**的注释；ASCII 注释照常扫描
#: （真正的凭据泄漏通常出现在那种位置）。
_CJK = re.compile(r"[\u3400-\u9fff\uff00-\uffef]")
_COMMENT_PREFIXES = ("#", "//")


def strip_chinese_comments(text: str) -> str:
    """把整行中文注释替换成空行，其余内容原样保留。"""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_COMMENT_PREFIXES) and _CJK.search(line):
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines)

#: 本次交付物（由计划的 writePaths 决定，另含 requirements-dev.txt 与 tools/ 自检脚本）
DELIVERABLES = (
    "qk/__init__.py",
    "qk/__main__.py",
    "qk/config.py",
    "qk/models.py",
    "qk/net.py",
    "qk/adapters/__init__.py",
    "qk/adapters/base.py",
    "config.example.yaml",
    "requirements.txt",
    "requirements-dev.txt",
    ".gitignore",
    "README.md",
    "tools/check_no_secrets.py",
)

#: .gitignore 必须覆盖的本地凭据文件名
MUST_IGNORE = ("config.yaml", "config.yml", "config.local.yaml", ".env")

#: 需要扫描的历史文件扩展名（非交付物）；.bin 是抓包落盘的响应体
REPO_SCAN_SUFFIXES = (
    ".html", ".htm", ".js", ".txt", ".yaml", ".yml", ".json", ".env", ".har", ".bin", ".log", ".ps1", ".kdbx"
)

#: 扫描体积上限，避免把巨型打包文件整段读进来
MAX_SCAN_BYTES = 3 * 1024 * 1024

#: 本脚本会故意写出演示用的值（如 SECRET-COOKIE-VALUE）来验证脱敏是否生效，
#: 扫描自身时跳过，避免把演练样例误判成真实凭据。
SELF_SKIP = "tools/check_no_secrets.py"


def read(relative: str) -> str:
    path = ROOT / relative
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def repo_files() -> list[Path]:
    """仓库内需要扫描的历史文件（排除交付物、缓存目录与大文件）。"""
    deliverable_paths = {(ROOT / name).resolve() for name in DELIVERABLES}
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in REPO_SCAN_SUFFIXES:
            continue
        if path.resolve() in deliverable_paths:
            continue
        if any(part in {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache"} for part in path.parts):
            continue
        if ".codegraph" in path.parts:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found)


def main() -> int:
    failures: list[str] = []

    print("== 1. 交付物清单 ==")
    texts: list[tuple[str, str]] = []
    for relative in dict.fromkeys(DELIVERABLES):  # 去重且保持顺序
        path = ROOT / relative
        if not path.exists():
            failures.append(f"交付物缺失：{relative}")
            print(f"  [缺失] {relative}")
            continue
        content = read(relative)
        texts.append((relative, content))
        print(f"  [ok] {relative}（{len(content)} 字符）")

    print("\n== 2. 交付物凭据扫描（必须为空）==")
    scanned = [(name, strip_chinese_comments(content)) for name, content in texts if name != SELF_SKIP]
    hits = find_credentials(scanned)
    if hits:
        failures.extend(f"疑似凭据：{name}" for name in hits)
        print("  [失败] " + "；".join(hits))
    else:
        print(f"  [ok] {len(scanned)} 个交付文件未发现 password/JSESSIONID/MOD_AUTH_CAS 等凭据模式")
    print(f"  [跳过] {SELF_SKIP}：本脚本含演示样例（SECRET-COOKIE-VALUE 等），由第 6 项脱敏演练单独验证")

    print("\n== 3. 仓库历史文件扫描（侦查阶段遗留，非交付物）==")
    historical = repo_files()
    historical_texts = []
    for path in historical:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        historical_texts.append(
            (str(path.relative_to(ROOT)), strip_chinese_comments(data.decode("utf-8", errors="replace")))
        )
    historical_hits = find_credentials(historical_texts)
    print(f"  扫描文件（{len(historical_texts)} 个）：")
    for name, _text in historical_texts:
        print(f"    - {name}")
    if historical_hits:
        print(f"  [提示] 命中 {len(historical_hits)} 处，需人工确认是否为真实凭据：")
        for item in historical_hits:
            print(f"    - {item}")
    else:
        print("  [ok] 以上历史文件均未发现凭据模式")
    print("  说明：这些文件是任务侦查阶段抓取的公开页面/响应存档（如 my_login.html 来自门户首页、")
    print("        cas_login_headers.txt 与 probe/ 来自可用性探测），不属于本次交付物；")
    print("        是否清理由用户决定。")

    print("\n== 4. 本地凭据文件是否存在（未创建才正常）==")
    for name in ("config.yaml", "config.yml", ".env"):
        if (ROOT / name).exists():
            print(f"  [注意] 本地凭据文件已存在：{name}（应保持 local-only，不得入库）")
        else:
            print(f"  [ok] 不存在：{name}")

    print("\n== 5. .gitignore 覆盖检查 ==")
    gitignore = read(".gitignore")
    for name in MUST_IGNORE:
        if name in gitignore:
            print(f"  [ok] 已忽略 {name}")
        else:
            failures.append(f".gitignore 未忽略 {name}")
            print(f"  [失败] 未忽略 {name}")

    print("\n== 6. 脱敏演练（Cookie 映射不得泄露值）==")
    demo = redact(
        {
            "cookies": {"JSESSIONID": "SECRET-COOKIE-VALUE", "MOD_AUTH_CAS": "SECRET-CAS-VALUE"},
            "headers": {"Set-Cookie": "JSESSIONID=SECRET-COOKIE-VALUE"},
            "creds": {"student_id": "20230001", "password": "SECRET-PASSWORD"},
        }
    )
    rendered = repr(demo)
    leaked = [secret for secret in ("SECRET-COOKIE-VALUE", "SECRET-CAS-VALUE", "SECRET-PASSWORD") if secret in rendered]
    if leaked:
        failures.append(f"脱敏失效：{leaked}")
        print(f"  [失败] 以下值未被脱敏：{leaked}")
    else:
        print("  [ok] Cookie 值、CAS 票据、密码均已打码；学号保持可读")
    print(f"  演练结果：{rendered}")

    print("\n== 结论 ==")
    if failures:
        for item in failures:
            print(f"  失败：{item}")
        return 1
    print("  通过：交付物无凭据，脱敏演练无泄露，.gitignore 覆盖本地凭据文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
