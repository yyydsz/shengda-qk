"""临时诊断：把当前可选的全量课程导出成一份分类清单（只读）。

用户要线上课但本轮不存在；改为提供完整可选项清单，便于挑选面授课。
只发**读**请求。用完即删。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.adapters.jwgl import KingosoftCourseAdapter, parse_kingosoft_table  # noqa: E402
from qk.config import load_config  # noqa: E402
from qk.net import QkSession  # noqa: E402
from qk.session_store import SessionStore, apply_session  # noqa: E402

RANGES = ("zxbnj", "zxfjjx", "zxggrx", "fx", "zxknj", "tspyxszy", "tspyggrx")


def main() -> int:
    config = load_config(ROOT / "config.example.yaml", environ={}, workspace=ROOT)
    session = QkSession.from_config(config)
    store = SessionStore(config.session.path, config.session.max_age_hours)
    cached, reason = store.load_usable()
    if cached is None:
        print(f"会话不可用：{reason}")
        return 2
    apply_session(session, cached)

    adapter = KingosoftCourseAdapter(config, session)
    adapter.ensure_session()
    base = adapter._base()
    referer = {"Referer": f"{base}/student/wsxk.zx.html"}
    url = f"{base}{config.jwgl.data_table_path}?tableId={config.jwgl.course_list_table_id}&fre=1"

    courses = []
    for kcfw in RANGES:
        p = adapter._form_params("")
        p["kcfw"] = kcfw
        p["_kcfw"] = kcfw
        courses.extend(parse_kingosoft_table(session.post(url, data=p, headers=referer).text or ""))

    # 去掉课程名里的 [课程号] 前缀，便于阅读
    def clean_name(name: str) -> str:
        if name.startswith("[") and "]" in name:
            return name.split("]", 1)[1].strip()
        return name

    grouped: dict[str, list] = defaultdict(list)
    for c in courses:
        attr = str((c.raw or {}).get("course_attr", "") or "(未标注)")
        grouped[attr].append(c)

    print(f"当前可选课程共 {len(courses)} 门（轮次：通识选修课正选）\n")
    print("=" * 92)
    for attr in sorted(grouped, key=lambda k: -len(grouped[k])):
        items = grouped[attr]
        print(f"\n【{attr}】{len(items)} 门")
        print("-" * 92)
        for c in sorted(items, key=lambda x: x.course_code):
            print(f"  {c.course_code:12} {clean_name(c.course_name)}")

    print("\n" + "=" * 92)
    print(f"合计 {len(courses)} 门 | 类别数 {len(grouped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
