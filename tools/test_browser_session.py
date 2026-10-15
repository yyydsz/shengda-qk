"""决定性实验：用**浏览器真实会话**的 JSESSIONID 测试本工具的提交参数。

为什么要这样测
--------------
1. 浏览器的教务会话身份是确定的（它刚刚成功选上了课）。
2. 本工具的会话是自己换票建的，可能缺少身份绑定 —— 这会造成「不是本人」。
3. 把浏览器的 JSESSIONID 直接交给工具，就能把「会话问题」与「参数问题」**彻底分离**：
   - 若提交成功/回业务消息（非「不是本人」）→ 参数正确，问题在会话
   - 若仍回「不是本人」             → 参数之外还有未知因素

同时对比浏览器请求头，补上可能缺失的 `X-Requested-With`。

**不写用户的 qk_state.json**：使用独立的临时会话文件与临时配置。

用法
----
会话 Cookie **不要写进代码**（会随仓库泄漏）。从环境变量或命令行传入：

    # Windows PowerShell
    $env:QK_TEST_JSESSIONID = "<从浏览器抓到的 JSESSIONID>"
    python tools\\test_browser_session.py

    # 或
    python tools\\test_browser_session.py --jsessionid <值> --student-id <学号>

获取方式：浏览器 F12 → Network → 任一 `jw.shengda.edu.cn` 请求 → Request Headers →
`Cookie:` 里的 `JSESSIONID=...`（该 Cookie 是 HttpOnly，`document.cookie` 读不到）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_SESSION = ROOT / "tmp_browser_session.json"
TMP_CONFIG = ROOT / "tmp_browser_config.yaml"


def build_config() -> Path:
    """复制一份 config.yaml，把会话文件指到临时路径。"""
    import yaml

    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    data.setdefault("session", {})
    data["session"]["path"] = str(TMP_SESSION)
    TMP_CONFIG.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                       default_flow_style=False), encoding="utf-8")
    return TMP_CONFIG


def write_session(jsessionid: str, student_id: str) -> None:
    """只写 JSESSIONID（不带 CASTGC）—— 这样 ensure_session 不会去换票。"""
    TMP_SESSION.write_text(json.dumps({
        "cookies": {"JSESSIONID": jsessionid},
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "student_id": student_id,
        "service_url": "https://jw.shengda.edu.cn:8090/caslogin",
        "source": "browser-jsessionid",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="用浏览器真实会话验证提交参数")
    ap.add_argument("--jsessionid", default=os.environ.get("QK_TEST_JSESSIONID", ""),
                    help="浏览器里 jw.shengda.edu.cn 的 JSESSIONID（默认取环境变量 QK_TEST_JSESSIONID）")
    ap.add_argument("--student-id", default=os.environ.get("QK_TEST_STUDENT_ID", ""),
                    help="学号（默认取环境变量 QK_TEST_STUDENT_ID）")
    args = ap.parse_args()

    jsessionid = args.jsessionid.strip()
    if not jsessionid:
        print("[X] 未提供 JSESSIONID。请用 --jsessionid 或设置环境变量 QK_TEST_JSESSIONID。")
        print("    获取：F12 → Network → 任一 jw.shengda.edu.cn 请求 → Request Headers → Cookie")
        return 2
    student_id = args.student_id.strip()

    from qk.adapters.jwgl import (  # noqa: PLC0415
        KingosoftCourseAdapter,
        encode_params,
        parse_kingosoft_table,
        split_encoded_body,
    )
    from qk.config import load_config  # noqa: PLC0415
    from qk.net import QkSession  # noqa: PLC0415
    from qk.session_store import SessionStore, apply_session  # noqa: PLC0415

    write_session(jsessionid, student_id)
    cfg_path = build_config()
    config = load_config(cfg_path, environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is not None:
        apply_session(session, cached)
    print(f"注入浏览器会话：JSESSIONID={jsessionid[:8]}…  (load_usable={cached is not None})")

    adapter = KingosoftCourseAdapter(config, session)
    try:
        adapter.ensure_session()
        ctx = adapter.context()
    except Exception as exc:  # noqa: BLE001
        print(f"[X] 会话不可用：{type(exc).__name__}: {str(exc)[:150]}")
        return 2
    print(f"[ok] 会话可用 | 轮次 {ctx.get('lcmc')!r} | xh={ctx.get('xh')!r}")
    time.sleep(2)

    jwgl = config.jwgl
    params = adapter._form_params("")
    params["kcfw"] = params["_kcfw"] = ctx.get("kcfw") or "zxggrx"
    courses = parse_kingosoft_table(session.post(
        adapter._url(f"/taglib/DataTable.jsp?tableId={jwgl.course_list_table_id}&fre=1"),
        data=params, headers={"Referer": adapter._url("/student/wsxk.zx.html")},
    ).text or "")
    sel = [c for c in courses if str((c.raw or {}).get("select_status", "")) == "选中"]
    print(f"课程 {len(courses)} 门 | 标记选中 {len(sel)} 门：{[c.course_code for c in sel]}")
    time.sleep(2)

    # 目标：已选课程（零副作用）；服务端应回「已选过」类消息
    target = next((c for c in sel if c.teaching_class_id), None)
    if target is None:
        print("[X] 没有已选课程可做零副作用测试")
        return 3
    chosen = replace(target, teaching_class_id=target.teaching_class_id)
    plain = adapter._write_params(chosen)
    key = session.get(adapter._url(jwgl.temp_key_path)).text.strip()
    stamp = session.get(adapter._url(jwgl.temp_time_path)).text.strip()
    body = split_encoded_body(encode_params(plain, key, stamp))
    print(f"\n目标（已选，零副作用）：{chosen.course_code} skbjdm={chosen.teaching_class_id}")

    # 关键对照：带 / 不带 X-Requested-With
    base_headers = {
        "Referer": adapter._popup_url or adapter._url("/student/wsxk.zx.html"),
        "Origin": adapter._base(),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    for label, extra in (
        ("不带 X-Requested-With", {}),
        ("带 X-Requested-With: XMLHttpRequest", {"X-Requested-With": "XMLHttpRequest"}),
    ):
        time.sleep(3)
        r = session.post(adapter._url("/jw/common/saveElectiveCourse.action"), data=body,
                         headers={**base_headers, **extra})
        text = (r.text or "").strip()
        print(f"\n  [{label}] HTTP {r.status} | {len(text)} 字符")
        print(f"     {text[:260]}")
        if text and "不是选课学生本人" not in text:
            print("     ↑ **非「不是本人」** —— 这就是突破点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
