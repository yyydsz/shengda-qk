"""核查：从教务系统**权威来源**读取已选课程，确认 01261231 是否真的选上。

不要再依赖「选课(按开课计划)」列表的 select_status —— 用户看不到该课，
必须查「选课结果」页（菜单里的独立功能）与已选学分统计。

只发**读**请求。用完即删。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import (  # noqa: E402
    KingosoftCourseAdapter,
    parse_kingosoft_table,
)
from qk.config import load_config  # noqa: E402
from qk.net import QkSession  # noqa: E402
from qk.session_store import SessionStore, apply_session  # noqa: E402

_TAGS = re.compile(r"<[^>]+>")


def text_of(html: str, limit: int = 700) -> str:
    return " ".join(_TAGS.sub(" ", html or "").split())[:limit]


def main() -> int:
    config = load_config(ROOT / "config.yaml", environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is None or "CASTGC" not in (cached.cookies or {}):
        print(f"[X] 会话不可用：{reason if cached is None else list(cached.cookies)}")
        print("    需要重新扫码登录。")
        return 2
    apply_session(session, cached)

    adapter = KingosoftCourseAdapter(config, session)
    adapter.ensure_session()
    ctx = adapter.context()
    ref = {"Referer": adapter._url("/frame/homes.html")}
    print(f"[ok] 会话就绪 | 轮次 {ctx.get('lcmc')!r}")
    print(f"     xn={ctx.get('xn')} xqM={ctx.get('xqM')} xh={ctx.get('xh')}\n")
    time.sleep(2)

    # ---- 1) 已选学分统计（权威）----
    print("=" * 74)
    print("1) 已选学分统计 getSelectLessonScoreKcsInfo.action")
    print("=" * 74)
    try:
        r = session.post(
            adapter._url("/jw/common/getSelectLessonScoreKcsInfo.action"),
            data={"xn": str(ctx.get("xn")), "xq_m": str(ctx.get("xqM")),
                  "xh": str(ctx.get("xh"))},
            headers=ref,
        )
        print(f"    HTTP {r.status} | {len(r.text or '')} 字节")
        print(f"    {text_of(r.text, 600)}")
    except Exception as exc:  # noqa: BLE001
        print(f"    异常：{type(exc).__name__}: {exc}")
    time.sleep(2)

    # ---- 2) 选课结果页（菜单「选课结果」）----
    print("\n" + "=" * 74)
    print("2) 选课结果页（逐个候选路径探测）")
    print("=" * 74)
    for path in (
        "/student/report/wsxk.zx_promt_yxkctj.jsp",
        "/student/wsxk.zx_yxkc.html",
        "/student/report/wsxk.zx_yxkc.jsp",
        "/student/wsxk.zx_promt_yxkctj.jsp",
    ):
        try:
            u = adapter._url(f"{path}?xn={ctx.get('xn')}&xq_m={ctx.get('xqM')}&xh={ctx.get('xh')}")
            r = session.get(u, headers={"Referer": adapter._url("/student/wsxk.zx.html")})
            body = r.text or ""
            hit = "01261231" in body
            print(f"\n    {path}")
            print(f"       HTTP {r.status} | {len(body)} 字节 | 含 01261231: {hit}")
            print(f"       {text_of(body, 300)}")
            if hit:
                for m in re.finditer(r".{0,90}01261231.{0,90}", body):
                    print(f"       >>> {' '.join(m.group(0).split())}")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"\n    {path} 异常：{type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(1.5)

    # ---- 3) 课程列表：全量找 01261231 并打印其状态 ----
    print("\n" + "=" * 74)
    print("3) 课程列表里 01261231 的当前状态")
    print("=" * 74)
    url = adapter._url(f"/taglib/DataTable.jsp?tableId={config.jwgl.course_list_table_id}&fre=1")
    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"
    courses = parse_kingosoft_table(session.post(url, data=params, headers={
        "Referer": adapter._url("/student/wsxk.zx.html")}).text or "")
    sel = [c for c in courses if str((c.raw or {}).get("select_status", "")) == "选中"]
    print(f"    共 {len(courses)} 门 | 标记「选中」{len(sel)} 门")
    for c in sel:
        print(f"      {c.course_code:12} skbjdm={c.teaching_class_id!r:20} {c.course_name[:30]}")
    mine = next((c for c in courses if c.course_code == "01261231"), None)
    if mine is not None:
        print(f"\n    01261231 状态 = {(mine.raw or {}).get('select_status')!r} | "
              f"班号 = {mine.teaching_class_id!r}")
    else:
        print("\n    01261231 不在当前课程列表里（可能已满员被移除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
