"""差量对比：浏览器真实提交 vs 本工具的提交（阶段 1 的诊断工具）。

用途
----
补选窗口开放后，先在浏览器里**手动成功选一门课**，把那次提交的请求体抓下来，
再用本脚本与工具生成的请求体做逐字段对比，定位「当前选课操作的用户不是选课学生本人」
这类业务校验失败的真因。

浏览器抓取方式（jsreverse）：
    list_network_requests(methods=["POST"], urlFilter="saveElectiveCourse")
    → list_network_requests(reqid=<id>, outputPart="requestBody", outputFile="browser_submit.txt")
    → 同一条再导出 outputPart="all" 拿到 Referer 等请求头

用法
----
    # 1) 只看工具侧的请求体（不联网）
    python tools/capture_submit.py --show-mine

    # 2) 与浏览器抓到的请求体对比
    python tools/capture_submit.py --browser-body browser_submit.txt

    # 3) 同时对比请求头
    python tools/capture_submit.py --browser-body browser_submit.txt --browser-req browser_req.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import KingosoftCourseAdapter  # noqa: E402
from qk.config import load_config  # noqa: E402
from qk.models import CandidateCourse  # noqa: E402

#: 字段名前缀（真实页面提交时带；mock 解析时会剥掉）
PREFIX = "electiveCourseForm."


def parse_body(text: str) -> dict[str, str]:
    """把表单体解析成字典（保留原始键名，做 URL 解码便于比较）。"""
    fields: dict[str, str] = {}
    for part in (text or "").strip().split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key:
            fields[unquote_plus(key)] = unquote_plus(value)
    return fields


def norm(key: str) -> str:
    """归一化键名（去掉表单前缀），用于跨写法比对。"""
    return key.replace(PREFIX, "")


def build_mine() -> tuple[dict[str, str], dict[str, str]]:
    """用生产代码生成工具侧的参数体（不联网）。"""
    config = load_config(ROOT / "config.yaml", environ={}, workspace=ROOT)
    adapter = KingosoftCourseAdapter.__new__(KingosoftCourseAdapter)
    adapter.config = config
    # 绕过 __init__ 构造，需手工补上它设置的实例属性
    adapter._ready = True
    adapter.last_message = ""
    adapter._popup_url = ""
    adapter._context = {
        "xn": "2026", "xqM": "0", "xh": "20230001",
        "lcid": "35732179068578980275991", "kcfw": "zxggrx",
        "nj": "2024", "zydm": "0501",
    }
    adapter.context = lambda: adapter._context  # type: ignore[method-assign]
    course = CandidateCourse(
        teaching_class_id="00000000-000", course_code="00000000",
        course_name="（样例课程）", credits=1.0,
        raw={"kclb1": "03", "kclb2": "01", "kclb3": "13", "khfs": "02"},
    )
    plain = adapter._write_params(course)
    fields = parse_body(plain)
    # 同时给出提交时会带的请求头
    headers = {
        "Referer": adapter._popup_url or adapter._url("/student/wsxk.zx.html"),
        "Origin": adapter._base(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    return fields, headers


def diff(mine: dict[str, str], theirs: dict[str, str]) -> None:
    mine_n = {norm(k): v for k, v in mine.items()}
    theirs_n = {norm(k): v for k, v in theirs.items()}

    print("\n" + "=" * 74)
    print(f"字段数：工具 {len(mine)}  |  浏览器 {len(theirs)}")
    print("=" * 74)

    only_theirs = [k for k in theirs_n if k not in mine_n]
    only_mine = [k for k in mine_n if k not in theirs_n]
    differing = [k for k in mine_n if k in theirs_n and mine_n[k] != theirs_n[k]]

    print(f"\n[A] 浏览器有、工具没有（{len(only_theirs)} 个）—— 最可能是根因")
    for k in only_theirs:
        print(f"    {k:32} = {theirs_n[k]!r}")

    print(f"\n[B] 工具有、浏览器没有（{len(only_mine)} 个）")
    for k in only_mine:
        print(f"    {k:32} = {mine_n[k]!r}")

    print(f"\n[C] 同名取值不同（{len(differing)} 个）")
    for k in differing:
        print(f"    {k:32} 工具={mine_n[k]!r}  浏览器={theirs_n[k]!r}")

    print("\n[D] 前缀写法")
    print(f"    工具  : {'带前缀' if any(k.startswith(PREFIX) for k in mine) else '不带前缀'}")
    print(f"    浏览器: {'带前缀' if any(k.startswith(PREFIX) for k in theirs) else '不带前缀'}")

    print("\n" + "=" * 74)
    if not only_theirs and not differing:
        print("结论：字段集合与取值一致 —— 差异在别处（请求头 / 会话态 / 流程步骤）")
    else:
        print("结论：先把上面 [A] 缺的字段与 [C] 取值差异补齐，再重试提交")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description="对比浏览器真实提交与本工具的提交")
    ap.add_argument("--show-mine", action="store_true", help="只打印工具侧请求体与请求头")
    ap.add_argument("--browser-body", metavar="路径",
                    help="浏览器提交的请求体文本（list_network_requests outputPart=requestBody）")
    ap.add_argument("--browser-req", metavar="路径",
                    help="浏览器提交的完整请求快照 JSON（outputPart=all），用于比对请求头")
    args = ap.parse_args()

    mine, headers = build_mine()

    print("=" * 74)
    print(f"工具侧请求体（{len(mine)} 字段）")
    print("=" * 74)
    for k, v in mine.items():
        print(f"    {k:36} = {v!r}")
    print("\n工具侧请求头：")
    for k, v in headers.items():
        print(f"    {k:12} = {str(v)[:90]!r}")

    if args.browser_req:
        p = Path(args.browser_req)
        if p.is_file():
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except ValueError as exc:
                print(f"\n[!] 无法解析 {p.name}：{exc}")
                snap = {}
            if snap:
                print("\n" + "=" * 74)
                print("浏览器请求头（仅列关键项）")
                print("=" * 74)
                rh = snap.get("requestHeaders") or {}
                for k in ("referer", "origin", "content-type", "cookie", "user-agent"):
                    if k in rh:
                        print(f"    {k:12} = {str(rh[k])[:90]!r}")
                print(f"    URL          = {str(snap.get('url', ''))[:120]}")
                print(f"    method       = {snap.get('method')!r} | resourceType={snap.get('resourceType')!r}")
        else:
            print(f"\n[!] 找不到 {p}")

    if args.browser_body:
        p = Path(args.browser_body)
        if not p.is_file():
            print(f"\n[!] 找不到 {p}")
            return 2
        raw = p.read_text(encoding="utf-8", errors="replace")
        # 导出文件可能是 JSON 包装（{"text": "..."}）或纯文本
        body = raw
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                body = obj["text"]
        except ValueError:
            pass
        theirs = parse_body(body)
        print(f"\n浏览器请求体已读取：{len(theirs)} 字段（来自 {p.name}）")
        diff(mine, theirs)
    elif not args.show_mine:
        print("\n提示：加 --browser-body 路径 才能做对比；当前只展示了工具侧。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
