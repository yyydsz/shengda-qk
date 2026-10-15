"""抢课编排测试：候选匹配、起抢时间、重试、已选跳过、会话重登、退课分支。

匹配类测试是纯函数；编排类测试全部对着 qk/mockserver.py 真跑，不联网。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from qk.config import CourseTarget, GrabConfig
from qk.grabber import Grabber, find_matches, matches_target, parse_start_time
from qk.mockserver import MockConfig
from qk.models import CandidateCourse, Outcome, SelectionResult

from .conftest import MOCK_PATHS, make_course


# ==========================================================================
# 纯函数：时间解析与候选匹配
# ==========================================================================


def test_parse_start_time_empty_means_now():
    assert parse_start_time("") is None
    assert parse_start_time("   ") is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-10-13 12:00:00", datetime(2026, 10, 13, 12, 0, 0)),
        ("2026-10-13 12:00", datetime(2026, 10, 13, 12, 0)),
        ("2026-10-13T12:00", datetime(2026, 10, 13, 12, 0)),
        ("2026/10/13 12:00:00", datetime(2026, 10, 13, 12, 0, 0)),
        ("2026-10-13", datetime(2026, 10, 13, 0, 0, 0)),
    ],
)
def test_parse_start_time_formats(text, expected):
    assert parse_start_time(text, now=datetime(2026, 10, 12, 9, 0, 0)) == expected


def test_parse_start_time_time_only_today():
    now = datetime(2026, 10, 13, 8, 0, 0)
    assert parse_start_time("12:00:00", now=now) == datetime(2026, 10, 13, 12, 0, 0)
    assert parse_start_time("12:00", now=now) == datetime(2026, 10, 13, 12, 0, 0)


def test_parse_start_time_time_only_rolls_to_tomorrow():
    now = datetime(2026, 10, 13, 13, 0, 0)
    assert parse_start_time("12:00:00", now=now) == datetime(2026, 10, 14, 12, 0, 0)


def test_parse_start_time_invalid():
    with pytest.raises(ValueError, match="无法识别起抢时间"):
        parse_start_time("明天中午")


def _course(teaching_class_id: str = "A1", name: str = "大学体育Ⅲ", teacher: str = "张老师",
            code: str = "1001", time_text: str = "周一 3-4 节", capacity: int = 40,
            enrolled: int = 0) -> CandidateCourse:
    return CandidateCourse(
        teaching_class_id=teaching_class_id,
        course_code=code,
        course_name=name,
        teacher=teacher,
        time_text=time_text,
        capacity=capacity,
        enrolled=enrolled,
    )


@pytest.mark.parametrize(
    "target, expected",
    [
        (CourseTarget(course_name="大学体育"), True),
        (CourseTarget(course_name="大学体育Ⅲ"), True),
        (CourseTarget(course_name="大学体育 Ⅲ"), True),  # 忽略空格
        (CourseTarget(course_name="大学英语"), False),
        (CourseTarget(course_code="1001"), True),
        (CourseTarget(course_code="9999"), False),
        (CourseTarget(teacher="张"), True),
        (CourseTarget(teacher="李"), False),
        (CourseTarget(teaching_class_id="A1"), True),
        (CourseTarget(teaching_class_id="A2"), False),
        (CourseTarget(time_text="周一"), True),
        (CourseTarget(time_text="周二"), False),
        (CourseTarget(course_name="大学体育", teacher="张老师"), True),
        (CourseTarget(course_name="大学体育", teacher="李老师"), False),
        (CourseTarget(), True),  # 空目标匹配全部
    ],
)
def test_matches_target(target, expected):
    assert matches_target(_course(), target) is expected


def test_find_matches_sorts_by_remaining_desc():
    courses = [
        _course("A1", capacity=40, enrolled=39),  # 余 1
        _course("A2", capacity=40, enrolled=10),  # 余 30
        _course("A3", capacity=40, enrolled=20),  # 余 20
        _course("B1", name="大学英语", code="2001"),
    ]
    matched = find_matches(courses, CourseTarget(course_name="大学体育"))
    assert [course.teaching_class_id for course in matched] == ["A2", "A3", "A1"]


def test_find_matches_puts_unknown_remaining_last():
    courses = [
        CandidateCourse(teaching_class_id="UNKNOWN", course_name="大学体育Ⅲ"),
        _course("A1", capacity=10, enrolled=0),
    ]
    matched = find_matches(courses, CourseTarget(course_name="大学体育"))
    assert [course.teaching_class_id for course in matched] == ["A1", "UNKNOWN"]


def test_find_matches_empty_when_nothing_hits():
    assert find_matches([_course()], CourseTarget(course_name="不存在的课")) == []


# ==========================================================================
# 编排：对 mock 服务真跑
# ==========================================================================


def _config_with_targets(config, *targets, **grab_overrides):
    config.grab = GrabConfig(
        targets=list(targets) or [],
        retry_interval=grab_overrides.get("retry_interval", 0.0),
        max_attempts=grab_overrides.get("max_attempts", 5),
        stop_on_success=grab_overrides.get("stop_on_success", True),
        drop_if_selected=grab_overrides.get("drop_if_selected", False),
        start_at=grab_overrides.get("start_at", ""),
    )
    return config


def _grabber(config, session, messages: list[str] | None = None, *,
             clock=None, sleep=None) -> Grabber:
    """组装编排器：登录与选课共用同一个会话（Cookie 才一致）。"""
    from qk.adapters.cas import CasLoginAdapter
    from qk.adapters.jwgl import JwglCourseAdapter

    log = messages.append if messages is not None else (lambda _message: None)
    extra = {}
    if clock is not None:
        extra["clock"] = clock
    if sleep is not None:
        extra["sleep"] = sleep
    else:
        extra["sleep"] = lambda _seconds: None  # 测试里不真的等待
    return Grabber(
        config,
        login_adapter=CasLoginAdapter(config, session),
        course_adapter=JwglCourseAdapter(config, session),
        logger=log,
        **extra,
    )


def test_grab_success_flow(mock_app):
    """登录 → 查询 → 抢课 → 抢中确认。"""
    config, server = mock_app
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ", priority=0))
    messages: list[str] = []
    report = _grabber(config, QkSession.from_config(config), messages).run()

    assert report.success is True
    assert report.targets[0].status == "SUCCESS"
    assert report.targets[0].course.teaching_class_id == "JXB001"
    assert report.targets[0].attempts == 1
    assert server.state.is_selected("JXB001") is True
    assert any("余量" in message for message in messages)


def test_grab_full_capacity_retries_then_reports_full(mock_app):
    """容量已满 → 按 retry_interval 重试到 max_attempts，最后如实报 FULL。"""
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(
        config, CourseTarget(course_name="大学英语Ⅱ"), retry_interval=0.0, max_attempts=3
    )
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.success is False
    target = report.targets[0]
    assert target.status == "FULL"
    assert target.attempts == 3
    assert "人数已满" in target.failures[0]


@pytest.mark.usefixtures("workspace")
def test_grab_full_then_free_succeeds_on_retry(make_mock_server, config_factory):
    """前 2 次容量满、第 3 次放行 → 重试后抢中（验证重试真的有用）。

    这个用例必须用**独立的 mock 服务**：共享 fixture 会被同一轮里的其它用例
    先选走课程，导致目标变成“已选过”而测不到重试。
    """
    from qk.mockserver import MockConfig, MockCourse
    from qk.net import QkSession

    server = make_mock_server(
        MockConfig.default(
            courses=[
                MockCourse(
                    "JXB003", "10000003", "通识选修：音乐鉴赏", "王老师", 1.5, 30, 30,
                    "周三 5-6 节", "full_then_free", free_after=2,
                )
            ],
            selected=[],
        )
    )
    config = config_factory(server.base_url)
    _config_with_targets(
        config, CourseTarget(course_name="音乐鉴赏"), retry_interval=0.0, max_attempts=5
    )
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.success is True
    assert report.targets[0].status == "SUCCESS"
    assert report.targets[0].attempts == 3
    assert server.state.select_attempts["JXB003"] == 3


def test_grab_already_selected_is_skipped(mock_app):
    """已在已选列表里 → 自动跳过，不浪费尝试次数。"""
    config, server = mock_app
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="程序设计基础"))
    report = _grabber(config, QkSession.from_config(config)).run()

    target = report.targets[0]
    assert target.status == "SKIPPED_ALREADY_SELECTED"
    assert target.attempts == 0
    assert target.course.teaching_class_id == "JXB004"
    assert server.state.select_attempts.get("JXB004") is None


def test_grab_select_returns_already_selected(make_mock_server, config_factory, workspace):
    """服务端在选课那一刻才回“已经选过” → 归类 ALREADY_SELECTED（不重试）。"""
    from qk.mockserver import MockConfig, MockCourse
    from qk.net import QkSession

    server = make_mock_server(
        MockConfig.default(
            courses=[
                MockCourse(
                    "JXB900", "90000001", "重复选课测试", "李老师", 1.0, 50, 10,
                    "周二 1-2 节", "already_selected",
                )
            ],
            selected=[],
        )
    )
    config = config_factory(server.base_url)
    _config_with_targets(config, CourseTarget(course_name="重复选课测试"))
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.targets[0].status == "ALREADY_SELECTED"
    assert report.targets[0].attempts == 1


def test_grab_drop_failure_is_recorded(make_mock_server, config_factory, workspace):
    """drop_if_selected=true 且退课失败 → 记录失败原因，继续尝试选课。"""
    from qk.mockserver import MockConfig, MockCourse
    from qk.net import QkSession

    server = make_mock_server(
        MockConfig.default(
            courses=[
                MockCourse(
                    "JXB901", "90000002", "退课失败测试", "钱老师", 1.0, 45, 5,
                    "周五 7-8 节", "drop_fail",
                )
            ],
            selected=["JXB901"],
        )
    )
    config = config_factory(server.base_url)
    _config_with_targets(
        config, CourseTarget(course_name="退课失败测试"), drop_if_selected=True, max_attempts=2
    )
    report = _grabber(config, QkSession.from_config(config)).run()

    target = report.targets[0]
    assert any("退课失败" in failure for failure in target.failures)
    assert target.dropped == []
    assert server.state.is_selected("JXB901") is True


def test_grab_drop_then_select_success(mock_app):
    """drop_if_selected=true：先退掉已选课程，再重新选上。"""
    config, server = mock_app
    from qk.net import QkSession

    _config_with_targets(
        config, CourseTarget(course_name="程序设计基础"), drop_if_selected=True, max_attempts=3
    )
    report = _grabber(config, QkSession.from_config(config)).run()
    target = report.targets[0]

    assert target.dropped and target.dropped[0].teaching_class_id == "JXB004"
    assert target.status == "SUCCESS"
    assert server.state.is_selected("JXB004") is True


def test_grab_no_match_reports_guidance(mock_app):
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="根本不存在的课"))
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.targets[0].status == "NO_MATCH"
    assert "python -m qk list" in report.targets[0].message


def test_grab_dry_run_changes_nothing(mock_app):
    config, server = mock_app
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"))
    report = _grabber(config, QkSession.from_config(config)).run(dry_run=True)

    assert report.targets[0].status == "DRY_RUN"
    assert server.state.is_selected("JXB001") is False
    assert server.state.credentials == []  # 演练不登录


def test_grab_stop_on_success_skips_remaining_targets(mock_app):
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(
        config,
        CourseTarget(course_name="大学体育Ⅲ", priority=0),
        CourseTarget(course_name="大学英语Ⅱ", priority=1),
        stop_on_success=True,
    )
    report = _grabber(config, QkSession.from_config(config)).run()

    assert len(report.targets) == 1
    assert report.targets[0].status == "SUCCESS"


def test_grab_continues_after_failure_when_stop_on_success_false(mock_app):
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(
        config,
        CourseTarget(course_name="大学英语Ⅱ", priority=0),
        CourseTarget(course_name="大学体育Ⅲ", priority=1),
        stop_on_success=False,
        max_attempts=1,
    )
    report = _grabber(config, QkSession.from_config(config)).run()

    assert [target.status for target in report.targets] == ["FULL", "SUCCESS"]
    assert report.success is True


def test_grab_relogins_after_session_expiry(make_mock_server, config_factory):
    """会话过期 → 自动重新登录并完成选课。

    用 mock 的一次性开关 ``expire_next_request`` 精确制造“查询已选时会话失效”：
    比调 ``session_ttl`` 更可控——TTL 是递减额度，取小了连登录校验一起过期，
    取大了整轮都不过期，都测不到重登。
    """
    server = make_mock_server(MockConfig.default())
    config = config_factory(server.base_url)
    from qk.adapters.cas import CasLoginAdapter
    from qk.adapters.jwgl import JwglCourseAdapter
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"), max_attempts=3)
    session = QkSession.from_config(config)
    adapter = JwglCourseAdapter(config, session)

    original_fetch_selected = adapter.fetch_selected
    state = {"armed": True}

    def fetch_selected_then_expire():
        result = original_fetch_selected()
        if state["armed"]:  # 只制造一次过期，否则重登后的重试又会被打断
            state["armed"] = False
            server.config.expire_next_request = True
        return result

    adapter.fetch_selected = fetch_selected_then_expire  # type: ignore[method-assign]

    grabber = Grabber(
        config,
        login_adapter=CasLoginAdapter(config, session),
        course_adapter=adapter,
        logger=lambda _message: None,
        sleep=lambda _seconds: None,
    )
    report = grabber.run()

    assert report.relogins == 1, f"应恰好重登 1 次，实际 {report.relogins} 次"
    assert report.targets[0].status == "SUCCESS"
    assert server.state.is_selected("JXB001") is True


def test_grab_respects_start_time(mock_app):
    """起抢时间等待：用注入时钟验证，不真的等待。"""
    config, _server = mock_app
    from qk.net import QkSession

    base = datetime(2026, 10, 13, 11, 59, 58)
    now = {"value": base}
    slept: list[float] = []

    def clock() -> datetime:
        return now["value"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["value"] = now["value"] + timedelta(seconds=seconds)

    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"), start_at="2026-10-13 12:00:00")
    # 登录与选课必须共用同一个会话，否则课程请求会因未登录而 401
    report = _grabber(config, QkSession.from_config(config), clock=clock, sleep=sleep).run()

    assert report.waited_seconds >= 2.0
    assert slept, "应发生等待"
    assert report.targets[0].status == "SUCCESS"


def test_grab_no_targets_is_reported(mock_app):
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(config)
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.targets[0].status == "NO_TARGET"
    assert report.success is False


def test_grab_report_json_shape(mock_app):
    config, _server = mock_app
    from qk.net import QkSession

    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"))
    report = _grabber(config, QkSession.from_config(config)).run()

    payload = report.redacted()
    assert set(payload) >= {
        "command", "dry_run", "started_at", "finished_at", "waited_seconds",
        "logged_in", "student_id", "relogins", "success", "stats", "targets",
    }
    assert payload["targets"][0]["course"]["teaching_class_id"] == "JXB001"
    text = report.to_json()
    assert "JXB001" in text and "password" not in text
    assert isinstance(report.summary_lines(), list)


def test_grab_unknown_path_reports_platform_error(mock_app):
    """接口路径未确认 → 目标结果里如实报错，而不是抛裸异常。"""
    config, _server = mock_app
    from qk.net import QkSession

    config.jwgl.course_list_path = ""
    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"))
    report = _grabber(config, QkSession.from_config(config)).run()

    assert report.targets[0].status == "ERROR"
    assert "jwgl.course_list_path" in report.targets[0].message


def test_mock_course_helper_roundtrip():
    course = make_course("JXB777", course_name="占位课程", capacity=5, enrolled=5)
    payload = course.to_json()
    assert payload["syrs"] == 0
    assert payload["kcmc"] == "占位课程"


# ==========================================================================
# 抢课层的授课形式兜底：被排除的课程**绝不提交**
#
# 适配器已按 jwgl.exclude_course_attrs 过滤候选；这里验证编排层还有一道独立保险，
# 即使换用别的调用路径（适配器没过滤），也不会对面授课发起 select。
# ==========================================================================


def test_grab_never_submits_excluded_delivery_mode(make_mock_server, config_factory):
    """适配器把课程**原样返回**时，抢课层的兜底过滤也必须拦住面授课。

    为什么必须用桩适配器：正常路径下适配器已按 ``jwgl.exclude_course_attrs``
    过滤过了，直接拿真实适配器测，兜底代码永远不会执行 —— 那种用例给不出任何
    保护（变异测试证实：删掉兜底它照样通过）。这里让 ``fetch_candidates`` 返回
    **未过滤**的课程，才真正验证到抢课层这道防线。
    """
    from qk.models import Outcome, SelectionResult, SessionInfo

    class UnfilteredCourseAdapter:
        """桩：不做任何过滤地返回一门面授课，模拟别的调用路径漏了过滤。"""

        name = "unfiltered-stub"

        def __init__(self) -> None:
            self.selects: list[str] = []

        def fetch_candidates(self, keyword: str = "") -> list[CandidateCourse]:
            return [CandidateCourse(
                teaching_class_id="JXBFACE", course_code="91100001",
                course_name="大学体育Ⅲ", credits=1.0, capacity=30, enrolled=1,
                raw={"course_attr": "体育专项类/通识面授课"},
            )]

        def select(self, course, *, dry_run: bool = False):
            self.selects.append(course.course_code)
            return SelectionResult(Outcome.SUCCESS, course, message="桩：本不该被调用")

        def drop(self, course, *, dry_run: bool = False):
            return SelectionResult(Outcome.SUCCESS, course, message="桩")

        def fetch_selected(self):
            return []

    class StubLogin:
        name = "stub"

        def login(self, *, dry_run: bool = False) -> SessionInfo:
            return SessionInfo(ok=True, student_id="20230001",
                               cookies={"CASTGC": "TGT-stub"}, message="桩登录")

        def is_session_alive(self) -> bool:
            return True

        def logout(self) -> None:
            return None

    server = make_mock_server()
    config = config_factory(server.base_url, jwgl_paths=MOCK_PATHS)
    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"))
    config.jwgl.exclude_course_attrs = ["面授课"]

    adapter = UnfilteredCourseAdapter()
    report = Grabber(
        config,
        login_adapter=StubLogin(),
        course_adapter=adapter,
        logger=lambda _m: None,
        sleep=lambda _s: None,
    ).run()

    assert adapter.selects == [], f"兜底没拦住，select 被调用了：{adapter.selects}"
    assert report.targets[0].status == "NO_MATCH", report.targets[0].message
    assert "排除条件" in report.targets[0].message


def test_grab_proceeds_when_exclusion_does_not_match(mock_app):
    """排除词不命中任何课程时，抢课照常进行（确认兜底没有误伤）。"""
    from qk.adapters.cas import CasLoginAdapter
    from qk.adapters.jwgl import JwglCourseAdapter
    from qk.net import QkSession

    config, _server = mock_app
    _config_with_targets(config, CourseTarget(course_name="大学体育Ⅲ"))
    config.jwgl.exclude_course_attrs = ["绝不存在的属性ZZZ"]

    session = QkSession.from_config(config)
    adapter = JwglCourseAdapter(config, session)

    calls: list[str] = []
    original = adapter.select

    def _spy(course, *, dry_run: bool = False):
        calls.append(course.course_code)
        return original(course, dry_run=dry_run)

    adapter.select = _spy  # type: ignore[method-assign]

    report = Grabber(
        config,
        login_adapter=CasLoginAdapter(config, session),
        course_adapter=adapter,
        logger=lambda _m: None,
        sleep=lambda _s: None,
    ).run()

    assert calls, "不命中的排除词不应阻止抢课"
    assert report.targets[0].status in ("SUCCESS", "FULL", "ALREADY_SELECTED")


# ==========================================================================
# 「只抢网课（非面授）」一键模式
#
# 用户需求：点「开始抢课」后自动查询 → 自动排除面授课 → 自动提交，
# 不需要先手动点查询、再往③里加目标。
#
# 关键约定：
#   * 只排除**属性含「面授」**的课；
#   * 已经「选中」的课要跳过（重复提交既无意义又会触发写操作频率限制）；
#   * 属性取不到的课**不排除** —— 宁可多试一门，也别漏掉线上课；
#   * 停止条件是 max_courses，不是 stop_on_success。
# ==========================================================================


def _online_course(code: str, name: str, attr: str, *,
                   status: str = "") -> CandidateCourse:
    """构造一门带「课程属性」的候选课（属性决定是不是面授课）。"""
    return CandidateCourse(
        teaching_class_id=f"{code}-001",
        course_code=code,
        course_name=name,
        credits=1.0,
        raw={"course_attr": attr, "select_status": status},
    )


class _StubCourseAdapter:
    """只实现 fetch_candidates / select 的最小桩，用于精确验证筛选与编排。"""

    def __init__(self, courses: list[CandidateCourse]) -> None:
        self.courses = courses
        self.selected: list[str] = []
        self.fetch_calls = 0

    def fetch_candidates(self, keyword: str = ""):
        self.fetch_calls += 1
        return list(self.courses)

    def select(self, course, *, dry_run: bool = False):
        self.selected.append(course.course_code)
        return SelectionResult(Outcome.SUCCESS, course, message=f"{course.course_code} 成功")

    def drop(self, course, *, dry_run: bool = False):
        return SelectionResult(Outcome.SUCCESS, course, message="drop")

    def fetch_selected(self):
        return []


class _StubLogin:
    """只需 ``login()`` 返回带 ok/message 的结果（Grabber.ensure_login 的契约）。"""

    def login(self, *, dry_run: bool = False, force: bool = False):
        return SimpleNamespace(ok=True, message="ok")


def _online_grabber(config, adapter, messages: list[str]) -> Grabber:
    return Grabber(
        config, login_adapter=_StubLogin(), course_adapter=adapter,
        logger=messages.append, sleep=lambda _s: None,
    )


def test_online_only_excludes_in_person_courses(config_factory):
    """只抢网课：属性含「面授」的课必须被排除，线上课保留。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=True, max_courses=5)
    courses = [
        _online_course("A1", "通识面授课甲", "经济管理类/通识面授课"),
        _online_course("A2", "网络课乙", "经济管理类/通识网络课"),
        _online_course("A3", "慕课丙", "人文科学类/慕课"),
        _online_course("A4", "面授丁", "艺术类/面授"),
        _online_course("A5", "属性缺失戊", ""),
    ]
    adapter = _StubCourseAdapter(courses)
    messages: list[str] = []
    picked = _online_grabber(config, adapter, messages).discover_online_courses()

    codes = [c.course_code for c in picked]
    assert codes == ["A2", "A3", "A5"], f"面授课没被滤干净：{codes}"
    assert any("已排除面授课 2 门" in m for m in messages), messages


def test_online_only_skips_already_selected(config_factory):
    """已经「选中」的课要跳过 —— 重复提交既无意义又会触发写操作频率限制。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=True, max_courses=5)
    courses = [
        _online_course("B1", "已选网课", "通识网络课", status="选中"),
        _online_course("B2", "未选网课", "通识网络课"),
    ]
    adapter = _StubCourseAdapter(courses)
    messages: list[str] = []
    picked = _online_grabber(config, adapter, messages).discover_online_courses()

    assert [c.course_code for c in picked] == ["B2"]
    assert any("已跳过已选中的课 1 门" in m for m in messages), messages


def test_online_only_respects_limit(config_factory):
    """limit / max_courses 限制返回条数（通识选修课每人限选两门，抢多了也会被拒）。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=True, max_courses=2)
    courses = [_online_course(f"C{i}", f"网课{i}", "通识网络课") for i in range(5)]
    adapter = _StubCourseAdapter(courses)
    picked = _online_grabber(config, adapter, []).discover_online_courses(limit=2)
    assert len(picked) == 2


def test_online_only_run_works_without_targets(config_factory):
    """核心需求：**没有配置任何目标**也能一键抢课（自动查询 → 筛选 → 提交）。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    assert config.grab.targets == []
    config.grab = GrabConfig(online_only=True, max_courses=2, retry_interval=0.0,
                             max_attempts=1, stop_on_success=True)
    courses = [
        _online_course("D1", "面授课", "通识面授课"),
        _online_course("D2", "网课一", "通识网络课"),
        _online_course("D3", "网课二", "通识网络课"),
    ]
    adapter = _StubCourseAdapter(courses)
    messages: list[str] = []
    report = _online_grabber(config, adapter, messages).run()

    assert adapter.fetch_calls >= 1, "应当自动查询过课程"
    assert adapter.selected == ["D2", "D3"], f"应只抢网课且抢到上限：{adapter.selected}"
    assert report.success is True
    assert "D1" not in adapter.selected, "面授课不该被提交"
    assert any("只抢网课" in m for m in messages), messages


def test_online_only_stops_at_max_courses_not_first_success(config_factory):
    """``stop_on_success`` 在网课模式下被忽略：停止条件是 max_courses。

    否则「最多抢 2 门」永远只会抢 1 门 —— 两个语义是冲突的。
    """
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=True, max_courses=3, retry_interval=0.0,
                             max_attempts=1, stop_on_success=True)
    courses = [_online_course(f"F{i}", f"网课{i}", "通识网络课") for i in range(5)]
    adapter = _StubCourseAdapter(courses)
    _online_grabber(config, adapter, []).run()
    assert adapter.selected == ["F0", "F1", "F2"], f"应抢满 3 门：{adapter.selected}"


def test_run_still_requires_targets_when_online_only_disabled(config_factory):
    """没开 online_only 且没有目标时，仍然报 NO_TARGET（不改变原有行为）。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=False)
    adapter = _StubCourseAdapter([])
    report = _online_grabber(config, adapter, []).run()
    assert report.targets[0].status == "NO_TARGET"
    assert adapter.fetch_calls == 0, "未开启时不应自动查询"


def test_online_only_reports_no_match_when_nothing_online(config_factory):
    """全是面授课时给出可读结论，而不是静默成功。"""
    config = _config_with_targets(config_factory("http://127.0.0.1:1"))
    config.grab = GrabConfig(online_only=True, max_courses=2)
    courses = [_online_course("E1", "只有面授", "通识面授课")]
    adapter = _StubCourseAdapter(courses)
    report = _online_grabber(config, adapter, []).run()
    assert report.success is False
    assert report.targets[0].status == "NO_MATCH"
    assert "网课" in report.targets[0].message


def test_max_courses_validation_rejects_zero(workspace):
    """max_courses 至少为 1（配错了要给出明确错误，而不是静默不抢）。"""
    from qk.config import AppConfig, ConfigError

    config = AppConfig()
    config.grab = GrabConfig(online_only=True, max_courses=0)
    with pytest.raises(ConfigError) as excinfo:
        config.validate()
    assert "max_courses" in str(excinfo.value)
