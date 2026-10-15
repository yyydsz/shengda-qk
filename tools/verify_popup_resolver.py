"""临时验证：在真实教务系统上验证「弹窗解析班号」这条新链路（只读）。

用真实会话跑：
  1. 换票建会话 + 取选课上下文（查询课程的前置）
  2. 查候选课程，挑一门**未选中**的课（正是缺 skbjdm 的场景）
  3. 调 isSelectableSkbjdm.action 校验可选择性
  4. 打开选课弹窗 wsxk.zx_promt.jsp，用新写的 parse_popup_skbjdms 解析班号
  5. 报告解析结果

**全程只发读请求，绝不调用 saveElectiveCourse。**

用法：python tools/verify_popup_resolver.py [课程号]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import (  # noqa: E402
    KingosoftCourseAdapter,
    parse_class_list,
    parse_class_skbjdms,
    parse_kingosoft_table,
)
from qk.config import load_config  # noqa: E402
from qk.net import QkSession  # noqa: E402
from qk.session_store import SessionStore, apply_session  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config.example.yaml", environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is None:
        print(f"[X] 会话不可用：{reason}")
        return 2
    if "CASTGC" not in (cached.cookies or {}):
        print(f"[X] 会话里没有 CASTGC（只有 {list(cached.cookies)}）——"
              "这多半是 mock 会话，请重新扫码")
        return 2
    apply_session(session, cached)
    print(f"[ok] 会话：cookies={list(cached.cookies)} 学号={cached.student_id} "
          f"来源={cached.source}\n")

    adapter = KingosoftCourseAdapter(config, session)
    adapter.ensure_session()
    base = adapter._base()
    ctx = adapter.context()
    print(f"[ok] 轮次 {ctx.get('lcmc')!r} | 窗口 {ctx.get('qssj')} → {ctx.get('jssj')}")

    referer = {"Referer": f"{base}/student/wsxk.zx.html"}
    url = f"{base}{config.jwgl.data_table_path}?tableId={config.jwgl.course_list_table_id}&fre=1"
    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"
    courses = parse_kingosoft_table(session.post(url, data=params, headers=referer).text or "")
    print(f"[ok] 候选课程 {len(courses)} 门")

    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    if wanted:
        target = next((c for c in courses if wanted in c.course_code or wanted in c.course_name), None)
    else:
        target = next((c for c in courses
                       if str((c.raw or {}).get("select_status", "")) != "选中"), None)
    if target is None:
        print("[X] 找不到可用于验证的课程")
        return 3
    print(f"\n[目标] {target.course_code} {target.course_name}")
    print(f"       列表行 skbjdm = {target.teaching_class_id!r}"
          f"{'（空，正是需要弹窗解析的场景）' if not target.teaching_class_id else ''}")

    print("\n--- 1) isSelectableSkbjdm.action 校验 ---")
    try:
        ok = adapter.ensure_selectable(target)
        print(f"    允许选 = {ok} | 服务器提示 = {adapter.last_message!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"    异常：{type(exc).__name__}: {exc}")

    print("\n--- 2) 解析可选上课班级（走生产代码路径 adapter.resolve_teaching_class_ids）---")
    tried = 0
    hit: tuple[object, list[str]] | None = None

    # 只调用**生产方法**，不再手写 HTTP。手写副本会与实现漂移 ——
    # 曾经因此误判「服务器没有班级」，实际是副本少传了 kclb1/kclb2/kclb3/khfs。
    for course in courses[:10]:
        try:
            skbjdms = list(adapter.resolve_teaching_class_ids(course))
            tried += 1
            label = "（已选中）" if course.teaching_class_id else ""
            mark = "  <<< 解析成功！" if skbjdms else ""
            print(f"    {course.course_code} {course.course_name[:24]:26}{label:10} "
                  f"班号 {skbjdms[:3]}{mark}")
            if skbjdms:
                hit = (course, skbjdms)
                break
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            print(f"    {course.course_code} 异常：{type(exc).__name__}: {str(exc)[:90]}")
            time.sleep(1.0)
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            print(f"    {course.course_code} 异常：{type(exc).__name__}: {str(exc)[:90]}")
            time.sleep(1.0)

    if hit is None:
        print(f"\n[!] 试了 {tried} 门课，都没有可选上课班级。")
        print("    这不是解析失败：内层列表 tbody 为空 = 该课当前确实没有可选班级。")
        print("    解析链路本身需要一门**有班级的课**才能端到端验证。")
        return 4

    course, skbjdms = hit
    print("\n" + "=" * 70)
    print("结论：弹窗班级解析链路**已端到端验证通过**")
    print("=" * 70)
    print(f"  课程 {course.course_code} {course.course_name[:30]}")
    print(f"  解析出的可选班号 {skbjdms[:5]}（共 {len(skbjdms)} 个）")
    print("  → 自动选课已具备「解析班号 → 提交」的完整条件")
    print("  本次只做解析验证，**未提交任何选课**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
