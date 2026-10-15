"""CLI 接线：把 ``run`` / ``login`` / ``list`` / ``grab`` 子命令接到配置层与适配器。

``qk/__main__.py`` 负责参数解析与错误到退出码的映射，本模块负责真正的业务：

* ``login``：执行 CAS 登录并打印会话摘要（Cookie 只显示**名字**，不显示值）
* ``list``：查询候选课程，按余量排序，打印表格
* ``grab``：按 ``grab.targets`` 抢课，打印每个目标的结果与尝试次数
* ``run``：``login`` → ``list``（可选）→ ``grab`` 的一站式流程

所有输出都对凭据脱敏；``--dry-run`` 不发送任何写请求。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .adapters.base import PlatformUnsupportedError
from .adapters.cas import CasLoginAdapter
from .adapters.jwgl import build_course_adapter
from .adapters.qr_render import MODE_AUTO, QRImageError, RenderResult, render_png
from .adapters.wechat import PASSWORD_FALLBACK_HINT, QrChallenge, WeChatQrLoginAdapter
from .config import AppConfig
from .grabber import Grabber, GrabReport
from .models import CandidateCourse, SessionInfo
from .net import QkSession
from .session_store import CachedSession, SessionStore, apply_session

EXIT_OK = 0
EXIT_FAILED = 1
#: 配置/凭据/可选依赖缺失（与 __main__ 的 EXIT_CONFIG 一致）
EXIT_CONFIG = 2
#: 需要先登录（未登录/会话过期/扫码失败）——与 __main__ 的「网络或登录失败」约定一致
EXIT_AUTH = 3

#: 二维码渲染模式可选值
QR_MODES = (MODE_AUTO, "half", "solid", "ascii")


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def render_courses(courses: Sequence[CandidateCourse]) -> str:
    """把课程列表渲染成对齐的表格文本。"""
    if not courses:
        return "（没有匹配的课程）"
    rows = [
        ("教学班号", "课程号", "课程名称", "教师", "学分", "容量", "已选", "余量", "上课时间")
    ]
    for course in courses:
        rows.append(
            (
                course.teaching_class_id or "-",
                course.course_code or "-",
                course.course_name or "-",
                course.teacher or "-",
                "" if course.credits is None else f"{course.credits:g}",
                "" if course.capacity is None else str(course.capacity),
                "" if course.enrolled is None else str(course.enrolled),
                "?" if course.remaining is None else str(course.remaining),
                course.time_text or "-",
            )
        )
    widths = [max(_display_width(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines = []
    for position, row in enumerate(rows):
        cells = [
            cell + " " * (widths[index] - _display_width(cell)) for index, cell in enumerate(row)
        ]
        lines.append("  ".join(cells).rstrip())
        if position == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _display_width(text: str) -> int:
    """中文按 2 个字符宽度计算，保证表格对齐。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def render_session(info: SessionInfo) -> str:
    lines = [
        f"登录结果：{'成功' if info.ok else '未确认'}",
        f"学号：{info.student_id or '(未提供)'}",
        f"会话 Cookie：{('、'.join(sorted(info.cookies)) or '(无)')}（仅显示名字，值不输出）",
    ]
    if info.service_url:
        lines.append(f"回跳地址：{info.service_url}")
    if info.message:
        lines.append(f"说明：{info.message}")
    return "\n".join(lines)


def render_grab_report(report: GrabReport) -> str:
    lines = [
        f"抢课{'演练' if report.dry_run else '执行'}结果：{'成功' if report.success else '未成功'}",
        f"时间：{report.started_at} → {report.finished_at}"
        + (f"（等待起抢 {report.waited_seconds:.1f} 秒）" if report.waited_seconds else ""),
        f"登录：{'已完成' if report.logged_in else '未确认'}"
        + (f"，重新登录 {report.relogins} 次" if report.relogins else ""),
        f"统计：{report.stats}",
    ]
    summary = report.summary_lines()
    if summary:
        lines.append("目标结果：")
        lines.extend(f"  {line}" if not line.startswith("    ") else line for line in summary)
    return "\n".join(lines)


def _build_session(config: AppConfig) -> QkSession:
    return QkSession.from_config(config)


# --------------------------------------------------------------------------
# 会话缓存（扫码一次、定时抢课复用）
# --------------------------------------------------------------------------


def _session_store(config: AppConfig) -> SessionStore:
    return SessionStore(config.session.path, config.session.max_age_hours)


def _save_session(config: AppConfig, info: SessionInfo, *, source: str = "") -> CachedSession:
    """把刚建立的会话落盘；只打印 Cookie 名字与有效期，不打印值。"""
    store = _session_store(config)
    cached = store.save(
        info.cookies,
        student_id=info.student_id,
        service_url=config.cas.service,
        source=source or config.cas.login_mode,
    )
    view = cached.redacted()
    print(
        f"会话已保存到 {store.path}：{view['saved_at']} 保存、{view['expires_at']} 过期"
        f"（Cookie：{'、'.join(view['cookie_names']) or '无'}，有效期 {view['max_age_hours']} 小时）"
    )
    print("  之后可以直接用 `python -m qk grab --use-session` 抢课，无需再扫码。")
    return cached


def _use_cached_session(config: AppConfig, session: QkSession) -> int | None:
    """把缓存会话注入到 ``session``；不可用时打印原因并返回退出码。"""
    store = _session_store(config)
    cached, reason = store.load_usable()
    if cached is None:
        print(f"无法复用缓存会话：{reason}。")
        print("  请先登录一次并保存会话：python -m qk login --wechat --save-session")
        return EXIT_AUTH
    note = apply_session(session, cached)
    view = cached.redacted()
    print(f"已复用缓存会话：{note}；Cookie：{'、'.join(view['cookie_names'])}；{view['expires_at']} 过期")
    return None


# --------------------------------------------------------------------------
# 微信扫码登录
# --------------------------------------------------------------------------


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except OSError:  # pragma: no cover - 极端环境
        return 80


def show_qr(challenge: QrChallenge, config: AppConfig, *, qr_file: str | None = None,
            mode: str = MODE_AUTO, color: bool | None = None, invert: bool | None = None) -> RenderResult | None:
    """在终端画出二维码（可选同时存一份 PNG）。返回渲染结果，失败返回 ``None``。"""
    wechat = config.cas.wechat
    if qr_file:
        try:
            Path(qr_file).write_bytes(challenge.image)
            print(f"二维码图片已保存：{qr_file}（也可以直接用手机相册扫码）")
        except OSError as exc:
            print(f"二维码图片保存失败：{exc}")

    try:
        result = render_png(
            challenge.image,
            quiet_zone=wechat.qr_quiet_zone,
            color=wechat.qr_color if color is None else color,
            invert=wechat.qr_invert if invert is None else invert,
            width=_terminal_width(),
            mode=mode or MODE_AUTO,
        )
    except QRImageError as exc:
        print(f"无法在终端绘制二维码：{exc}")
        if not qr_file:
            print("  提示：加 `--qr-file qr.png` 保存图片后用手机相册扫码；" + PASSWORD_FALLBACK_HINT)
        return None

    print(result.text())
    print(f"（二维码：{result.modules} 模块 / {result.columns} 列 x {result.rows} 行，"
          f"渲染方式 {mode or MODE_AUTO}）")
    if result.warning:
        print(f"提示：{result.warning}")
    return result


def login_wechat(args: argparse.Namespace, config: AppConfig) -> int:
    """``login --wechat``：取码 → 终端展示 → 等扫码 → 建立会话（可选保存会话）。"""
    session = _build_session(config)
    adapter = WeChatQrLoginAdapter(config, session)

    timeout = float(getattr(args, "timeout", 0) or 0)
    if timeout > 0:
        # 至少 1 秒：否则提示里会显示成“0 秒”，用户看不懂
        adapter.wechat = replace(config.cas.wechat, poll_timeout=max(1.0, timeout))
    effective_timeout = adapter.wechat.poll_timeout

    qr_file = getattr(args, "qr_file", None)
    mode = getattr(args, "qr_mode", "") or MODE_AUTO
    color = False if getattr(args, "no_color", False) else None
    invert = True if getattr(args, "qr_invert", False) else None
    counter = {"shown": 0}

    def on_qrcode(challenge: QrChallenge) -> None:
        counter["shown"] += 1
        if counter["shown"] > 1:
            print(f"（上一个二维码已过期，已重新取码：第 {counter['shown']} 张）")
        show_qr(challenge, config, qr_file=qr_file, mode=mode, color=color, invert=invert)
        print("请用微信「扫一扫」扫描上面的二维码，并在手机上确认。")
        print(f"正在等待扫码（最多 {effective_timeout:.0f} 秒）…")

    def on_status(outcome: Any) -> None:
        if outcome.message:
            print(f"  · {outcome.state}：{outcome.message}")

    info = adapter.login(on_qrcode=on_qrcode, on_status=on_status)

    if wants_json(args):
        emit_json({"session": info.redacted(), "qr_shown": counter["shown"],
                   "establish_strategy": config.cas.establish_strategy})
    else:
        print(render_session(info))
    if info.ok and getattr(args, "save_session", False):
        _save_session(config, info, source="wechat")
    return EXIT_OK if info.ok else EXIT_FAILED


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace, config: AppConfig) -> int:
    """登录：默认账号密码；``--wechat`` 或 ``cas.login_mode=wechat`` 时走微信扫码。"""
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        _print_dry_run_plan(config, "login")
        return EXIT_OK

    use_wechat = bool(getattr(args, "wechat", False)) or (config.cas.login_mode or "").lower() == "wechat"
    if use_wechat:
        return login_wechat(args, config)

    session = _build_session(config)
    adapter = CasLoginAdapter(config, session)
    info = adapter.login()
    if wants_json(args):
        emit_json(info.redacted())
    else:
        print(render_session(info))
    if info.ok and getattr(args, "save_session", False):
        _save_session(config, info, source="password")
    return EXIT_OK if info.ok else EXIT_FAILED


def cmd_session(args: argparse.Namespace, config: AppConfig) -> int:
    """查看或清除本地会话缓存（只显示 Cookie 名字与有效期，不显示值）。"""
    store = _session_store(config)

    if getattr(args, "clear", False):
        removed = store.clear()
        print(f"已删除会话缓存：{store.path}" if removed else f"没有找到会话缓存：{store.path}（无需清除）")
        return EXIT_OK

    cached = store.load()
    if wants_json(args):
        emit_json({
            "path": str(store.path),
            "max_age_hours": config.session.max_age_hours,
            "exists": cached is not None,
            "session": cached.redacted() if cached else None,
        })
        return EXIT_OK

    if cached is None:
        print(f"没有找到会话缓存：{store.path}")
        print("  先扫码登录一次并保存会话：python -m qk login --wechat --save-session")
        return EXIT_OK

    view = cached.redacted()
    print(f"会话缓存：{store.path}")
    print(f"  保存时间：{view['saved_at']}    过期时间：{view['expires_at']}"
          f"    剩余：{view['remaining_minutes']} 分钟    有效期设置：{view['max_age_hours']} 小时")
    print(f"  Cookie 名：{'、'.join(view['cookie_names']) or '（无）'}（值不显示）")
    print(f"  票据名：{'、'.join(view['ticket_names']) or '（无）'}")
    print(f"  学号：{view['student_id'] or '（未知）'}    来源：{view['source'] or '（未知）'}"
          f"    是否过期：{'是' if view['expired'] else '否'}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace, config: AppConfig) -> int:
    """查询候选课程与余量。

    ``--use-session`` 时复用已保存的会话（扫码登录拿不到密码，青果 Kingosoft 教务
    只有带 CAS 会话才能建教务会话、查询课程）。
    """
    config.require_credentials() if not getattr(args, "use_session", False) else None
    session = _build_session(config)
    student_id = config.creds.student_id

    if getattr(args, "use_session", False):
        store = _session_store(config)
        cached, reason = store.load_usable()
        if cached is None:
            print(f"无法复用缓存会话：{reason}。")
            print("  请先扫码登录一次并保存会话：python -m qk login --wechat --save-session")
            return EXIT_AUTH
        note = apply_session(session, cached)
        view = cached.redacted()
        print(f"已复用缓存会话：{note}；Cookie：{'、'.join(view['cookie_names'])}；{view['expires_at']} 过期")
        student_id = cached.student_id or student_id
    else:
        login = CasLoginAdapter(config, session)
        info = login.login()
        if not info.ok and not wants_json(args):
            print(f"警告：登录未确认成功（{info.message}），仍尝试查询课程。")
        student_id = info.student_id or student_id

    adapter = build_course_adapter(config, session)
    keyword = getattr(args, "course", None) or ""
    # 授课形式排除由**图形界面**的勾选框维护（写入 config.yaml 的
    # jwgl.exclude_course_attrs），适配器查询时自动应用，命令行不再提供同名参数。
    courses = list(adapter.fetch_candidates(keyword))
    if wants_json(args):
        emit_json({"student_id": student_id, "keyword": keyword,
                   "excluded_attrs": list(config.jwgl.exclude_course_attrs),
                   "courses": [course.redacted() for course in courses]})
    else:
        print(render_courses(courses))
    return EXIT_OK


class CachedSessionLogin:
    """把「已缓存的会话」包装成登录适配器：不再要求账号密码或扫码。

    扫码登录拿不到密码，定时抢课时只能复用会话；这个适配器让 ``Grabber`` 无需改动
    就能走「已登录」路径，并用一次轻量探测确认会话是否还有效。
    """

    name = "cached-session"

    def __init__(self, config: AppConfig, session: QkSession, cached: CachedSession) -> None:
        self.config = config
        self.session = session
        self.cached = cached
        self._probe = CasLoginAdapter(config, session)

    def login(self, *, dry_run: bool = False) -> SessionInfo:
        if dry_run:
            return SessionInfo(ok=False, message="演练模式：未使用缓存会话")
        alive = self._probe.is_session_alive()
        cookies = self.session.cookies() or dict(self.cached.cookies)
        return SessionInfo(
            ok=alive,
            student_id=self.cached.student_id,
            cookies=cookies,
            service_url=self.config.cas.service,
            message=(
                f"复用 {self.cached.saved_at} 保存的会话"
                if alive
                else "缓存的会话已失效，请重新扫码：python -m qk login --wechat --save-session"
            ),
        )

    def is_session_alive(self) -> bool:
        return self._probe.is_session_alive()

    def logout(self) -> None:
        pass


def _make_grabber(config: AppConfig, session: QkSession, *,
                  login_adapter: Any | None = None) -> Grabber:
    """组装编排器；默认用 CAS 登录，也可以传入「复用缓存会话」的适配器。"""
    adapter = login_adapter if login_adapter is not None else CasLoginAdapter(config, session)
    return Grabber(config, login_adapter=adapter,
                   course_adapter=build_course_adapter(config, session))


def cmd_grab(args: argparse.Namespace, config: AppConfig) -> int:
    """按 grab.targets 抢课。"""
    dry_run = bool(getattr(args, "dry_run", False))
    once = bool(getattr(args, "once", False))
    start_at = getattr(args, "start_at", None)
    if not config.grab.targets:
        if dry_run:
            _print_dry_run_plan(config, "grab")
            return EXIT_OK
        print("配置错误：grab.targets 为空，没有要抢的课程。请先编辑 config.yaml。")
        return 2

    session = _build_session(config)
    login_adapter = None
    if getattr(args, "use_session", False):
        store = _session_store(config)
        cached, reason = store.load_usable()
        if cached is None:
            print(f"无法复用缓存会话：{reason}。")
            print("  请先扫码登录一次并保存会话：python -m qk login --wechat --save-session")
            return EXIT_AUTH
        note = apply_session(session, cached)
        view = cached.redacted()
        print(f"已复用缓存会话：{note}；Cookie：{'、'.join(view['cookie_names'])}；{view['expires_at']} 过期")
        login_adapter = CachedSessionLogin(config, session, cached)

    grabber = _make_grabber(config, session, login_adapter=login_adapter)
    report = grabber.run(dry_run=dry_run, once=once, start_at=start_at)
    if wants_json(args):
        emit_json(report.redacted())
    else:
        print(render_grab_report(report))
    return EXIT_OK if (report.success or dry_run) else EXIT_FAILED


def cmd_run(args: argparse.Namespace, config: AppConfig) -> int:
    """一站式：登录 → 查询 → 抢课。"""
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        _print_dry_run_plan(config, "run")
        return EXIT_OK

    session = _build_session(config)
    login_adapter = None
    if getattr(args, "use_session", False):
        store = _session_store(config)
        cached, reason = store.load_usable()
        if cached is None:
            print(f"无法复用缓存会话：{reason}。")
            print("  请先扫码登录一次并保存会话：python -m qk login --wechat --save-session")
            return EXIT_AUTH
        note = apply_session(session, cached)
        view = cached.redacted()
        print(f"已复用缓存会话：{note}；Cookie：{'、'.join(view['cookie_names'])}；{view['expires_at']} 过期")
        login_adapter = CachedSessionLogin(config, session, cached)
        info = login_adapter.login()
    else:
        login = CasLoginAdapter(config, session)
        info = login.login()
    if not wants_json(args):
        print(render_session(info))

    adapter = build_course_adapter(config, session)
    try:
        courses = list(adapter.fetch_candidates())
    except PlatformUnsupportedError as exc:
        if not wants_json(args):
            print(f"跳过课程列表：{exc}")
        courses = []
    if courses and not wants_json(args):
        print("\n候选课程：")
        print(render_courses(courses))

    grabber = _make_grabber(config, session, login_adapter=login_adapter)
    report = grabber.run(dry_run=False, once=bool(getattr(args, "once", False)))
    if wants_json(args):
        emit_json({"session": info.redacted(), "grab": report.redacted()})
    else:
        print()
        print(render_grab_report(report))
    return EXIT_OK if report.success else EXIT_FAILED


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------


def _print_dry_run_plan(config: AppConfig, command: str) -> None:
    is_kingosoft = (config.jwgl.platform or "").lower() == "kingosoft"
    print(f"[dry-run] 命令：{command}")
    print(f"[dry-run] 配置来源：{' → '.join(config.sources)}")
    print(f"[dry-run] 登录地址：{config.cas.login_url}")
    # 青果平台的真实教务基址是 kingosoft_base_url（jw.shengda.edu.cn:8090），不是门户 base_url
    print(f"[dry-run] 教务地址："
          f"{config.jwgl.kingosoft_base_url if is_kingosoft else config.jwgl.base_url}")
    print(f"[dry-run] 教务平台：{config.jwgl.platform}")
    if is_kingosoft:
        print("[dry-run] 接口路径：青果契约已内置在 KingosoftCourseAdapter 里，"
              "course_list_path 等四项留空即正确（无需手填）")
    else:
        print(f"[dry-run] 未确认的接口路径："
              f"course_list_path={config.jwgl.course_list_path or '（空）'}、"
              f"select_path={config.jwgl.select_path or '（空）'}、"
              f"drop_path={config.jwgl.drop_path or '（空）'}")
    print(f"[dry-run] 请求间隔 {config.http.interval}s，重试 {config.http.max_retries} 次，"
          f"并发 {config.http.concurrency}")
    if config.grab.start_at:
        print(f"[dry-run] 起抢时间：{config.grab.start_at}")
    print("[dry-run] 抢课目标：")
    for line in config.describe_targets():
        print(line)
    print("[dry-run] 演练结束：没有发起任何网络请求，也没有提交选课。")


def cmd_gui(args: argparse.Namespace, config: AppConfig) -> int:
    """打开图形界面。

    图形界面是**可选功能**：``qk.gui`` 顶层不导入 PyQt，真正的导入发生在
    :func:`qk.gui.run_gui` 里；因此没装 PyQt6 时这里会给出可操作的安装提示并返回
    退出码 2（``EXIT_CONFIG``），**不打印堆栈**。命令行其它子命令完全不依赖 Qt。
    """
    try:
        from .gui import QtMissingError, run_gui
    except ImportError as exc:  # pragma: no cover - qk.gui 自身损坏时
        print(f"图形界面模块无法加载：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return run_gui(
            config,
            mock=bool(getattr(args, "mock", False)),
            screenshot=getattr(args, "screenshot", None),
            argv=[sys.argv[0]],
        )
    except QtMissingError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        return 130  # 与命令行其它子命令一致的「用户中断」退出码
    except Exception as exc:  # noqa: BLE001 - 界面初始化失败也要给可操作提示
        print(f"图形界面启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("可加 --verbose 查看详细堆栈，或把上面的信息发给开发者。", file=sys.stderr)
        return EXIT_CONFIG


__all__ = [
    "CachedSessionLogin",
    "QR_MODES",
    "cmd_grab",
    "cmd_gui",
    "cmd_list",
    "cmd_login",
    "cmd_run",
    "cmd_session",
    "emit_json",
    "login_wechat",
    "render_courses",
    "render_grab_report",
    "render_session",
    "show_qr",
]
