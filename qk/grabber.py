"""抢课编排：起抢时间、候选匹配、限速重试、已选跳过、会话重登、结果归类。

设计要点：

* 只依赖适配器抽象（:class:`~qk.adapters.base.LoginAdapter` /
  :class:`~qk.adapters.base.CourseAdapter`），所以核心逻辑与平台无关；
* 时间与休眠可注入（``now`` / ``sleep``），起抢时间等待可以在测试里瞬间验证；
* 每个目标都产出结构化结果，CLI 直接打印，便于事后核对“到底试了几次、为什么没选上”。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from .adapters.base import (
    AdapterError,
    CourseAdapter,
    LoginAdapter,
    PlatformUnsupportedError,
    SessionExpiredError,
    filter_excluded_attrs,
)
from .config import AppConfig, CourseTarget
from .models import CandidateCourse, Outcome, SelectionResult

#: 这些结果值得继续重试（容量满、被限流、未知错误）
RETRYABLE_OUTCOMES = frozenset({Outcome.FULL, Outcome.RATE_LIMITED, Outcome.UNKNOWN})

#: 这些结果视为“没必要继续”（已选/冲突/找不到/未确认接口）
TERMINAL_OUTCOMES = frozenset({Outcome.ALREADY_SELECTED, Outcome.CONFLICT, Outcome.NOT_FOUND})

#: 判定「面授课」用的关键词。课程属性形如 ``经济管理类/通识面授课``，
#: 只要属性里出现「面授」就当作线下课排除。
#: 用「面授」而不是「面授课」是为了同时覆盖 ``通识面授课``、``面授`` 等写法。
OFFLINE_ATTR_PATTERN = "面授"


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------


def parse_start_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """解析起抢时间。

    支持：``2026-10-13 12:00:00``、``2026-10-13T12:00``、``2026/10/13 12:00``、
    ``12:00:00``（今天，若已过则顺延到明天）、空字符串（返回 ``None`` = 立即开始）。
    """
    value = (text or "").strip()
    if not value:
        return None
    reference = now or datetime.now()
    normalized = value.replace("/", "-").replace("T", " ")
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        candidate = reference.replace(
            hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0
        )
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate
    raise ValueError(
        f"无法识别起抢时间：{text!r}。可用格式：2026-10-13 12:00:00 / 12:00:00"
    )


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------


def matches_target(course: CandidateCourse, target: CourseTarget) -> bool:
    """判断一门候选课程是否命中抢课目标。

    规则（已填写的字段都要满足）：

    * ``course_name``：包含匹配（忽略大小写与空格）
    * ``course_code``：完全相同
    * ``teacher``：包含匹配
    * ``teaching_class_id``：完全相同
    * ``time_text``：包含匹配
    """
    if target.course_name:
        wanted = target.course_name.replace(" ", "").lower()
        if wanted not in course.course_name.replace(" ", "").lower():
            return False
    if target.course_code and target.course_code.strip() != course.course_code.strip():
        return False
    if target.teacher:
        wanted_teacher = target.teacher.replace(" ", "").lower()
        if wanted_teacher not in course.teacher.replace(" ", "").lower():
            return False
    if target.teaching_class_id and target.teaching_class_id.strip() != course.teaching_class_id.strip():
        return False
    if target.time_text:
        wanted_time = target.time_text.replace(" ", "").lower()
        if wanted_time not in course.time_text.replace(" ", "").lower():
            return False
    return True


def find_matches(courses: Sequence[CandidateCourse], target: CourseTarget) -> list[CandidateCourse]:
    """返回命中的候选课程；有教师筛选时按余量从多到少排序（更容易抢到）。"""
    matched = [course for course in courses if matches_target(course, target)]
    matched.sort(key=lambda item: (item.remaining is None, -(item.remaining or 0)))
    return matched


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


@dataclass
class TargetReport:
    """单个抢课目标的结果。"""

    target: CourseTarget
    status: str
    course: CandidateCourse | None = None
    attempts: int = 0
    message: str = ""
    failures: list[str] = field(default_factory=list)
    details: list[SelectionResult] = field(default_factory=list)
    dropped: list[CandidateCourse] = field(default_factory=list)

    def redacted(self) -> dict[str, Any]:
        return {
            "target": {
                "course_name": self.target.course_name,
                "course_code": self.target.course_code,
                "teacher": self.target.teacher,
                "teaching_class_id": self.target.teaching_class_id,
                "time_text": self.target.time_text,
                "priority": self.target.priority,
            },
            "status": self.status,
            "course": self.course.redacted() if self.course else None,
            "attempts": self.attempts,
            "message": self.message,
            "failures": list(self.failures),
            "dropped": [course.redacted() for course in self.dropped],
        }


@dataclass
class GrabReport:
    """整轮抢课的结果。"""

    command: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    waited_seconds: float = 0.0
    logged_in: bool = False
    student_id: str = ""
    relogins: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    targets: list[TargetReport] = field(default_factory=list)
    success: bool = False

    def redacted(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "waited_seconds": round(self.waited_seconds, 3),
            "logged_in": self.logged_in,
            "student_id": self.student_id,
            "relogins": self.relogins,
            "success": self.success,
            "stats": self.stats,
            "targets": [item.redacted() for item in self.targets],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.redacted(), ensure_ascii=False, indent=indent)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        for item in self.targets:
            label = item.target.course_name or item.target.course_code or item.target.teaching_class_id
            course = item.course
            detail = f"{course.course_name}#{course.teaching_class_id}" if course else "未匹配到教学班"
            lines.append(f"{item.status}：{label or '（未命名目标）'} → {detail}（尝试 {item.attempts} 次）")
            for failure in item.failures[-3:]:
                lines.append(f"    · {failure}")
        return lines


# --------------------------------------------------------------------------
# 抢课编排
# --------------------------------------------------------------------------


class Grabber:
    """把“登录 → 查询 → 抢课 → 确认”串起来的编排器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        login_adapter: LoginAdapter,
        course_adapter: CourseAdapter,
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.login_adapter = login_adapter
        self.course_adapter = course_adapter
        self.clock = clock
        self.sleep = sleep
        self.logger = logger
        self.relogins = 0

    # ------------------------------------------------------------------ 时间
    def wait_until_start(self, start_at: str | None = None) -> float:
        """等到起抢时间；返回实际等待秒数（0 表示无需等待）。"""
        raw = start_at if start_at is not None else self.config.grab.start_at
        if not raw:
            return 0.0
        try:
            target_time = parse_start_time(raw, now=self.clock())
        except ValueError as exc:
            raise AdapterError(str(exc)) from exc
        now = self.clock()
        if target_time is None or target_time <= now:
            return 0.0
        remaining = (target_time - now).total_seconds()
        self.logger(f"等待起抢时间 {target_time:%Y-%m-%d %H:%M:%S}（还有 {remaining:.1f} 秒）")
        # 分段休眠：便于 Ctrl+C 及时响应，也避免长睡期间时钟漂移
        waited = 0.0
        while waited < remaining:
            step = min(5.0, remaining - waited)
            self.sleep(step)
            waited += step
            remaining = (target_time - self.clock()).total_seconds()
            if remaining <= 0:
                break
        self.logger("起抢时间到，开始抢课")
        return waited

    # ------------------------------------------------------------------ 登录
    def ensure_login(self, *, force: bool = False, dry_run: bool = False) -> bool:
        """确保处于登录状态；``force=True`` 时重新登录（会话失效时用）。

        演练模式只验证配置、不发登录请求，因此“未确认成功”不算失败。
        """
        if force:
            self.relogins += 1
            self.logger(f"会话失效，重新登录（第 {self.relogins} 次）")
        info = self.login_adapter.login(dry_run=dry_run)
        if not info.ok and not dry_run:
            self.logger(f"登录未确认成功：{info.message}")
            return False
        return True

    # ------------------------------------------------------------------ 已选
    def _fetch_selected(self) -> list[CandidateCourse]:
        try:
            return list(self.course_adapter.fetch_selected())
        except PlatformUnsupportedError:
            return []
        except SessionExpiredError:
            raise  # 交给上层重登
        except AdapterError as exc:
            self.logger(f"查询已选课程失败（继续抢课）：{exc}")
            return []

    def _handle_drop(self, report: TargetReport, selected_courses: Sequence[CandidateCourse],
                     target: CourseTarget, *, dry_run: bool) -> None:
        """已选课程按配置决定：跳过，或先退掉再抢。"""
        for course in selected_courses:
            if not matches_target(course, target):
                continue
            if not self.config.grab.drop_if_selected:
                self.logger(f"已选过 {course.course_name}#{course.teaching_class_id}，跳过该目标")
                return
            result = self.course_adapter.drop(course, dry_run=dry_run)
            report.details.append(result)
            if result.ok:
                report.dropped.append(course)
                self.logger(f"已退课：{course.course_name}#{course.teaching_class_id}（{result.text()}）")
            else:
                report.failures.append(f"退课失败：{result.text()}")

    # ------------------------------------------------------------------ 单目标
    def _work_target(self, target: CourseTarget, *, dry_run: bool, once: bool) -> TargetReport:
        report = TargetReport(target=target, status="PENDING")
        max_attempts = 1 if once else max(1, self.config.grab.max_attempts)
        interval = max(0.0, self.config.grab.retry_interval)

        if dry_run:
            # 演练模式不发任何请求（包括查询），只说明“将会做什么”
            report.status = "DRY_RUN"
            report.message = "演练模式：未查询、未提交"
            return report

        selected = self._fetch_selected()
        if selected:
            self._handle_drop(report, selected, target, dry_run=dry_run)
            # 退课后继续抢；未退课且已命中则本目标无需再抢
            still_selected = [
                course
                for course in (self._fetch_selected() if self.config.grab.drop_if_selected else selected)
                if matches_target(course, target)
            ]
            if still_selected and not report.dropped:
                # 明明配了 drop_if_selected 却还留着已选课程 → 说明退课没成功，
                # 不能报成“按配置跳过”，否则用户会以为是正常的跳过。
                if report.failures or self.config.grab.drop_if_selected:
                    report.status = "DROP_FAILED"
                    report.course = still_selected[0]
                    report.message = "退课未成功，课程仍在已选列表中，未继续抢课"
                else:
                    report.status = "SKIPPED_ALREADY_SELECTED"
                    report.course = still_selected[0]
                    report.message = "已选过该课程，按配置跳过"
                return report

        candidates = list(self.course_adapter.fetch_candidates())
        # 抢课层再兜一道底：即使调用方忘了过滤，也**绝不提交**被排除的授课形式。
        # （适配器已按 jwgl.exclude_course_attrs 过滤；这里防的是别的调用路径漏配。）
        patterns = list(getattr(self.config.jwgl, "exclude_course_attrs", []) or [])
        if patterns:
            allowed = filter_excluded_attrs(candidates, patterns)
            removed = len(candidates) - len(allowed)
            if removed:
                self.logger(f"按授课形式排除条件（{'、'.join(patterns)}）滤掉 {removed} 门候选课程")
            candidates = allowed
        matches = find_matches(candidates, target)
        if not matches:
            report.status = "NO_MATCH"
            report.message = (
                "候选列表里没有匹配的教学班（请核对课程名/课程号/教师，或先跑 python -m qk list）"
                + (f"；当前排除条件 {'、'.join(patterns)} 可能把目标也滤掉了" if patterns else "")
            )
            return report

        for attempt in range(1, max_attempts + 1):
            for course in matches:
                result = self.course_adapter.select(course, dry_run=dry_run)
                report.attempts += 1
                report.details.append(result)
                report.course = course
                self.logger(
                    f"[第 {attempt}/{max_attempts} 次] {course.course_name}#{course.teaching_class_id}"
                    f" 余量 {course.remaining} → {result.text()}"
                )

                if result.outcome is Outcome.SUCCESS:
                    report.status = "SUCCESS"
                    report.message = result.text()
                    return report
                if result.outcome is Outcome.DRY_RUN:
                    report.status = "DRY_RUN"
                    report.message = result.text()
                    return report
                if result.outcome is Outcome.ALREADY_SELECTED:
                    # 注意：服务端说“已经选过”并不等于本次抢中，必须如实上报，
                    # 否则报告会显示成功而实际没有选中。
                    report.status = "ALREADY_SELECTED"
                    report.message = result.text()
                    return report
                if result.outcome in TERMINAL_OUTCOMES:
                    report.status = result.outcome.value
                    report.message = result.text()
                    return report
                report.failures.append(result.text())

            if attempt >= max_attempts:
                break
            if interval > 0:
                self.sleep(interval)

        last = report.details[-1] if report.details else None
        report.status = last.outcome.value if last else "UNKNOWN"
        report.message = "尝试次数用尽仍未选上"
        return report

    #: 会话过期后最多重登几次（每次重登都会重跑当前目标）
    MAX_RELOGINS = 3

    def _work_target_with_relogin(self, target: CourseTarget, *, dry_run: bool, once: bool) -> TargetReport:
        """跑一个目标；会话过期时重登并重跑（真实环境里这是最常见的干扰）。"""
        last_error = ""
        for round_index in range(self.MAX_RELOGINS + 1):
            try:
                return self._work_target(target, dry_run=dry_run, once=once)
            except SessionExpiredError as exc:
                last_error = str(exc)
                if round_index >= self.MAX_RELOGINS:
                    break
                self.logger(f"会话失效（{exc}），重新登录后重试该目标")
                self.ensure_login(force=True, dry_run=dry_run)
            except AdapterError as exc:
                # 含 PlatformUnsupportedError：接口未确认时如实报错，不中断整轮
                return TargetReport(target=target, status="ERROR", message=str(exc))
        return TargetReport(
            target=target,
            status="SESSION_EXPIRED",
            message=f"会话反复失效，已重登 {self.MAX_RELOGINS} 次仍未完成：{last_error}",
        )

    # ------------------------------------------------------------------ 主流程
    def discover_online_courses(self, *, keyword: str = "",
                                limit: int | None = None) -> list[CandidateCourse]:
        """自动查询课程 → 排除面授课 → 返回可抢的网课（按查询顺序）。

        「只抢网课」模式的核心：用户不必先点查询、再加目标，点开始就自动完成这三步。

        排除规则（任一命中即剔除）：

        * 课程属性含 :data:`OFFLINE_ATTR_PATTERN`（``面授``）→ 线下课；
        * 已经处于「选中」状态的课 → 抢了也是白提交，还会触发写操作频率限制；
        * 适配器已按 ``jwgl.exclude_course_attrs`` 做过一轮过滤（见
          :meth:`~qk.adapters.jwgl.KingosoftCourseAdapter.fetch_candidates`）。

        属性取不到的课程**不会被排除** —— 宁可多试一门，也不要因为字段缺失漏掉线上课。
        """
        courses = list(self.course_adapter.fetch_candidates(keyword))
        self.logger(f"自动查询到 {len(courses)} 门候选课程")

        online = filter_excluded_attrs(courses, [OFFLINE_ATTR_PATTERN])
        skipped_offline = len(courses) - len(online)
        if skipped_offline:
            self.logger(f"已排除面授课 {skipped_offline} 门，剩余线上课 {len(online)} 门")

        picked = [c for c in online
                  if str((c.raw or {}).get("select_status", "")) != "选中"]
        skipped_selected = len(online) - len(picked)
        if skipped_selected:
            self.logger(f"已跳过已选中的课 {skipped_selected} 门")

        if limit is not None and limit > 0:
            picked = picked[:limit]
        return picked

    def run(self, *, dry_run: bool = False, once: bool = False,
            start_at: str | None = None) -> GrabReport:
        """执行整轮抢课。

        ``grab.online_only`` 开启时走「一键抢网课」流程：登录后**自动查询**课程、
        排除面授课、按顺序抢，最多抢 ``grab.max_courses`` 门 —— 不需要预先配置目标。
        此时 ``stop_on_success`` 被忽略（它的语义是「抢到一门就收工」，
        与「抢到 N 门」冲突；这里的停止条件是``max_courses``）。
        """
        started = self.clock()
        report = GrabReport(
            command="grab",
            dry_run=dry_run,
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
            student_id=self.config.creds.student_id,
        )

        online_only = bool(self.config.grab.online_only)
        if not self.config.grab.targets and not online_only:
            report.targets.append(
                TargetReport(target=CourseTarget(), status="NO_TARGET", message="配置里没有任何抢课目标")
            )
            report.finished_at = self.clock().strftime("%Y-%m-%d %H:%M:%S")
            return report

        report.waited_seconds = self.wait_until_start(start_at)
        try:
            report.logged_in = self.ensure_login(dry_run=dry_run)
        except SessionExpiredError:
            report.logged_in = self.ensure_login(force=True, dry_run=dry_run)

        # ---- 「只抢网课」：自动查询并生成目标 ----
        targets = sorted(self.config.grab.targets, key=lambda item: item.priority)
        if online_only:
            limit = max(1, int(self.config.grab.max_courses))
            self.logger(f"「只抢网课（非面授）」模式：自动查询，最多抢 {limit} 门")
            try:
                discovered = self.discover_online_courses(limit=limit)
            except AdapterError as exc:
                report.targets.append(TargetReport(
                    target=CourseTarget(), status="ERROR",
                    message=f"自动查询课程失败：{exc}",
                ))
                discovered = []
            if not discovered:
                if not report.targets:
                    report.targets.append(TargetReport(
                        target=CourseTarget(), status="NO_MATCH",
                        message="当前没有可抢的非面授（网课）课程",
                    ))
                report.success = False
                report.relogins = self.relogins
                report.finished_at = self.clock().strftime("%Y-%m-%d %H:%M:%S")
                report.stats = {"targets": len(report.targets), "attempts": 0,
                                "success": 0, "dry_run": dry_run}
                return report
            targets = [
                CourseTarget(course_code=course.course_code,
                             course_name=course.course_name,
                             priority=index)
                for index, course in enumerate(discovered)
            ]
            for item in targets:
                self.logger(f"自动加入目标：{item.course_name or item.course_code}")

        # ---- 逐个目标抢课 ----
        grabbed = 0
        for target in targets:
            self.logger(f"开始处理目标：{target.course_name or target.course_code or target.teaching_class_id}")
            target_report = self._work_target_with_relogin(target, dry_run=dry_run, once=once)
            report.targets.append(target_report)
            self.logger(f"目标结果：{target_report.status}（{target_report.message}）")

            if target_report.status in ("SUCCESS", "DRY_RUN"):
                grabbed += 1
                if online_only:
                    # 停止条件由 max_courses 决定（discovered 已按该上限裁剪）
                    if grabbed >= len(targets):
                        self.logger(f"已处理完全部 {grabbed} 门目标")
                        break
                    continue
                if self.config.grab.stop_on_success:
                    self.logger("已抢到目标课程，按配置停止后续目标")
                    break

        report.success = any(item.status == "SUCCESS" for item in report.targets)
        report.relogins = self.relogins
        report.finished_at = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        report.stats = {
            "targets": len(report.targets),
            "attempts": sum(item.attempts for item in report.targets),
            "success": sum(1 for item in report.targets if item.status == "SUCCESS"),
            "dry_run": dry_run,
        }
        return report


def build_grabber(
    config: AppConfig,
    *,
    session: Any | None = None,
    logger: Callable[[str], None] = print,
) -> Grabber:
    """按配置组装登录适配器、选课适配器与编排器（CLI 与测试共用）。"""
    from .adapters.cas import CasLoginAdapter
    from .adapters.jwgl import build_course_adapter
    from .net import QkSession

    shared_session = session or QkSession.from_config(config)
    login_adapter = CasLoginAdapter(config, shared_session)
    course_adapter = build_course_adapter(config, shared_session)
    return Grabber(
        config,
        login_adapter=login_adapter,
        course_adapter=course_adapter,
        logger=logger,
    )


__all__ = [
    "Grabber",
    "GrabReport",
    "OFFLINE_ATTR_PATTERN",
    "RETRYABLE_OUTCOMES",
    "TERMINAL_OUTCOMES",
    "TargetReport",
    "build_grabber",
    "find_matches",
    "matches_target",
    "parse_start_time",
]
