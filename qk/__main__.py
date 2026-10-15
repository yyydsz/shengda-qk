"""命令行入口：``python -m qk``。

本文件提供**可运行的 argparse 骨架**：``run`` / ``login`` / ``list`` / ``grab``
四个子命令已注册，参数解析与错误提示在此完成；真正的业务处理优先交给
``qk.cli``（后续阶段接线），缺失时回落到本模块的占位实现并给出明确提示。

退出码约定：

====  ==================================================
0     成功（含 ``--dry-run`` 演练）
2     配置或凭据问题（缺学号/密码、配置文件写错）
3     网络失败，或登录/选课被拒绝（含需要验证码、会话失效）
4     其它未预期错误（已转成中文提示，不打印裸堆栈）
130   用户中断（Ctrl+C）
====  ==================================================
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .adapters.base import AdapterError
from .config import AppConfig, ConfigError, load_config
from .net import TransportError

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NETWORK = 3
EXIT_UNEXPECTED = 4
EXIT_INTERRUPTED = 130

PROGRAM = "qk"
DESCRIPTION = "郑州升达经贸管理学院教务系统抢课工具（python -m qk）"

#: 默认会话缓存文件名（与 qk.config.SessionConfig.path 保持一致，仅用于帮助文本）
DEFAULT_STATE_FILE = "qk_state.json"

EPILOG = """\
使用示例：
  python -m qk gui                           # 图形界面：扫码登录 → 查询课程 → 加目标 → 抢课
  python -m qk gui --mock                    # 图形界面离线演示（不碰真实教务）
  python -m qk run --dry-run                 # 演练：只打印将要做什么，不联网、不需要密码
  python -m qk run                           # 按配置正式抢课
  python -m qk login                         # 账号密码登录，确认账号与接口通不通
  python -m qk login --wechat                # 微信扫码登录（终端直接画二维码，手机扫）
  python -m qk login --wechat --save-session # 扫码一次并把会话存下来
  python -m qk grab --use-session            # 复用已保存的会话抢课（无需再扫码）
  python -m qk session --info                # 查看会话缓存状态
  python -m qk list                          # 列出候选课程与余量
  python -m qk grab --dry-run                # 演练抢课流程

不想敲命令：双击仓库根目录的 start-gui.bat 即可打开图形界面（会自动切目录、按需装 PyQt6）。
凭据请放在 config.yaml 或环境变量里（QK_PASSWORD / QK_STUDENT_ID），不要写进命令行参数。
"""


class CliError(Exception):
    """所有可直接展示给用户、无需堆栈的错误。"""


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="路径",
        default=None,
        help="配置文件路径（默认自动查找当前目录下的 config.yaml / config.yml / config.toml）",
    )
    common.add_argument(
        "--dotenv",
        metavar="路径",
        default=None,
        help="放凭据的 .env 文件路径（KEY=VALUE 形式，仅用于凭据）",
    )
    common.add_argument(
        "--workspace",
        metavar="目录",
        default=None,
        help="工作区目录，用于查找默认配置文件（默认当前目录）",
    )
    common.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出结果（便于脚本化处理）",
    )

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    parser.add_argument("--verbose", action="store_true", help="出错时打印详细堆栈，便于排查")
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    run_parser = subparsers.add_parser(
        "run", parents=[common], help="按配置执行抢课（等价于 login + list + grab）"
    )
    run_parser.add_argument("--dry-run", action="store_true", help="只演练，不联网、不提交选课")
    run_parser.add_argument("--once", action="store_true", help="只尝试一轮，失败即退出")
    run_parser.add_argument(
        "--use-session", action="store_true",
        help="复用本地缓存的登录会话（配合 login --wechat --save-session 使用，无需重新扫码）",
    )

    login_parser = subparsers.add_parser("login", parents=[common], help="登录（账号密码或微信扫码）")
    login_parser.add_argument("--dry-run", action="store_true", help="只检查配置，不真正登录")
    login_parser.add_argument(
        "--wechat", action="store_true",
        help="用微信扫码登录（在终端直接画出二维码，用手机扫）",
    )
    login_parser.add_argument(
        "--qr-file", metavar="路径", default=None,
        help="同时把二维码保存成 PNG（终端画不出来时，可用手机相册扫码）",
    )
    login_parser.add_argument(
        "--qr-mode", choices=("auto", "half", "solid", "ascii"), default="auto",
        help="二维码绘制方式（默认 auto：按终端编码能力自动选择）",
    )
    login_parser.add_argument("--no-color", action="store_true", help="二维码不使用 ANSI 反色")
    login_parser.add_argument("--qr-invert", action="store_true", help="反转二维码黑白（深色主题终端可试）")
    login_parser.add_argument(
        "--timeout", type=float, default=0.0, metavar="秒",
        help="等待扫码的总时长（默认取配置 cas.wechat.poll_timeout）",
    )
    login_parser.add_argument(
        "--save-session", action="store_true",
        help=f"登录成功后把会话保存到本地（默认 {DEFAULT_STATE_FILE}，已在 .gitignore 中排除）",
    )

    list_parser = subparsers.add_parser("list", parents=[common], help="查询候选课程与余量")
    list_parser.add_argument("--course", metavar="名称", default=None, help="只看包含该名称的课程")
    list_parser.add_argument(
        "--use-session", action="store_true",
        help="复用已保存的会话（扫码登录后无需再扫码；青果 Kingosoft 教务必须这样查）",
    )

    grab_parser = subparsers.add_parser("grab", parents=[common], help="按 grab.targets 抢课")
    grab_parser.add_argument("--dry-run", action="store_true", help="只演练，不提交选课")
    grab_parser.add_argument("--once", action="store_true", help="只尝试一轮，失败即退出")
    grab_parser.add_argument(
        "--start-at",
        metavar="时间",
        default=None,
        help="起抢时间，如 2026-10-13 12:00:00 或 12:00:00（覆盖配置里的 grab.start_at）",
    )
    grab_parser.add_argument(
        "--use-session", action="store_true",
        help="复用本地缓存的登录会话（配合 login --wechat --save-session 使用，无需重新扫码）",
    )

    session_parser = subparsers.add_parser(
        "session", parents=[common], help="查看或清除本地缓存的登录会话"
    )
    session_parser.add_argument("--info", action="store_true", help="查看会话缓存信息（默认行为）")
    session_parser.add_argument("--clear", action="store_true", help="删除会话缓存")

    gui_parser = subparsers.add_parser(
        "gui", parents=[common],
        help="打开图形界面（可选功能，需要先 pip install PyQt6）",
    )
    gui_parser.add_argument(
        "--mock", action="store_true",
        help="启动内置 mock 教务服务并指向它（离线演示界面，不访问真实教务）",
    )
    gui_parser.add_argument(
        "--screenshot", metavar="路径", default=None,
        help="渲染一张窗口截图到该路径后退出（配合离屏模式做自检/取证）",
    )

    return parser


# --------------------------------------------------------------------------
# 占位实现（S2 的 qk.cli 接线后不再使用）
# --------------------------------------------------------------------------


def _print_dry_run_plan(config: AppConfig, command: str) -> None:
    print(f"[dry-run] 命令：{command}")
    print(f"[dry-run] 配置来源：{' → '.join(config.sources)}")
    print(f"[dry-run] 登录地址：{config.cas.login_url}")
    print(f"[dry-run] 教务地址：{config.jwgl.base_url}")
    print(f"[dry-run] 请求间隔 {config.http.interval}s，重试 {config.http.max_retries} 次，并发 {config.http.concurrency}")
    if config.grab.start_at:
        print(f"[dry-run] 起抢时间：{config.grab.start_at}")
    print("[dry-run] 抢课目标：")
    for line in config.describe_targets():
        print(line)
    print("[dry-run] 演练结束：没有发起任何网络请求，也没有提交选课。")


def placeholder_run(args: argparse.Namespace, config: AppConfig) -> int:
    """``run`` 的占位实现：演练可用；正式执行需要业务接线。"""
    if getattr(args, "dry_run", False):
        _print_dry_run_plan(config, "run")
        return EXIT_OK
    raise CliError(
        "抢课业务逻辑尚未接线（当前为骨架版本）。请先用 `python -m qk run --dry-run` 检查配置；"
        "接线完成后即可直接运行 `python -m qk run`。"
    )


def placeholder_login(args: argparse.Namespace, config: AppConfig) -> int:
    if getattr(args, "dry_run", False):
        _print_dry_run_plan(config, "login")
        return EXIT_OK
    config.require_credentials()
    raise CliError("登录适配器尚未接线（当前为骨架版本），无法真正登录。")


def placeholder_list(args: argparse.Namespace, config: AppConfig) -> int:
    config.require_credentials()
    raise CliError("选课接口适配器尚未接线（当前为骨架版本），无法查询课程。")


def placeholder_grab(args: argparse.Namespace, config: AppConfig) -> int:
    if getattr(args, "dry_run", False):
        _print_dry_run_plan(config, "grab")
        return EXIT_OK
    config.require_credentials()
    raise CliError("抢课编排尚未接线（当前为骨架版本），无法提交选课。")


def placeholder_session(args: argparse.Namespace, config: AppConfig) -> int:
    raise CliError("会话缓存功能尚未接线（当前为骨架版本）。")


def placeholder_gui(args: argparse.Namespace, config: AppConfig) -> int:
    print(
        "图形界面需要可选依赖 PyQt6（命令行功能不需要它）。请先安装：\n"
        "    python -m pip install PyQt6\n"
        "或：python -m pip install -r requirements-gui.txt",
        file=sys.stderr,
    )
    return EXIT_CONFIG


PLACEHOLDERS: dict[str, Callable[[argparse.Namespace, AppConfig], int]] = {
    "run": placeholder_run,
    "login": placeholder_login,
    "list": placeholder_list,
    "grab": placeholder_grab,
    "session": placeholder_session,
    "gui": placeholder_gui,
}


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def load_config_for_args(args: argparse.Namespace) -> AppConfig:
    return load_config(
        getattr(args, "config", None),
        dotenv_path=getattr(args, "dotenv", None),
        workspace=getattr(args, "workspace", None),
    )


def dispatch(args: argparse.Namespace, config: AppConfig) -> int:
    """优先交给 qk.cli（后续阶段接线），否则使用本模块的占位实现。"""
    command = args.command
    try:
        from . import cli as cli_module  # 延迟导入：缺失时不影响骨架运行
    except ImportError:
        cli_module = None  # type: ignore[assignment]
    if cli_module is not None:
        handler = getattr(cli_module, f"cmd_{command}", None) or getattr(
            cli_module, command, None
        )
        if callable(handler):
            result = handler(args, config)
            return EXIT_OK if result is None else int(result)
    placeholder = PLACEHOLDERS.get(command)
    if placeholder is None:  # pragma: no cover - argparse 已限制取值
        raise CliError(f"未知子命令：{command}")
    return placeholder(args, config)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    try:
        config = load_config_for_args(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return dispatch(args, config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except CliError as exc:
        print(f"无法继续：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except TransportError as exc:
        print(f"网络错误：{exc}", file=sys.stderr)
        print("提示：校园网/校内 VPN 之外通常无法访问教务系统，请确认网络环境。", file=sys.stderr)
        return EXIT_NETWORK
    except AdapterError as exc:
        # 登录/选课被拒绝（账号密码错、需要验证码、会话反复失效……）：
        # 这些是“设计好的失败”，必须给出清晰提示，不能落进“未预期的错误”。
        print(f"执行失败：{exc}", file=sys.stderr)
        return EXIT_NETWORK
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - 兜底：不向用户抛裸堆栈
        print(f"未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            print("可加 --verbose 查看详细堆栈，或把上面的信息发给开发者。", file=sys.stderr)
        return EXIT_UNEXPECTED


def app_config_json(config: AppConfig) -> str:
    """把脱敏后的配置渲染成 JSON（供 ``--json`` 使用）。"""
    return json.dumps(config.redacted(), ensure_ascii=False, indent=2, default=str)


def workspace_root() -> Path:
    """当前工作目录，供内部自检使用。"""
    return Path.cwd()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
