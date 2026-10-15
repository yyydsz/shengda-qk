"""配置加载：本地配置文件 + 环境变量覆盖 + 凭据脱敏。

配置优先级（后者覆盖前者）：

1. 内置默认值
2. ``--config`` 指定的 YAML / TOML / JSON 文件
3. ``--dotenv`` 指定的 ``.env`` 风格文件（仅用于放凭据）
4. 环境变量（前缀 ``QK_``，嵌套用双下划线，如 ``QK_CAS__PASSWORD``）

凭据只从配置文件或环境变量读取，绝不写入代码、日志或终端明文输出。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, get_args, get_origin, get_type_hints

from .models import mask_url, redact

ENV_PREFIX = "QK_"
ENV_NESTED_SEPARATOR = "__"

#: 各字段允许的环境变量写法（同时接受 QK_CAS__PASSWORD 与 QK_CAS_PASSWORD）
_FLAT_KEYS: dict[str, tuple[str, ...]] = {
    "student_id": ("STUDENT_ID", "USERNAME", "USER"),
    "password": ("PASSWORD", "PASSWD", "PWD"),
    "captcha_code": ("CAPTCHA_CODE",),
}


class ConfigError(Exception):
    """配置缺失或格式错误。CLI 会把它转成明确的中文提示与固定退出码。"""


# --------------------------------------------------------------------------
# 配置小节
# --------------------------------------------------------------------------


@dataclass
class HttpConfig:
    """HTTP 行为：超时、限速、重试、并发。"""

    timeout: float = 10.0
    interval: float = 0.35
    max_retries: int = 3
    backoff: float = 0.6
    concurrency: int = 2
    verify_tls: bool = True
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CasConfig:
    """统一身份认证（CAS）登录参数。"""

    login_url: str = "https://cas.shengda.edu.cn/lyuapServer/login"
    service: str = "https://my.shengda.edu.cn/"
    logout_url: str = ""
    #: ticket 校验端点。**默认留空**：留空时由 login_url 推导（同主机、路径换成
    #: ``/validate``），这样把 login_url 指向其它地址（例如本地 mock）时不会
    #: 误打真实主机。真实端点未确认前不要写死。
    validate_url: str = ""
    username_field: str = "username"
    password_field: str = "password"
    extra_fields: dict[str, str] = field(default_factory=dict)
    #: 登录成功后判定用关键字（如“选课”“退出”），留空则不校验
    success_keywords: list[str] = field(default_factory=list)
    #: 登录后用于确认会话有效的页面路径（相对教务基址），留空则跳过校验
    test_login_path: str = ""
    #: 需要验证码时的提示与本地识别开关
    captcha_field: str = "captcha"
    captcha_hint: str = ""
    #: 登录方式：password=账号密码（默认，保持既有行为）；wechat=微信扫码
    login_mode: str = "password"
    #: 微信扫码相关端点与轮询参数
    wechat: "WeChatConfig" = field(default_factory=lambda: WeChatConfig())
    #: 扫码拿到凭据后如何建立会话：auto=先试 REST 再回退 HTML 表单；rest；form
    establish_strategy: str = "auto"


@dataclass
class WeChatConfig:
    """微信扫码登录端点。默认值来自 2026-10 对升达统一认证的真实探测：

    * ``GET {cas}/lyuapServer/loginType`` → ``data.isWeChat == "1"``
    * ``GET {cas}/lyuapServer/weChat/CreateQRcode``（无参数）→ 平铺 ``{uuid, content}``，
      ``content`` 是 ``data:image/png;base64,...``（实测 PNG 220x220/8bit/RGBA）
    * ``POST {cas}/lyuapServer/weChat/wx/CheckScan``（JSON ``{"state": uuid}``）
      等待态为 ``meta.success=false`` + ``statusCode=500``

    成功态字段名（``userName``/``passWord``/``service``）来自同族平台前端代码，
    升达实例上尚未实测到，因此全部做成可配置项。
    """

    #: 探测平台登录方式（用于确认是否启用微信扫码）
    login_type_path: str = "/lyuapServer/loginType"
    #: 取二维码
    qrcode_path: str = "/lyuapServer/weChat/CreateQRcode"
    #: 轮询扫码状态
    check_scan_path: str = "/lyuapServer/weChat/wx/CheckScan"
    #: CAS REST 建会话（取 TGT / 用 TGT 换 ticket）
    rest_tickets_path: str = "/lyuapServer/v1/tickets"
    #: 轮询间隔与总超时（秒）
    poll_interval: float = 1.5
    poll_timeout: float = 180.0
    #: 二维码过期时自动重取的次数上限
    max_qr_refresh: int = 1
    #: 响应字段名
    uuid_field: str = "uuid"
    content_field: str = "content"
    user_field: str = "userName"
    password_field: str = "passWord"
    service_field: str = "service"
    #: 扫码成功响应里的登录类型字段；真实前端扫码分支固定传 "3"
    #: （它决定建会话时密码是否需要 RSA 加密：loginType 非空 ⇒ 用明文密码）
    type_field: str = "type"
    #: 成功判定字段（meta.success）与等待态状态码
    success_field: str = "success"
    waiting_status_code: int = 500
    #: 终端渲染
    qr_width: int = 0  # 0=按模块自适应
    qr_quiet_zone: int = 2
    qr_color: bool = True
    qr_invert: bool = False


@dataclass
class SessionConfig:
    """登录会话缓存：扫码一次后可复用，供定时抢课使用。"""

    #: 缓存文件路径（已在 .gitignore 中排除）
    path: str = "qk_state.json"
    #: 有效期（小时）；超过则视为过期，需要重新扫码/登录
    max_age_hours: float = 8.0


@dataclass
class JwglConfig:
    """教务（选课）接口参数。

    真实契约已于 2026-10-13 在校园网实测（青果 Kingosoft，见 README 的
    「教务系统（Kingosoft）选课接口现状」）；``platform="kingosoft"`` 时下列路径生效，
    其余平台仍按 ``course_list_path`` 等手工配置的路径工作（留空即明确报错、不猜路径）。
    """

    base_url: str = "https://my.shengda.edu.cn/"
    index_path: str = "/"
    course_list_path: str = ""
    select_path: str = ""
    drop_path: str = ""
    selected_list_path: str = ""
    #: 平台标识：``kingosoft``（金智/青果，**本校教务，默认**）——路径内置在
    #: ``KingosoftCourseAdapter`` 里，四个 ``*_path`` 保持留空即正确。
    #: 通用实现保留 ``auto`` / ``custom`` / ``qz``（强智风格的 JSON/HTML 表格解析），
    #: 这三个值必须手工填 ``*_path``，留空即明确报错、不猜路径。
    platform: str = "kingosoft"
    #: 选课请求的额外固定参数（如学期、志愿类型）
    extra_params: dict[str, str] = field(default_factory=dict)
    #: **查询后**按课程属性排除的课程：命中的课程不出现在列表里（子串匹配，大小写不敏感）。
    #: 例：``["通识面授课"]`` 可把面授课全部滤掉，只看网络课/慕课等其它授课形式。
    #: 属性值取自真实响应的 ``course_attr``（形如 ``经济管理类/通识面授课``）。
    #: 留空 = 不过滤（默认，保持既有行为）。注意这是**客户端过滤**：服务器仍会返回全部课程，
    #: 过滤只影响展示与抢课目标匹配，不改变向服务器发出的请求。
    exclude_course_attrs: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- Kingosoft
    #: 教务系统基址（Kingosoft）。真实值 https://jw.shengda.edu.cn:8090/（http 同端口会 302 到 https）
    kingosoft_base_url: str = "https://jw.shengda.edu.cn:8090/"
    #: 教务系统的 CAS service（**必须与浏览器一致**，实测为 http；换票与回调都用它）
    cas_service: str = "http://jw.shengda.edu.cn:8090/caslogin"
    #: 菜单上下文接口与选课菜单号（缺上下文直接访问页面会报「无效访问请求」）
    menu_context_path: str = "/frame/normalusemenu/getBreadcrumbsInfoNew.action"
    menucode: str = "S2020202"
    #: 课程列表：表单 POST 到 DataTable.jsp?tableId=…
    data_table_path: str = "/taglib/DataTable.jsp"
    course_list_table_id: str = "2568"
    #: 选课上下文（学年/学期/课程范围/轮次）
    time_range_path: str = "/jw/common/getWsxkTimeRange.action"
    xktype: str = "2"
    #: 写操作参数的加密所需（临时 DES 密钥 / 服务端时间戳）
    temp_key_path: str = "/frame/homepage?method=getTempDeskey"
    temp_time_path: str = "/frame/homepage?method=getTempNowtime"


@dataclass
class CourseTarget:
    """一条抢课目标。至少给 course_name 或 course_code 之一。"""

    course_name: str = ""
    course_code: str = ""
    teacher: str = ""
    teaching_class_id: str = ""
    time_text: str = ""
    priority: int = 0


@dataclass
class GrabConfig:
    """抢课编排参数。"""

    start_at: str = ""
    retry_interval: float = 1.5
    max_attempts: int = 60
    stop_on_success: bool = True
    drop_if_selected: bool = False
    targets: list[CourseTarget] = field(default_factory=list)


@dataclass
class Credentials:
    """登录凭据。只允许来自配置文件、.env 文件或环境变量。"""

    student_id: str = ""
    password: str = ""
    captcha_code: str = ""
    source: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.student_id and self.password)

    def redacted(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id or "(未提供)",
            "password": "***" if self.password else "(未提供)",
            "captcha_code": "***" if self.captcha_code else "(未提供)",
            "source": self.source or "(未提供)",
        }


DEFAULT_HTTP_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


@dataclass
class AppConfig:
    """完整配置树。"""

    http: HttpConfig = field(default_factory=HttpConfig)
    cas: CasConfig = field(default_factory=CasConfig)
    jwgl: JwglConfig = field(default_factory=JwglConfig)
    grab: GrabConfig = field(default_factory=GrabConfig)
    creds: Credentials = field(default_factory=Credentials)
    session: SessionConfig = field(default_factory=SessionConfig)
    #: 记录配置来源，便于排错（不含凭据内容）
    sources: list[str] = field(default_factory=list)
    config_path: str = ""

    # ---------------------------------------------------------------- 校验
    def validate(self) -> None:
        """结构与取值范围校验。凭据缺失不在这里报错（见 ``require_credentials``）。"""
        if self.http.timeout <= 0:
            raise ConfigError("http.timeout 必须大于 0")
        if self.http.interval < 0:
            raise ConfigError("http.interval 不能为负数")
        if self.http.max_retries < 0:
            raise ConfigError("http.max_retries 不能为负数")
        if self.http.concurrency < 1:
            raise ConfigError("http.concurrency 至少为 1")
        if self.grab.max_attempts < 1:
            raise ConfigError("grab.max_attempts 至少为 1")
        if self.grab.retry_interval < 0:
            raise ConfigError("grab.retry_interval 不能为负数")
        if not self.cas.login_url.startswith(("http://", "https://")):
            raise ConfigError("cas.login_url 必须是 http(s) 地址")
        if not self.jwgl.base_url.startswith(("http://", "https://")):
            raise ConfigError("jwgl.base_url 必须是 http(s) 地址")
        for index, pattern in enumerate(self.jwgl.exclude_course_attrs, start=1):
            if not str(pattern).strip():
                raise ConfigError(
                    f"jwgl.exclude_course_attrs[{index}] 不能为空字符串"
                    "（空串会匹配所有课程，导致列表恒为空）；不需要过滤就留空整个列表"
                )
        for index, target in enumerate(self.grab.targets, start=1):
            if not (target.course_name or target.course_code or target.teaching_class_id):
                raise ConfigError(
                    f"grab.targets[{index}] 至少需要 course_name / course_code / teaching_class_id 之一"
                )
        if self.cas.service and not self.cas.service.startswith(("http://", "https://")):
            raise ConfigError("cas.service 必须是 http(s) 地址")
        if self.cas.login_mode not in ("password", "wechat"):
            raise ConfigError("cas.login_mode 只能是 password 或 wechat")
        if self.cas.establish_strategy not in ("auto", "rest", "form"):
            raise ConfigError("cas.establish_strategy 只能是 auto / rest / form")
        if self.cas.wechat.poll_interval <= 0:
            raise ConfigError("cas.wechat.poll_interval 必须大于 0")
        if self.cas.wechat.poll_timeout <= 0:
            raise ConfigError("cas.wechat.poll_timeout 必须大于 0")
        if self.cas.wechat.max_qr_refresh < 0:
            raise ConfigError("cas.wechat.max_qr_refresh 不能为负数")
        if self.cas.wechat.qr_quiet_zone < 0:
            raise ConfigError("cas.wechat.qr_quiet_zone 不能为负数")
        if self.session.max_age_hours < 0:
            raise ConfigError("session.max_age_hours 不能为负数")
        if not self.session.path:
            raise ConfigError("session.path 不能为空")
        for name, path in (
            ("cas.wechat.login_type_path", self.cas.wechat.login_type_path),
            ("cas.wechat.qrcode_path", self.cas.wechat.qrcode_path),
            ("cas.wechat.check_scan_path", self.cas.wechat.check_scan_path),
        ):
            if not path.startswith("/"):
                raise ConfigError(f"{name} 必须以 / 开头（相对统一认证主机的路径）")

    def require_credentials(self) -> Credentials:
        """取凭据；缺失时抛出可直接展示给用户的中文错误。"""
        if not self.creds.student_id:
            raise ConfigError(
                "缺少学号。请在 config.yaml 的 credentials.student_id 填写，"
                f"或设置环境变量 {ENV_PREFIX}STUDENT_ID。"
            )
        if not self.creds.password:
            raise ConfigError(
                "缺少密码。请在 config.yaml 的 credentials.password 填写，"
                f"或设置环境变量 {ENV_PREFIX}PASSWORD（推荐，避免明文落盘）。"
            )
        return self.creds

    # ------------------------------------------------------------ 输出辅助
    def redacted(self) -> dict[str, Any]:
        """供终端打印的脱敏视图。"""
        data = asdict(self)
        data["creds"] = self.creds.redacted()
        data["sources"] = list(self.sources)
        data["cas"] = {
            **data["cas"],
            "login_url": mask_url(self.cas.login_url),
            "service": mask_url(self.cas.service),
        }
        data["jwgl"] = {**data["jwgl"], "base_url": mask_url(self.jwgl.base_url)}
        data["session"] = {**data["session"], "path": self.session.path}
        return redact(data)

    def describe_targets(self) -> list[str]:
        """把抢课目标渲染成人类可读的几行，用于 --dry-run 与日志。"""
        lines: list[str] = []
        for index, target in enumerate(sorted(self.grab.targets, key=lambda t: t.priority), start=1):
            parts = []
            if target.course_name:
                parts.append(f"课程={target.course_name}")
            if target.course_code:
                parts.append(f"课程号={target.course_code}")
            if target.teacher:
                parts.append(f"教师={target.teacher}")
            if target.teaching_class_id:
                parts.append(f"教学班={target.teaching_class_id}")
            if target.time_text:
                parts.append(f"时间={target.time_text}")
            parts.append(f"优先级={target.priority}")
            lines.append(f"  {index}. " + "，".join(parts))
        return lines or ["  （未配置任何抢课目标：请在 config.yaml 的 grab.targets 中添加）"]


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并字典，返回新字典。``None`` 值表示保留 base。"""
    result = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def parse_dotenv(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 .env 文本（忽略注释与空行）。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _flat_alias_names() -> frozenset[str]:
    """``QK_STUDENT_ID`` / ``QK_PASSWORD`` 这类扁平凭据写法的变量名集合。

    ``QK_CAS__PASSWORD`` 这种写法解析后与 ``QK_CASPASSWORD`` 等价，所以两种都登记。
    """
    names: set[str] = set()
    for aliases in _FLAT_KEYS.values():
        for alias in aliases:
            names.add(alias)
            names.add(f"CAS{ENV_NESTED_SEPARATOR}{alias}")
            names.add(f"CAS{alias}")
    return frozenset(names)


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """把 ``QK_`` 环境变量转换成嵌套字典。

    扁平凭据写法（``QK_STUDENT_ID``、``QK_PASSWORD`` 等）由
    :func:`apply_flat_credentials` 归并到 ``credentials`` 小节，这里跳过，
    避免它们被当成顶层小节、或在嵌套解析中丢失。
    """
    environ = os.environ if environ is None else environ
    flat_names = _flat_alias_names()
    tree: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or value is None or value == "":
            continue
        rest = key[len(ENV_PREFIX):]
        if rest.upper() in flat_names:
            continue
        path = rest.lower().split(ENV_NESTED_SEPARATOR)
        node = tree
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):  # 冲突写法，跳过
                break
        else:
            node[path[-1]] = value
    return tree


def _coerce(value: Any, annotation: str) -> Any:
    """按字段类型把字符串（多来自环境变量）转成目标类型。"""
    if value is None:
        return None
    if annotation.startswith("list") and isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if annotation.startswith("dict") and isinstance(value, str):
        pairs = [chunk for chunk in value.split(",") if chunk.strip()]
        return dict(pair.split("=", 1) for pair in (p.strip() for p in pairs) if "=" in pair)
    if annotation in ("int",) and isinstance(value, str):
        try:
            return int(float(value))
        except ValueError as exc:
            raise ConfigError(f"配置项需要整数，但收到 {value!r}") from exc
    if annotation in ("float",) and isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"配置项需要数字，但收到 {value!r}") from exc
    if annotation in ("bool",) and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off"):
            return False
        raise ConfigError(f"配置项需要布尔值，但收到 {value!r}")
    return value


def _build(cls: type, data: Mapping[str, Any] | None, *, path: str = "") -> Any:
    """用字典构造 dataclass。

    字段类型通过 :func:`typing.get_type_hints` 解析：本模块使用了
    ``from __future__ import annotations``，``dataclasses.fields()`` 给出的
    是字符串注解，不能直接用于 issubclass / is_dataclass 判断。
    """
    data = dict(data or {})
    try:
        hints = get_type_hints(cls)
    except Exception:  # noqa: BLE001 - 无法解析时退回字符串注解
        hints = {}
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        full = f"{path}." if path else ""
        raise ConfigError(f"配置项 {full}{'、'.join(unknown)} 无法识别，请检查拼写")

    def field_type(field_obj: Any) -> Any:
        return hints.get(field_obj.name, field_obj.type)

    def coerce_value(value: Any, declared: Any, item_path: str) -> Any:
        if is_dataclass(declared) and isinstance(value, Mapping):
            return _build(declared, value, path=item_path)
        origin = get_origin(declared)
        if origin is list and isinstance(value, (list, tuple)):
            args = get_args(declared)
            inner = args[0] if args else Any
            if is_dataclass(inner):
                return [
                    _build(inner, item, path=f"{item_path}[{index}]")
                    for index, item in enumerate(value)
                    if isinstance(item, Mapping)
                ]
            return [item for item in value]
        if origin is dict and isinstance(value, Mapping):
            return {str(k): str(v) for k, v in value.items()}
        return _coerce(value, _annotation_name(declared))

    kwargs: dict[str, Any] = {}
    for name, spec in known.items():
        if name not in data:
            continue
        item_path = f"{path}.{name}" if path else name
        kwargs[name] = coerce_value(data[name], field_type(spec), item_path)
    return cls(**kwargs)


def _annotation_name(annotation: Any) -> str:
    """把字段类型渲染成 ``_coerce`` 认识的名字（str/int/float/bool/list/dict/…）。"""
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "str"
    origin = get_origin(annotation)
    if origin is list:
        return "list"
    if origin is dict:
        return "dict"
    return getattr(annotation, "__name__", str(annotation))


def load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """按扩展名读取 YAML / TOML / JSON 配置文件。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"配置文件不存在：{file_path}")
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"配置文件无法读取：{file_path}（{exc}）") from exc
    suffix = file_path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover - 环境已装 PyYAML
                raise ConfigError(
                    "读取 YAML 需要 PyYAML，请先执行：python -m pip install -r requirements.txt"
                ) from exc
            data = yaml.safe_load(text)
        elif suffix == ".toml":
            import tomllib

            data = tomllib.loads(text)
        elif suffix == ".json":
            data = json.loads(text)
        else:
            raise ConfigError(f"不支持的配置文件类型：{file_path.suffix}（支持 .yaml/.yml/.toml/.json）")
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成配置错误
        raise ConfigError(f"配置文件解析失败：{file_path}（{exc}）") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件顶层必须是键值表：{file_path}")
    return dict(data)


def apply_flat_credentials(
    data: dict[str, Any], flat: Mapping[str, str], *, force: bool = False
) -> None:
    """把 ``QK_STUDENT_ID`` / ``QK_PASSWORD`` 这类扁平键并入 ``credentials`` 小节。

    :param force: ``True`` 时覆盖已存在的值。环境变量优先级最高，因此来自
        ``environ`` 的调用会传 ``force=True``；``.env`` 文件的调用则不覆盖配置文件里
        已经显式写好的字段。
    """
    creds = dict(data.get("credentials") or {})
    for field_name, aliases in _FLAT_KEYS.items():
        if creds.get(field_name) and not force:
            continue
        for alias in aliases:
            for candidate in (
                f"{ENV_PREFIX}{alias}",
                f"{ENV_PREFIX}CAS{ENV_NESTED_SEPARATOR}{alias}",
                f"{ENV_PREFIX}CAS{alias}",
            ):
                value = flat.get(candidate)
                if value:
                    creds[field_name] = value
                    break
            if creds.get(field_name):
                break
    if creds:
        data["credentials"] = creds


def find_default_config(workspace: str | os.PathLike[str] | None = None) -> str:
    """在工作区里寻找默认配置文件（config.yaml → config.yml → config.toml）。"""
    root = Path(workspace) if workspace else Path.cwd()
    for name in ("config.yaml", "config.yml", "config.toml"):
        candidate = root / name
        if candidate.exists():
            return str(candidate)
    return ""


def is_within(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 之内（用于防止越界读取）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def get_by_path(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """按 ``a.b.c`` 取值，缺失返回 default。"""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
) -> AppConfig:
    """加载并校验配置。

    :param config_path: 显式指定的配置文件；为 ``None`` 时使用工作区默认文件
    :param dotenv_path: 可选，放凭据的 ``.env`` 文件
    :param environ: 环境变量映射（默认为 ``os.environ``，测试可注入）
    :param workspace: 工作区根目录（默认当前目录）
    """
    environ = dict(os.environ if environ is None else environ)
    root = Path(workspace) if workspace else Path.cwd()
    sources: list[str] = ["默认值"]

    data: dict[str, Any] = {}
    chosen = str(config_path) if config_path else ""
    if not chosen:
        found = find_default_config(root)
        if found:
            chosen = found
    if chosen:
        data = deep_merge(data, load_config_file(chosen))
        sources.append(f"配置文件 {chosen}")

    if dotenv_path:
        env_file = Path(dotenv_path)
        if not env_file.exists():
            raise ConfigError(f"dotenv 文件不存在：{env_file}")
        env_values = parse_dotenv(env_file.read_text(encoding="utf-8"))
        if env_values:
            # .env 的优先级高于配置文件（< 环境变量），因此覆盖文件里的同名值
            apply_flat_credentials(data, env_values, force=True)
            sources.append(f"env 文件 {env_file}")

    overrides = env_overrides(environ)
    if overrides:
        data = deep_merge(data, overrides)
    # 扁平凭据写法（QK_STUDENT_ID / QK_PASSWORD）已被 env_overrides 过滤掉，
    # 必须无条件归并一次，否则环境变量里的凭据永远不会生效。
    creds_before = sum(1 for value in (data.get("credentials") or {}).values() if value)
    apply_flat_credentials(data, environ, force=True)
    creds_after = sum(1 for value in (data.get("credentials") or {}).values() if value)
    if overrides or creds_after > creds_before:
        sources.append("环境变量 QK_*")

    http_data = data.get("http") or {}
    if not isinstance(http_data, Mapping):
        raise ConfigError("配置项 http 必须是键值表")
    headers = dict(DEFAULT_HTTP_HEADERS)
    headers.update({str(k): str(v) for k, v in (http_data.get("headers") or {}).items()})

    #: 顶层小节白名单；写错小节名时明确报错，而不是被静默忽略
    known_sections = {"http", "cas", "jwgl", "grab", "credentials", "session"}
    unknown_sections = sorted(set(data) - known_sections)
    if unknown_sections:
        raise ConfigError(
            f"配置项 {'、'.join(unknown_sections)} 无法识别，请检查拼写"
            f"（可用的顶层小节：{'、'.join(sorted(known_sections))}）"
        )

    config = AppConfig(
        http=_build(HttpConfig, http_data, path="http"),
        cas=_build(CasConfig, data.get("cas"), path="cas"),
        jwgl=_build(JwglConfig, data.get("jwgl"), path="jwgl"),
        grab=_build(GrabConfig, data.get("grab"), path="grab"),
        creds=_build(Credentials, data.get("credentials"), path="credentials"),
        session=_build(SessionConfig, data.get("session"), path="session"),
    )
    config.http.headers = headers
    config.sources = sources
    config.config_path = chosen
    config.creds.source = config.creds.source or ("环境变量" if "环境变量 QK_*" in sources else "配置文件")
    config.validate()
    return config


def iter_secret_values(config: AppConfig) -> Iterable[tuple[str, str]]:
    """列出配置里的敏感值（供脱敏自检用，绝不打印其内容）。"""
    if config.creds.password:
        yield ("credentials.password", config.creds.password)
    if config.creds.captcha_code:
        yield ("credentials.captcha_code", config.creds.captcha_code)


__all__ = [
    "AppConfig",
    "CasConfig",
    "ConfigError",
    "CourseTarget",
    "Credentials",
    "GrabConfig",
    "HttpConfig",
    "JwglConfig",
    "SessionConfig",
    "WeChatConfig",
    "DEFAULT_HTTP_HEADERS",
    "ENV_PREFIX",
    "apply_flat_credentials",
    "deep_merge",
    "env_overrides",
    "find_default_config",
    "get_by_path",
    "is_within",
    "iter_secret_values",
    "load_config",
    "load_config_file",
    "parse_dotenv",
]
