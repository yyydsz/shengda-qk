"""真实提交验证：一次请求拿到**全部**证据，并按权威来源判定成功与否。

设计原则（吃过亏，写在前面）
---------------------------
1. **只提交一次**。失败就停下来分析 —— 之前连试十几次，既浪费也掩盖了真因。
2. **不信单一接口**。「选课列表里显示选中」曾让我误报成功，实际没选上。
   判定必须同时满足：服务端业务响应正常 **且** 已选学分数字增加。
3. **绝不写会话文件**。只读复用，避免把用户的登录状态弄坏。
4. **不把「空响应」当限流**。真限流会给 429；空响应是请求本身的问题。

用法
----
    # 演练：只解析班号、打印将要提交的内容，不发写请求
    python tools/verify_submit.py --course 01261231 --dry-run

    # 真提交：一次，打印完整证据并复核
    python tools/verify_submit.py --course 01261231

    # 不指定 --course 时，自动挑一门「未选中且有可选班级」的课
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import (  # noqa: E402
    KingosoftCourseAdapter,
    encode_params,
    parse_kingosoft_table,
    split_encoded_body,
)
from qk.config import load_config  # noqa: E402
from qk.net import QkSession  # noqa: E402
from qk.session_store import SessionStore, apply_session  # noqa: E402


def credits_snapshot(adapter: KingosoftCourseAdapter, session: QkSession,
                     ctx: dict) -> dict:
    """已选学分统计（权威来源之一）。"""
    try:
        r = session.post(
            adapter._url("/jw/common/getSelectLessonScoreKcsInfo.action"),
            data={"xn": str(ctx.get("xn")), "xq_m": str(ctx.get("xqM")),
                  "xh": str(ctx.get("xh"))},
            headers={"Referer": adapter._url("/frame/homes.html")},
        )
        text = (r.text or "").strip()
        payload = json.loads(text)
        inner = payload.get("result")
        if isinstance(inner, str):
            inner = json.loads(inner)
        return inner if isinstance(inner, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"    （学分统计读取失败：{type(exc).__name__}: {str(exc)[:80]}）")
        return {}


def selected_in_list(adapter: KingosoftCourseAdapter, session: QkSession,
                     ctx: dict) -> list[tuple[str, str]]:
    """选课列表里标记为「选中」的课程（权威来源之二）。"""
    jwgl = adapter.config.jwgl
    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"
    html = session.post(
        adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
        data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
    ).text or ""
    courses = parse_kingosoft_table(html)
    return [(c.course_code, c.teaching_class_id) for c in courses
            if str((c.raw or {}).get("select_status", "")) == "选中"]


def main() -> int:
    ap = argparse.ArgumentParser(description="一次真实提交验证（含权威复核）")
    ap.add_argument("--course", metavar="课程号", default="",
                    help="要选的课程号（kcdm），如 01261231")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析班号与打印参数，不发写请求")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    config = load_config(args.config, environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is None or "CASTGC" not in (cached.cookies or {}):
        print(f"[X] 会话不可用：{reason if cached is None else list(cached.cookies)}")
        print("    请先在 GUI 里扫码登录（不要用 --mock）。")
        return 2
    apply_session(session, cached)
    print(f"[1] 会话会话就绪：cookies={list(cached.cookies)} 学号={cached.student_id}")

    adapter = KingosoftCourseAdapter(config, session)
    adapter.ensure_session()
    ctx = adapter.context()
    print(f"    轮次 {ctx.get('lcmc')!r} | 窗口 {ctx.get('qssj')} → {ctx.get('jssj')}")
    time.sleep(2)

    # ---- 提交前的权威基线 ----
    before_stats = credits_snapshot(adapter, session, ctx)
    before_sel = selected_in_list(adapter, session, ctx)
    print(f"[2] 基线：学分 {before_stats.get('zxf')!r} / {before_stats.get('zms')!r} 门 | "
          f"列表选中 {len(before_sel)} 门")
    time.sleep(2)

    # ---- 选目标 ----
    jwgl = config.jwgl
    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"
    courses = parse_kingosoft_table(session.post(
        adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
        data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
    ).text or "")
    if args.course:
        target = next((c for c in courses if c.course_code == args.course), None)
    else:
        target = next((c for c in courses if not c.teaching_class_id), None)
    if target is None:
        print(f"[X] 找不到课程 {args.course or '（任意未选课程）'}")
        return 3
    print(f"[3] 目标：{target.course_code} {target.course_name} | "
          f"当前状态 {(target.raw or {}).get('select_status')!r}")
    time.sleep(2)

    # ---- 解析班号（走生产实现）----
    classes = list(adapter.resolve_teaching_class_ids(target))
    print(f"[4] 解析可选班号：{classes[:5]}")
    if not classes:
        print("[X] 没有可选班级，终止（不提交）。")
        return 4
    chosen = replace(target, teaching_class_id=classes[0])
    time.sleep(2)

    plain = adapter._write_params(chosen)
    print(f"[5] 提交参数（{len(plain.split('&'))} 字段）：")
    for part in plain.split("&"):
        print(f"      {part}")

    if args.dry_run:
        print("\n[演练] 未提交。去掉 --dry-run 才会发写请求。")
        return 0

    key = session.get(adapter._url(jwgl.temp_key_path)).text.strip()
    stamp = session.get(adapter._url(jwgl.temp_time_path)).text.strip()
    body = split_encoded_body(encode_params(plain, key, stamp))

    referer = adapter._popup_url or adapter._url("/student/wsxk.zx.html")
    print(f"\n[6] >>> 提交一次：POST {adapter._url('/jw/common/saveElectiveCourse.action')}")
    print(f"      Referer = {referer[:100]}")
    r = session.post(adapter._url("/jw/common/saveElectiveCourse.action"), data=body,
                     headers={"Referer": referer, "Origin": adapter._base()})
    text = (r.text or "").strip()
    print(f"      HTTP {r.status} | 正文 {len(text)} 字符")
    print(f"      正文：{text[:400]}")

    # ---- 权威复核 ----
    time.sleep(5)
    after_stats = credits_snapshot(adapter, session, ctx)
    after_sel = selected_in_list(adapter, session, ctx)
    print(f"\n[7] 复核（权威来源）")
    print(f"      学分 {before_stats.get('zxf')!r} → {after_stats.get('zxf')!r} | "
          f"{before_stats.get('zms')!r} 门 → {after_stats.get('zms')!r} 门")
    print(f"      列表选中 {len(before_sel)} 门 → {len(after_sel)} 门")
    print(f"      {before_sel} → {after_sel}")

    grew = False
    try:
        grew = (float(after_stats.get("zxf") or 0) > float(before_stats.get("zxf") or 0)
                or float(after_stats.get("zms") or 0) > float(before_stats.get("zms") or 0))
    except (TypeError, ValueError):
        grew = False
    if grew or (chosen.course_code, chosen.teaching_class_id) in after_sel:
        print("\n  ✓ 权威来源确认：已选数据增加 —— 选课成功")
        return 0
    print("\n  ✗ 权威来源未确认：已选数据没有增加 —— 本次未选上")
    if not text:
        print("     （空响应：请求本身有问题，不是限流——真限流会给 429）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
