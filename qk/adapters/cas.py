"""CAS 统一身份认证登录适配器。

流程（与学校门户线索一致：``cas.shengda.edu.cn/lyuapServer`` 一套 CAS）：

1. ``GET {login_url}?service={service}`` 取登录页，解析表单隐藏字段（``lt`` /
   ``execution`` / ``_eventId`` 等）与是否出现验证码；
2. ``POST {login_url}`` 提交账号密码，**不自动跟随重定向**，从 ``Location`` 抓 ticket；
3. 请求 ``service?ticket=...`` 完成会话建立（服务端签发 JSESSIONID 等 Cookie）；
4. 可选：请求一个需登录才能访问的页面（``cas.test_login_path``）确认会话真的有效。

验证码策略（C3 要求两种情形都有明确提示）：

* 登录页**没有**验证码 → 正常自动登录；
* 登录页**有**验证码 → 抛 :class:`CaptchaRequiredError`，消息里给出两种可行做法
  （在配置里填 ``credentials.captcha_code``，或在校园网下手动登录一次后复用会话）。

所有日志与异常均经过脱敏，密码与 ticket 不会出现在输出里。
"""

from __future__ import annotations

import ipaddress
import re
from html import unescape
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from ..config import AppConfig
from ..models import HttpResult, SessionInfo, mask_url
from ..net import QkSession, TransportError
from .base import AuthError, CaptchaRequiredError, LoginAdapter

#: 表单里 ``name="..." value="..."`` 的字段
_FORM_FIELD = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*["']?(?P<name>[^"'\s>]+)["']?[^>]*?\bvalue\s*=\s*["'](?P<value>[^"']*)["'][^>]*>""",
    re.IGNORECASE,
)
_FORM_FIELD_REVERSED = re.compile(
    r"""<input\b[^>]*\bvalue\s*=\s*["'](?P<value>[^"']*)["'][^>]*?\bname\s*=\s*["']?(?P<name>[^"'\s>]+)["']?[^>]*>""",
    re.IGNORECASE,
)
_FORM_ACTION = re.compile(r"""<form\b[^>]*\baction\s*=\s*["']?(?P<action>[^"'\s>]+)["']?""", re.IGNORECASE)
_CAPTCHA_HINTS = (
    "captcha",
    "validatecode",
    "checkcode",
    "verifycode",
    "验证码",
    "captchaimg",
    "randcode",
)
_AUTH_FAIL_HINTS = (
    "用户名或密码错误",
    "账号或密码错误",
    "密码错误",
    "用户名不存在",
    "认证失败",
    "invalid credentials",
    "authentication failed",
)
#: 登录成功页面上常见的“已登录”标志
_LOGGED_IN_HINTS = ("退出", "注销", "logout", "欢迎", "个人中心", "选课")


def extract_form_fields(html: str) -> dict[str, str]:
    """解析登录页表单里的隐藏字段（``lt`` / ``execution`` / ``_eventId`` / ``service`` 等）。

    同时兼容 ``name`` 在 ``value`` 前后两种写法。
    """
    if not html:
        return {}
    fields: dict[str, str] = {}
    for match in _FORM_FIELD.finditer(html):
        fields[match.group("name")] = unescape(match.group("value"))
    for match in _FORM_FIELD_REVERSED.finditer(html):
        fields.setdefault(match.group("name"), unescape(match.group("value")))
    return fields


def extract_form_action(html: str, default: str = "") -> str:
    """解析登录表单的 ``action``，便于按真实提交地址发请求。"""
    match = _FORM_ACTION.search(html or "")
    if not match:
        return default
    action = unescape(match.group("action")).strip()
    return action or default


def detect_captcha(html: str, field_name: str = "captcha") -> bool:
    """判断登录页是否要求验证码。"""
    lowered = (html or "").lower()
    if field_name and f'name="{field_name.lower()}"' in lowered:
        return True
    return any(hint.lower() in lowered for hint in _CAPTCHA_HINTS)


def extract_ticket(text: str) -> str:
    """从 URL 或重定向地址里取出 CAS ticket。"""
    if not text:
        return ""
    parsed = urlparse(text)
    values = parse_qs(parsed.query).get("ticket")
    if values:
        return values[0]
    match = re.search(r"ticket=([^&\s\"']+)", text)
    return match.group(1) if match else ""


def looks_authenticated(html: str, keywords: list[str] | None = None) -> bool:
    """根据页面关键字判断“看起来已经登录”。"""
    haystack = (html or "").lower()
    hints = [keyword.lower() for keyword in (keywords or [])] or list(_LOGGED_IN_HINTS)
    return any(hint in haystack for hint in hints)


def looks_like_auth_failure(html: str) -> bool:
    """根据页面文案判断“账号密码错误”。"""
    haystack = html or ""
    return any(hint in haystack for hint in _AUTH_FAIL_HINTS)


def _is_ip_literal(host: str) -> bool:
    """判断是不是 IP 字面量（IPv4/IPv6），用于跳过网关 Cookie 播种。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class CasLoginAdapter(LoginAdapter):
    """CAS 登录实现。"""

    name = "cas"

    def __init__(self, config: AppConfig, session: QkSession | None = None) -> None:
        self.config = config
        self.session = session or QkSession.from_config(config)
        self._session_ok = False
        self._session_info: SessionInfo | None = None

    # ------------------------------------------------------------------ 内部
    def cas_base(self) -> str:
        """统一认证主机（由 ``cas.login_url`` 推导，mock 场景下不会打真实主机）。"""
        parsed = urlparse(self.config.cas.login_url)
        if not parsed.scheme or not parsed.netloc:
            raise AuthError(f"cas.login_url 不是有效地址：{self.config.cas.login_url}")
        return f"{parsed.scheme}://{parsed.netloc}"

    def _login_form_url(self, service: str | None = None) -> str:
        target = service if service is not None else self.config.cas.service
        query = {"service": target} if target else {}
        base = self.config.cas.login_url
        if not query:
            return base
        joiner = "&" if "?" in base else "?"
        return f"{base}{joiner}{urlencode(query)}"

    def _credential_payload(self, html: str, *, user_name: str | None = None,
                            password: str | None = None) -> dict[str, str]:
        if user_name is None or password is None:
            creds = self.config.require_credentials()
            user_name = creds.student_id if user_name is None else user_name
            password = creds.password if password is None else password
            captcha_code = creds.captcha_code
        else:
            captcha_code = self.config.creds.captcha_code
        fields = extract_form_fields(html)
        # 只保留服务端下发的隐藏字段，避免把用户名/密码字段名重复写入
        payload: dict[str, str] = {
            key: value
            for key, value in fields.items()
            if key.lower() not in (self.config.cas.username_field.lower(), self.config.cas.password_field.lower())
        }
        payload[self.config.cas.username_field] = user_name
        payload[self.config.cas.password_field] = password
        if self.config.cas.service:
            payload.setdefault("service", self.config.cas.service)
        if captcha_code:
            payload[self.config.cas.captcha_field] = captcha_code
        payload.update(self.config.cas.extra_fields or {})
        return payload

    # ------------------------------------------------------------------ 接口
    def login(self, *, dry_run: bool = False, credentials: tuple[str, str] | None = None,
              service: str | None = None) -> SessionInfo:
        """登录并返回会话信息。失败抛 :class:`AuthError` / :class:`CaptchaRequiredError`。

        :param credentials: 可选 ``(账号, 密码)``；扫码登录等场景下用它替代配置文件凭据。
        :param service: 可选，覆盖 ``cas.service``（扫码凭据里带的 service 优先）。
        """
        if dry_run:
            info = SessionInfo(ok=False, student_id=self.config.creds.student_id, message="演练模式：未发起登录请求")
            self._session_info = info
            return info

        user_name, password = credentials if credentials else (None, None)
        if user_name is None or password is None:
            creds = self.config.require_credentials()
            user_name, password = creds.student_id, creds.password
            captcha_code = creds.captcha_code
        else:
            captcha_code = self.config.creds.captcha_code

        form_url = self._login_form_url(service)
        page = self.session.get(form_url)
        if not page.ok:
            raise AuthError(f"登录页无法访问（HTTP {page.status}）：{mask_url(form_url)}")

        if detect_captcha(page.text, self.config.cas.captcha_field) and not captcha_code:
            self._session_info = None
            raise CaptchaRequiredError(
                "登录页需要验证码，脚本无法自动识别。两种处理方式：\n"
                "  1) 在校园网下打开登录页，把看到的验证码填进配置的 credentials.captcha_code"
                "（或设置环境变量 QK_CAPTCHA_CODE）后重跑；\n"
                f"  2) 或先手动登录一次，确认 {mask_url(form_url)} 是否需要验证码，并把结论反馈给开发者以增强识别。\n"
                f"  验证码字段名：{self.config.cas.captcha_field}",
                captcha_url=urljoin(form_url, "/lyuapServer/captcha"),
            )

        action = extract_form_action(page.text, default=form_url)
        action_url = urljoin(form_url, action)
        payload = self._credential_payload(page.text, user_name=user_name, password=password)

        posted = self.session.post(action_url, data=payload, allow_redirects=False)
        location = posted.headers.get("Location") or posted.headers.get("location") or ""
        ticket = extract_ticket(location)
        text = posted.text or ""

        if looks_like_auth_failure(text):
            self._session_info = None
            raise AuthError(
                "登录被拒绝：账号或密码不正确。请核对配置文件与环境变量里的学号/密码"
                "（密码优先取环境变量 QK_PASSWORD）。"
            )

        if not ticket and not location:
            # 提交后既没跳转也没有 ticket：通常是被打回登录页
            if detect_captcha(text, self.config.cas.captcha_field):
                self._session_info = None
                raise AuthError(
                    "登录未通过：提交后仍停留在需要验证码的登录页，最常见的原因是验证码错误或已过期。\n"
                    "  请重新打开登录页获取新验证码，填入 credentials.captcha_code"
                    f"（或环境变量 QK_CAPTCHA_CODE）后重试；字段名：{self.config.cas.captcha_field}"
                )
            self._session_info = None
            raise AuthError(
                "表单登录未通过：提交账号密码后服务器既没有跳转也没有下发 ticket。"
                "注意升达的登录页是 SPA（没有服务端渲染的表单），这条路径在本平台通常无效，"
                "扫码登录请依赖 /lyuapServer/v1/tickets；账号密码登录请核对 cas.login_url 是否正确。"
            )

        if ticket:
            callback = self._build_callback(location, ticket)
            final = self.session.get(callback)
            if not final.ok:
                raise AuthError(f"ticket 校验失败（HTTP {final.status}）：{mask_url(callback)}")
            if looks_like_auth_failure(final.text):
                self._session_info = None
                raise AuthError("登录被拒绝：账号或密码不正确。")
            text = final.text

        if not self.session.cookies():
            self._session_info = None
            raise AuthError(
                "登录流程结束但服务器没有下发会话 Cookie，无法继续。"
                "请在校园网下用浏览器登录一次，核对 cas.login_url / cas.service 与真实地址是否一致。"
            )

        session_ok = True
        if self.config.cas.test_login_path:
            probe_url = urljoin(self.config.jwgl.base_url, self.config.cas.test_login_path)
            probe = self.session.get(probe_url)
            session_ok = probe.ok and looks_authenticated(probe.text, self.config.cas.success_keywords)
            if not session_ok:
                text = probe.text or text

        info = SessionInfo(
            ok=session_ok,
            student_id=user_name,
            cookies=self.session.cookies(),
            service_url=self.config.cas.service,
            message="登录成功" if session_ok else "登录后校验页面未通过，可能需要验证码或账号受限",
        )
        self._session_info = info
        self._session_ok = session_ok
        return info

    def _derive_validate_url(self) -> str:
        """推导 ticket 校验端点：与 ``login_url`` 同主机，路径换成 ``/validate``。

        这样它永远跟随 ``login_url``（把 login_url 指向 mock 时校验端点也落在 mock），
        同时避免把“未确认的真实端点”写死进默认配置。
        """
        login = self.config.cas.login_url
        if not login.startswith(("http://", "https://")):
            return ""
        parsed = urlparse(login)
        path = parsed.path.rstrip("/")
        for suffix in ("/login", "/Login", "/login.do"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return parsed._replace(path=f"{path}/validate", query="", fragment="").geturl()

    # ------------------------------------------------------------------ REST 建会话
    def login_with_rest(self, user_name: str, password: str, *, service: str | None = None,
                        login_type: str = "3", identifier: str = "", code: str = "") -> SessionInfo:
        """用平台的 ``/v1/tickets`` 接口建立会话（微信扫码后的建会话路径）。

        **契约来自真实前端包（2026-10 实测确认）**：CAS 登录页加载的
        ``assets/js/app.4f499a503cbced5ac091.js`` 里，所有登录方式最终都调用

        .. code-block:: javascript

            axios.post(CAS + "/v1/tickets", qs.stringify({
                username, password, service, loginType, id, code
            }))

        两点关键：① 请求体是**表单编码**（不是 JSON）；② 键名是**小写**
        ``username`` / ``password``。扫码流程传 ``loginType="3"``，此时密码为**明文**
        （只有 ``loginType`` 为空时才需要 RSA 加密密码，那条路径本工具不使用）。

        响应 ``200/201`` 后从 ``data.tgt``（或直接是 TGT 字符串 / ``Location`` 头）取 TGT；
        第二步 ``POST {tickets}/{TGT}`` 表单编码 ``service=…``，拿回带 ticket 的地址，
        最后 ``GET`` 该地址完成会话建立。
        """
        base = self.cas_base()
        tickets_path = (self.config.cas.wechat.rest_tickets_path or "").strip() or "/lyuapServer/v1/tickets"
        ticket_url = f"{base}{tickets_path}"
        target_service = service or self.config.cas.service
        # 与真实前端一致的字段名（前端用 qs.stringify，未定义的 id/code 会被**省略**，
        # 因此这里只在有值时才带上，避免空串被服务端当成非法参数）
        body = {
            "username": user_name,
            "password": password,
            "service": target_service,
            "loginType": login_type or "3",
        }
        if identifier:
            body["id"] = identifier
        if code:
            body["code"] = code

        created = self.session.post(ticket_url, data=body, allow_redirects=False,
                                    headers={"Accept": "application/json"})
        if created.status in (401, 403):
            self._session_info = None
            raise AuthError(
                "REST 建会话被拒绝（HTTP %d）：账号或密码不正确。" % created.status
            )
        tgt = self._extract_tgt(created, ticket_url)
        seeded = self._seed_tgt_cookie(tgt)

        # 真实前端的收尾（app bundle 里的 login action）：
        #   var _ = service + "ticket=" + o.data.ticket; window.location = _
        # 即**第一步响应可能同时带回 ticket**，此时直接跳转即可；
        # 而 POST /v1/tickets/{TGT} 那一步是"复用已有 TGT"时用的。
        # 两条路都还没有真机定论，因此这里**在一次运行里依次试完**，避免一次失败就
        # 浪费用户一次扫码；成功时记录实际生效的那条路径。
        attempts: list[str] = []

        def landed_on_login(response: Any) -> bool:
            """是否被弹回了登录页（SPA 门户的登录前后 HTML 可能一样，所以只看跳转与登录页特征）。"""
            location = (response.headers.get("Location") or "").lower()
            if response.status in (301, 302, 303, 307, 308) and "login" in location:
                return True
            head = (response.text or "")[:2000].lower()
            return "lyuapserver/login" in head or "casloginurl" in head and "?service=" in head

        def try_service_url(label: str, url: str) -> SessionInfo | None:
            """访问候选地址并判断会话是否建立。

            判据刻意保守：**下发了 Cookie 且没有被弹回登录页**即算成功。
            不用"页面关键字"当硬判据——升达门户是 SPA，登录前后的 HTML 可能完全一样，
            用它会把已经建立的会话误判为失败（真机联调时踩过这个坑）。
            """
            try:
                final = self.session.get(url)
            except TransportError as exc:
                attempts.append(f"{label}：请求失败（{exc}）")
                return None
            cookie_names = sorted(self.session.cookies())
            location = final.headers.get("Location") or ""
            has_set_cookie = bool(final.headers.get("Set-Cookie") or final.headers.get("set-cookie"))
            if not cookie_names:
                # 只报"有没有 Set-Cookie"与跳转目标，**不打印 Cookie 值**
                attempts.append(
                    f"{label}：HTTP {final.status}"
                    f"、{'有 Set-Cookie 头' if has_set_cookie else '无 Set-Cookie 头'}"
                    + (f"、Location={mask_url(location)}" if location else "")
                    + f"、未下发 Cookie（{mask_url(url)}）"
                )
                return None
            if landed_on_login(final):
                attempts.append(
                    f"{label}：HTTP {final.status}、拿到 Cookie {cookie_names} 但被弹回登录页"
                    f"（{mask_url(url)}）"
                )
                return None
            probe = self.is_session_alive()
            info = SessionInfo(
                ok=True,
                student_id=user_name,
                cookies=self.session.cookies(),
                service_url=mask_url(url),
                message=(
                    f"登录成功（REST 建会话：{label}；Cookie {cookie_names}"
                    f"；会话探测{'通过' if probe else '未通过（门户是 SPA，仅作参考）'}）"
                ),
            )
            self._session_info = info
            return info

        inline_ticket = self._extract_service_ticket(created)
        if inline_ticket:
            info = try_service_url(
                "第一步响应内联 ticket", self._service_url_with_ticket(target_service, inline_ticket)
            )
            if info is not None:
                return info

        def mint_ticket() -> str:
            """用同一个 TGT 现取一张新的 service ticket（ticket 是一次性的）。"""
            granted = self.session.post(
                f"{ticket_url}/{tgt}", data={"service": target_service}, allow_redirects=False,
                headers={"Accept": "application/json"},
            )
            return self._extract_service_ticket(granted) or self._extract_ticket_url(
                granted, ticket_url
            )

        # 第二步：用 TGT 换取 service ticket（标准 CAS 路径）
        try:
            info = try_service_url(
                "TGT 换 ticket", self._service_url_with_ticket(target_service, mint_ticket())
            )
            if info is not None:
                return info
        except AuthError as exc:
            attempts.append(f"TGT 换 ticket：{exc}")

        # CAS 2.0 校验：既可能直接下发会话，也能拿到服务器认可的学号（对排错极有价值）
        validate_url = self.config.cas.validate_url or self._derive_validate_url()
        if validate_url:
            try:
                fresh = mint_ticket()
                validated = self.session.get(
                    f"{validate_url}?ticket={fresh}&service={target_service}",
                    headers={"Accept": "application/json"},
                )
                body = (validated.text or "").strip().splitlines()
                verdict = body[0] if body else ""
                who = body[1] if len(body) > 1 else ""
                attempts.append(
                    f"CAS validate：HTTP {validated.status}、校验结果 {verdict or '(空)'}"
                    + (f"、服务器返回学号 {who}" if who else "")
                )
                if who:
                    # 校验通过：再取一张新 ticket，先访问 CAS 登录页取回会话，再回跳服务
                    self.session.get(f"{self.config.cas.login_url}?service={target_service}")
                    info = try_service_url(
                        "校验通过后回跳服务",
                        self._service_url_with_ticket(target_service, mint_ticket()),
                    )
                    if info is not None:
                        return info
                else:
                    info = try_service_url("TGT 直接当 ticket",
                                           self._service_url_with_ticket(target_service, tgt))
                    if info is not None:
                        return info
            except AuthError as exc:
                attempts.append(f"CAS validate：{exc}")

        keys = ", ".join(self._response_keys(created))
        raise AuthError(
            "REST 建会话没有拿到可用的会话 Cookie。\n"
            f"  第一步响应字段：{keys}\n"
            f"  已写入的前端等价 Cookie：{'、'.join(seeded) if seeded else '（无，底层会话不支持写 Cookie）'}\n"
            f"  已尝试的路径：{'；'.join(attempts)}\n"
            "  请把上面几行发给开发者；据此可判定真实生效的取票方式。"
        )

    def login_with_credentials(self, user_name: str, password: str, *, service: str | None = None,
                               strategy: str | None = None, login_type: str = "3") -> SessionInfo:
        """用外部凭据（例如微信扫码得到的账号密码）建立会话。

        ``strategy``：``rest`` 只走 ``/v1/tickets``；``form`` 只走 HTML 表单；``auto``（默认）
        先试 REST，失败再试表单，并在最终失败时同时给出两条路径的原因。

        注：升达的登录页是 **SPA 外壳**（没有 ``<form>``），因此该平台上 ``form`` 路径
        实际上不可能成功——``rest`` 才是真实可用的建会话方式（依据是 CAS 登录页自身的
        前端包，见 :meth:`login_with_rest` 的说明）。
        """
        if not user_name or not password:
            raise AuthError("凭据不完整（缺少账号或密码），无法建立会话。")
        chosen = (strategy or self.config.cas.establish_strategy or "auto").lower()

        if chosen == "form":
            return self.login(credentials=(user_name, password), service=service)
        if chosen == "rest":
            return self.login_with_rest(user_name, password, service=service, login_type=login_type)

        try:
            return self.login_with_rest(user_name, password, service=service, login_type=login_type)
        except CaptchaRequiredError:
            raise
        except AuthError as rest_error:
            try:
                info = self.login(credentials=(user_name, password), service=service)
                info.message = f"{info.message}（REST 不可用，已回退 HTML 表单；原因：{rest_error}）"
                return info
            except (AuthError, CaptchaRequiredError) as form_error:
                raise AuthError(
                    "扫码拿到了凭据，但两种建会话方式都没成功。\n"
                    f"  1) REST（{self.config.cas.wechat.rest_tickets_path}）：{rest_error}\n"
                    f"  2) HTML 表单（{mask_url(self.config.cas.login_url)}）：{form_error}\n"
                    "  REST 才是本平台真实可用的建会话方式；表单路径只在登录页真的是"
                    "服务端渲染表单时才可能成功（升达的登录页是 SPA，没有表单）。\n"
                    "  可改用账号密码登录：python -m qk login（先在 config.yaml 填好学号与密码）"
                ) from form_error

    @staticmethod
    def _response_keys(response: Any) -> list[str]:
        """响应的字段名（**只列名字，不含任何值**，便于排错又不泄露凭据）。"""
        payload = response.json()
        if isinstance(payload, Mapping):
            keys = sorted(str(key) for key in payload)
            data = payload.get("data")
            if isinstance(data, Mapping):
                keys += [f"data.{key}" for key in sorted(str(k) for k in data)]
            return keys
        return [f"<非 JSON，前 20 字符：{(response.text or '')[:20]!r}>"]

    @staticmethod
    def _extract_service_ticket(response: Any) -> str:
        """取第一步响应里直接给出的 service ticket（``ticket`` / ``data.ticket``）。"""
        payload = response.json()
        candidates: list[Any] = []
        if isinstance(payload, Mapping):
            candidates += [payload.get("ticket"), payload.get("ST"), payload.get("serviceTicket")]
            data = payload.get("data")
            if isinstance(data, Mapping):
                candidates += [data.get("ticket"), data.get("ST"), data.get("serviceTicket")]
            elif isinstance(data, str):
                candidates.append(data)
        for value in candidates:
            if isinstance(value, str) and value.strip():
                match = re.search(r"(ST-[A-Za-z0-9._\-]+)", value)
                return match.group(1) if match else value.strip()
        # 有些部署把 ticket 放在响应头
        for header in ("X-Ticket", "Ticket", "ticket"):
            value = response.headers.get(header) or response.headers.get(header.lower()) or ""
            if value:
                match = re.search(r"(ST-[A-Za-z0-9._\-]+)", value)
                return match.group(1) if match else value.strip()
        return ""

    @staticmethod
    def _service_url_with_ticket(service: str, ticket: str) -> str:
        """按前端方式拼出 ``service?ticket=…``（service 已带 query 时用 ``&``）。"""
        decoded = unquote(service)
        joiner = "&" if ("?" in decoded or "&" in decoded) else "?"
        return f"{decoded}{joiner}ticket={ticket}"

    def _seed_tgt_cookie(self, tgt: str) -> list[str]:
        """把 TGT 写成名为 ``CASTGC`` 的 Cookie（**平台前端就是这么做的**）。

        真实前端在 ``POST /v1/tickets`` 成功后会执行
        ``setCookie("CASTGC", tgt)`` 再跳转 ``service?ticket=…``；网关凭 ``CASTGC``
        识别会话。缺少这一步时，门户只会返回 SPA 外壳且**不下发任何会话 Cookie**
        （真机联调实测：三次 ``service?ticket=`` 全是 HTTP 200、无 Set-Cookie）。
        这里同时给 ``.shengda.edu.cn`` 与 CAS 主机各写一份，保证门户与教务都能读到。

        返回实际写入的 Cookie 名（供日志显示，不涉及值）。
        """
        jar = getattr(getattr(self.session, "transport", None), "_session", None)
        cookies = getattr(jar, "cookies", None)
        if cookies is None or not hasattr(cookies, "set"):
            return []
        host = urlparse(self.cas_base()).hostname or ""
        if not host or host == "localhost" or "." not in host or _is_ip_literal(host):
            # 只对真实域名播种：IP/localhost（例如本地 mock）没有这套网关 Cookie 机制，
            # 写进去只会污染会话里的 Cookie 名单。
            return []
        parent = "." + ".".join(host.split(".")[-2:])
        written: list[str] = []
        for domain in dict.fromkeys([parent, host]):  # 去重且保持顺序
            try:
                cookies.set("CASTGC", tgt, domain=domain, path="/")
                written.append(f"CASTGC@{domain}")
            except (TypeError, ValueError):  # pragma: no cover - 域名异常时忽略
                continue
        return written

    def _extract_tgt(self, response: Any, ticket_url: str) -> str:
        """从 REST 第一步的响应里取出 TGT。

        真实前端对响应的处理是：``if (o.data.tgt) c = o.data.tgt; else c = o.data``
        ——即 TGT 可能在 JSON 的 ``tgt`` 字段、``data`` 字段，或直接是响应正文本身；
        另外标准 CAS 会把 TGT 放在 ``Location`` 头。这里把这几种形态都认了。
        """
        location = response.headers.get("Location") or response.headers.get("location") or ""
        for candidate in (location, response.text):
            if not candidate:
                continue
            match = re.search(r"(TGT-[A-Za-z0-9._\-]+)", candidate)
            if match:
                return match.group(1)
        payload = response.json()
        if isinstance(payload, Mapping):
            for key in ("tgt", "TGT", "data", "ticket"):
                value = payload.get(key)
                if isinstance(value, Mapping):
                    value = value.get("tgt") or value.get("TGT")
                if isinstance(value, str) and value:
                    nested = re.search(r"(TGT-[A-Za-z0-9._\-]+)", value)
                    return nested.group(1) if nested else value
        raise AuthError(
            f"REST 建会话第一步没有返回 TGT（HTTP {response.status}，地址 {mask_url(ticket_url)}）。"
            f"响应片段：{mask_url(response.text[:120])}"
        )

    def _extract_ticket_url(self, response: Any, ticket_url: str) -> str:
        """从 REST 第二步的响应里取出带 ticket 的 service 地址。

        前端取的是响应正文（``getService1``）；可能是完整 URL、JSON 里的字符串，
        也可能只有 ``ticket=`` 片段，因此逐个形态兜底。
        """
        target_service = self.config.cas.service or self.config.jwgl.base_url
        candidates = [
            response.headers.get("Location") or "",
            response.text or "",
        ]
        payload = response.json()
        if isinstance(payload, Mapping):
            for key in ("data", "service", "url", "redirectUrl", "location"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)
        elif isinstance(payload, str):
            candidates.append(payload)

        for candidate in candidates:
            if not candidate:
                continue
            if "ticket=" in candidate:
                match = re.search(r"https?://[^\s\"'<>]+ticket=[^\s\"'<>]+", candidate)
                if match:
                    return match.group(0)
                match = re.search(r"/[^\s\"'<>]*ticket=[^\s\"'<>]+", candidate)
                if match:
                    return urljoin(target_service, match.group(0))
            # 只给了 ticket 本身（如 "ST-1-abc"）时按 service 拼出地址
            bare = re.fullmatch(r"\s*(ST-[A-Za-z0-9._\-]+)\s*", candidate)
            if bare:
                joiner = "&" if "?" in target_service else "?"
                return f"{target_service}{joiner}ticket={bare.group(1)}"
        raise AuthError(
            f"REST 建会话第二步没有拿到带 ticket 的地址（HTTP {response.status}，地址 {mask_url(ticket_url)}）。"
            f"响应片段：{mask_url(response.text[:120])}"
        )

    def _build_callback(self, location: str, ticket: str) -> str:
        """构造 ticket 校验地址（标准 CAS：``validate?ticket=...&service=...``）。

        ``cas.validate_url`` 显式配置时用它；留空则按 ``login_url`` 推导。
        真实服务器的 ``Location`` 有时只是 ``/portal`` 之类的相对路径，因此
        ``http(s)://`` 开头的绝对地址直接当 service 用，否则与 ``cas.service`` 拼接。
        """
        validate = self.config.cas.validate_url or self._derive_validate_url()
        if not validate:
            # 既没配置也推导不出时，退回“带 ticket 直接访问 service”的老式流程
            if location.startswith(("http://", "https://")):
                return location
            service = self.config.cas.service or self.config.jwgl.base_url
            return urljoin(service, location) if location.startswith("/") else service

        if location.startswith(("http://", "https://")):
            service = location
        else:
            base = self.config.cas.service or self.config.jwgl.base_url
            service = urljoin(base, location) if location else base
        params = {"ticket": ticket}
        if service:
            params["service"] = service
        joiner = "&" if "?" in validate else "?"
        return f"{validate}{joiner}{urlencode(params)}"

    def is_session_alive(self) -> bool:
        """探测当前会话是否仍然有效。"""
        if not self.session.cookies():
            return False
        target = self.config.cas.test_login_path or self.config.jwgl.index_path
        if not target:
            return bool(self.session.cookies())
        url = urljoin(self.config.jwgl.base_url, target)
        try:
            result: HttpResult = self.session.get(url)
        except TransportError:
            return False
        if result.status in (301, 302, 303, 307, 308):
            location = result.headers.get("Location", "")
            return "login" not in location.lower()
        return result.ok and looks_authenticated(result.text, self.config.cas.success_keywords)

    def logout(self) -> None:
        """尽力登出；失败不抛异常。"""
        url = self.config.cas.logout_url
        if not url:
            return
        try:
            self.session.get(url)
        except TransportError:
            return
        finally:
            self._session_ok = False
            self._session_info = None

    @property
    def info(self) -> SessionInfo | None:
        return self._session_info


def build_session_for_cas(config: AppConfig, **overrides: Any) -> QkSession:
    """按配置构造 CAS 会话（供 CLI 与测试复用）。"""
    return QkSession.from_config(config, **overrides)


__all__ = [
    "CasLoginAdapter",
    "build_session_for_cas",
    "detect_captcha",
    "extract_form_action",
    "extract_form_fields",
    "extract_ticket",
    "looks_authenticated",
    "looks_like_auth_failure",
]
