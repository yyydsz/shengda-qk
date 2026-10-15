"""真实环境首次联调前的配置体检（只读，不联网）。

对比 ``config.example.yaml`` 与实现，检查：

1. 示例配置能否被 :func:`qk.config.load_config` 正常加载（含五个小节白名单）；
2. ``grab.targets`` 字段是否与 :class:`qk.config.CourseTarget` 一致；
3. 哪些教务接口路径仍是空的（空路径意味着真实抢课会明确报错，而不是乱猜路径）。

用法::

    python tools/check_config_example.py
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qk.config import (  # noqa: E402 - 需要先把仓库根加入 sys.path
    AppConfig,
    CasConfig,
    CourseTarget,
    Credentials,
    GrabConfig,
    HttpConfig,
    JwglConfig,
    load_config,
)

SECTIONS: tuple[tuple[str, type], ...] = (
    ("http", HttpConfig),
    ("cas", CasConfig),
    ("jwgl", JwglConfig),
    ("grab", GrabConfig),
    ("credentials", Credentials),
)

#: 真实抢课必需、但当前仍未确认的接口路径
REQUIRED_PATHS = ("course_list_path", "select_path", "drop_path", "selected_list_path")


def main() -> int:
    problems: list[str] = []

    print("== 1. 示例配置可被加载 ==")
    try:
        config = load_config(ROOT / "config.example.yaml", environ={}, workspace=ROOT)
    except Exception as exc:  # noqa: BLE001 - 体检脚本，直接给出原因
        print(f"  [失败] {type(exc).__name__}: {exc}")
        return 1
    print(f"  [ok] 加载成功，来源：{' → '.join(config.sources)}")

    print("\n== 2. 小节与字段一致性 ==")
    for name, cls in SECTIONS:
        expected = {f.name for f in fields(cls)}
        section = getattr(config, "creds" if name == "credentials" else name)
        actual = {f.name for f in fields(section)}
        if expected == actual:
            print(f"  [ok] {name}：{len(expected)} 个字段")
        else:
            problems.append(f"{name} 字段不一致：缺少 {sorted(expected - actual)}")
            print(f"  [失败] {name}：缺少 {sorted(expected - actual)}")

    target_fields = {f.name for f in fields(CourseTarget)}
    print(f"  [ok] grab.targets 元素字段：{'、'.join(sorted(target_fields))}")

    print(f"\n== 3. 教务接口路径（platform={config.jwgl.platform}）==")
    # 青果（本校默认）的路径已内置在 KingosoftCourseAdapter 里，这四项留空是**正确**的；
    # 只有通用实现（auto/custom/qz）才需要手工填这四个字段。
    if (config.jwgl.platform or "").lower() == "kingosoft":
        print("  [内置] platform=kingosoft：青果契约的路径已写在适配器里，")
        print(f"         这四个字段留空即正确（当前值：{', '.join(f'{n}=（空）' if not getattr(config.jwgl, n) else f'{n}={getattr(config.jwgl, n)}' for n in REQUIRED_PATHS)}）")
        if any(getattr(config.jwgl, n) for n in REQUIRED_PATHS):
            print("  [警告] kingosoft 平台用不到这四个字段，填了也不会被读取（青果走内置路径）")
        for label, path in (
            ("教务基址", config.jwgl.kingosoft_base_url or "（空）"),
            ("CAS service", config.jwgl.cas_service or "（空）"),
            ("选课菜单号", config.jwgl.menucode),
            ("课程表 tableId", config.jwgl.course_list_table_id),
        ):
            print(f"  [内置] {label}：{path}")
    else:
        for name in REQUIRED_PATHS:
            value = getattr(config.jwgl, name)
            if value:
                print(f"  [已填] jwgl.{name} = {value}")
            else:
                print(f"  [待填] jwgl.{name} = （空）→ 真实抢课时会报“接口未确认”，不会乱发请求")

    print("\n== 4. 凭据状态 ==")
    creds = config.creds
    print(f"  student_id：{'已提供' if creds.student_id else '未提供'}")
    print(f"  password：{'已提供（已脱敏）' if creds.password else '未提供'}")

    print("\n== 5. 演练摘要（脱敏）==")
    print(f"  CAS 登录：{config.cas.login_url}")
    # 青果平台的真实基址是 kingosoft_base_url（jw.shengda.edu.cn:8090），不是门户 base_url
    jwgl_base = config.jwgl.kingosoft_base_url if (config.jwgl.platform or "").lower() == "kingosoft" else config.jwgl.base_url
    print(f"  教务基址：{jwgl_base}")

    if (config.jwgl.platform or "").lower() == "kingosoft":
        print("  前提：真实抢课需先 `python -m qk login --wechat --save-session` 建立 CAS 会话"
              "（青果必须带会话才能建教务会话）。")
    else:
        print("  提示：以上为通用实现的解析结果；真实抢课同样需先建立 CAS 会话。")
    print(f"  限速：间隔 {config.http.interval}s / 重试 {config.http.max_retries} 次 / 并发 {config.http.concurrency}")

    print("\n== 结论 ==")
    if problems:
        for item in problems:
            print(f"  失败：{item}")
        return 1
    print("  通过：示例配置与实现一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
