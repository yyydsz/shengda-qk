"""图形界面的离线取证用例（对应准则 G2）。

全部跑在**自带 mock 服务**上（``qk/mockserver.py``），离屏驱动，不访问真实教务：

* 线程：登录/查询/抢课三条网络路径都必须记录到「非主线程」的线程 id；
* 不冻结：长任务进行中主事件循环仍在处理事件（能及时点到「停止」）；
* 演练零网络：以 **mock 服务端收到的请求数**为判据（不是只看本地状态）；
* 二次确认：取消 → 写请求数 0；确认 → 写请求数 ≥1（正反对照）；
* 停止：运行中点停止后循环退出，此后请求增量为 0；
* 不提供退课入口、不展示未确认的「已选列表」。

没有装 PyQt6 时整个模块 **skip**（保证无 Qt 环境仍全绿）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 PyQt6 之前

import pytest  # noqa: E402

# 可选依赖：**不要**用 importorskip——模块级 importorskip 会让无 Qt 环境「零收集」，
# pytest 因此以退出码 5（no tests collected）结束；而 G3 要求这种环境下
# `pytest qk/tests/test_gui.py -q` 报告 **skipped 且 exit 0**。
# 这里改为「受控导入 + 模块级 skipif」：用例仍会被收集，只是整片标记为跳过。
try:  # pragma: no cover - 取决于环境是否装了可选依赖
    from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402
    QT_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - 任何导入/加载失败都退化为「跳过」
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    QT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

pytestmark = pytest.mark.skipif(
    QtCore is None,
    reason="图形界面用例需要可选依赖 PyQt6（python -m pip install PyQt6）：" + QT_IMPORT_ERROR,
)

from qk.config import CourseTarget  # noqa: E402
from qk.gui import (  # noqa: E402
    COURSE_COLUMNS,
    EMPTY_COURSE_HINT,
    MODE_DRY_RUN,
    MainWindow,
    apply_ui_font,
    prepare_fonts,
)
from qk.mockserver import MockConfig, MockCourse, MockHandler  # noqa: E402
from qk.session_store import SessionStore  # noqa: E402

from .conftest import MOCK_PASSWORD, MOCK_PATHS, MOCK_STUDENT_ID, make_course  # noqa: E402

#: pytest 主线程 id（模块导入发生在主线程；QThread 里的调用必然不同）
MAIN_THREAD_ID = threading.get_ident()

TGT = "TGT-gui-test-1"


class CountingHandler(MockHandler):
    """给 mock 服务端加两个计数器：**总请求数**与**写请求数**。

    不改动 ``qk/mockserver.py``：只把服务器的 ``RequestHandlerClass`` 换成这个子类
    （请求处理时才实例化，因此启动后替换同样生效）。
    """

    WRITE_MARKERS = ("saveElectiveCourse", "cancelElectiveCourse", "xkSelect", "xkDrop")

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        self.server.request_count += 1  # type: ignore[attr-defined]
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        self.server.request_count += 1  # type: ignore[attr-defined]
        if any(marker in self.path for marker in self.WRITE_MARKERS):
            self.server.write_requests += 1  # type: ignore[attr-defined]
        return super().do_POST()


def instrument(server: Any) -> Any:
    """把 mock 服务换成带计数器的处理器并清零计数。"""
    server.RequestHandlerClass = CountingHandler
    server.request_count = 0
    server.write_requests = 0
    return server


@pytest.fixture
def gui_app() -> Any:
    """离屏 QApplication（整个模块共用一个）。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
    prepare_fonts()
    apply_ui_font(app, QtGui)
    return app


def pump(app: Any, predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    """在「持续处理事件」的同时等待条件成立（因此可用来验证界面未冻结）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def make_env(make_mock_server: Any, config_factory: Any, *, kingosoft: bool = True,
             mock_config: MockConfig | None = None, max_attempts: int = 1) -> tuple[Any, Any]:
    """构造「计数 mock + 指向它的配置 + 一份可用会话缓存」。"""
    server = instrument(make_mock_server(mock_config))
    base = server.base_url.rstrip("/")
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    config.cas.login_url = f"{base}/lyuapServer/login"
    config.cas.wechat.poll_interval = 0.01
    config.cas.wechat.poll_timeout = 10.0
    config.grab.retry_interval = 0.05
    config.grab.max_attempts = max_attempts
    if kingosoft:
        config.jwgl.platform = "kingosoft"
        config.jwgl.kingosoft_base_url = base
        config.jwgl.cas_service = f"{base}/caslogin"
        config.jwgl.course_list_path = ""
        config.jwgl.select_path = ""
        config.jwgl.drop_path = ""
        config.jwgl.selected_list_path = ""

    # 预置会话缓存（等价于「已经登录过」）
    store = SessionStore(config.session.path, config.session.max_age_hours)
    if kingosoft:
        # 青果契约：mock 侧登记一个 TGT，把它当作 CASTGC 存进缓存即可
        with server.state.lock:
            server.state.tgt_users[TGT] = config.creds.student_id
        store.save({"CASTGC": TGT}, student_id=config.creds.student_id,
                   service_url=config.jwgl.cas_service, source="wechat")
    else:
        # 通用契约：mock 的 /jwglxt/** 需要真实登录产生的 JSESSIONID，先登录一次再存
        from qk.adapters.cas import CasLoginAdapter
        from qk.net import QkSession

        session = QkSession.from_config(config)
        info = CasLoginAdapter(config, session).login()
        assert info.ok, f"mock 登录失败：{info.message}"
        store.save(info.cookies, student_id=info.student_id or config.creds.student_id,
                   service_url=config.cas.service, source="cas")
    return server, config


@pytest.fixture
def kingosoft_env(make_mock_server: Any, config_factory: Any) -> tuple[Any, Any]:
    """青果契约环境（课程可正常选中）。"""
    return make_env(make_mock_server, config_factory)


@pytest.fixture
def full_course_env(make_mock_server: Any, config_factory: Any) -> tuple[Any, Any]:
    """通用契约 + 一门「余量 1 但服务端总是回满员」的课。

    刻意让**余量非 0**：余量为 0 时抢课器会走本地预检直接跳过，不会发写请求，
    那样「停止」用例会因为任务很快自己结束而空过。
    """
    course = MockCourse("JXBFULL", "91100002", "占位已满课程", "王老师", 1.0, 30, 29,
                        "周一 1-2", "full")
    return make_env(make_mock_server, config_factory, kingosoft=False,
                    mock_config=MockConfig.default(student_id=MOCK_STUDENT_ID,
                                                  password=MOCK_PASSWORD, courses=[course]),
                    max_attempts=9999)


@pytest.fixture
def json_env(make_mock_server: Any, config_factory: Any) -> tuple[Any, Any]:
    """通用契约（JSON）环境 + 默认课程表：中文课程名不会受 mock 的 GBK 声明影响。

    用于产出 README 里引用的入库截图（见本文件末尾的可重放用例）。
    """
    return make_env(make_mock_server, config_factory, kingosoft=False)


@pytest.fixture
def window(gui_app: Any):
    """按需构造窗口的工厂（每个用例独立建窗、结束即关闭）。"""
    created: list[Any] = []

    def factory(config: Any) -> Any:
        win = MainWindow(config)
        win.show()
        gui_app.processEvents()
        created.append(win)
        return win

    yield factory
    for win in created:
        win.close()
    gui_app.processEvents()


# --------------------------------------------------------------------------
# ① 三条网络路径都在后台线程
# --------------------------------------------------------------------------


def test_network_paths_run_off_main_thread(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """登录、课程查询、抢课（含 Grabber 循环内）都必须不在主线程执行。"""
    server, config = kingosoft_env
    win = window(config)

    # --- 登录（真实走 mock 的扫码接口）---
    win.on_qr_login()
    assert pump(gui_app, lambda: bool(win.backend.thread_trace["login"]), timeout=10), "登录未执行"
    assert pump(gui_app, lambda: not win.is_busy(), timeout=15), "登录任务未结束"
    login_thread = win.backend.thread_trace["login"][0]
    assert login_thread != MAIN_THREAD_ID, "登录跑在主线程上"

    # --- 课程查询 ---
    win.on_query()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0, timeout=15), "查询没有结果"
    query_threads = win.backend.thread_trace["query"]
    assert query_threads and all(tid != MAIN_THREAD_ID for tid in query_threads), "查询跑在主线程上"

    # --- 抢课（真实提交路径）---
    win.targets = [CourseTarget(course_name="占位课程", course_code="", teacher="")]
    win.real_submit_confirmed = True
    win.confirm_real_submit = lambda: True  # 用例内直接确认
    win.on_start()
    assert pump(gui_app, lambda: bool(win.backend.thread_trace["grab"]), timeout=15), "抢课未执行"
    assert pump(gui_app, lambda: not win.is_busy(), timeout=20), "抢课任务未结束"
    grab_threads = win.backend.thread_trace["grab"]
    assert grab_threads and all(tid != MAIN_THREAD_ID for tid in grab_threads), "抢课跑在主线程上"

    print(f"主线程={MAIN_THREAD_ID} 登录={login_thread} 查询={set(query_threads)} 抢课={set(grab_threads)}")
    print(f"mock 请求数={server.request_count} 写请求数={server.write_requests}")


# --------------------------------------------------------------------------
# ② 长任务中界面不冻结  + ⑥ 停止能中断循环
# --------------------------------------------------------------------------


def test_gui_stays_responsive_and_stop_interrupts_loop(
        gui_app: Any, full_course_env: Any, window: Any) -> None:
    """长任务运行中主事件循环仍在处理事件；点「停止」后循环退出且不再发请求。"""
    server, config = full_course_env
    win = window(config)

    ticks: list[float] = []
    QtCore.QTimer.singleShot(0, lambda: ticks.append(time.monotonic()))  # 事件循环是否还在跑

    win.spin_interval.setValue(5.0)     # 每轮之间睡 5 秒 → 点停顿时任务必然仍在运行
    win.spin_attempts.setValue(60)
    win.targets = [CourseTarget(course_name="占位已满课程", course_code="91100002", teacher="王老师")]
    win.real_submit_confirmed = True
    win.confirm_real_submit = lambda: True
    win.on_start()

    assert pump(gui_app, lambda: len(ticks) > 0, timeout=5), "任务期间事件循环没有处理事件"
    assert pump(gui_app, lambda: server.write_requests >= 2, timeout=20), (
        f"抢课没有持续重试（写请求数={server.write_requests}）")
    assert win.is_busy(), "点停止前任务已经自己结束，本用例会空过"

    win.on_stop()
    assert pump(gui_app, lambda: not win.is_busy(), timeout=15), "点了停止但任务没退出"
    settled = server.request_count
    time.sleep(0.4)
    gui_app.processEvents()
    assert server.request_count == settled, "停止之后仍在发请求"
    print(f"事件循环处理了 {len(ticks)} 次定时器；停止后请求数固定在 {settled}")


# --------------------------------------------------------------------------
# ③④⑤ 演练零网络 / 二次确认的取消与确认
# --------------------------------------------------------------------------


def test_dry_run_sends_zero_requests(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """演练：不创建任何网络任务，mock 服务端收到的请求数为 0。"""
    server, config = kingosoft_env
    win = window(config)
    win.targets = [CourseTarget(course_name="占位课程")]
    before = server.request_count

    win.on_start()  # 默认演练模式

    gui_app.processEvents()
    assert not win.is_busy(), "演练不应启动后台任务"
    assert server.request_count == before, "演练期间发出了网络请求"
    log_text = win.log_view.toPlainText()
    assert "[dry-run] 演练结束：没有发起任何网络请求，也没有提交选课。" in log_text
    print(f"演练前后 mock 请求数：{before} → {server.request_count}")


def test_cancel_confirmation_sends_no_write(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """真实模式下取消二次确认：不发写请求，日志写明「已取消：未发出写请求」。"""
    server, config = kingosoft_env
    win = window(config)
    win.targets = [CourseTarget(course_name="占位课程")]
    win.real_submit_confirmed = True           # 切到真实提交模式
    win.confirm_real_submit = lambda: False    # 用户在弹窗里点了「取消」
    before_write, before_all = server.write_requests, server.request_count

    win.on_start()
    gui_app.processEvents()

    assert not win.is_busy(), "取消确认后不应启动任务"
    assert server.write_requests == before_write, "取消确认却发了写请求"
    assert server.request_count == before_all, "取消确认却发了请求"
    assert "已取消：未发出写请求" in win.log_view.toPlainText()
    print(f"取消后写请求数仍为 {server.write_requests}")


def test_confirm_sends_write_request(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """正向对照：确认提交后 mock 端确实收到写请求（否则「永远不发」也能过）。"""
    server, config = kingosoft_env
    win = window(config)

    win.on_query()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0, timeout=15), "查询没有结果"
    row_cells = [win.table_courses.item(0, column).text()
                 for column in range(win.table_courses.columnCount())]
    assert len(row_cells) == len(COURSE_COLUMNS)
    win.table_courses.setCurrentCell(0, 0)
    win.on_target_from_course()               # 一键加入目标（真实课程名/教师）
    assert win.targets, "一键加入目标失败"

    win.real_submit_confirmed = True
    win.confirm_real_submit = lambda: True
    before_write = server.write_requests

    win.on_start()
    assert pump(gui_app, lambda: not win.is_busy(), timeout=20), "抢课任务未结束"

    assert server.write_requests > before_write, "确认提交后 mock 端没有收到写请求"
    print(f"确认提交后写请求数：{before_write} → {server.write_requests}")
    print("日志尾部：", win.log_view.toPlainText().splitlines()[-3:])


# --------------------------------------------------------------------------
# ⑦ 目标管理  +  ⑧ 不提供退课入口 / 不展示未确认的已选列表
# --------------------------------------------------------------------------


def test_target_management_add_remove_clear(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """目标表格：一键加入、手动添加、删除选中、清空。"""
    _server, config = kingosoft_env
    win = window(config)

    win.on_query()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0, timeout=15)
    win.table_courses.setCurrentCell(0, 0)
    win.on_target_from_course()
    assert len(win.targets) == 1 and win.table_targets.rowCount() == 1
    assert win.targets[0].course_name, "一键加入应带上课程名"

    win.on_target_append()                     # 手动添加一个空目标
    assert len(win.targets) == 2 and win.table_targets.rowCount() == 2
    win.table_targets.setCurrentCell(1, 0)
    win.on_target_remove()
    assert len(win.targets) == 1 and win.table_targets.rowCount() == 1
    win.on_target_clear()
    assert win.targets == [] and win.table_targets.rowCount() == 0
    print("目标增删清空均正确")


def test_no_drop_entry_and_no_selected_list(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """界面不提供退课写入入口，也不展示未确认的「已选记录」控件/标签。"""
    _server, config = kingosoft_env
    win = window(config)

    forbidden = ("退课", "已选记录", "已选课程", "已选列表")
    hits: list[str] = []
    for widget in win.findChildren(QtWidgets.QWidget):
        if widget is win.log_view:
            continue  # 日志里可能出现这些词（例如提示语），不算控件
        text = ""
        for getter in ("text", "title"):
            method = getattr(widget, getter, None)
            if callable(method):
                try:
                    text = str(method())
                except TypeError:  # pragma: no cover - 需要参数的 text()
                    text = ""
                if text:
                    break
        if any(word in text for word in forbidden):
            hits.append(f"{type(widget).__name__}:{text}")
    assert not hits, f"界面出现了退课/已选相关控件：{hits}"
    assert win.label_mode.text() in (MODE_DRY_RUN, "当前模式：真实提交（提交前需二次确认）")
    print("未发现退课入口或未确认的已选列表控件")


# --------------------------------------------------------------------------
# 终局交互取证：真实按钮 + 真实 QMessageBox（不替换任何桩）
# --------------------------------------------------------------------------


def schedule_dialog_answer(app: Any, standard_button: Any, *, timeout: float = 10.0) -> dict[str, Any]:
    """等真实模态对话框弹出后点它的指定标准按钮（**不替换** ``confirm_real_submit``）。

    ``QMessageBox.question`` 会阻塞在它自己的事件循环里，因此用 ``QTimer`` 在循环内部
    点按钮；用标准按钮枚举定位，不受界面语言影响。
    """
    state: dict[str, Any] = {"clicked": False}

    def act() -> None:
        dialog = app.activeModalWidget()
        if dialog is None:
            if not state["clicked"]:
                QtCore.QTimer.singleShot(10, act)  # 对话框还没弹出来，稍后再试
            return
        assert isinstance(dialog, QtWidgets.QMessageBox), f"弹出的不是 QMessageBox：{dialog}"
        button = dialog.button(standard_button)
        assert button is not None, f"对话框里没有 {standard_button}"
        state["clicked"] = True
        button.click()

    QtCore.QTimer.singleShot(0, act)
    return state


def test_real_button_and_real_dialog_control_writes(
        gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """用**真实按钮**与**真实确认对话框**走完：取消 → 0 写请求；确认 → ≥1 写请求。"""
    server, config = kingosoft_env
    win = window(config)

    # 真实按钮：查询 → 选中第一行 → 一键加入目标
    win.btn_query.click()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0, timeout=15), "查询没有结果"
    win.table_courses.setCurrentCell(0, 0)
    win.btn_target_add.click()
    assert win.targets, "一键加入目标失败"

    # 真实按钮：切到「真实提交」模式
    win.btn_real_submit.click()
    assert win.real_submit_confirmed is True
    assert "真实提交" in win.label_mode.text()

    # ① 对话框点「No」→ 不发出写请求
    before = server.write_requests
    answered = schedule_dialog_answer(gui_app, QtWidgets.QMessageBox.StandardButton.No)
    win.btn_start.click()
    assert pump(gui_app, lambda: "已取消：未发出写请求" in win.log_view.toPlainText(),
                timeout=10), "取消后日志没有如实写明"
    assert answered["clicked"], "确认对话框没有被点到（用例未真正驱动对话框）"
    assert server.write_requests == before, "对话框点取消却发了写请求"
    print(f"真实对话框取消：写请求数保持 {server.write_requests}")

    # ② 对话框点「Yes」→ 确实发出写请求
    answered = schedule_dialog_answer(gui_app, QtWidgets.QMessageBox.StandardButton.Yes)
    win.btn_start.click()
    assert pump(gui_app, lambda: not win.is_busy(), timeout=25), "抢课任务未结束"
    assert answered["clicked"], "确认对话框没有被点到"
    assert server.write_requests > before, "对话框点确认后 mock 端没有收到写请求"
    print(f"真实对话框确认：写请求数 {before} → {server.write_requests}")


def test_stop_click_takes_effect_promptly(gui_app: Any, full_course_env: Any, window: Any) -> None:
    """操作进行中点真实「停止」按钮：**时延有上限**，且此后不再发请求。"""
    server, _config = full_course_env
    win = window(config=_config)
    win.spin_interval.setValue(5.0)          # 界面上的重试间隔（秒）
    win.spin_attempts.setValue(60)
    win.targets = [CourseTarget(course_name="占位已满课程", course_code="91100002", teacher="王老师")]
    win.btn_real_submit.click()
    win.confirm_real_submit = lambda: True   # 本用例只测停止；对话框联动由上一条用例覆盖
    win.btn_start.click()
    assert pump(gui_app, lambda: server.write_requests >= 2, timeout=20), "抢课没有进入重试循环"
    assert win.is_busy(), "点停止前任务已经自己结束，本用例会空过"
    writes_before = server.write_requests

    started = time.monotonic()
    win.btn_stop.click()                     # 真实按钮
    assert pump(gui_app, lambda: not win.is_busy(), timeout=10), "点了停止但任务没退出"
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"停止用了 {elapsed:.2f}s（上限 3s，重试间隔 5s）"

    settled = server.request_count
    time.sleep(0.4)
    gui_app.processEvents()
    assert server.request_count == settled, "停止之后仍在发请求"
    print(f"点停止到任务退出用了 {elapsed:.2f}s（重试间隔 5s）；"
          f"停止前写请求 {writes_before} 次；此后请求数固定在 {settled}")


# --------------------------------------------------------------------------
# 入库截图的可重放产出（README「图形界面（可选）」引用的 docs/gui-*.png）
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_doc_screenshots_are_reproducible(
        gui_app: Any, json_env: Any, window: Any, tmp_path: Path) -> None:
    """产出 README 引用的三张界面截图——**这就是那些图的来源，可重放**。

    * 默认写临时目录（不污染仓库，CI 友好）；
    * 设 ``QK_WRITE_DOC_SHOTS=1`` 时写入仓库 ``docs/``，用于刷新入库截图。

    重放命令：

    ```bash
    QT_QPA_PLATFORM=offscreen QK_WRITE_DOC_SHOTS=1 python -m pytest qk/tests/test_gui.py -q -k doc_screenshots
    ```
    """
    from qk.gui import run_gui

    _server, config = json_env
    if os.environ.get("QK_WRITE_DOC_SHOTS"):
        out_dir = REPO_ROOT / "docs"
    else:
        out_dir = Path(tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ① 初始界面：经**真实入口** run_gui（与 `python -m qk gui --screenshot` 同一函数）渲染
    assert run_gui(config, screenshot=str(out_dir / "gui-main.png")) == 0

    # ② 查询出课程 → 一键加入两个目标 → 演练（只打印计划，零网络）
    win = window(config)
    win.resize(1180, 900)
    gui_app.processEvents()
    win.btn_query.click()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0, timeout=15), "查询没有结果"
    for row in (0, 1):
        win.table_courses.setCurrentCell(row, 0)
        win.btn_target_add.click()
    win.btn_dry_run.click()
    win.btn_start.click()
    gui_app.processEvents()
    win.render_to(out_dir / "gui-courses.png")

    # ③ 查一个不存在的关键字 → 空态提示
    win.edit_keyword.setText("不存在的课程ZZZ")
    win.btn_query.click()
    assert pump(gui_app, lambda: EMPTY_COURSE_HINT in win.label_query_status.text()), "空态提示没有出现"
    win.render_to(out_dir / "gui-empty.png")

    for name in ("gui-main.png", "gui-courses.png", "gui-empty.png"):
        path = out_dir / name
        assert path.is_file() and path.stat().st_size > 1000, f"{name} 没生成或过小"
    print(f"已产出截图到 {out_dir}："
          + "、".join(f"{n}={ (out_dir / n).stat().st_size }B"
                     for n in ("gui-main.png", "gui-courses.png", "gui-empty.png")))


# ==========================================================================
# mock 模式不得写会话文件（回归用例）
#
# 实测事故：在 `qk gui --mock` 离线演示界面里扫码一次，会话被写进真实的
# qk_state.json，把用户真实的 CASTGC 会话覆盖成了指向 127.0.0.1 的 mock 会话，
# 导致随后的真实查询一律报「凭证已失效」。
# ==========================================================================


class _FakeSessionInfo:
    """替身：只提供 GuiBackend.qr_login 需要的字段。"""

    ok = True
    student_id = MOCK_STUDENT_ID
    cookies = {"CASTGC": "TGT-fake-1"}
    message = ""


@pytest.mark.parametrize("mock_mode", [True, False])
def test_qr_login_only_writes_session_file_when_not_mock(
    mock_mode: bool, workspace: Path, config_factory: Any, make_mock_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mock=True`` 时不落盘；``mock=False``（真实使用）时照旧保存。"""
    from qk import gui as gui_module
    from qk.gui import GuiBackend

    server = make_mock_server()
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    # 关键：把会话文件重定向到临时工作区。默认 path 是仓库根目录的 qk_state.json，
    # 测试若直接用它就会动到开发者的真实会话（本用例的失败教训）。
    config.session.path = str(workspace / "qk_state.json")
    store = SessionStore(config.session.path, config.session.max_age_hours)
    assert store.load() is None, "前置条件：会话文件应不存在"

    # 避免真的去连 CAS：让适配器的 login 直接返回一份假会话
    monkeypatch.setattr(gui_module.WeChatQrLoginAdapter, "login",
                        lambda self, **kwargs: _FakeSessionInfo())

    backend = GuiBackend(config, log=lambda _m: None, mock=mock_mode)
    result = backend.qr_login(log=lambda _m: None, emit_qr=lambda _b: None,
                              stop_event=threading.Event())

    assert result["ok"] is True
    assert result["saved"] is (not mock_mode)
    cached = store.load()
    if mock_mode:
        assert cached is None, "mock 模式不应写出会话文件（会覆盖真实会话）"
        assert result["view"] == {}
    else:
        assert cached is not None, "非 mock 模式应正常保存会话"
        assert "CASTGC" in cached.cookies


def test_start_mock_backend_uses_separate_session_file(
    workspace: Path, config_factory: Any, make_mock_server: Any,
) -> None:
    """``--mock`` 必须换用独立的会话文件，从物理上不可能覆盖真实 qk_state.json。

    真实事故：演示界面扫码一次，真实 CASTGC 会话被 mock 会话（service_url 指向
    127.0.0.1）顶掉，随后所有真实查询都报「凭证已失效」。
    """
    from qk.gui import start_mock_backend

    server = make_mock_server()
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    config.session.path = str(workspace / "qk_state.json")
    original_path = config.session.path

    mock_server = start_mock_backend(config)
    try:
        assert config.session.path != original_path, "mock 模式不应沿用真实会话文件"
        assert config.session.path.endswith(".mock")
        # mock 后端确实把基址指向了本地 mock
        assert str(mock_server.base_url).rstrip("/") in config.jwgl.kingosoft_base_url
    finally:
        mock_server.stop()


# ==========================================================================
# 授课形式过滤的可视化操作（勾选框 + 自定义输入 + 保存到配置）
# ==========================================================================


def test_exclude_controls_default_off(gui_app: Any, json_env: Any, window: Any) -> None:
    """默认不排除任何课程：勾选框关、输入框空、exclude_patterns 为空。"""
    _server, config = json_env
    assert config.jwgl.exclude_course_attrs == []
    win = window(config)
    assert win.chk_exclude_offline.isChecked() is False
    assert win.edit_exclude_attrs.text() == ""
    assert win.exclude_patterns() == []


def test_exclude_patterns_combines_checkbox_and_custom_input(
    gui_app: Any, json_env: Any, window: Any
) -> None:
    """勾选框与自定义输入合并去重；分隔符支持中英文逗号/顿号/分号/空格。"""
    _server, config = json_env
    win = window(config)
    win.chk_exclude_offline.setChecked(True)
    win.edit_exclude_attrs.setText("艺术类，语言文学类、体育专项类;国际化类 面授课")
    patterns = win.exclude_patterns()
    assert patterns[0] == "面授课"          # 勾选框优先
    assert "艺术类" in patterns and "国际化类" in patterns
    assert patterns.count("面授课") == 1     # 与勾选框重复项应去重
    assert len(patterns) == 5


def test_exclude_from_config_is_reflected_in_ui(gui_app: Any, json_env: Any, window: Any) -> None:
    """配置里的排除项要回填到界面：含「面授」的进勾选框，其余进自定义输入框。"""
    _server, config = json_env
    config.jwgl.exclude_course_attrs = ["面授课", "艺术类"]
    win = window(config)
    assert win.chk_exclude_offline.isChecked() is True
    assert win.edit_exclude_attrs.text() == "艺术类"


def test_gui_query_applies_ui_exclusion(gui_app: Any, kingosoft_env: Any, window: Any) -> None:
    """界面上的排除条件必须真的作用到查询结果（离线 mock）。

    用 ``kingosoft_env``：属性过滤依赖响应里的 ``course_attr``，只有青果 HTML
    表格路径会提供它（通用 JSON 路径没有该字段，过滤自然不适用）。
    也**不要**拿「面授课」去断言 mock 的课会被滤掉 —— mock 的属性是
    「通识选修课」，那种断言是错的。这里测的是通用契约。
    """
    from qk.adapters.base import course_attr_of

    _server, config = kingosoft_env
    win = window(config)

    win.edit_keyword.setText("")
    win.btn_query.click()
    assert pump(gui_app, lambda: win.table_courses.rowCount() > 0), "基线查询应有课程"

    # 后端返回的属性值（比通过界面反射更直接，也避免依赖列顺序）
    sample = win.backend.fetch_courses("", log=lambda _m: None)
    assert sample, "前置：应能取到课程"
    attr = course_attr_of(sample[0])
    assert attr, "前置：青果 mock 课程应带课程属性"

    # 1) 排除命中属性 → 结果应为空
    narrowed = win.backend.fetch_courses("", log=lambda _m: None, excluded_attrs=[attr])
    assert narrowed == [], f"排除 {attr!r} 后应无剩余课程"

    # 2) 排除不存在的属性 → 数量不变
    untouched = win.backend.fetch_courses("", log=lambda _m: None,
                                          excluded_attrs=["绝不存在的属性ZZZ"])
    assert len(untouched) == len(sample)

    # 3) 界面输入框填入命中属性后重查 → 表格应为空
    win.edit_exclude_attrs.setText(attr)
    win.on_query()
    assert pump(gui_app, lambda: win.table_courses.rowCount() == 0, timeout=15.0), (
        f"界面排除 {attr!r} 后表格应为空，实际 {win.table_courses.rowCount()} 行"
    )


def test_save_exclude_patterns_writes_and_roundtrips(
    gui_app: Any, json_env: Any, window: Any
) -> None:
    """「保存到 config.yaml」应真正落盘，且能被重新加载读回、平台不回落。"""
    from qk.config import load_config

    _server, config = json_env
    assert config.config_path, "前置：夹具应带 config_path"
    win = window(config)
    win.chk_exclude_offline.setChecked(True)

    saved_to = win.backend.save_exclude_patterns(win.exclude_patterns())
    assert saved_to == config.config_path
    assert Path(saved_to).is_file()

    reloaded = load_config(saved_to, environ={}, workspace=Path(saved_to).parent)
    assert list(reloaded.jwgl.exclude_course_attrs) == ["面授课"]
    # 关键：往返后平台不能被回落成 auto（那会丢失青果适配器）
    assert reloaded.jwgl.platform == config.jwgl.platform


def test_save_exclude_patterns_refuses_without_jwgl_section(
    workspace: Path, config_factory: Any, make_mock_server: Any
) -> None:
    """配置里没有 jwgl 小节时必须拒绝写入（否则 platform 会回落成 auto）。

    直接测 ``GuiBackend``：落盘逻辑不依赖窗口，用真实实现而不是替身。
    """
    from qk.config import ConfigError
    from qk.gui import GuiBackend

    server = make_mock_server()
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    bare = workspace / "bare.yaml"
    bare.write_text("http:\n  timeout: 5.0\n", encoding="utf-8")
    config.config_path = str(bare)
    before = bare.read_text(encoding="utf-8")

    backend = GuiBackend(config, log=lambda _m: None)
    with pytest.raises(ConfigError) as excinfo:
        backend.save_exclude_patterns(["面授课"])
    assert "jwgl" in str(excinfo.value)
    assert bare.read_text(encoding="utf-8") == before, "拒绝时不得改动文件"


def test_save_exclude_patterns_returns_empty_without_config_file(
    workspace: Path, config_factory: Any, make_mock_server: Any
) -> None:
    """没有配置文件时返回空串（由界面提示「仅本次运行有效」），绝不擅自新建文件。"""
    from qk.gui import GuiBackend

    server = make_mock_server()
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    config.config_path = ""
    backend = GuiBackend(config, log=lambda _m: None)
    assert backend.save_exclude_patterns(["面授课"]) == ""


# ==========================================================================
# 布局回归：五个区都必须进 splitter，且课程表格要有可用高度
#
# 实测事故：①「登录与会话」区曾用 root.addWidget 直接挂在根布局上（不在 splitter 内），
# 于是它独占约 250px、splitter 只剩 4 个区；再叠加 1180x820 的默认窗口，
# 「② 课程查询」表格只分到约 50px —— 117 门课在界面上看起来「只有一行」。
# ==========================================================================


def test_splitter_holds_all_five_sections(gui_app: Any, json_env: Any, window: Any) -> None:
    """splitter 必须含 5 个区；少一个就说明有区被挂到根布局上去了。"""
    _server, config = json_env   # make_env 返回 (server, config)
    win = window(config)
    assert win._splitter.count() == 5, (
        f"splitter 只装了 {win._splitter.count()} 个区（应为 5）："
        "检查是否有区被 root.addWidget 挂到根布局"
    )
    # setSizes 的目标值个数也要匹配，否则 Qt 按比例摊分、某些区被压扁
    assert len(win._splitter_sizes) == 5


def _prod_size(win: Any, gui_app: Any) -> None:
    """把窗口调到**生产尺寸**再量布局。

    offscreen 平台的虚拟屏幕只有约 800x600，MainWindow 会退到自己的尺寸下限
    （960 高），这不是用户实际看到的布局 —— Windows 上屏幕可用约 1032 高，
    窗口取 1280x1010。布局断言必须在这个尺寸下做才有意义。
    """
    win.resize(1280, 1010)
    gui_app.processEvents()
    win._splitter.setSizes(win._splitter_sizes)
    gui_app.processEvents()


def test_course_table_has_usable_height(gui_app: Any, json_env: Any, window: Any) -> None:
    """课程表格的可视高度至少要能显示 3 行，否则查询结果「看不见」。"""
    _server, config = json_env   # make_env 返回 (server, config)
    win = window(config)
    _prod_size(win, gui_app)
    win.populate_courses([make_course(f"JXB{i:03d}") for i in range(30)])
    gui_app.processEvents()
    viewport = win.table_courses.viewport().height()
    row_height = win.table_courses.rowHeight(0) or 1
    visible_rows = viewport // row_height
    assert visible_rows >= 3, (
        f"课程表格只能显示 {visible_rows} 行（viewport={viewport}px、行高={row_height}px）；"
        "布局把表格压得太扁了"
    )


def test_log_view_has_usable_height(gui_app: Any, json_env: Any, window: Any) -> None:
    """⑤ 运行日志的可视高度要能看清多行 —— 抢课时主要盯的就是它。

    反馈历史：日志区最初只分到约 200px（约 6 行），先后被压缩到看不清
    「目标结果：…」这类关键行。现在的约定：

    * splitter 的第 5 区拿到**最大**的初始份额（多余空间也全给它）；
    * 日志控件本身设了最小高度下限，不会被别的区挤没。
    """
    _server, config = json_env   # make_env 返回 (server, config)
    win = window(config)
    _prod_size(win, gui_app)
    for i in range(40):
        win.append_log(f"第 {i} 行日志：目标结果：SUCCESS（占位，用于量高度）")
    gui_app.processEvents()

    viewport = win.log_view.viewport().height()
    line_height = win.log_view.fontMetrics().lineSpacing() or 1
    visible_lines = viewport // line_height

    # 生产尺寸下应能看清 15 行以上（日志行较长，会折行，实际能看的条数更少）
    assert visible_lines >= 15, (
        f"日志区只能显示 {visible_lines} 行（viewport={viewport}px、行高={line_height}px）；"
        "日志是抢课时最常看的地方，不该被压这么扁"
    )
    # 初始份额里日志必须是最大的一区
    assert win._splitter_sizes[4] == max(win._splitter_sizes), (
        f"⑤日志区初始份额不是最大：{win._splitter_sizes}"
    )
    # 控件自身有下限，避免被挤没
    assert win.log_view.minimumHeight() >= 200, (
        f"日志控件最小高度过低：{win.log_view.minimumHeight()}px"
    )


# ==========================================================================
# 「只抢网课（非面授）」一键模式的界面控件
#
# 需求：不再需要「先点查询 → 再加目标 → 再开始」。勾上这个框、点开始，
# 工具自己查询、自己筛掉面授课、自己提交。
# ==========================================================================


def test_online_only_controls_default_off(gui_app: Any, json_env: Any, window: Any) -> None:
    """默认不开：仍按③里的目标抢，最多抢门数默认 2。"""
    _server, config = json_env
    win = window(config)
    assert win.chk_online_only.isChecked() is False
    assert win.spin_max_courses.value() == 2
    assert win.config.grab.online_only is False


def test_online_only_toggle_updates_config(gui_app: Any, json_env: Any, window: Any) -> None:
    """勾选/取消要同步到 config，并在日志里说清当前是哪种模式。"""
    _server, config = json_env
    win = window(config)
    win.chk_online_only.setChecked(True)
    gui_app.processEvents()
    assert win.config.grab.online_only is True
    assert "只抢网课" in win.log_view.toPlainText()

    win.spin_max_courses.setValue(3)
    gui_app.processEvents()
    assert win.config.grab.max_courses == 3

    win.chk_online_only.setChecked(False)
    gui_app.processEvents()
    assert win.config.grab.online_only is False
    assert "已切回" in win.log_view.toPlainText()


def test_online_only_reflected_from_config(gui_app: Any, json_env: Any, window: Any) -> None:
    """配置里已开启时，界面打开就要是勾上的状态。"""
    _server, config = json_env
    config.grab.online_only = True
    config.grab.max_courses = 4
    win = window(config)
    assert win.chk_online_only.isChecked() is True
    assert win.spin_max_courses.value() == 4


def test_start_without_targets_is_allowed_in_online_only(
    gui_app: Any, json_env: Any, window: Any
) -> None:
    """核心行为：**没有目标**时，勾了「只抢网课」就不该再拦着不让开始。

    旧行为是无条件提示「还没有抢课目标：请先查询课程并加入抢课目标」，
    这正好是用户抱怨的那一步多余操作。
    """
    _server, config = json_env
    win = window(config)
    assert win.targets == []

    win.chk_online_only.setChecked(True)
    win.on_start()                       # 演练模式（默认）→ 只打印计划
    gui_app.processEvents()
    log = win.log_view.toPlainText()
    assert "还没有抢课目标" not in log, "勾了「只抢网课」就不该再要求先加目标"
    assert "只抢网课（非面授）" in log


def test_start_without_targets_still_blocked_when_online_only_off(
    gui_app: Any, json_env: Any, window: Any
) -> None:
    """没勾「只抢网课」时保持原行为：没有目标就提示去加目标。"""
    _server, config = json_env
    win = window(config)
    win.on_start()
    gui_app.processEvents()
    assert "还没有抢课目标" in win.log_view.toPlainText()


def test_dry_run_plan_explains_online_only_flow(gui_app: Any, json_env: Any, window: Any) -> None:
    """演练计划要说清「会自动查询 → 排除面授 → 最多抢几门」，且**零网络请求**。"""
    _server, config = json_env
    win = window(config)
    win.chk_online_only.setChecked(True)
    win.spin_max_courses.setValue(2)
    win.log_view.clear()
    win.on_start()
    gui_app.processEvents()
    log = win.log_view.toPlainText()
    assert "自动查询" in log
    assert "面授" in log
    assert "最多抢 2 门" in log
    assert "没有发起任何网络请求" in log
