"""qk 图形界面（PyQt6，**可选依赖**）。

设计要点
--------

* **延迟导入**：本模块顶层不导入 PyQt，只有 :func:`run_gui` 与 :class:`MainWindow`
  内部才通过 :func:`load_qt` 导入。因此没装 PyQt6 时 CLI、既有用例与 ``import qk.gui``
  都不会失败；``python -m qk gui`` 会给出 ``pip install PyQt6`` 的可操作提示。
* **网络操作一律在后台线程**：登录/查询/抢课都交给 :class:`TaskWorker`（``QThread``）
  执行，主线程只更新控件——界面不会因为一次慢查询而卡死，运行中也能点「停止」。
* **安全边界**：演练（dry-run）不发任何网络请求；点「真实提交」必须先经过二次确认
  弹窗，取消则不发出任何写请求。界面**不提供退课入口**，也不展示未确认的「已选列表」。

界面分五区（见 :meth:`MainWindow._build_ui`）：① 登录与会话 ② 课程查询 ③ 抢课目标
④ 抢课控制 ⑤ 运行日志。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from .adapters.base import AdapterError, filter_excluded_attrs
from .adapters.jwgl import build_course_adapter
from .adapters.wechat import WeChatQrLoginAdapter
from .config import AppConfig, ConfigError, CourseTarget, load_config
from .grabber import Grabber
from .models import CandidateCourse
from .net import QkSession
from .session_store import SessionStore, apply_session

#: 课程表列（**刻意不设容量/余量**：真实青果课程表没有这两列）
COURSE_COLUMNS: tuple[str, ...] = ("教学班号", "课程号", "课程名称", "教师", "学分", "上课时间")
#: 抢课目标列（对应 grab.targets 的三个匹配字段）
TARGET_COLUMNS: tuple[str, ...] = ("课程名", "课程号", "教师")

PIP_COMMAND = "python -m pip install PyQt6"

QT_MISSING_HINT = (
    "图形界面需要可选依赖 PyQt6（命令行功能不需要它）。请先安装：\n"
    f"    {PIP_COMMAND}\n"
    "或：python -m pip install -r requirements-gui.txt"
)

EMPTY_COURSE_HINT = "当前轮次没有可选课程（可用 `python -m qk gui --mock` 离线演示）"

MODE_DRY_RUN = "当前模式：演练（不发网络请求）"
MODE_REAL = "当前模式：真实提交（提交前需二次确认）"


class QtMissingError(RuntimeError):
    """PyQt6 未安装——调用方应把它转成可操作提示，而不是打印堆栈。"""


def load_qt() -> tuple[Any, Any, Any]:
    """导入 PyQt6 并返回 ``(QtCore, QtGui, QtWidgets)``。"""
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise QtMissingError(QT_MISSING_HINT) from exc
    return QtCore, QtGui, QtWidgets


# --------------------------------------------------------------------------
# 后台线程
# --------------------------------------------------------------------------


def build_worker_class() -> type:
    """构造 ``TaskWorker`` 类（QThread 子类；信号在运行时绑定）。"""
    QtCore, _QtGui, _QtWidgets = load_qt()

    class TaskWorker(QtCore.QThread):  # type: ignore[misc, valid-type]
        """在**后台线程**里跑一个函数，把日志/结果/错误通过信号送回主线程。"""

        log = QtCore.pyqtSignal(str)
        qr = QtCore.pyqtSignal(bytes)
        done = QtCore.pyqtSignal(object)
        failed = QtCore.pyqtSignal(str)
        cancelled = QtCore.pyqtSignal()

        def __init__(self, fn: Callable[[Callable[[str], None]], Any],
                     parent: Any = None, name: str = "任务") -> None:
            super().__init__(parent)
            self._fn = fn
            self.task_name = name
            #: 实际执行该函数的线程 id（供「网络操作不在主线程」的断言使用）
            self.thread_id: int | None = None
            #: 置位后，长循环应在下一轮自行退出
            self.stop_event = threading.Event()

        def run(self) -> None:  # noqa: D102 - QThread 约定
            self.thread_id = threading.get_ident()
            self.log.emit(f"[{self.task_name}] 开始（后台线程 {self.thread_id}）")
            try:
                result = self._fn(self.log.emit)
            except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
                self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if self.stop_event.is_set():
                self.cancelled.emit()
                return
            self.done.emit(result)

        # 允许业务函数以关键字形式拿到取消标志与「发二维码」能力；
        # **按函数签名传参**：只接受 log 的调用方不该被迫接收 stop_event/emit_qr。
        def bind_helpers(self, fn: Callable[..., Any]) -> Callable[[Callable[[str], None]], Any]:
            import inspect

            parameters = inspect.signature(fn).parameters
            accepts_any = any(
                item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
            )

            def runner(log: Callable[[str], None]) -> Any:
                kwargs: dict[str, Any] = {"log": log}
                if accepts_any or "stop_event" in parameters:
                    kwargs["stop_event"] = self.stop_event
                if accepts_any or "emit_qr" in parameters:
                    kwargs["emit_qr"] = self.qr.emit
                return fn(**kwargs)

            return runner

    return TaskWorker


_WORKER_CLASS: type | None = None


def worker_class() -> type:
    global _WORKER_CLASS
    if _WORKER_CLASS is None:
        _WORKER_CLASS = build_worker_class()
    return _WORKER_CLASS


# --------------------------------------------------------------------------
# 业务后端（与 Qt 无关，便于单独测试）
# --------------------------------------------------------------------------


class GuiBackend:
    """界面用到的业务动作：会话、查询、抢课（全部复用既有模块，不重写逻辑）。"""

    def __init__(self, config: AppConfig, log: Callable[[str], None] = print, *,
                 mock: bool = False) -> None:
        self.config = config
        self.log = log
        self.session: QkSession = QkSession.from_config(config)
        self.store: SessionStore = SessionStore(config.session.path, config.session.max_age_hours)
        #: 是否为 ``--mock`` 离线演示。为 True 时**绝不把会话写进** ``session.path``：
        #: mock 会话的 service_url 指向 127.0.0.1，写进去会覆盖掉用户真实的 CASTGC 会话
        #: （实测发生过：在演示界面扫码一次，真实会话就被 mock 会话顶掉了）。
        self.mock = mock
        #: 各条网络路径**实际执行所在的线程 id**（供「不在主线程」的取证；只记录 id，不含敏感值）
        self.thread_trace: dict[str, list[int]] = {"login": [], "query": [], "grab": []}

    def _trace(self, kind: str) -> int:
        current = threading.get_ident()
        self.thread_trace.setdefault(kind, []).append(current)
        return current

    # ------------------------------------------------------------ 会话
    def session_view(self) -> dict[str, Any]:
        """返回缓存会话的脱敏信息（不含 Cookie 值）。"""
        cached, reason = self.store.load_usable()
        if cached is None:
            return {"ok": False, "reason": reason}
        return {"ok": True, "view": cached.redacted(), "cached": cached}

    def use_cached_session(self) -> dict[str, Any]:
        """把缓存会话注入底层会话对象。"""
        info = self.session_view()
        if not info["ok"]:
            return info
        note = apply_session(self.session, info["cached"])
        info["note"] = note
        return info

    def clear_session(self) -> str:
        path = Path(self.store.path)
        existed = path.exists()
        if existed:
            path.unlink()
        return f"已清除会话缓存 {path}" if existed else f"会话缓存本就不存在（{path}）"

    def qr_login(self, *, log: Callable[[str], None], emit_qr: Callable[[bytes], None],
                 stop_event: threading.Event) -> dict[str, Any]:
        """扫码登录并把会话落盘（在工作线程里执行）。"""
        self._trace("login")
        adapter = WeChatQrLoginAdapter(self.config, self.session)

        def on_qrcode(challenge: Any) -> None:
            if challenge.image:
                emit_qr(challenge.image)
                log(f"二维码已生成（uuid 前缀 {challenge.uuid[:8]}…），请用微信扫一扫")
            else:  # pragma: no cover - 平台没有返回图片内容时
                log(f"二维码内容（非图片）：{challenge.content[:48]}")

        def on_status(outcome: Any) -> None:
            log(f"扫码状态：{outcome.state}（{outcome.message}）")

        info = adapter.login(on_qrcode=on_qrcode, on_status=on_status)
        if not info.ok:
            return {"ok": False, "message": info.message}
        if self.mock:
            # 离线演示：只把会话留在内存里，不落盘，避免覆盖用户真实的会话文件
            log("离线演示模式：会话仅保留在内存中，未写入 "
                f"{self.config.session.path}（以免覆盖你的真实会话）。")
            return {"ok": True, "student_id": info.student_id, "view": {}, "saved": False}
        view = self.store.save(info.cookies, student_id=info.student_id,
                               service_url=self.config.cas.service, source="wechat")
        redacted = view.redacted()
        log(f"会话已保存：{redacted['saved_at']} 保存、{redacted['expires_at']} 过期")
        return {"ok": True, "student_id": info.student_id, "view": redacted, "saved": True}

    # ------------------------------------------------------------ 查询
    def save_exclude_patterns(self, patterns: Sequence[str]) -> str:
        """把排除条件写进当前配置文件（``config.config_path``）。

        安全约定：
        1. 只在 ``config.yaml`` 这类 YAML 文件上写；**没有配置文件时返回空串**，
           由调用方提示「仅本次运行有效」，绝不擅自新建文件或改动默认值来源；
        2. 先在内存里做一次「转储 → 重新加载」往返校验：若转储结果无法被
           ``load_config`` 正常读回（例如丢了 jwgl 小节），就**中止写入**，
           而不是把一份坏配置落盘；
        3. 丢失注释会给出明确警告 —— PyYAML 会抹掉 YAML 注释，作业级工具的配置
           文件通常带大量说明，静默清空是不可接受的。
        """
        path = str(getattr(self.config, "config_path", "") or "")
        if not path or not Path(path).is_file():
            return ""
        if Path(path).suffix.lower() not in (".yaml", ".yml"):
            raise ConfigError(f"只支持写 YAML 配置文件，当前是 {Path(path).suffix or '（无扩展名）'}")

        import yaml

        original = Path(path).read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(original) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件无法解析，未做修改：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("配置文件顶层不是键值表，未做修改")

        cleaned = [str(p).strip() for p in (patterns or []) if str(p).strip()]
        # 必须已有 jwgl 小节才写：若用 setdefault 新建一个空小节，重新加载时该小节
        # 会**覆盖内置默认值**（platform 回落成 auto），把青果适配器弄丢。
        section = data.get("jwgl")
        if not isinstance(section, dict):
            raise ConfigError(
                "配置文件里没有 jwgl 小节，写入会覆盖内置默认值（platform 会回落成 auto），"
                f"已中止。请先把 jwgl 小节（可参考 config.example.yaml）补进 {Path(path).name}"
            )
        section["exclude_course_attrs"] = cleaned

        # ---- 往返校验：转储结果必须能被正常读回，且关键小节还在 ----
        dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        probe_dir = Path(tempfile.mkdtemp(prefix="qk-cfg-probe-"))
        probe_path = probe_dir / "config.yaml"
        probe_path.write_text(dumped, encoding="utf-8")
        try:
            probe = load_config(probe_path, environ={}, workspace=probe_dir)
        except ConfigError as exc:
            raise ConfigError(f"写入前的往返校验失败，未做修改：{exc}") from exc
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        if list(probe.jwgl.exclude_course_attrs) != cleaned:
            raise ConfigError("写入前的往返校验不一致，未做修改")

        # ---- 原子写入（临时文件 + replace），避免中途失败留下半个文件 ----
        target = Path(path)
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(original, encoding="utf-8")
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(dumped, encoding="utf-8")
        tmp.replace(target)

        if "#" in original and "#" not in dumped:
            self.log("注意：写入时 PyYAML 抹掉了 config.yaml 里的注释"
                     f"（原文件已备份到 {backup.name}，需要可自行合并）")
        # 同步内存里的配置，避免界面与文件不一致
        self.config.jwgl.exclude_course_attrs = list(cleaned)
        return str(target)

    def fetch_courses(self, keyword: str, *, log: Callable[[str], None],
                      excluded_attrs: Sequence[str] | None = None,
                      ) -> list[CandidateCourse]:
        """查询候选课程（复用真实青果/通用适配器）。

        ``excluded_attrs`` 是界面上现选的排除项，与配置里的
        ``jwgl.exclude_course_attrs`` **叠加**（适配器已应用过配置项）。
        """
        self._trace("query")
        info = self.use_cached_session()
        if not info["ok"]:
            raise AdapterError(
                f"需要先有可用会话（{info['reason']}）。请先点「扫码登录」保存会话。"
            )
        log(f"已复用缓存会话：{info.get('note', '')}")
        adapter = build_course_adapter(self.config, self.session)
        courses = list(adapter.fetch_candidates(keyword))
        before = len(courses)
        ui_patterns = [p for p in (excluded_attrs or []) if str(p).strip()]
        if ui_patterns:
            courses = filter_excluded_attrs(courses, ui_patterns)
            log(f"界面排除条件 {'、'.join(ui_patterns)}：滤掉 {before - len(courses)} 门")
        log(f"查询完成：{len(courses)} 门课程")
        return courses

    # ------------------------------------------------------------ 抢课
    def make_grabber(self, targets: Sequence[CourseTarget], *, log: Callable[[str], None],
                     stop_event: threading.Event) -> Grabber:
        """组装编排器：复用会话登录 + 真实课程适配器；日志走界面，且可被「停止」打断。"""
        from .cli import CachedSessionLogin

        self._trace("grab")
        info = self.use_cached_session()
        if not info["ok"]:
            raise AdapterError(
                f"需要先有可用会话（{info['reason']}）。请先点「扫码登录」保存会话。"
            )
        cached = info["cached"]
        self.config.grab.targets = list(targets)

        def _traced_log(message: str) -> None:
            # Grabber 的每一条日志（含进入循环、匹配目标、提交写请求前后）都在这里留痕
            self._trace("grab")
            log(message)

        def _sleep(seconds: float) -> None:
            # 用 stop_event 代替无条件休眠：点「停止」后能很快退出
            self._trace("grab")
            if stop_event.wait(min(seconds, 1.0)):
                raise AdapterError("已停止：用户点了「停止」")
            remaining = seconds - min(seconds, 1.0)
            while remaining > 0:
                if stop_event.wait(min(remaining, 1.0)):
                    raise AdapterError("已停止：用户点了「停止」")
                remaining -= 1.0

        return Grabber(
            self.config,
            login_adapter=CachedSessionLogin(self.config, self.session, cached),
            course_adapter=build_course_adapter(self.config, self.session),
            sleep=_sleep,
            logger=_traced_log,
        )


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------


class MainWindow:
    """主窗口工厂：``MainWindow(...)`` 返回一个 ``QMainWindow`` 实例。"""

    def __new__(cls, config: AppConfig, *, backend: GuiBackend | None = None,
                mock_server: Any = None, log_lines: int = 4000) -> Any:  # noqa: D102
        QtCore, QtGui, QtWidgets = load_qt()

        class _Window(QtWidgets.QMainWindow):  # type: ignore[misc, valid-type]
            """五区主窗口。"""

            def __init__(self) -> None:
                super().__init__()
                # 离屏平台默认没有字体（文字会变方块）；这里补齐，测试直接建窗也受益
                font_dir = prepare_fonts()
                existing_app = QtWidgets.QApplication.instance()
                font_family = apply_ui_font(existing_app, QtGui) if existing_app else ""
                self.setWindowTitle("qk 抢课工具 —— 可视化界面")
                # 五个区都要放得下：二维码(约190) + 课程表 + 目标 + 控制 + 日志。
                # 早期默认 1180x820，五个区的最小高度加起来约 910px，必然有一个被压扁
                # ——被压的恰好是课程表格（实测只剩 ~50px，117 门课看起来「只有一行」）。
                # 后来又反馈「日志窗口太小」，于是**按屏幕可用高度自适应**：
                # 尽量用满屏幕高度（留 20px 给任务栏/边框），并把多出来的空间给日志区。
                # 注意下限取 1200x960 —— 这是先前调好的基准：再小就会把②区课程表格
                # 压到只能显示 1 行（offscreen 平台的虚拟屏幕只有 ~800x600，
                # 若按它自适应会把窗口缩到 760 高，直接触发该回归）。
                screen = QtWidgets.QApplication.primaryScreen()
                avail = screen.availableGeometry() if screen is not None else None
                want_w, want_h = 1280, 1010
                if avail is not None:
                    want_w = min(want_w, max(1200, avail.width() - 40))
                    want_h = min(want_h, max(960, avail.height() - 20))
                self.resize(want_w, want_h)
                self.config = config
                self.backend = backend or GuiBackend(config, log=self.append_log,
                                                     mock=mock_server is not None)
                self.mock_server = mock_server
                self.log_lines = log_lines
                self.workers: list[Any] = []
                self.targets: list[CourseTarget] = list(config.grab.targets)
                self.real_submit_confirmed = False
                self.last_error = ""
                self.font_family = font_family
                self.font_dir = font_dir
                self._build_ui()
                self._wire()
                self._queried_once = False
                #: 是否处于「可以安全发起下一次查询」的状态（worker 结束时置位）
                self._query_idle = True
                self._load_exclude_from_config()
                self.append_log("界面已就绪。先「扫码登录」保存会话，再查询课程、加入目标。")
                self.append_log(f"字体：{font_family or '（未找到中文字体候选，可能显示为方块）'}"
                                f"；字体目录：{font_dir or '（未指定）'}")
                if mock_server is not None:
                    self.append_log(f"离线演示模式：mock 服务已启动于 {mock_server.base_url}")

            # -------------------------------------------------- 构建界面
            def _build_ui(self) -> None:
                central = QtWidgets.QWidget(self)
                self.setCentralWidget(central)
                root = QtWidgets.QVBoxLayout(central)

                # ① 登录与会话
                box_session = QtWidgets.QGroupBox("① 登录与会话")
                s_layout = QtWidgets.QHBoxLayout(box_session)
                self.label_qr = QtWidgets.QLabel("未获取二维码")
                self.label_qr.setObjectName("label_qr")
                # 二维码只在登录时看一次，日志区却是全程都在看 —— 所以把这里收窄，
                # 把高度让给⑤运行日志（用户反馈「日志窗口太小」）。
                # 128px 仍能正常扫码（服务端二维码原图 220x220，等比缩小）。
                self.label_qr.setFixedSize(128, 128)
                self.label_qr.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.label_qr.setFrameShape(QtWidgets.QFrame.Shape.Box)
                s_layout.addWidget(self.label_qr)
                right = QtWidgets.QVBoxLayout()
                self.label_session = QtWidgets.QLabel("会话状态：未检查")
                self.label_session.setObjectName("label_session")
                self.label_session.setWordWrap(True)
                right.addWidget(self.label_session)
                row = QtWidgets.QHBoxLayout()
                self.btn_qr_login = QtWidgets.QPushButton("扫码登录")
                self.btn_session_info = QtWidgets.QPushButton("查看会话")
                self.btn_session_clear = QtWidgets.QPushButton("清除会话")
                for button in (self.btn_qr_login, self.btn_session_info, self.btn_session_clear):
                    button.setObjectName(f"btn_{button.text()}")
                    row.addWidget(button)
                right.addLayout(row)
                right.addStretch(1)
                s_layout.addLayout(right, 1)

                splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
                root.addWidget(splitter, 1)
                # ①区必须也进 splitter：早期它被 root.addWidget 直接挂在根布局上，
                # 于是 splitter 里只有 4 个区（②③④⑤），①区却在外面独占约 250px，
                # 把课程表格挤到只剩 ~50px 高（117 门课看起来「只有一行」）。
                splitter.addWidget(box_session)

                # ② 课程查询
                box_courses = QtWidgets.QGroupBox("② 课程查询")
                c_layout = QtWidgets.QVBoxLayout(box_courses)
                c_row = QtWidgets.QHBoxLayout()
                self.edit_keyword = QtWidgets.QLineEdit()
                self.edit_keyword.setObjectName("edit_keyword")
                self.edit_keyword.setPlaceholderText("课程名/课程号关键字（留空查全部）")
                self.btn_query = QtWidgets.QPushButton("查询")
                self.btn_query.setObjectName("btn_query")
                self.label_query_status = QtWidgets.QLabel("尚未查询")
                self.label_query_status.setObjectName("label_query_status")
                c_row.addWidget(QtWidgets.QLabel("关键字："))
                c_row.addWidget(self.edit_keyword, 1)
                c_row.addWidget(self.btn_query)
                c_row.addWidget(self.label_query_status, 1)
                c_layout.addLayout(c_row)

                # 授课形式过滤（可视化操作，无需命令行参数）
                f_row = QtWidgets.QHBoxLayout()
                self.chk_exclude_offline = QtWidgets.QCheckBox("排除面授课")
                self.chk_exclude_offline.setObjectName("chk_exclude_offline")
                self.chk_exclude_offline.setToolTip(
                    "勾选后从查询结果里滤掉授课形式为「面授课」的课程，只留网络课/慕课等形式。\n"
                    "这是客户端过滤：服务器仍返回全部课程，只影响列表显示与抢课目标匹配。"
                )
                f_row.addWidget(self.chk_exclude_offline)
                f_row.addWidget(QtWidgets.QLabel("自定义排除："))
                self.edit_exclude_attrs = QtWidgets.QLineEdit()
                self.edit_exclude_attrs.setObjectName("edit_exclude_attrs")
                self.edit_exclude_attrs.setPlaceholderText(
                    "按课程属性排除，逗号或空格分隔；如：通识面授课、艺术类")
                self.edit_exclude_attrs.setToolTip(
                    "子串匹配、大小写不敏感。属性原文形如「经济管理类/通识面授课」。\n"
                    "与上面勾选框、以及配置 jwgl.exclude_course_attrs 三者叠加生效。"
                )
                f_row.addWidget(self.edit_exclude_attrs, 1)
                self.chk_persist_exclude = QtWidgets.QCheckBox("保存到 config.yaml")
                self.chk_persist_exclude.setObjectName("chk_persist_exclude")
                self.chk_persist_exclude.setToolTip("把当前排除条件写进 config.yaml，下次打开界面依然生效")
                f_row.addWidget(self.chk_persist_exclude)
                c_layout.addLayout(f_row)
                self.table_courses = QtWidgets.QTableWidget(0, len(COURSE_COLUMNS))
                self.table_courses.setObjectName("table_courses")
                self.table_courses.setHorizontalHeaderLabels(list(COURSE_COLUMNS))
                self.table_courses.setSelectionBehavior(
                    QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
                self.table_courses.setEditTriggers(
                    QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
                self.table_courses.horizontalHeader().setSectionResizeMode(
                    QtWidgets.QHeaderView.ResizeMode.Stretch)
                c_layout.addWidget(self.table_courses, 1)
                splitter.addWidget(box_courses)

                # ③ 抢课目标
                box_targets = QtWidgets.QGroupBox("③ 抢课目标")
                t_layout = QtWidgets.QVBoxLayout(box_targets)
                self.table_targets = QtWidgets.QTableWidget(0, len(TARGET_COLUMNS))
                self.table_targets.setObjectName("table_targets")
                self.table_targets.setHorizontalHeaderLabels(list(TARGET_COLUMNS))
                self.table_targets.setSelectionBehavior(
                    QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
                self.table_targets.horizontalHeader().setSectionResizeMode(
                    QtWidgets.QHeaderView.ResizeMode.Stretch)
                t_layout.addWidget(self.table_targets, 1)
                t_row = QtWidgets.QHBoxLayout()
                self.btn_target_add = QtWidgets.QPushButton("加入抢课目标")
                self.btn_target_add.setObjectName("btn_target_add")
                self.btn_target_append = QtWidgets.QPushButton("手动添加")
                self.btn_target_append.setObjectName("btn_target_append")
                self.btn_target_remove = QtWidgets.QPushButton("删除选中")
                self.btn_target_remove.setObjectName("btn_target_remove")
                self.btn_target_clear = QtWidgets.QPushButton("清空")
                self.btn_target_clear.setObjectName("btn_target_clear")
                self.chk_save_config = QtWidgets.QCheckBox("保存到 config.yaml")
                self.chk_save_config.setObjectName("chk_save_config")
                for widget in (self.btn_target_add, self.btn_target_append,
                               self.btn_target_remove, self.btn_target_clear):
                    t_row.addWidget(widget)
                t_row.addWidget(self.chk_save_config)
                t_row.addStretch(1)
                t_layout.addLayout(t_row)
                splitter.addWidget(box_targets)

                # ④ 抢课控制
                box_control = QtWidgets.QGroupBox("④ 抢课控制")
                g_layout = QtWidgets.QVBoxLayout(box_control)
                mode_row = QtWidgets.QHBoxLayout()
                self.btn_dry_run = QtWidgets.QPushButton("演练（dry-run）")
                self.btn_dry_run.setObjectName("btn_dry_run")
                self.btn_real_submit = QtWidgets.QPushButton("真实提交")
                self.btn_real_submit.setObjectName("btn_real_submit")
                self.label_mode = QtWidgets.QLabel(MODE_DRY_RUN)
                self.label_mode.setObjectName("label_mode")
                mode_row.addWidget(self.btn_dry_run)
                mode_row.addWidget(self.btn_real_submit)
                mode_row.addWidget(self.label_mode, 1)
                g_layout.addLayout(mode_row)
                param_row = QtWidgets.QHBoxLayout()
                self.spin_interval = QtWidgets.QDoubleSpinBox()
                self.spin_interval.setObjectName("spin_interval")
                self.spin_interval.setRange(0.5, 60.0)
                self.spin_interval.setSingleStep(0.5)
                self.spin_interval.setValue(float(config.grab.retry_interval))
                self.spin_attempts = QtWidgets.QSpinBox()
                self.spin_attempts.setObjectName("spin_attempts")
                self.spin_attempts.setRange(1, 9999)
                self.spin_attempts.setValue(int(config.grab.max_attempts))
                self.edit_start_at = QtWidgets.QLineEdit(config.grab.start_at or "")
                self.edit_start_at.setObjectName("edit_start_at")
                self.edit_start_at.setPlaceholderText("卡点开始时间，如 2026-10-14 12:00:00（可留空）")
                param_row.addWidget(QtWidgets.QLabel("间隔(秒)："))
                param_row.addWidget(self.spin_interval)
                param_row.addWidget(QtWidgets.QLabel("最大尝试："))
                param_row.addWidget(self.spin_attempts)
                param_row.addWidget(QtWidgets.QLabel("起抢时间："))
                param_row.addWidget(self.edit_start_at, 1)
                g_layout.addLayout(param_row)
                run_row = QtWidgets.QHBoxLayout()
                self.btn_start = QtWidgets.QPushButton("开始")
                self.btn_start.setObjectName("btn_start")
                self.btn_stop = QtWidgets.QPushButton("停止")
                self.btn_stop.setObjectName("btn_stop")
                self.btn_stop.setEnabled(False)
                run_row.addWidget(self.btn_start)
                run_row.addWidget(self.btn_stop)
                run_row.addStretch(1)
                g_layout.addLayout(run_row)
                splitter.addWidget(box_control)

                # ⑤ 运行日志
                box_log = QtWidgets.QGroupBox("⑤ 运行日志")
                l_layout = QtWidgets.QVBoxLayout(box_log)
                self.log_view = QtWidgets.QPlainTextEdit()
                self.log_view.setObjectName("log_view")
                self.log_view.setReadOnly(True)
                self.log_view.setMaximumBlockCount(self.log_lines)
                # 日志是抢课时盯得最多的地方：给它一个**较高**的下限，
                # 否则会被其它区的最小高度挤到只剩十来行。
                # 为腾出空间，①区的二维码已从 150px 收到 128px（登录时才看一次）。
                # 想要更大可以把分隔条往上拖（splitter 可拖动）。
                self.log_view.setMinimumHeight(300)
                l_layout.addWidget(self.log_view)
                splitter.addWidget(box_log)

                # 五个区的初始高度分配。**必须给满 5 个值**：早期只写了 4 个
                # （[260, 200, 170, 280]），Qt 按比例摊分后第②区「课程查询」只拿到约
                # 174px，再扣掉分组框外壳与关键字行，课程表格只剩 ~90px —— 117 门课
                # 在界面上看起来「只有一行」。
                # 后续反馈「日志窗口太小」，于是把大空间让给要看数据的②区与**⑤日志**，
                # ①区按二维码实际尺寸给固定高度，③④是列表/按钮，小一点够用。
                self._splitter = splitter
                self._splitter_sizes = [180, 300, 150, 165, 430]

                # 多余空间一律给日志区（抢课时最常盯的就是它）
                splitter.setStretchFactor(4, 1)

            # -------------------------------------------------- 授课形式过滤
            def _load_exclude_from_config(self) -> None:
                """把 config 里的排除项回填到界面（勾选框 + 自定义输入框）。

                配置里若含「面授」关键词就勾上「排除面授课」；其余项进自定义输入框。
                """
                patterns = [str(p).strip() for p in (config.jwgl.exclude_course_attrs or [])
                            if str(p).strip()]
                offline = [p for p in patterns if "面授" in p]
                others = [p for p in patterns if p not in offline]
                self.chk_exclude_offline.setChecked(bool(offline))
                self.edit_exclude_attrs.setText("、".join(others))
                if patterns:
                    self.append_log(f"已从配置载入授课形式排除条件：{'、'.join(patterns)}")

            def exclude_patterns(self) -> list[str]:
                """当前界面上的排除项（勾选框 + 自定义输入，去重保序）。"""
                patterns: list[str] = []
                if self.chk_exclude_offline.isChecked():
                    patterns.append(OFFLINE_ATTR_PATTERN)
                # 中英文逗号 / 顿号 / 空格 / 分号都当分隔符
                for piece in re.split(r"[,，、;；\s]+", self.edit_exclude_attrs.text() or ""):
                    piece = piece.strip()
                    if piece and piece not in patterns:
                        patterns.append(piece)
                return patterns

            def on_exclude_changed(self) -> None:
                """勾选框/输入框变化时重新查询（前提是已经成功查过一次）。

                不能用 ``is_busy()`` 当守卫：查询刚填完表格时 worker 还没走到结束，
                此时用户（或初始化回填）改勾选框会被**静默忽略**。改为以
                ``_query_idle`` 为准，由 worker 结束时置位。
                """
                if self._queried_once and self._query_idle:
                    self.on_query()

            def on_persist_exclude(self) -> None:
                """把当前排除条件写进 config.yaml。"""
                patterns = self.exclude_patterns()
                try:
                    saved_to = self.backend.save_exclude_patterns(patterns)
                except Exception as exc:  # noqa: BLE001 - 落盘失败要给出可读提示
                    self.append_log(f"保存排除条件失败：{type(exc).__name__}: {exc}")
                    self.chk_persist_exclude.setChecked(False)
                    return
                if saved_to:
                    self.append_log(
                        f"排除条件已保存到 {saved_to}：{'、'.join(patterns) or '（空，已清除）'}")
                else:
                    self.append_log("当前没有配置文件可写，排除条件仅本次运行有效"
                                    "（需要 config.yaml，可从 config.example.yaml 复制）")

            # -------------------------------------------------- 事件
            def showEvent(self, event: Any) -> None:  # noqa: D102 - Qt 约定
                super().showEvent(event)
                # setSizes 在窗口 show 之前可能被布局覆盖，这里（只在首次显示时）再套一次，
                # 保证「课程查询」表格拿到足够高度、而不是被①区挤成一条。
                if not getattr(self, "_sizes_applied", False):
                    self._sizes_applied = True
                    self._splitter.setSizes(self._splitter_sizes)

            # -------------------------------------------------- 信号
            def _wire(self) -> None:
                self.btn_qr_login.clicked.connect(self.on_qr_login)
                self.btn_session_info.clicked.connect(self.on_session_info)
                self.btn_session_clear.clicked.connect(self.on_session_clear)
                self.btn_query.clicked.connect(self.on_query)
                # 授课形式过滤：勾选框一变就重查；输入框回车/失焦时重查
                self.chk_exclude_offline.toggled.connect(self.on_exclude_changed)
                self.edit_exclude_attrs.returnPressed.connect(self.on_exclude_changed)
                self.edit_exclude_attrs.editingFinished.connect(self.on_exclude_changed)
                self.chk_persist_exclude.toggled.connect(self.on_persist_exclude)
                self.btn_target_add.clicked.connect(self.on_target_from_course)
                self.btn_target_append.clicked.connect(self.on_target_append)
                self.btn_target_remove.clicked.connect(self.on_target_remove)
                self.btn_target_clear.clicked.connect(self.on_target_clear)
                self.btn_dry_run.clicked.connect(lambda: self.set_mode(real=False))
                self.btn_real_submit.clicked.connect(lambda: self.set_mode(real=True))
                self.btn_start.clicked.connect(self.on_start)
                self.btn_stop.clicked.connect(self.on_stop)

            # -------------------------------------------------- 日志与状态
            def append_log(self, text: str) -> None:
                self.log_view.appendPlainText(str(text))

            def set_mode(self, *, real: bool) -> None:
                self.real_submit_confirmed = bool(real)
                self.label_mode.setText(MODE_REAL if real else MODE_DRY_RUN)
                self.append_log("已切换到「真实提交」模式：开始前会弹窗确认" if real
                                else "已切换到「演练」模式：不会发出任何网络请求")

            def set_session_label(self, text: str) -> None:
                self.label_session.setText(text)

            # -------------------------------------------------- 线程工具
            def run_task(self, fn: Callable[..., Any], *, name: str = "任务",
                         on_done: Callable[[Any], None] | None = None,
                         with_helpers: bool = False) -> Any:
                worker = worker_class()(fn if not with_helpers else None, self, name)
                if with_helpers:
                    worker._fn = worker.bind_helpers(fn)  # noqa: SLF001 - 内部装配
                worker.log.connect(self.append_log)
                if on_done is not None:
                    worker.done.connect(on_done)
                worker.done.connect(lambda _result: self._task_finished(worker))
                worker.failed.connect(self._task_failed)
                worker.failed.connect(lambda _msg: self._task_finished(worker))
                worker.cancelled.connect(lambda: self.append_log(f"[{name}] 已停止"))
                worker.cancelled.connect(lambda: self._task_finished(worker))
                worker.qr.connect(self.show_qr)
                self.workers.append(worker)
                worker.start()
                return worker

            def _task_finished(self, worker: Any) -> None:
                self.btn_start.setEnabled(True)
                self.btn_stop.setEnabled(False)
                if getattr(worker, "task_name", "") == "查询课程":
                    self._query_idle = True
                self.append_log(f"[{worker.task_name}] 结束")

            def _task_failed(self, message: str) -> None:
                self.last_error = message
                self._query_idle = True   # 失败也要放行，否则勾选框会一直不生效
                self.append_log(f"出错了：{message}")

            def show_qr(self, image: bytes) -> None:
                pixmap = QtGui.QPixmap()
                if pixmap.loadFromData(image):
                    self.label_qr.setPixmap(pixmap.scaled(
                        self.label_qr.size(),
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation))
                    self.append_log("二维码已显示在窗口左上角，请用微信扫一扫")

            # -------------------------------------------------- 会话动作
            def on_qr_login(self) -> None:
                if self.is_busy():
                    return
                self.append_log("开始扫码登录…")
                self.run_task(
                    lambda *, log, emit_qr, stop_event: self.backend.qr_login(
                        log=log, emit_qr=emit_qr, stop_event=stop_event),
                    name="扫码登录", with_helpers=True, on_done=self._after_login)

            def _after_login(self, result: Any) -> None:
                if isinstance(result, dict) and result.get("ok"):
                    self.set_session_label(
                        f"会话状态：已登录（学号 {result.get('student_id', '')}；"
                        f"{result.get('view', {}).get('expires_at', '')} 过期）")
                else:
                    message = (result or {}).get("message", "登录未成功") if isinstance(result, dict) else "登录未成功"
                    self.set_session_label(f"会话状态：登录失败（{message}）")

            def on_session_info(self) -> None:
                info = self.backend.session_view()
                if not info["ok"]:
                    self.set_session_label(f"会话状态：{info['reason']}")
                    self.append_log(f"查看会话：{info['reason']}")
                    return
                view = info["view"]
                self.set_session_label(
                    f"会话状态：可用（学号 {view.get('student_id') or '未知'}；"
                    f"{view.get('saved_at')} 保存、{view.get('expires_at')} 过期；"
                    f"Cookie：{'、'.join(view.get('cookie_names', []))}）")
                self.append_log("查看会话：缓存可用（Cookie 值不显示）")

            def on_session_clear(self) -> None:
                message = self.backend.clear_session()
                self.set_session_label("会话状态：已清除")
                self.append_log(message)

            # -------------------------------------------------- 查询动作
            def on_query(self) -> None:
                if self.is_busy():
                    return
                keyword = self.edit_keyword.text().strip()
                patterns = self.exclude_patterns()
                self._queried_once = True
                self._query_idle = False
                self.label_query_status.setText("查询中…")
                self.run_task(
                    lambda *, log, stop_event: self.backend.fetch_courses(
                        keyword, log=log, excluded_attrs=patterns),
                    name="查询课程", with_helpers=True, on_done=self._after_query)

            def _after_query(self, courses: Any) -> None:
                self.populate_courses(list(courses or []))

            def populate_courses(self, courses: Sequence[CandidateCourse]) -> None:
                self.table_courses.setRowCount(0)
                for course in courses:
                    row = self.table_courses.rowCount()
                    self.table_courses.insertRow(row)
                    values = (
                        course.teaching_class_id or "-",
                        course.course_code or "-",
                        course.course_name or "-",
                        course.teacher or "-",
                        "" if course.credits is None else f"{course.credits:g}",
                        course.time_text or "-",
                    )
                    for column, value in enumerate(values):
                        self.table_courses.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
                if courses:
                    self.label_query_status.setText(f"共 {len(courses)} 门课程")
                else:
                    self.label_query_status.setText(EMPTY_COURSE_HINT)
                    self.append_log(EMPTY_COURSE_HINT)

            # -------------------------------------------------- 目标动作
            def selected_course(self) -> CandidateCourse | None:
                row = self.table_courses.currentRow()
                if row < 0:
                    return None
                def cell(column: int) -> str:
                    item = self.table_courses.item(row, column)
                    return item.text() if item is not None else ""
                return CandidateCourse(
                    teaching_class_id=cell(0), course_code=cell(1), course_name=cell(2),
                    teacher=cell(3),
                    credits=float(cell(4)) if cell(4) not in ("", "-") else None,
                    time_text=cell(5),
                )

            def add_target(self, target: CourseTarget) -> None:
                self.targets.append(target)
                self.refresh_targets()
                self.append_log(f"已加入目标：{target.course_name or target.course_code}")

            def refresh_targets(self) -> None:
                self.table_targets.setRowCount(0)
                for target in self.targets:
                    row = self.table_targets.rowCount()
                    self.table_targets.insertRow(row)
                    for column, value in enumerate(
                            (target.course_name, target.course_code, target.teacher)):
                        self.table_targets.setItem(row, column, QtWidgets.QTableWidgetItem(value or ""))

            def on_target_from_course(self) -> None:
                course = self.selected_course()
                if course is None:
                    self.append_log("请先在「课程查询」里选中一行，再点「加入抢课目标」")
                    return
                self.add_target(CourseTarget(
                    course_name=course.course_name, course_code=course.course_code,
                    teacher=course.teacher, teaching_class_id=course.teaching_class_id))

            def on_target_append(self) -> None:
                self.add_target(CourseTarget())

            def on_target_remove(self) -> None:
                row = self.table_targets.currentRow()
                if row < 0 or row >= len(self.targets):
                    self.append_log("请先选中要删除的目标行")
                    return
                removed = self.targets.pop(row)
                self.refresh_targets()
                self.append_log(f"已删除目标：{removed.course_name or removed.course_code or '(空目标)'}")

            def on_target_clear(self) -> None:
                self.targets.clear()
                self.refresh_targets()
                self.append_log("已清空全部抢课目标")

            # -------------------------------------------------- 抢课动作
            def is_busy(self) -> bool:
                return any(worker.isRunning() for worker in self.workers)

            def on_start(self) -> None:
                if self.is_busy():
                    self.append_log("已有任务在运行，请先等待或点「停止」")
                    return
                if not self.targets:
                    self.append_log("还没有抢课目标：请先查询课程并「加入抢课目标」")
                    return
                if not self.real_submit_confirmed:
                    # 演练与 `qk run --dry-run` 同口径：只打印计划，**零网络请求**
                    self.print_plan()
                    return
                if not self.confirm_real_submit():
                    self.append_log("已取消：未发出写请求")
                    return
                self.config.grab.retry_interval = float(self.spin_interval.value())
                self.config.grab.max_attempts = int(self.spin_attempts.value())
                start_at = self.edit_start_at.text().strip() or None
                self.btn_start.setEnabled(False)
                self.btn_stop.setEnabled(True)
                self.append_log(f"开始抢课：模式=真实提交，目标 {len(self.targets)} 个")
                self.run_task(
                    lambda *, log, stop_event: self.backend.make_grabber(
                        self.targets, log=log, stop_event=stop_event
                    ).run(dry_run=False, once=False, start_at=start_at),
                    name="抢课", with_helpers=True, on_done=self._after_grab)

            def print_plan(self) -> None:
                """演练：只打印将要做什么，**不联网、不提交**（与 ``qk run --dry-run`` 同口径）。"""
                self.append_log("[dry-run] 演练开始：不会发起任何网络请求，也不会提交选课")
                self.append_log("[dry-run] 模式：演练（真实提交需先切换到真实模式并二次确认）")
                self.append_log(f"[dry-run] 抢课目标（{len(self.targets)} 个）：")
                for target in self.targets:
                    label = target.course_name or target.course_code or target.teaching_class_id or "(空目标)"
                    self.append_log(f"[dry-run]   · {label}（{target.teacher or '不限教师'}）")
                self.append_log(f"[dry-run] 间隔 {self.spin_interval.value():g}s，"
                                f"最大尝试 {self.spin_attempts.value()} 次")
                start_at = self.edit_start_at.text().strip()
                if start_at:
                    self.append_log(f"[dry-run] 起抢时间：{start_at}")
                self.append_log("[dry-run] 演练结束：没有发起任何网络请求，也没有提交选课。")

            def _after_grab(self, report: Any) -> None:
                for target_report in getattr(report, "targets", []) or []:
                    self.append_log(f"目标结果：{target_report.status}（{target_report.message}）")
                self.append_log("抢课结束")

            def confirm_real_submit(self) -> bool:
                """真实提交前的二次确认；默认返回 False（测试可替换）。"""
                listing = "\n".join(
                    f"  · {t.course_name or t.course_code}（{t.teacher or '不限教师'}）"
                    for t in self.targets)
                answer = QtWidgets.QMessageBox.question(
                    self, "确认真实提交",
                    "以下课程将被**真实提交选课**（会改变你的选课结果）：\n"
                    f"{listing}\n\n确认提交吗？",
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                return answer == QtWidgets.QMessageBox.StandardButton.Yes

            def on_stop(self) -> None:
                for worker in self.workers:
                    if worker.isRunning():
                        worker.stop_event.set()
                self.append_log("已请求停止：正在等待当前一轮结束")
                self.btn_stop.setEnabled(False)

            # -------------------------------------------------- 截图
            def render_to(self, path: str | Path) -> str:
                """把窗口渲染成 PNG（离屏模式下可用）。"""
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                pixmap = self.grab()
                if not pixmap.save(str(target)):
                    raise RuntimeError(f"截图保存失败：{target}")
                self.append_log(f"已保存界面截图：{target}")
                return str(target)

        return _Window()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def prepare_fonts() -> str:
    """让 Qt 找得到系统字体，返回实际使用的字体目录（可能是空串）。

    **为什么需要**：离屏平台（``QT_QPA_PLATFORM=offscreen``）默认加载到 **0 个字体族**，
    界面文字会全部渲染成方块（□），截图取证与肉眼检查都会失真。这里在创建
    ``QApplication`` 之前把系统字体目录告诉 Qt（``QT_QPA_FONTDIR``），并随后再挑一个
    存在的中文字体作为应用默认字体。
    """
    if os.environ.get("QT_QPA_FONTDIR"):
        return os.environ["QT_QPA_FONTDIR"]
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/System/Library/Fonts"),
    ]
    for path in candidates:
        if path.is_dir():
            os.environ["QT_QPA_FONTDIR"] = str(path)
            return str(path)
    return ""


#: 「排除面授课」勾选框使用的匹配词。真实课程属性形如 ``经济管理类/通识面授课``，
#: 用子串匹配即可命中所有面授形式（通识面授课、专业面授课…）。
OFFLINE_ATTR_PATTERN = "面授课"


#: 应用默认字体的候选（按优先级；取第一个系统里真的存在的）
FONT_CANDIDATES: tuple[str, ...] = (
    "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun",
    "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC",
)


def apply_ui_font(app: Any, QtGui: Any) -> str:
    """给应用挑一个存在的中文字体；返回选中的字体族名（找不到返回空串）。"""
    families = set(QtGui.QFontDatabase.families())
    for family in FONT_CANDIDATES:
        if family in families:
            app.setFont(QtGui.QFont(family, 10))
            return family
    return ""


def start_mock_backend(config: AppConfig) -> Any:
    """启动内置 mock 服务，并把配置指向它（离线演示用）。"""
    from .mockserver import start_mock_server

    server = start_mock_server()
    base = server.base_url.rstrip("/")
    # 离线演示用**独立的会话文件**：即使别处漏了守卫，也绝不可能覆盖用户真实的
    # qk_state.json（真实事故：演示界面扫码一次，真实 CASTGC 会话被 mock 会话顶掉）。
    config.session.path = f"{config.session.path}.mock"
    config.jwgl.platform = "kingosoft"
    config.jwgl.kingosoft_base_url = base
    config.jwgl.cas_service = f"{base}/caslogin"
    config.cas.login_url = f"{base}/lyuapServer/login"
    return server


def run_gui(config: AppConfig, *, mock: bool = False, screenshot: str | None = None,
            argv: Sequence[str] | None = None) -> int:
    """启动图形界面（阻塞到窗口关闭）；返回进程退出码。"""
    QtCore, QtGui, QtWidgets = load_qt()  # 缺失时抛 QtMissingError，由调用方处理
    prepare_fonts()  # 必须在 QApplication 之前（离屏平台默认没有字体）
    mock_server = start_mock_backend(config) if mock else None
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(list(argv or []))
    apply_ui_font(app, QtGui)
    window = MainWindow(config, mock_server=mock_server)
    window.show()
    app.processEvents()
    if screenshot:
        window.render_to(screenshot)
        window.append_log("已按 --screenshot 要求保存截图，随后退出")
        window.close()
        return 0
    return int(app.exec())
