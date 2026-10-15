"""临时：解密浏览器抓到的提交请求体，与我们的参数做逐字段对比。

用法：python tools\\decrypt_browser_body.py <body文件> <密钥>
密钥从浏览器取：fetch('/frame/homepage?method=getTempDeskey')
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import decode_kingo_params  # noqa: E402


def parse(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split("&"):
        k, _, v = part.partition("=")
        if k:
            out[unquote_plus(k)] = unquote_plus(v)
    return out


def main() -> int:
    body_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "browser_submit_body.txt"
    key = sys.argv[2] if len(sys.argv) > 2 else ""
    raw = body_file.read_text(encoding="utf-8", errors="replace").strip()
    fields = parse(raw)
    print(f"表单字段：{sorted(fields)}")
    encrypted = fields.get("params", "")
    stamp = fields.get("timestamp", "")
    print(f"params 长度 {len(encrypted)} | timestamp={stamp!r}")

    if not key:
        print("\n未提供密钥，无法解密。")
        return 2
    print(f"使用密钥：{key!r}\n")
    try:
        plain = decode_kingo_params(f"params={encrypted}&timestamp={stamp}", key)
    except Exception as exc:  # noqa: BLE001
        print(f"解密失败：{type(exc).__name__}: {exc}")
        return 3
    if not plain:
        print("解密结果为空（密钥很可能已轮换 —— 密钥含时间戳，会变化）")
        return 4
    print("=== 解密后的明文参数 ===")
    for part in plain.split("&"):
        print(f"  {part}")
    print(f"\n共 {len(plain.split('&'))} 个字段")

    # 与我们生成的对比
    from qk.adapters.jwgl import KingosoftCourseAdapter
    from qk.config import load_config
    from qk.models import CandidateCourse

    config = load_config(ROOT / "config.yaml", environ={}, workspace=ROOT)
    adapter = KingosoftCourseAdapter.__new__(KingosoftCourseAdapter)
    adapter.config = config
    adapter._ready = True
    adapter.last_message = ""
    adapter._popup_url = ""
    adapter._context = {"xn": "2026", "xqM": "0", "xh": "20230001",
                        "lcid": "35732179068578980275991", "kcfw": "zxggrx",
                        "nj": "2024", "zydm": "0501"}
    adapter.context = lambda: adapter._context  # type: ignore[method-assign]
    theirs = parse(plain)
    # 注意：浏览器提交的字段名**不带** electiveCourseForm. 前缀
    course = CandidateCourse(
        teaching_class_id=theirs.get("skbjdm", "01265731-002"),
        course_code=theirs.get("kcdm", "01265731"),
        course_name="", credits=1.0,
        raw={"kclb1": theirs.get("kclb1", ""),
             "kclb2": theirs.get("kclb2", ""),
             "kclb3": theirs.get("kclb3", ""),
             "khfs": theirs.get("khfs", "")},
    )
    mine = parse(adapter._write_params(course))

    print("\n" + "=" * 74)
    print(f"字段数：浏览器 {len(theirs)} | 本工具 {len(mine)}")
    print("=" * 74)
    only_theirs = [k for k in theirs if k not in mine]
    only_mine = [k for k in mine if k not in theirs]
    differing = [k for k in mine if k in theirs and mine[k] != theirs[k]]
    print(f"\n[A] 浏览器有、本工具没有（{len(only_theirs)}）")
    for k in only_theirs:
        print(f"    {k:38} = {theirs[k]!r}")
    print(f"\n[B] 本工具有、浏览器没有（{len(only_mine)}）")
    for k in only_mine:
        print(f"    {k:38} = {mine[k]!r}")
    print(f"\n[C] 同名取值不同（{len(differing)}）")
    for k in differing:
        print(f"    {k:38} 本工具={mine[k]!r}  浏览器={theirs[k]!r}")
    if not only_theirs and not differing:
        print("\n结论：字段集合与取值一致 —— 差异不在参数，而在请求头或会话流程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
