"""微信扫码登录适配器（联奕 lyuapServer 平台）。

实测流程（2026-10 对升达统一认证的真实探测）：

1. ``GET {cas}/lyuapServer/loginType`` → ``data.isWeChat == "1"`` 表示启用扫码；
2. ``GET {cas}/lyuapServer/weChat/CreateQRcode``（**无参数**）→ 平铺 ``{uuid, content}``，
   ``content`` 为 ``data:image/png;base64,...``；
3. 把二维码画到终端，用户手机扫码；
4. ``POST {cas}/lyuapServer/weChat/wx/CheckScan``（JSON ``{"state": uuid}``）轮询：
   等待态为 ``meta.success=false`` + ``statusCode=500``；成功后返回账号凭据
   （字段名默认 ``userName`` / ``passWord`` / ``service``，来自同族平台前端代码，
   升达实例上尚未实测到，故全部可配置）；
5. 用拿到的凭据建立 CAS 会话（REST 或 HTML 表单，见 ``cas.establish_strategy``），
   随后复用既有抢课链路。

失败语义（C4）：未启用扫码、二维码过期、轮询超时、接口不可达都会抛出**带中文说明且
含"可回退到密码登录"指引**的异常，由 CLI 映射为固定退出码 3。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ..config import AppConfig
from ..models import SessionInfo
from ..net import QkSession, TransportError
from .base import AdapterError, LoginAdapter
from .qr_render import QRImageError, RenderResult, parse_data_uri, render_png

#: 所有扫码失败提示都会带上这句，方便用户立刻换一条路走通
PASSWORD_FALLBACK_HINT = "可改用账号密码登录：python -m qk login（先在 config.yaml 填好学号与密码）"

#: 平台接口按 ``Accept`` 头做内容协商：CLI 默认带的浏览器式 Accept
#: （``text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8``）会让它返回
#: **XML**（实测形如 ``<WebResponse><meta><success>true</success>…``），
#: 于是按 JSON 解析会直接失败。因此调用扫码接口时必须显式要求 JSON。
#: （2026-10 真机联调发现：离线 mock 不区分 Accept，所以只有真实环境会暴露。）
JSON_ACCEPT_HEADER: dict[str, str] = {"Accept": "application/json"}


class WeChatLoginNotEnabled(AdapterError):
    """学校未启用微信扫码登录。"""


class QRCodeExpiredError(AdapterError):
    """二维码已过期，需要重新取码。"""


class ScanTimeoutError(AdapterError):
    """等待扫码超时。"""


class WeChatInterfaceError(AdapterError):
    """扫码接口不可用或返回无法识别的结构。"""


@dataclass
class QrChallenge:
    """一次取码结果。"""

    uuid: str
    image: bytes = b""
    content: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def render(self, *, quiet_zone: int = 2, color: bool = True, invert: bool = False,
               width: int = 0) -> RenderResult:
        """把二维码画成终端行。"""
        if not self.image:
            raise QRImageError("这次取码没有拿到图片内容，无法在终端显示。")
        return render_png(self.image, quiet_zone=quiet_zone, color=color, invert=invert, width=width)

    def redacted(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "image_bytes": len(self.image), "content_prefix": self.content[:24]}


@dataclass
class ScannedCredential:
    """扫码成功后服务端返回的登录凭据。"""

    user_name: str = ""
    password: str = ""
    service: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.user_name and self.password)

    def redacted(self) -> dict[str, Any]:
        return {
            "user_name": self.user_name,
            "password": "***" if self.password else "",
            "service": self.service,
        }


@dataclass
class PollOutcome:
    """一次轮询的分类结果，便于测试与日志。"""

    state: str  # waiting / success / expired / error
    message: str = ""
    credential: ScannedCredential | None = None


class WeChatQrLoginAdapter(LoginAdapter):
    """扫码登录实现（终端展示二维码 + 轮询 + 建立会话）。"""

    name = "wechat"

    def __init__(self, config: AppConfig, session: QkSession,
                 cas_adapter: LoginAdapter | None = None) -> None:
        self.config = config
        self.session = session
        self.wechat = config.cas.wechat
        self._cas_adapter = cas_adapter
        self._info: SessionInfo | None = None

    # ------------------------------------------------------------------ 内部
    @property
    def cas_adapter(self) -> LoginAdapter:
        if self._cas_adapter is None:
            from .cas import CasLoginAdapter  # 延迟导入，避免循环依赖

            self._cas_adapter = CasLoginAdapter(self.config, self.session)
        return self._cas_adapter

    def cas_base(self) -> str:
        """统一认证主机（由 ``cas.login_url`` 推导，保证 mock 场景不会打真实主机）。"""
        parsed = urlparse(self.config.cas.login_url)
        if not parsed.scheme or not parsed.netloc:
            raise WeChatInterfaceError(
                f"cas.login_url 不是有效地址，无法据此推导扫码接口地址：{self.config.cas.login_url}"
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    def url(self, path: str) -> str:
        return f"{self.cas_base()}{path}"

    def _get_json(self, path: str, *, purpose: str, **kwargs: Any) -> Mapping[str, Any]:
        headers = {**JSON_ACCEPT_HEADER, **dict(kwargs.pop("headers", {}) or {})}
        try:
            result = self.session.get(self.url(path), headers=headers, **kwargs)
        except TransportError as exc:
            raise WeChatInterfaceError(
                f"{purpose}失败（网络不可达）：{exc}。请确认处在校园网/校内 VPN 环境后重试；{PASSWORD_FALLBACK_HINT}"
            ) from exc
        payload = result.json()
        if not isinstance(payload, Mapping):
            snippet = (result.text or "").strip()[:60].replace("\n", " ")
            hint = ""
            if snippet.startswith("<"):
                hint = (
                    "（服务器返回了 XML/HTML：该平台按 Accept 头做内容协商，"
                    "扫码接口已显式请求 application/json，若仍如此请检查是否有代理/中间层改写了请求头）"
                )
            raise WeChatInterfaceError(
                f"{purpose}返回的不是 JSON（HTTP {result.status}，响应开头：{snippet!r}）{hint}；"
                f"{PASSWORD_FALLBACK_HINT}"
            )
        return payload

    @staticmethod
    def _meta(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        meta = payload.get("meta")
        return meta if isinstance(meta, Mapping) else {}

    # ------------------------------------------------------------------ 能力
    def fetch_login_type(self) -> Mapping[str, Any]:
        """读取平台登录方式配置（``/lyuapServer/loginType``）。"""
        return self._get_json(self.wechat.login_type_path, purpose="读取平台登录方式")

    def check_available(self) -> tuple[bool, str]:
        """是否启用微信扫码；返回 (是否可用, 说明)。"""
        payload = self.fetch_login_type()
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return False, (
                "平台登录方式接口没有返回 data 字段，无法确认是否启用微信扫码"
                f"（响应键：{sorted(payload)[:6]}）；{PASSWORD_FALLBACK_HINT}"
            )
        enabled = str(data.get("isWeChat", "")).strip() == "1"
        open_login = str(data.get("isWeChatOpenLogin", "")).strip() == "1"
        if enabled:
            detail = "平台已启用微信扫码登录"
            if open_login:
                detail += "（同时启用了微信开放平台模式）"
            return True, detail
        if open_login:
            return False, (
                "平台只启用了微信开放平台扫码（isWeChatOpenLogin=1），"
                f"当前脚本尚未实现该模式；{PASSWORD_FALLBACK_HINT}"
            )
        return False, f"平台未启用微信扫码登录（isWeChat=0）；{PASSWORD_FALLBACK_HINT}"

    # ------------------------------------------------------------------ 取码
    def create_qrcode(self) -> QrChallenge:
        """取一张登录二维码（无参数 GET；实测返回平铺 ``{uuid, content}``）。"""
        payload = self._get_json(self.wechat.qrcode_path, purpose="获取登录二维码")
        # 实测为平铺结构；同时兼容被 meta/data 包裹的部署
        source: Mapping[str, Any] = payload
        meta = self._meta(payload)
        if meta and isinstance(payload.get("data"), Mapping) and not payload.get(self.wechat.uuid_field):
            source = payload["data"]  # type: ignore[assignment]

        uuid_value = str(source.get(self.wechat.uuid_field, "") or "").strip()
        content = str(source.get(self.wechat.content_field, "") or "").strip()
        if not uuid_value:
            raise WeChatInterfaceError(
                f"取二维码的响应里没有 {self.wechat.uuid_field} 字段（响应键：{sorted(source)[:8]}），"
                f"可能是平台版本差异，请核对 cas.wechat.uuid_field；{PASSWORD_FALLBACK_HINT}"
            )
        image = b""
        if content:
            try:
                image, _mime = parse_data_uri(content)
            except QRImageError:
                image = b""  # 不是图片（例如纯文本内容）时保留原始内容，交由渲染层报告
        return QrChallenge(uuid=uuid_value, image=image, content=content, raw=payload)

    # ------------------------------------------------------------------ 轮询
    def classify_poll(self, payload: Mapping[str, Any], status: int = 200) -> PollOutcome:
        """把一次轮询响应归类为 waiting / success / expired / error。"""
        meta = self._meta(payload)
        message = str(meta.get("message", "") or "")
        status_code = meta.get("statusCode", status)
        data = payload.get("data")

        if meta.get(self.wechat.success_field) is True:
            if isinstance(data, Mapping):
                credential = ScannedCredential(
                    user_name=str(data.get(self.wechat.user_field, "") or ""),
                    password=str(data.get(self.wechat.password_field, "") or ""),
                    service=str(data.get(self.wechat.service_field, "") or ""),
                    raw=dict(data),
                )
                if credential.complete:
                    return PollOutcome("success", "扫码成功", credential)
                return PollOutcome("waiting", "已扫码，等待服务器返回凭据", None)
            return PollOutcome("waiting", "服务器已响应但还没有凭据", None)

        lowered = message.lower()
        if any(word in message for word in ("过期", "失效", "重新扫描", "刷新")) or "expire" in lowered:
            return PollOutcome("expired", message or "二维码已过期", None)
        if status_code == self.wechat.waiting_status_code or status_code in (204, 404):
            return PollOutcome("waiting", message or "等待扫码", None)
        if any(word in message for word in ("未检测到用户登录", "等待", "未扫码")):
            return PollOutcome("waiting", message, None)
        return PollOutcome("error", message or f"未识别的扫码状态（HTTP {status}）", None)

    def poll_once(self, uuid_value: str) -> PollOutcome:
        """轮询一次扫码状态。"""
        url = self.url(self.wechat.check_scan_path)
        try:
            result = self.session.post(url, json_body={"state": uuid_value},
                                       headers=dict(JSON_ACCEPT_HEADER))
        except TransportError as exc:
            raise WeChatInterfaceError(
                f"轮询扫码状态失败（网络不可达）：{exc}。请确认网络后重试；{PASSWORD_FALLBACK_HINT}"
            ) from exc
        payload = result.json()
        if not isinstance(payload, Mapping):
            # 成功态响应里含扫码得到的账号密码，这里**不回显响应体**，避免把凭据打进日志
            raise WeChatInterfaceError(
                f"轮询扫码状态返回的不是 JSON（HTTP {result.status}）；{PASSWORD_FALLBACK_HINT}"
            )
        return self.classify_poll(payload, status=result.status)

    # ------------------------------------------------------------------ 等待
    def wait_for_scan(self, uuid_value: str, *, timeout: float | None = None,
                      interval: float | None = None,
                      on_status: Callable[[PollOutcome], None] | None = None,
                      sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> ScannedCredential:
        """轮询直到扫码成功、二维码过期或超时。"""
        timeout = self.wechat.poll_timeout if timeout is None else timeout
        interval = self.wechat.poll_interval if interval is None else interval
        deadline = clock() + timeout
        last: PollOutcome | None = None

        while True:
            outcome = self.poll_once(uuid_value)
            if on_status is not None and (last is None or outcome.state != last.state
                                         or outcome.message != last.message):
                on_status(outcome)
            last = outcome

            if outcome.state == "success" and outcome.credential is not None:
                return outcome.credential
            if outcome.state == "expired":
                raise QRCodeExpiredError(
                    f"二维码已过期（{outcome.message}）。请重新运行以获取新二维码；{PASSWORD_FALLBACK_HINT}"
                )
            if outcome.state == "error":
                raise WeChatInterfaceError(
                    f"轮询扫码状态返回异常：{outcome.message}；{PASSWORD_FALLBACK_HINT}"
                )
            if clock() >= deadline:
                raise ScanTimeoutError(
                    f"等待扫码超时（{timeout:.0f} 秒内没有完成扫码）。"
                    f"请重试并尽快用微信扫描二维码；{PASSWORD_FALLBACK_HINT}"
                )
            sleep(interval)

    # ------------------------------------------------------------------ 登录
    def login(self, *, dry_run: bool = False,
              on_qrcode: Callable[[QrChallenge], None] | None = None,
              on_status: Callable[[PollOutcome], None] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> SessionInfo:
        """完整扫码登录：确认可用 → 取码 → 展示 → 等扫码 → 建立会话。"""
        if dry_run:
            info = SessionInfo(ok=False, message="演练模式：未发起任何扫码请求")
            self._info = info
            return info

        available, reason = self.check_available()
        if not available:
            raise WeChatLoginNotEnabled(reason)

        attempts = max(1, self.wechat.max_qr_refresh + 1)
        credential: ScannedCredential | None = None
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            challenge = self.create_qrcode()
            if on_qrcode is not None:
                on_qrcode(challenge)
            try:
                credential = self.wait_for_scan(
                    challenge.uuid, on_status=on_status, sleep=sleep, clock=clock
                )
                break
            except QRCodeExpiredError as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                continue

        if credential is None:  # pragma: no cover - 上面的分支已覆盖
            raise WeChatInterfaceError(
                f"扫码未完成：{last_error}；{PASSWORD_FALLBACK_HINT}"
            )

        info = self._establish_session(credential)
        self._info = info
        return info

    def _establish_session(self, credential: ScannedCredential) -> SessionInfo:
        """用扫码得到的凭据建立 CAS 会话（策略见 ``cas.establish_strategy``）。

        ``login_type`` 取扫码成功响应里的 ``type`` 字段（真实前端扫码分支固定传 ``"3"``），
        它决定建会话时是否需要 RSA 加密密码：``loginType`` 非空时前端发的是**明文密码**。
        """
        service = credential.service or self.config.cas.service
        establish = getattr(self.cas_adapter, "login_with_credentials", None)
        if not callable(establish):
            raise WeChatInterfaceError(
                "当前 CAS 适配器不支持用外部凭据建立会话（内部实现不匹配）；"
                f"{PASSWORD_FALLBACK_HINT}"
            )
        login_type = str(credential.raw.get(self.wechat.type_field, "") or "") or "3"
        return establish(
            credential.user_name,
            credential.password,
            service=service,
            strategy=self.config.cas.establish_strategy,
            login_type=login_type,
        )

    # ------------------------------------------------------------------ 会话
    def is_session_alive(self) -> bool:
        return bool(getattr(self.cas_adapter, "is_session_alive", lambda: False)())

    def logout(self) -> None:
        logout = getattr(self.cas_adapter, "logout", None)
        if callable(logout):
            logout()

    @property
    def info(self) -> SessionInfo | None:
        return self._info


def render_instructions(result: RenderResult) -> str:
    """给用户的扫码操作提示（含渲染告警）。"""
    lines = [
        f"二维码已生成（{result.modules} 模块，{result.columns} 列 x {result.rows} 行）。",
        "请用微信「扫一扫」扫描下方二维码；扫码后请在手机上确认。",
    ]
    if result.warning:
        lines.append(f"提示：{result.warning}")
    return "\n".join(lines)


__all__ = [
    "PASSWORD_FALLBACK_HINT",
    "PollOutcome",
    "QrChallenge",
    "QRCodeExpiredError",
    "ScanTimeoutError",
    "ScannedCredential",
    "WeChatInterfaceError",
    "WeChatLoginNotEnabled",
    "WeChatQrLoginAdapter",
    "render_instructions",
]
