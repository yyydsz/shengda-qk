"""退课工具：按课程号退掉**已选中**的课程。

参数来自页面 ``cancelElectiveCourse()`` 的真实拼串（8 字段，无前缀，见
``KingosoftCourseAdapter._drop_params``）。每门只提交**一次**，中间留间隔；
全程用权威来源（学分统计）复核，避免只看单一接口就下结论。

用法
----
    python tools\\do_drop.py 01261231 01265731   # 退这两门
    python tools\\do_drop.py --all               # 退掉当前所有「选中」的课程

⚠️ 退课会**真实改变**选课记录，请确认后再执行（退补选窗口内通常可再选回来）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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


def credits(adapter, session, ctx) -> dict:
    try:
        r = session.post(
            adapter._url("/jw/common/getSelectLessonScoreKcsInfo.action"),
            data={"xn": str(ctx.get("xn")), "xq_m": str(ctx.get("xqM")),
                  "xh": str(ctx.get("xh"))},
            headers={"Referer": adapter._url("/frame/homes.html")})
        payload = json.loads((r.text or "").strip())
        inner = payload.get("result")
        if isinstance(inner, str):
            inner = json.loads(inner)
        return inner if isinstance(inner, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"  （学分统计读取失败：{type(exc).__name__}: {str(exc)[:70]}）")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="退掉已选中的课程")
    ap.add_argument("codes", nargs="*", metavar="课程号",
                    help="要退的课程号，如 01261231；不给则需 --all")
    ap.add_argument("--all", action="store_true", help="退掉当前所有「选中」的课程")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    if not args.codes and not args.all:
        ap.error("请给出课程号，或用 --all 退掉全部已选课程")

    config = load_config(args.config, environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is None or "CASTGC" not in (cached.cookies or {}):
        print(f"[X] 会话不可用：{reason if cached is None else list(cached.cookies)}")
        return 2
    apply_session(session, cached)

    adapter = KingosoftCourseAdapter(config, session)
    adapter.ensure_session()
    ctx = adapter.context()
    jwgl = config.jwgl
    print(f"轮次 {ctx.get('lcmc')!r} | 窗口 {ctx.get('qssj')} → {ctx.get('jssj')}")
    time.sleep(2)

    before = credits(adapter, session, ctx)
    print(f"退课前（权威）：{before.get('zxf')} 学分 / {before.get('zms')} 门\n")

    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"

    # --all：先查出当前所有「选中」的课程号
    targets = list(args.codes)
    if args.all:
        courses_now = parse_kingosoft_table(session.post(
            adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
            data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
        ).text or "")
        targets = [c.course_code for c in courses_now
                   if str((c.raw or {}).get("select_status", "")) == "选中"]
        print(f"[--all] 当前选中 {len(targets)} 门：{targets}\n")
        time.sleep(2)

    for code in targets:
        courses = parse_kingosoft_table(session.post(
            adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
            data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
        ).text or "")
        target = next((c for c in courses if c.course_code == code), None)
        if target is None:
            print(f"[{code}] 候选列表里找不到，跳过")
            continue
        status = str((target.raw or {}).get("select_status", ""))
        print(f"[{code}] {target.course_name[:32]} | 状态={status!r} | 班号={target.teaching_class_id!r}")
        if status != "选中" or not target.teaching_class_id:
            print(f"    未处于「选中」状态，跳过")
            continue

        plain = adapter._drop_params(target)
        key = session.get(adapter._url(jwgl.temp_key_path)).text.strip()
        stamp = session.get(adapter._url(jwgl.temp_time_path)).text.strip()
        body = split_encoded_body(encode_params(plain, key, stamp))
        print(f"    退课参数（{len(plain.split('&'))} 字段）：{plain}")

        time.sleep(3)
        print(f"    >>> 提交退课（一次）")
        r = session.post(adapter._url("/jw/common/cancelElectiveCourse.action"), data=body,
                         headers={"Referer": adapter._url("/student/wsxk.zx.html"),
                                  "Origin": adapter._base(),
                                  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                  "X-Requested-With": "XMLHttpRequest"})
        text = (r.text or "").strip()
        print(f"    HTTP {r.status} | 正文 {len(text)} 字符")
        print(f"    正文：{text[:300]}")
        time.sleep(5)

    after = credits(adapter, session, ctx)
    print(f"\n退课后（权威）：{after.get('zxf')} 学分 / {after.get('zms')} 门")
    print(f"  变化：{before.get('zxf')}→{after.get('zxf')} 学分，"
          f"{before.get('zms')}→{after.get('zms')} 门")

    courses = parse_kingosoft_table(session.post(
        adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
        data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
    ).text or "")
    sel = [c.course_code for c in courses
           if str((c.raw or {}).get("select_status", "")) == "选中"]
    print(f"  列表仍标记「选中」：{sel}")

    print("\n=== 判读 ===")
    try:
        dropped = (float(after.get("zms") or 0) < float(before.get("zms") or 0))
    except (TypeError, ValueError):
        dropped = False
    if dropped:
        print("  ✓ 权威确认：已选门数减少 —— 退课成功")
    else:
        print("  ✗ 权威来源未显示减少 —— 退课未生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
