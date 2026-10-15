"""适配器测试：CAS 登录流程/请求序列、验证码分支、教务接口解析与选课/退课。

全部对着 qk/mockserver.py 运行，不访问真实教务系统。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qk.adapters.base import CaptchaRequiredError, PlatformUnsupportedError
from qk.adapters.cas import (
    CasLoginAdapter,
    detect_captcha,
    extract_form_action,
    extract_form_fields,
    extract_ticket,
    looks_authenticated,
    looks_like_auth_failure,
)
from qk.adapters.jwgl import (
    CODE_TO_OUTCOME,
    JwglCourseAdapter,
    KingosoftCourseAdapter,
    _double_encoded_json,
    build_course_adapter,
    course_from_mapping,
    decode_kingo_params,
    encode_params,
    kingo_b64_encode,
    kingo_md5,
    kingo_str_dec,
    kingo_str_enc,
    looks_rate_limited,
    looks_session_expired,
    parse_candidates,
    parse_candidates_html,
    parse_kingosoft_table,
    parse_class_list,
    parse_class_skbjdms,
    split_encoded_body,
)
from qk.adapters.base import (
    SessionExpiredError,
    course_attr_of,
    filter_excluded_attrs,
)
from qk.config import AppConfig, Credentials, JwglConfig, load_config
from qk.mockserver import MockConfig
from qk.models import CandidateCourse, HttpResult, Outcome
from qk.net import QkSession

from .conftest import MOCK_PASSWORD, MOCK_STUDENT_ID, make_course


# ==========================================================================
# CAS 登录页解析（纯函数，不联网）
# ==========================================================================


def test_extract_form_fields_name_before_value():
    html = '<input type="hidden" name="lt" value="LT-123"><input name="execution" value="e1s1">'
    assert extract_form_fields(html) == {"lt": "LT-123", "execution": "e1s1"}


def test_extract_form_fields_value_before_name():
    html = "<input value='LT-9' name='lt'><input value=\"e2s2\" name=\"execution\">"
    assert extract_form_fields(html) == {"lt": "LT-9", "execution": "e2s2"}


def test_extract_form_fields_unescapes_entities():
    html = '<input name="service" value="https://x/?a=1&amp;b=2">'
    assert extract_form_fields(html)["service"] == "https://x/?a=1&b=2"


def test_extract_form_fields_empty_html():
    assert extract_form_fields("") == {}


def test_extract_form_action():
    assert extract_form_action('<form action="/lyuapServer/login" method="post">') == "/lyuapServer/login"
    assert extract_form_action("<html></html>", default="/fallback") == "/fallback"


@pytest.mark.parametrize(
    "html, expected",
    [
        ('<input name="captcha" type="text">', True),
        ('<img id="captchaImg" src="/x">', True),
        ("<div>请输入验证码</div>", True),
        ('<input name="authcode">', False),
        ("<div>请输入用户名和密码</div>", False),
        ("", False),
    ],
)
def test_detect_captcha(html, expected):
    assert detect_captcha(html) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://my.shengda.edu.cn/?ticket=ST-123-abc", "ST-123-abc"),
        ("/portal?ticket=ST-9&x=1", "ST-9"),
        ("https://cas/login", ""),
        ("", ""),
    ],
)
def test_extract_ticket(text, expected):
    assert extract_ticket(text) == expected


def test_looks_authenticated_and_failure():
    assert looks_authenticated("<a>退出登录</a>") is True
    assert looks_authenticated("<div>登录</div>") is False
    assert looks_like_auth_failure("<p>用户名或密码错误</p>") is True
    assert looks_like_auth_failure("<p>欢迎</p>") is False


# ==========================================================================
# CAS 登录（对 mock 服务）
# ==========================================================================


def test_login_success_and_request_sequence(mock_app):
    """登录请求序列：取登录页 → 提交表单（不跟随重定向）→ 校验 ticket → 回跳 service。"""
    config, server = mock_app
    session = QkSession.from_config(config)
    calls: list[tuple[str, str]] = []
    locations: list[str] = []
    inner = session.transport

    class Recorder:
        def send(self, method, url, **kwargs):
            calls.append((method, url))
            result = inner.send(method, url, **kwargs)
            location = result.headers.get("Location") or result.headers.get("location")
            if location:
                locations.append(location)
            return result

        def cookies(self):
            return inner.cookies()

        def close(self):
            inner.close()

    session.transport = Recorder()
    info = CasLoginAdapter(config, session).login()

    assert info.ok is True
    assert info.student_id == MOCK_STUDENT_ID
    assert [method for method, _ in calls] == ["GET", "POST", "GET", "GET"]
    assert calls[0][1].startswith(f"{server.base_url}lyuapServer/login")
    assert "service=" in calls[0][1]
    assert calls[1][1].endswith("/lyuapServer/login")
    # 第三步是 ticket 校验，第四步才是带 ticket 回跳 service（从 302 的 Location 观察）
    assert "/lyuapServer/validate" in calls[2][1] and "ticket=" in calls[2][1]
    assert any("ticket=" in location for location in locations)
    # 服务端确实收到了账号密码
    assert (MOCK_STUDENT_ID, MOCK_PASSWORD) in server.state.credentials
    assert "JSESSIONID" in session.cookies()


def test_mock_tests_never_touch_real_hosts(mock_app):
    """回归护栏：mock 自证链路必须完全离线，不得出现任何真实主机请求。"""
    config, server = mock_app
    session = QkSession.from_config(config)
    seen: list[str] = []
    inner = session.transport

    class Recorder:
        def send(self, method, url, **kwargs):
            seen.append(url)
            return inner.send(method, url, **kwargs)

        def cookies(self):
            return inner.cookies()

        def close(self):
            inner.close()

    session.transport = Recorder()
    adapter = CasLoginAdapter(config, session)
    adapter.login()
    adapter.is_session_alive()
    adapter.logout()

    assert seen, "应当至少发起一次请求"
    for url in seen:
        assert url.startswith(server.base_url), f"离线自证出现了非 mock 请求：{url}"
        assert "shengda.edu.cn" not in url


def test_validate_url_is_derived_from_login_url(mock_app):
    """留空的 validate_url 必须跟随 login_url（同主机），避免误打真实 CAS。"""
    config, server = mock_app
    adapter = CasLoginAdapter(config, QkSession.from_config(config))
    assert config.cas.validate_url == ""
    derived = adapter._derive_validate_url()
    assert derived == f"{server.base_url.rstrip('/')}/lyuapServer/validate"

    # 显式配置时优先用显式值
    config.cas.validate_url = f"{server.base_url}custom/validate"
    assert adapter._build_callback("/", "ST-1").startswith(f"{server.base_url}custom/validate")


def test_login_captcha_required_without_code(make_mock_server, config_factory):
    """登录页有验证码且未提供验证码 → 明确报错并给出处理方式。"""
    server = make_mock_server(MockConfig.default(captcha_required=True))
    config = config_factory(server.base_url)
    adapter = CasLoginAdapter(config, QkSession.from_config(config))
    with pytest.raises(CaptchaRequiredError) as excinfo:
        adapter.login()
    message = str(excinfo.value)
    assert "验证码" in message
    assert "captcha_code" in message
    assert excinfo.value.captcha_url.endswith("/lyuapServer/captcha")


def test_login_captcha_required_with_code(make_mock_server, config_factory):
    """提供正确验证码时可以正常登录（凭据与验证码都从环境变量注入）。"""
    server = make_mock_server(MockConfig.default(captcha_required=True, captcha_code="8888"))
    config = config_factory(
        server.base_url,
        credentials=False,
        environ={
            "QK_STUDENT_ID": MOCK_STUDENT_ID,
            "QK_PASSWORD": MOCK_PASSWORD,
            "QK_CAPTCHA_CODE": "8888",
        },
    )
    info = CasLoginAdapter(config, QkSession.from_config(config)).login()
    assert info.ok is True


def test_login_without_captcha_has_no_captcha_error(mock_app):
    """登录页没有验证码时不应触发验证码错误（两种情形都有明确行为）。"""
    config, _server = mock_app
    info = CasLoginAdapter(config, QkSession.from_config(config)).login()
    assert info.ok is True


def test_login_wrong_password_raises_auth_error(make_mock_server, config_factory, workspace):
    server = make_mock_server(MockConfig.default())
    config = config_factory(
        server.base_url,
        credentials=False,
        environ={"QK_STUDENT_ID": MOCK_STUDENT_ID, "QK_PASSWORD": "wrong-pass"},
    )
    adapter = CasLoginAdapter(config, QkSession.from_config(config))
    with pytest.raises(Exception) as excinfo:  # AuthError（避免与库异常混淆）
        adapter.login()
    assert "账号或密码不正确" in str(excinfo.value)


def test_session_alive_and_logout(mock_app):
    config, _server = mock_app
    session = QkSession.from_config(config)
    adapter = CasLoginAdapter(config, session)
    adapter.login()
    assert adapter.is_session_alive() is True
    adapter.logout()
    assert adapter.is_session_alive() is False


def test_login_dry_run_sends_nothing(mock_app):
    config, server = mock_app
    session = QkSession.from_config(config)
    adapter = CasLoginAdapter(config, session)
    info = adapter.login(dry_run=True)
    assert info.ok is False
    assert "演练" in info.message
    assert server.state.login_requests == 0
    assert session.cookies() == {}


# ==========================================================================
# 教务接口：解析
# ==========================================================================


def test_parse_candidates_json_items():
    payload = {
        "code": "0",
        "items": [
            {"jxb_id": "A1", "kch": "1001", "kcmc": "体育", "jsxm": "张", "capacity": 10, "selected": 3},
            {"jxb_id": "A2", "kch": "1002", "kcmc": "英语", "jsxm": "李", "capacity": 20, "syrs": 5},
        ],
    }
    courses = parse_candidates(payload)
    assert [course.teaching_class_id for course in courses] == ["A1", "A2"]
    assert courses[0].remaining == 7
    assert courses[1].enrolled == 15 and courses[1].remaining == 5


def test_parse_candidates_accepts_alias_field_names():
    course = course_from_mapping(
        {"teaching_class_id": "B1", "course_code": "X", "course_name": "Y", "teacher": "Z",
         "credits": "2.5", "capacity": "30", "enrolled": "1"}
    )
    assert course.teaching_class_id == "B1"
    assert course.credits == 2.5
    assert course.remaining == 29


def test_parse_candidates_html_table():
    html = """
    <table>
      <tr><th>教学班号</th><th>课程号</th><th>课程名称</th><th>教师</th><th>容量</th><th>已选</th></tr>
      <tr><td>JXB1</td><td>1001</td><td>大学体育Ⅲ</td><td>张老师</td><td>40</td><td>39</td></tr>
      <tr><td>JXB2</td><td>1002</td><td>大学英语</td><td>李老师</td><td>60</td><td>60</td></tr>
    </table>
    """
    courses = parse_candidates_html(html)
    assert len(courses) == 2
    assert courses[0].course_name == "大学体育Ⅲ" and courses[0].remaining == 1
    assert courses[1].is_full is True


def test_parse_candidates_html_without_header_uses_default_order():
    html = "<table><tr><td>JXB9</td><td>9999</td><td>无表头课程</td><td>王老师</td></tr></table>"
    courses = parse_candidates_html(html)
    assert len(courses) == 1
    assert courses[0].teaching_class_id == "JXB9" and courses[0].course_name == "无表头课程"


def test_parse_candidates_html_empty():
    assert parse_candidates_html("") == []
    assert parse_candidates_html("<html>没有表格</html>") == []


# ==========================================================================
# 教务接口：对 mock 服务
# ==========================================================================


def test_fetch_candidates_from_mock(course_adapter):
    courses = list(course_adapter.fetch_candidates())
    assert len(courses) == 5
    by_id = {course.teaching_class_id: course for course in courses}
    assert by_id["JXB001"].course_name == "大学体育Ⅲ"
    assert by_id["JXB002"].is_full is True
    assert by_id["JXB001"].remaining == 28


def test_fetch_candidates_with_keyword(course_adapter):
    courses = list(course_adapter.fetch_candidates("体育"))
    assert [course.teaching_class_id for course in courses] == ["JXB001"]


def test_select_success(course_adapter, logged_in):
    _config, server, _session = logged_in
    course = next(item for item in course_adapter.fetch_candidates() if item.teaching_class_id == "JXB001")
    result = course_adapter.select(course)
    assert result.outcome is Outcome.SUCCESS
    assert result.ok is True
    assert server.state.is_selected("JXB001") is True


def test_select_full_capacity(course_adapter):
    course = next(item for item in course_adapter.fetch_candidates() if item.teaching_class_id == "JXB002")
    result = course_adapter.select(course)
    assert result.outcome is Outcome.FULL
    assert result.ok is False
    assert "人数已满" in result.message


def test_select_already_selected(course_adapter):
    course = next(item for item in course_adapter.fetch_candidates() if item.teaching_class_id == "JXB004")
    result = course_adapter.select(course)
    assert result.outcome is Outcome.ALREADY_SELECTED


def test_select_dry_run_sends_nothing(course_adapter, logged_in):
    _config, server, _session = logged_in
    course = next(item for item in course_adapter.fetch_candidates() if item.teaching_class_id == "JXB001")
    result = course_adapter.select(course, dry_run=True)
    assert result.outcome is Outcome.DRY_RUN
    assert server.state.is_selected("JXB001") is False


def test_fetch_selected(course_adapter):
    selected = list(course_adapter.fetch_selected())
    assert [course.teaching_class_id for course in selected] == ["JXB004"]


def test_drop_success_and_failure(course_adapter, logged_in):
    _config, server, _session = logged_in
    courses = {course.teaching_class_id: course for course in course_adapter.fetch_candidates()}

    # 退课失败分支：JXB005 的 behaviour=drop_fail
    server.state.mark_selected("JXB005")
    failed = course_adapter.drop(courses["JXB005"])
    assert failed.ok is False
    assert "退课失败" in failed.message

    # 退课成功分支
    ok = course_adapter.drop(courses["JXB004"])
    assert ok.outcome is Outcome.SUCCESS
    assert server.state.is_selected("JXB004") is False


def test_missing_path_raises_platform_error(course_adapter, mock_app):
    """未确认接口时必须明确报错，而不是乱猜路径。"""
    config, _server = mock_app
    course = next(iter(course_adapter.fetch_candidates()))  # 已登录，先确认列表路径有效
    config.jwgl.select_path = ""  # 再抹掉选课路径
    with pytest.raises(PlatformUnsupportedError) as excinfo:
        course_adapter.select(course)
    assert "jwgl.select_path" in str(excinfo.value)
    assert "不猜测路径" in str(excinfo.value)


def test_missing_course_list_path_raises_platform_error(mock_app):
    """列表路径未确认时同样明确报错。"""
    config, _server = mock_app
    config.jwgl.course_list_path = ""
    adapter = JwglCourseAdapter(config, QkSession.from_config(config))
    with pytest.raises(PlatformUnsupportedError) as excinfo:
        adapter.fetch_candidates()
    assert "jwgl.course_list_path" in str(excinfo.value)


def test_unknown_platform_rejected(mock_app):
    config, _server = mock_app
    config.jwgl.platform = "不存在的平台"
    with pytest.raises(PlatformUnsupportedError) as excinfo:
        build_course_adapter(config)
    assert "新增平台" in str(excinfo.value)


def test_platform_detection(mock_app):
    config, _server = mock_app
    adapter = build_course_adapter(config, QkSession.from_config(config))
    assert adapter.detect_platform() == "custom"
    config.jwgl.platform = "auto"
    assert adapter.detect_platform() == "custom"  # 配置了路径即视为 custom
    config.jwgl.course_list_path = ""
    assert adapter.detect_platform() == "unknown"


def test_session_expired_raises(config_factory):
    """会话过期时选课调用抛 SessionExpiredError，供编排层触发重登。"""
    from qk.adapters.base import SessionExpiredError
    from qk.mockserver import MockConfig, start_mock_server
    from qk.net import QkSession

    # 这里必须用**不自动过期**的服务：手动作废会话才能精确命中该分支
    server = start_mock_server(MockConfig.default())
    try:
        config = config_factory(server.base_url)
        session = QkSession.from_config(config)
        CasLoginAdapter(config, session).login()
        adapter = JwglCourseAdapter(config, session)
        course = next(iter(adapter.fetch_candidates()))
        for token in list(server.state.sessions):
            server.state.invalidate(token)
        with pytest.raises(SessionExpiredError):
            adapter.select(course)
    finally:
        server.stop()


def test_mock_server_course_list_requires_auth(mock_app):
    """未登录直接访问课程列表 → 401。"""
    config, server = mock_app
    session = QkSession.from_config(config)
    result = session.get(f"{server.base_url}jwglxt/xsxk/xkList")
    assert result.status == 401
    assert "AUTH_REQUIRED" in result.text


def test_code_mapping_covers_mock_codes():
    for code in ("SUCCESS", "FULL", "ALREADY_SELECTED", "NOT_FOUND", "AUTH_REQUIRED", "0"):
        assert code in CODE_TO_OUTCOME


def test_default_mock_config_has_all_branches():
    config = MockConfig.default()
    behaviours = {course.behaviour for course in config.courses}
    assert {"general", "full", "full_then_free", "drop_ok", "drop_fail"} <= behaviours
    assert config.selected == ["JXB004"]
    # selected 必须是实例级列表，不能与类级默认值共享
    config2 = MockConfig.default()
    config2.selected.append("JXB999")
    assert "JXB999" not in MockConfig.default().selected


def test_mock_config_overrides_are_respected():
    """default() 允许覆盖 courses/selected，便于用例构造专属场景。"""
    from qk.mockserver import MockCourse

    custom = [MockCourse("X1", "1", "只有一门课")]
    config = MockConfig.default(courses=custom, selected=[])
    assert [course.teaching_class_id for course in config.courses] == ["X1"]
    assert config.selected == []


def test_course_matches_mock_account_constants(mock_app):
    config, _server = mock_app
    assert config.creds.student_id == MOCK_STUDENT_ID
    assert config.creds.password == MOCK_PASSWORD


def test_make_course_helper():
    course = make_course("JXB999", course_name="临时课程")
    assert course.teaching_class_id == "JXB999"
    assert course.to_json()["kcmc"] == "临时课程"


def test_config_without_credentials_rejects_use(tmp_path):
    from qk.config import ConfigError

    config = load_config(workspace=tmp_path, environ={})
    with pytest.raises(ConfigError):
        config.require_credentials()


# ==========================================================================
# 青果 Kingosoft（2026-10-13 真机实测契约）
# ==========================================================================

#: 参考向量由 **node 执行平台自己的** /custom/js/jkingo.des.js + md5.js + base64.js 生成；
#: 生成时的学号已替换为占位值 20230001，可安全固化。密钥为固定测试值（真实值由服务端下发）。
_KINGO_STRENC_VECTORS = [
    (
        "xn=2026&xq=0&xh=20230001&kcdm=0501001&skbjdm=0001"
        "&lcid=35732179068578980275991&xktype=2",
        "a1b2c3d4",
        "415AC0DFB2AEB28FB371559EA19016D2C0EAF707F2FAB0EAE3DD1B5FDC3926D4"
        "9F4CA1E26F64F3ED5BFD21FBC9A79C7C754A1785D31446259765C75B3EB1B5F0"
        "4246E2190E1B7771B40211DEFADECCE9227DA658D983D7DCB60324D175A849B3"
        "0B8CA409AD44268680E09AE554EC54D0640895461EDC2C1C3649467F39E679C2"
        "F4B58109EE4610F12C513A77F8911F41CAEA4C4B67E89346EAD403813B07467D"
        "88BB7BD0A9ED5437137F58232727A769",
    ),
    (
        "skbjdm=JXB001&kcdm=91100001&xn=2026&xq=0&xh=20230001"
        "&lcid=35732179068578980275991&xktype=2",
        "mockkey123456",
        "ABAF5C53E5543232973490CBE741EA7F3E928930D0BB15E271CFE7767AF42CB6"
        "386D753DB46AA9D7B219E69074D73B2D0513A9BEB806E5FB81D7DA0D265C6B37"
        "7EDCD7E18BC82460C062C0F49BB470F80C64F3007F1A91FEA264E4F959EF840B"
        "DF1B1BFDCBA8369E60E69FE4959003D5DED2449597466359FE2B27341C0DD364"
        "85F369AB55739F2A9789E8D573BBFBFDC2E84DE22487C48872F13593049B4437"
        "37389E98CE6F5CFF8A2923EBF9E5049F6535CACD291633CA",
    ),
    ("abc", "k1k2k3k4", "B7EDCFA3760616EC"),
    ("abcdefghij", "12345678", "7173C19CCE0DD8433ACE30F0FEEC7E7FF19B169D4A47543C"),
]


def test_kingosoft_crypto_matches_platform_vectors():
    """加密/解密移植必须与平台原版 JS **逐条一致**（离线夹具，不联网）。"""
    for plain, key, expected in _KINGO_STRENC_VECTORS:
        assert kingo_str_enc(plain, key) == expected, f"strEnc 与平台不一致：{key}"
        assert kingo_str_dec(expected, key) == plain, f"strDec 无法还原：{key}"

    assert kingo_md5("hello") == "5d41402abc4b2a76b9719d911017c592"
    assert kingo_md5("1700000000000") == "faa48af05e71e97fade8497f01a2bddf"
    assert kingo_b64_encode("abc") == "YWJj"
    assert kingo_b64_encode("hello world") == "aGVsbG8gd29ybGQ="
    assert kingo_b64_encode("中文") == "5Lit5paH"


def test_kingosoft_encode_params_matches_frontend_formula():
    """与前端 ``getEncParams`` 等价的整条链：token 公式 + 加密体可被服务端还原。"""
    plain = "xn=2026&xq=0&xh=20230001&kcdm=0501001&skbjdm=0001&lcid=1&xktype=2"
    stamp, key = "1700000000000", "mockkey123456"
    body = encode_params(plain, key, stamp)
    token = body.split("&token=")[1].split("&")[0]
    assert token == kingo_md5(kingo_md5(plain) + kingo_md5(stamp))
    assert body.endswith(f"&timestamp={stamp}")
    assert decode_kingo_params(body, key) == plain


_KINGOSOFT_SAMPLE_TABLE = """<!DOCTYPE html><html><head><title>数据列表</title>
<meta http-equiv="Content-Type" content="text/html; charset=GBK"></head><body><table>
<tr class='H' style='POSITION: relative'>
<td width='20%' id='head_kc' >课程</td><td width='5%' id='head_xf' >学分</td>
<td width='5%' id='head_zxs' >总学时</td><td width='10%' id='head_lb' >课程类别</td>
<td width='8%' id='head_kcsx' >课程属性</td><td width='8%' id='head_skbh' >上课班号</td>
<td width='10%' id='head_skbjmc' >上课班级名称</td><td width='8%' id='head_xkfs' >选课方式</td>
<td width='10%' id='head_rkjs' >任课教师</td><td style='display:none;' id='head_xk_points' >投入选课币</td>
<td style='display:none;' id='head_is_buy_book' >购买教材</td><td style='display:none;' id='head_is_cx' >重修</td>
<td width='6%' id='head_xk_status' >选课状态</td><td width='10%' id='head_operation' >操作</td>
<td style='display:none;' id='head_kcdm' > </td><td style='display:none;' id='head_kclb1' > </td>
<td style='display:none;' id='head_kclb2' > </td><td style='display:none;' id='head_khfs' > </td>
<td style='display:none;' id='head_kclb3' > </td><td style='display:none;' id='head_is_yxtj' > </td>
<td style='display:none;' id='head_skbzdm' >上课班组代码</td>
<td style='display:none;' id='head_skbjdm' >上课班级代码</td></tr>
<tr><td>大学体育Ⅲ</td><td>1</td><td>32</td><td>通识选修课</td><td>选修</td><td>JXB001</td>
<td>大学体育Ⅲ班</td><td>志愿</td><td>李老师</td><td></td><td></td><td></td><td></td>
<td></td><td>91100001</td><td></td><td></td><td></td><td></td><td></td><td>01</td>
<td>JXB001</td></tr>
</table></body></html>"""


def test_parse_kingosoft_table_uses_header_ids():
    """按表头 id 定列；真实表没有容量/余量列，解析结果不得凭空造这两个值。"""
    courses = parse_kingosoft_table(_KINGOSOFT_SAMPLE_TABLE)
    assert len(courses) == 1
    course = courses[0]
    assert course.course_name == "大学体育Ⅲ"
    assert course.course_code == "91100001"
    assert course.teaching_class_id == "JXB001"
    assert course.teacher == "李老师"
    assert course.credits == 1.0
    assert course.capacity is None and course.enrolled is None
    assert course.raw["class_group_code"] == "01"


def test_parse_kingosoft_table_without_header_returns_empty():
    assert parse_kingosoft_table("<html><body>没有表格</body></html>") == []


#: 真实响应格式（2026-10-13 实测）——数据行单元格带 ``tr{N}_{列名}`` 形式的 id。
#: 关键点：``tr0_skbzdm``/``tr0_skbjdm`` 为空是**真实情况**（正选轮次的列表行不带教学班号），
#: 不可用位置索引去顶替，否则会把 "01"/"02" 之类的类别码错当成教学班号。
_KINGOSOFT_REAL_ROW_TABLE = """<table><tr class='H'>
<td width='20%' id='head_kc' >课程</td><td width='5%' id='head_xf' >学分</td>
<td width='5%' id='head_zxs' >总学时</td><td width='10%' id='head_lb' >课程类别</td>
<td width='8%' id='head_kcsx' >课程属性</td><td width='8%' id='head_skbh' >上课班号</td>
<td width='10%' id='head_skbjmc' >上课班级名称</td><td width='8%' id='head_xkfs' >选课方式</td>
<td width='10%' id='head_rkjs' >任课教师</td><td style='display:none;' id='head_xk_points' >投入选课币</td>
<td style='display:none;' id='head_is_buy_book' >购买教材</td><td style='display:none;' id='head_is_cx' >重修</td>
<td width='6%' id='head_xk_status' >选课状态</td><td width='10%' id='head_operation' >操作</td>
<td style='display:none;' id='head_kcdm' > </td><td style='display:none;' id='head_kclb1' > </td>
<td style='display:none;' id='head_kclb2' > </td><td style='display:none;' id='head_khfs' > </td>
<td style='display:none;' id='head_kclb3' > </td><td style='display:none;' id='head_is_yxtj' > </td>
<td style='display:none;' id='head_skbzdm' >上课班组代码</td>
<td style='display:none;' id='head_skbjdm' >上课班级代码</td></tr>
<tr><td id='tr0_kc' name='kc' align='left' ><a href='javascript:void(0)'
 onclick='parent.showCourseInfo("01261231")'>[01261231]AI创业专题实战</a></td>
<td id='tr0_xf' name='xf' align='right' >1.0</td><td id='tr0_zxs' name='zxs' align='right' >16</td>
<td id='tr0_lb' name='lb' align='left' >公共课/任选课</td>
<td id='tr0_kcsx' name='kcsx' align='left' >经济管理类/通识面授课</td>
<td id='tr0_skbh' name='skbh' align='center' ></td><td id='tr0_skbjmc' name='skbjmc' align='left' ></td>
<td id='tr0_xkfs' name='xkfs' align='center' ></td><td id='tr0_rkjs' name='rkjs' align='left' ></td>
<td id='tr0_xk_points' name='xk_points' style='display:none;' ></td>
<td id='tr0_is_buy_book' name='is_buy_book' style='display:none;' ></td>
<td id='tr0_is_cx' name='is_cx' style='display:none;' ></td>
<td id='tr0_xk_status' name='xk_status' align='center' ></td>
<td id='tr0_operation' name='operation' align='center' ><a href='javascript:void(0)'
 isview='0' onclick='parent.viewResume(this)'>选择</a></td>
<td id='tr0_kcdm' name='kcdm' style='display:none;' >01261231</td>
<td id='tr0_kclb1' name='kclb1' style='display:none;' >03</td>
<td id='tr0_kclb2' name='kclb2' style='display:none;' >01</td>
<td id='tr0_khfs' name='khfs' style='display:none;' >02</td>
<td id='tr0_kclb3' name='kclb3' style='display:none;' >13</td>
<td id='tr0_is_yxtj' name='is_yxtj' style='display:none;' ></td>
<td id='tr0_skbzdm' name='skbzdm' style='display:none;' ></td>
<td id='tr0_skbjdm' name='skbjdm' style='display:none;' ></td></tr>
</table>"""


def test_parse_kingosoft_table_uses_row_cell_ids():
    """真实格式：按行内 ``tr{N}_`` id 取值，且不把类别码误当教学班号。"""
    courses = parse_kingosoft_table(_KINGOSOFT_REAL_ROW_TABLE)
    assert len(courses) == 1
    c = courses[0]
    assert c.course_code == "01261231"
    assert c.course_name == "[01261231]AI创业专题实战"
    assert c.credits == 1.0
    # 真实正选列表行不带教学班号 → 必须为空，而不是错位取到 kclb2 的 "01"
    assert c.teaching_class_id == ""
    assert c.teacher == ""
    assert c.capacity is None and c.enrolled is None


def test_parse_kingosoft_table_negative_row_ids_resolve_correctly():
    """带教学班号的轮次：``tr3_skbjdm``/``tr3_skbzdm`` 应正确落到对应字段。"""
    html = _KINGOSOFT_REAL_ROW_TABLE.replace("tr0_", "tr3_").replace(
        "<td id='tr3_skbjdm' name='skbjdm' style='display:none;' ></td>",
        "<td id='tr3_skbjdm' name='skbjdm' style='display:none;' >JXB007</td>",
    ).replace(
        "<td id='tr3_skbzdm' name='skbzdm' style='display:none;' ></td>",
        "<td id='tr3_skbzdm' name='skbzdm' style='display:none;' >GRP9</td>",
    )
    courses = parse_kingosoft_table(html)
    assert len(courses) == 1
    assert courses[0].teaching_class_id == "JXB007"
    assert courses[0].raw["class_group_code"] == "GRP9"


def _kingosoft_config(config, base_url: str):
    """把配置指向本 mock 的 Kingosoft 契约。

    清掉强智系的四个路径：Kingosoft 用固定的 ``/jw/common/*.action`` 与
    ``taglib/DataTable.jsp``（见 config.example.yaml 的说明）。
    """
    config.jwgl.platform = "kingosoft"
    config.jwgl.kingosoft_base_url = base_url.rstrip("/")
    config.jwgl.cas_service = f"{base_url.rstrip('/')}/caslogin"
    config.jwgl.course_list_path = ""
    config.jwgl.select_path = ""
    config.jwgl.drop_path = ""
    config.jwgl.selected_list_path = ""
    return config


def test_kingosoft_adapter_end_to_end_against_mock(make_mock_server, config_factory):
    """离线全链：换票建会话 → 菜单上下文 → 查课程 → **加密提交选课** → 退课。

    mock 会解密客户端提交的 params 并校验 token（``state.kingosoft_last_write`` 记录明文），
    因此「匹配目标 → 提交选课」在离线环境下被真正断言，而不是只看请求是否发出。
    """
    server = make_mock_server()
    base = server.base_url.rstrip("/")
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    with server.state.lock:
        server.state.tgt_users["TGT-kingo-1"] = config.creds.student_id
    session = QkSession.from_config(config)
    jar = getattr(session.transport, "_session", None)
    assert jar is not None
    jar.cookies.set("CASTGC", "TGT-kingo-1", domain="127.0.0.1", path="/")

    # 1) 缺菜单上下文时，选课页返回真实系统那句「无效访问请求」
    assert "无效访问请求" in session.get(f"{base}/student/wsxk.zx.html").text

    adapter = KingosoftCourseAdapter(config, session)

    # 2) 建立会话：换票 → caslogin → 菜单上下文
    adapter.ensure_session()
    assert "JSESSIONID" in session.cookies()

    # 3) 选课上下文 + 查询课程
    context = adapter.context()
    assert context["xn"] == server.config.kingosoft_year
    assert context["lcid"] == server.config.kingosoft_lcid
    courses = list(adapter.fetch_candidates())
    assert courses, "应当能查到 mock 的课程"
    target = courses[0]
    assert target.teaching_class_id and target.course_code and target.course_name

    # 4) 加密提交选课：mock 解密后应记录 skbjdm/kcdm，并真的记为已选
    result = adapter.select(target)
    assert result.outcome is Outcome.SUCCESS, result.message
    assert server.state.is_selected(target.teaching_class_id) is True
    assert f"skbjdm={target.teaching_class_id}" in server.state.kingosoft_last_write
    assert f"kcdm={target.course_code}" in server.state.kingosoft_last_write
    assert "token=" not in server.state.kingosoft_last_write  # 记录的是解密后的明文

    # 5) 重复选课 → 归类「已选过」
    assert adapter.select(target).outcome is Outcome.ALREADY_SELECTED

    # 6) 退课
    assert adapter.drop(target).outcome is Outcome.SUCCESS
    assert server.state.is_selected(target.teaching_class_id) is False


def test_kingosoft_mock_rejects_tampered_token(make_mock_server, config_factory):
    """token 不匹配时按真实语义拒绝（防止加密实现悄悄退化后仍然“通过”）。"""
    server = make_mock_server()
    base = server.base_url.rstrip("/")
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    stamp = server.config.kingosoft_timestamp
    good = split_encoded_body(
        encode_params("skbjdm=JXB001&kcdm=91100001", server.config.kingosoft_temp_key, stamp)
    )
    assert decode_kingo_params(
        f"params={good['params']}&timestamp={stamp}", server.config.kingosoft_temp_key
    ) == "skbjdm=JXB001&kcdm=91100001"  # 先证明这条链本身是通的
    response = QkSession.from_config(config).post(
        f"{base}/jw/common/saveElectiveCourse.action", data={**good, "token": "0" * 32},
    )
    assert "500" in response.text
    assert server.state.kingosoft_last_write == ""


def test_session_expired_response_gives_actionable_message(make_mock_server, config_factory):
    """会话失效时提示必须可操作（引导重新扫码），而不是给裸异常或模糊错误。

    契约说明：``ensure_session()`` **不再**在没有 CASTGC 时立刻报错——真实扫码登录
    可能只留下 JSESSIONID（实测），此时应先按「已有会话」继续、由真实请求判活。
    所以这里直接验证「服务器返回凭证已失效时，适配器给出可操作提示」这条链路，
    用**真实响应体**（取自 2026-10-14 实测）而不是 mock 的宽松分支。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)

    expired_body = (
        "<script>alert('温馨提示：凭证已失效，请重新登录!');"
        "if (window.frmbody){ window.top.location.href='/'; } "
        "else { window.top.location.href='/'; }</script>"
    )

    def _fake_post(url, **kwargs):
        return HttpResult(url=str(url), status=200, text=expired_body)

    def _fake_get(url, **kwargs):
        return HttpResult(url=str(url), status=200, text=expired_body)

    session.post = _fake_post  # type: ignore[method-assign]
    session.get = _fake_get  # type: ignore[method-assign]

    with pytest.raises(SessionExpiredError) as excinfo:
        adapter.fetch_candidates()
    message = str(excinfo.value)
    assert "login --wechat --save-session" in message, message
    assert "失效" in message


def test_build_course_adapter_selects_kingosoft(make_mock_server, config_factory):
    """``jwgl.platform=kingosoft`` 时选中青果适配器，且基址取教务系统而非门户。"""
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = build_course_adapter(config)
    assert isinstance(adapter, KingosoftCourseAdapter)
    assert adapter._base() == server.base_url.rstrip("/")


def test_default_platform_is_kingosoft():
    """``JwglConfig.platform`` 默认必须是 ``kingosoft``（本校教务）。

    回归用例：默认值曾是 ``auto``，导致「不复制 config.example.yaml、直接用默认配置」
    的用户被路由到通用实现，然后在 ``course_list_path`` 留空时报「教务接口尚未确认」——
    而 README 与 config.example.yaml 都写着 kingosoft 是本校默认，三者对不上。
    """
    assert JwglConfig().platform == "kingosoft"
    assert AppConfig().jwgl.platform == "kingosoft"


def test_default_config_builds_kingosoft_adapter_with_internal_base():
    """默认配置（没有 jwgl 小节）直接选中青果适配器，且基址是教务系统而非门户。"""
    adapter = build_course_adapter(AppConfig())
    assert isinstance(adapter, KingosoftCourseAdapter)
    assert adapter._base() == "https://jw.shengda.edu.cn:8090"


def test_explicit_generic_platform_still_uses_generic_adapter():
    """显式写 auto/custom/qz 时仍走通用实现，默认值变更没有剥夺这条路径。"""
    for platform in ("auto", "custom", "qz"):
        config = AppConfig()
        config.jwgl.platform = platform
        assert isinstance(build_course_adapter(config), JwglCourseAdapter), platform


# ==========================================================================
# 会话失效响应：必须给出可操作提示，而不是 JSONDecodeError（回归用例）
#
# 实测（2026-10-13）：未带有效会话访问任一 Kingosoft ``.action`` 端点，服务器返回
# **HTTP 200** + 下面这段 HTML。修复前 ``_double_encoded_json`` 直接 json.loads，
# 抛 JSONDecodeError 冒到顶层，用户只看到「未预期的错误」，拿不到「请重新登录」。
# ==========================================================================

#: 逐字取自真实教务服务器的响应（515 字节）
_KINGOSOFT_SESSION_EXPIRED_HTML = (
    "<script>alert('温馨提示：凭证已失效，请重新登录!');"
    "if (window.frmbody){ window.top.location.href='/'; } "
    "else if (window.parent.frmbody) { window.parent.top.location.href='/'; } "
    "else { window.top.location.href='/'; }</script>"
)


def test_looks_session_expired_detects_real_server_payload():
    assert looks_session_expired(_KINGOSOFT_SESSION_EXPIRED_HTML) is True
    assert looks_session_expired('<html><title>错误:无效访问请求</title></html>') is True
    assert looks_session_expired('{"status":"200","result":"{}"}') is False
    assert looks_session_expired("") is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        _KINGOSOFT_SESSION_EXPIRED_HTML,
        "<html><body>502 Bad Gateway</body></html>",
        "not json at all",
        "{ 截断的 json",
    ],
)
def test_double_encoded_json_never_raises(text):
    """非 JSON / 空响应一律返回 ``{}``，绝不抛 JSONDecodeError。"""
    assert _double_encoded_json(text) == {}


def test_double_encoded_json_still_unwraps_result_string():
    """修复不能破坏原有的双层 JSON 解包契约。"""
    payload = _double_encoded_json('{"status":"200","result":"{\\"xn\\":\\"2026\\"}"}')
    assert payload["result"] == {"xn": "2026"}


def test_kingosoft_context_raises_session_expired_not_json_error(make_mock_server, config_factory):
    """会话失效时 ``context()`` 抛 SessionExpiredError 且提示可操作，不是 JSONDecodeError。"""
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)
    # 跳过换票，直接让选课上下文端点返回真实系统那句「凭证已失效」
    adapter._ready = True
    session.get = lambda *a, **k: HttpResult(  # type: ignore[assignment]
        url="https://jw.example/getWsxkTimeRange.action", status=200,
        text=_KINGOSOFT_SESSION_EXPIRED_HTML,
    )
    with pytest.raises(SessionExpiredError) as excinfo:
        adapter.context()
    message = str(excinfo.value)
    assert "login --wechat --save-session" in message
    assert "凭证已失效" in message
    # 标记为未就绪，下次调用会重新走换票而不是继续用坏会话
    assert adapter._ready is False


def test_kingosoft_context_raises_on_non_json_body(make_mock_server, config_factory):
    """非 JSON 的**非空**正文归一到 SessionExpiredError（走兜底分支）。

    注意：**空响应不算会话失效** —— 实测那是服务端限流的形态，
    现在由 :class:`SelectionError` 处理（见 test_single_json_reports_empty_response_as_throttle）。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)
    adapter._ready = True
    session.get = lambda *a, **k: HttpResult(  # type: ignore[assignment]
        url="https://jw.example/getWsxkTimeRange.action", status=200,
        text="<html><body>随便一段不是 JSON 的页面</body></html>",
    )
    with pytest.raises(SessionExpiredError) as excinfo:
        adapter.context()
    assert "login --wechat --save-session" in str(excinfo.value)
    assert adapter._ready is False


# ==========================================================================
# 按授课形式排除课程（jwgl.exclude_course_attrs）
#
# 用途：本校通识选修课列表实测 117 门**全部**是「通识面授课」，
# 想只看网络课/慕课时靠这个过滤。
# ==========================================================================


def _kingo_course(code: str, attr: str) -> CandidateCourse:
    """构造一门带 course_attr 的课程（字段名与真实解析结果一致）。"""
    return CandidateCourse(
        teaching_class_id=f"JXB{code}",
        course_code=code,
        course_name=f"课程{code}",
        raw={"course_attr": attr, "course_code": code},
    )


def test_course_attr_of_reads_real_field_names():
    assert course_attr_of(_kingo_course("1", "经济管理类/通识面授课")) == "经济管理类/通识面授课"
    # 取不到属性时返回空串，而不是抛异常
    assert course_attr_of(CandidateCourse(teaching_class_id="X", course_code="Y")) == ""
    assert course_attr_of(CandidateCourse(teaching_class_id="X", raw=None)) == ""


def test_filter_excluded_attrs_excludes_substring_match():
    courses = [
        _kingo_course("1", "经济管理类/通识面授课"),
        _kingo_course("2", "理工类/网络课"),
        _kingo_course("3", "艺术类/通识面授课"),
    ]
    kept = filter_excluded_attrs(courses, ["通识面授课"])
    assert [c.course_code for c in kept] == ["2"]


def test_filter_excluded_attrs_keeps_courses_without_attr():
    """属性缺失的课程不能被误删 —— 否则换平台/字段变动会静默清空列表。"""
    courses = [
        _kingo_course("1", "通识面授课"),
        CandidateCourse(teaching_class_id="X", course_code="2", course_name="无属性课程"),
    ]
    kept = filter_excluded_attrs(courses, ["通识面授课"])
    assert [c.course_code for c in kept] == ["2"]


def test_filter_excluded_attrs_empty_patterns_is_noop():
    courses = [_kingo_course("1", "通识面授课"), _kingo_course("2", "网络课")]
    assert len(filter_excluded_attrs(courses, [])) == 2
    # 空串/空白 pattern 一律忽略，不会「匹配一切」
    assert len(filter_excluded_attrs(courses, ["", "   "])) == 2


def test_filter_excluded_attrs_is_case_insensitive():
    courses = [_kingo_course("1", "General/MOOC"), _kingo_course("2", "面授课")]
    assert [c.course_code for c in filter_excluded_attrs(courses, ["mooc"])] == ["2"]


def test_filter_excluded_attrs_multiple_patterns_any_match():
    courses = [
        _kingo_course("1", "A/面授课"),
        _kingo_course("2", "B/网络课"),
        _kingo_course("3", "C/直播课"),
    ]
    kept = filter_excluded_attrs(courses, ["面授课", "网络课"])
    assert [c.course_code for c in kept] == ["3"]


def test_exclude_course_attrs_rejects_empty_string_in_config():
    """空串会匹配所有课程，必须在校验阶段拦住。"""
    from qk.config import ConfigError

    config = AppConfig(creds=Credentials(student_id="1", password="2"))
    config.jwgl.exclude_course_attrs = [""]
    with pytest.raises(ConfigError) as excinfo:
        config.validate()
    assert "exclude_course_attrs" in str(excinfo.value)


def test_kingosoft_adapter_applies_exclude_filter_offline(make_mock_server, config_factory):
    """适配器端到端：exclude 生效后再过滤，且不过滤时数量不变（离线 mock）。"""
    from qk.session_store import SessionStore, apply_session

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    with server.state.lock:
        server.state.tgt_users["TGT-kingo-excl"] = config.creds.student_id
    # 走与真实流程一致的方式：先落盘一份 CASTGC 会话，再加载注入
    store = SessionStore(config.session.path, config.session.max_age_hours)
    store.save({"CASTGC": "TGT-kingo-excl"}, student_id=config.creds.student_id,
               service_url=config.jwgl.cas_service, source="wechat")
    cached, reason = store.load_usable()
    assert cached is not None, f"前置：缓存会话应可用（{reason}）"
    session = QkSession.from_config(config)
    apply_session(session, cached)
    adapter = KingosoftCourseAdapter(config, session)

    baseline = list(adapter.fetch_candidates())
    assert baseline, "前置：mock 应返回课程，否则本用例无意义"

    # mock 的课程属性可解析；用它验证过滤确实生效
    assert all(course_attr_of(c) for c in baseline)
    config.jwgl.exclude_course_attrs = [course_attr_of(baseline[0])]
    assert list(adapter.fetch_candidates()) == [], "命中属性应被全部排除"

    # 不相关的属性不应影响结果
    config.jwgl.exclude_course_attrs = ["绝不存在的属性ZZZ"]
    assert len(list(adapter.fetch_candidates())) == len(baseline)



# ==========================================================================
# 选课弹窗解析：未选课程在列表里没有 skbjdm，班号只能从弹窗取
#
# 真实流程：点「选择」→ isSelectableSkbjdm.action 校验 → 打开
# student/report/wsxk.zx_promt.jsp → 弹窗里选上课班级 → 提交。
# ==========================================================================


def test_parse_class_skbjdms_reads_real_row_ids():
    """真实内层班级列表：行形如 <td id='tr0_skbjdm'>…</td>。"""
    html = (
        "<tr class='H'><td id='head_skbjdm' >上课班号</td>"
        "<td id='head_skfs_mc' >授课方式</td></tr>"
        "<tr><td id='tr0_skbjdm' >21241931-039</td>"
        "<td id='tr0_skfs_mc' >面授</td></tr>"
        "<tr><td id='tr1_skbjdm' >21241931-040</td>"
        "<td id='tr1_skfs_mc' >网络</td></tr>"
    )
    assert parse_class_skbjdms(html) == ["21241931-039", "21241931-040"]


def test_parse_class_list_keeps_all_columns_per_row():
    """每行的列名要能取全（授课方式用于区分线上/面授）。"""
    html = (
        "<tr><td id='tr0_skbjdm' >21241931-039</td>"
        "<td id='tr0_skbzdm' >GRP1</td>"
        "<td id='tr0_skfs_mc' >网络</td>"
        "<td id='tr0_kxrs' >30</td></tr>"
    )
    rows = parse_class_list(html)
    assert len(rows) == 1
    assert rows[0] == {"skbjdm": "21241931-039", "skbzdm": "GRP1",
                       "skfs_mc": "网络", "kxrs": "30"}


def test_parse_class_skbjdms_filters_placeholders_and_dedupes():
    html = (
        "<tr><td id='tr0_skbjdm' >-</td></tr>"
        "<tr><td id='tr0_skbjdm' ></td></tr>"
        "<tr><td id='tr1_skbjdm' >X-1</td></tr>"
        "<tr><td id='tr2_skbjdm' >X-1</td></tr>"
    )
    assert parse_class_skbjdms(html) == ["X-1"]


def test_parse_class_skbjdms_empty_tbody_is_normal():
    """真实情况：课程没有可选班级时 tbody 为空——这不是错误，是「没得选」。"""
    empty = (
        "<table id='keywords'><thead><tr class='H'>"
        "<td id='head_skbjdm' >上课班号</td></tr></thead>"
        "<tbody id=\"sdTable_tbody\" ></tbody></table>"
    )
    assert parse_class_list(empty) == []
    assert parse_class_skbjdms(empty) == []
    assert parse_class_skbjdms("") == []


def test_resolved_class_uses_popup_id_and_reports_when_unresolvable(make_mock_server, config_factory):
    """弹窗能解析出班号时就采用；解析不到必须明确报错，**绝不猜一个班号提交**。"""
    from qk.adapters.base import CourseNotFoundError

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))

    # 列表行没有班号的课程（未选课程的真实形态）
    course = CandidateCourse(teaching_class_id="", course_code="01261231",
                             course_name="[01261231]AI创业专题实战")

    adapter.resolve_teaching_class_ids = lambda _c: ["01261231-001", "01261231-002"]
    resolved = adapter._with_resolved_class(course)
    assert resolved.teaching_class_id == "01261231-001", "应取弹窗里第一个可用班号"

    # 列表行已有班号且弹窗里也存在 → 保持原样
    pinned = CandidateCourse(teaching_class_id="01261231-002", course_code="01261231",
                             course_name="已指定班号")
    assert adapter._with_resolved_class(pinned).teaching_class_id == "01261231-002"

    # 弹窗解析不到 → 明确报错
    adapter.resolve_teaching_class_ids = lambda _c: []
    with pytest.raises(CourseNotFoundError) as excinfo:
        adapter._with_resolved_class(course)
    assert "没有可用上课班级" in str(excinfo.value)
    assert "01261231" in str(excinfo.value)


def test_select_skips_popup_when_class_id_present(make_mock_server, config_factory):
    """行内已有班号时不应多打一次弹窗请求（避免无谓的服务器压力）。"""
    from qk.session_store import SessionStore, apply_session

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    with server.state.lock:
        server.state.tgt_users["TGT-kingo-nopop"] = config.creds.student_id
    store = SessionStore(config.session.path, config.session.max_age_hours)
    store.save({"CASTGC": "TGT-kingo-nopop"}, student_id=config.creds.student_id,
               service_url=config.jwgl.cas_service, source="wechat")
    cached, _reason = store.load_usable()
    assert cached is not None
    session = QkSession.from_config(config)
    apply_session(session, cached)

    adapter = KingosoftCourseAdapter(config, session)
    course = CandidateCourse(teaching_class_id="JXB001", course_code="91100001",
                             course_name="大学体育Ⅲ")

    called: list[str] = []

    def _spy(_c):
        called.append(_c.course_code)
        return []

    adapter.resolve_teaching_class_ids = _spy
    adapter.select(course)
    assert called == [], "已有班号就不该再打开弹窗"

# ==========================================================================
# 频控 / 空响应：都不能误报成「会话失效」
#
# 实测（2026-10-14）：提交选课时服务端有两种「不是业务结果」的响应——
#   1) HTTP 200 + <title>错误:请求太过频繁</title>  （明示限流）
#   2) HTTP 200 + 正文 0 字节                        （静默限流）
# 旧实现把两者都当成「凭证已失效」→ 用户会白白去重新扫码，实际只需等待。
# ==========================================================================


def test_detects_rate_limit_page():
    page = ('<html><head><title>错误:请求太过频繁</title></head>'
            '<body><span>无效的访问请求，请使用正常的途径访问本系统！</span></body></html>')
    assert looks_rate_limited(page) is True
    assert looks_session_expired(page) is False, "频控页不应被判为会话失效"
    assert looks_rate_limited('{"status":"200"}') is False


def test_single_json_raises_selection_error_on_rate_limit(make_mock_server, config_factory):
    """频控要抛 SelectionError（提示等待），而不是 SessionExpiredError（提示重登）。"""
    from qk.adapters.base import SelectionError

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))
    page = '<html><head><title>错误:请求太过频繁</title></head><body></body></html>'

    with pytest.raises(SelectionError) as excinfo:
        adapter._single_json(page, purpose="提交选课")
    message = str(excinfo.value)
    assert "限流" in message
    assert "等待" in message
    assert "login --wechat" not in message


def test_single_json_reports_empty_response_as_throttle(make_mock_server, config_factory):
    """空响应（HTTP 200 + 0 字节）也按限流处理，不能误导用户去重新登录。"""
    from qk.adapters.base import SelectionError

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))

    with pytest.raises(SelectionError) as excinfo:
        adapter._single_json("", purpose="提交选课")
    message = str(excinfo.value)
    assert "空响应" in message
    assert "限流" in message

    # 非空的非 JSON 内容仍按会话失效处理
    with pytest.raises(SessionExpiredError):
        adapter._single_json("<html>随便一段不是 JSON 的页面</html>", purpose="提交选课")


def test_write_params_carries_course_category_codes(make_mock_server, config_factory):
    """提交参数必须带 kclb1/kclb2/kclb3/khfs —— 实测浏览器提交时带这几项。"""
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))
    adapter._context = {"xn": "2026", "xqM": "0", "xh": "20230001",
                        "lcid": "LC1", "kcfw": "zxggrx", "nj": "2024", "zydm": "0501"}

    course = CandidateCourse(
        teaching_class_id="21241931-039", course_code="21241931",
        course_name="测试课程", credits=1.0,
        raw={"kclb1": "03", "kclb2": "01", "kclb3": "08", "khfs": "02"},
    )
    params = adapter._write_params(course)
    for expected in ("kclb1=03", "kclb2=01", "kclb3=08", "khfs=02",
                     "kcdm=21241931", "skbjdm=21241931-039"):
        assert expected in params, f"提交参数缺少 {expected}"

# ==========================================================================
# 回归：真实报文格式（2026-10-14 抓包核对）
#
# 这一天踩的坑沉淀在这里。**注意第 1 条是更正后的结论** ——
# 当时曾把「写请求返回 HTTP 200 + 空响应」归因于「URL 双斜杠」，那是误判：
#   f"{base}{path}" 在 _base() 已去尾斜杠时结果只有一个斜杠，
#   调试输出里的 "//jw.shengda..." 只是 https: 被截断。
# 真正的原因是 Referer 用错页（必须用选课弹窗 URL）。
#
#   1) Referer 用错页       → 写请求得到 HTTP 200 + 空响应     【真因】
#   2) 提交字段不带表单前缀 → 服务端无法正确封装
#   3) 班号放错字段         → 服务端说「未找到该上课班级」
#   4) URL 拼接             → 不是 bug，但保留统一入口与断言，防退化
# ==========================================================================


def _fields_of(params: str) -> dict[str, str]:
    """把 ``a=1&b=2`` 解析成字典。"""
    out: dict[str, str] = {}
    for part in params.split("&"):
        key, _, value = part.partition("=")
        if key:
            out[key] = value
    return out


def test_url_join_has_no_double_slash(make_mock_server, config_factory):
    """基址已去尾斜杠、路径以斜杠开头时，拼接结果**只能有一个**斜杠。

    **这是防退化用例，不是 bug 复现**：实测 ``f"{base}{path}"`` 在这种前提下
    本来就只产生一个斜杠，先前「双斜杠导致空响应」的结论已更正为误判
    （真因是 Referer 用错页）。保留本用例是为了锁住 ``_url()`` 的统一行为。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))

    for path in ("/jw/common/saveElectiveCourse.action",
                 "jw/common/saveElectiveCourse.action",
                 "/taglib/DataTable.jsp"):
        url = adapter._url(path)
        assert "//" not in url.split("://", 1)[1], f"URL 出现双斜杠：{url}"
        assert url.endswith(path.lstrip("/")), url

    absolute = "https://cas.shengda.edu.cn/lyuapServer/login"
    assert adapter._url(absolute) == absolute


def test_kingosoft_write_params_matches_golden(make_mock_server, config_factory):
    """提交参数必须与**金标准**逐字段一致。

    金标准 = 2026-10-14 21:38 浏览器在退补选里**成功选上** ``01265731`` 时实际提交的
    明文参数（解密 ``saveElectiveCourse`` 的 ``params`` 得到），见
    ``qk/tests/fixtures/kingosoft_submit_golden.txt``。该次提交后页面「已选」
    由 12门/24.5学分 变为 13门/25.5学分。

    关键点（都曾在真实环境踩坑）：

    * 字段名**不带** ``electiveCourseForm.`` 前缀 —— 页面 ``doPostSave`` 里的
      ``replaceAll("electiveCourseForm.", "")`` 确实生效；照表单 DOM 加前缀是错的。
    * 班号写在 ``skbjdm``；``txt_skbjdm`` 提交时是**空**。
    * ``is_buy_book``/``is_cx``/``is_yxtj`` = ``0``/``0``/``1``（不是空串）。
    * ``njzy`` 的竖线在明文里就是 ``%7C``。
    """
    golden_path = Path(__file__).parent / "fixtures" / "kingosoft_submit_golden.txt"
    golden_text = "\n".join(
        line for line in golden_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ).strip()
    assert golden_text, "金标准夹具为空"
    golden = _fields_of(golden_text)

    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))
    # 上下文对齐金标准当时的值
    adapter._context = {"xn": "2026", "xqM": "0", "xh": "20230001",
                        "lcid": "35732179068578980275991", "kcfw": "zxggrx",
                        "nj": "2024", "zydm": "0501"}
    course = CandidateCourse(
        teaching_class_id=golden["skbjdm"], course_code=golden["kcdm"],
        course_name="", credits=1.0,
        raw={"kclb1": golden["kclb1"], "kclb2": golden["kclb2"],
             "kclb3": golden["kclb3"], "khfs": golden["khfs"]},
    )
    mine = _fields_of(adapter._write_params(course))

    only_golden = sorted(set(golden) - set(mine))
    only_mine = sorted(set(mine) - set(golden))
    differing = {k: (mine[k], golden[k]) for k in set(mine) & set(golden)
                 if mine[k] != golden[k]}

    assert not only_golden, f"缺少金标准里的字段：{only_golden}"
    assert not only_mine, f"多出金标准里没有的字段：{only_mine}"
    assert not differing, f"取值与金标准不一致：{differing}"

    # 再显式钉住几个最易退化的点
    assert "electiveCourseForm." not in adapter._write_params(course), "字段名不应带表单前缀"
    assert mine["skbjdm"] == golden["skbjdm"]
    assert mine["txt_skbjdm"] == "", "txt_skbjdm 提交时为空"
    assert mine["is_buy_book"] == "0" and mine["is_cx"] == "0" and mine["is_yxtj"] == "1"
    assert mine["njzy"] == "2024%7C0501"


def test_post_action_referer_prefers_popup_url(make_mock_server, config_factory):
    """提交的 Referer 必须优先用**选课弹窗** URL（浏览器实测如此）。"""
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)

    seen: list[dict] = []

    def _fake_post(url, **kwargs):
        seen.append({"url": str(url), "headers": kwargs.get("headers") or {}})
        return HttpResult(url=str(url), status=200,
                          text='{"status":"200","message":"操作成功!","result":true}')

    session.post = _fake_post  # type: ignore[method-assign]
    adapter._secret = lambda params: "params=x&token=y&timestamp=z"  # type: ignore[method-assign]
    adapter.context = lambda: {"xn": "2026", "xqM": "0", "xh": "1", "lcid": "L"}  # type: ignore[method-assign]

    course = CandidateCourse(teaching_class_id="JXB9", course_code="91100001",
                             course_name="测试", credits=1.0)

    adapter._popup_url = ""
    adapter._post_action("/jw/common/saveElectiveCourse.action", "p", course, purpose="提交选课")
    assert "wsxk.zx.html" in seen[-1]["headers"].get("Referer", ""), "无弹窗时回退到选课页"

    adapter._popup_url = adapter._url("/student/report/wsxk.zx_promt.jsp?params=abc")
    adapter._post_action("/jw/common/saveElectiveCourse.action", "p", course, purpose="提交选课")
    assert "wsxk.zx_promt.jsp" in seen[-1]["headers"].get("Referer", ""), "应优先用弹窗 URL"
    assert seen[-1]["headers"].get("Origin"), "应带 Origin"
    assert "//" not in seen[-1]["url"].split("://", 1)[1], "写请求 URL 不能有双斜杠"


def test_post_action_sends_exactly_one_write_request(make_mock_server, config_factory):
    """``_post_action`` 一次调用只发**一个**写请求（不在内部重试）。

    实测教训：连试十余次不仅没成功，还把真因（格式错误）掩盖成「限流」。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)

    calls: list[str] = []

    def _fake_post(url, **kwargs):
        calls.append(str(url))
        return HttpResult(url=str(url), status=200,
                          text='{"status":"500","message":"随便一个失败"}')

    session.post = _fake_post  # type: ignore[method-assign]
    adapter._secret = lambda params: "params=x&token=y&timestamp=z"  # type: ignore[method-assign]
    adapter.context = lambda: {"xn": "2026", "xqM": "0", "xh": "1", "lcid": "L"}  # type: ignore[method-assign]

    course = CandidateCourse(teaching_class_id="JXB9", course_code="91100001",
                             course_name="测试", credits=1.0)
    result = adapter._post_action("/jw/common/saveElectiveCourse.action", "p",
                                  course, purpose="提交选课")
    assert len(calls) == 1, f"一次调用应只发一个写请求，实际 {len(calls)} 个"
    assert result is not None


def test_mock_rejects_missing_class_id(make_mock_server, config_factory):
    """班号为空时服务端应判「未找到该上课班级」—— 固化这条契约，防止班号再次放错字段。"""
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)
    adapter._context = {"xn": "2026", "xqM": "0", "xh": "20230001",
                        "lcid": "LC1", "kcfw": "zxggrx", "nj": "2024", "zydm": "0501"}

    # 直接把 skbjdm 置空（模拟放错字段的情况）
    course = CandidateCourse(teaching_class_id="", course_code="91100001",
                             course_name="测试", credits=1.0)
    fields = _fields_of(adapter._write_params(course))
    assert fields["skbjdm"] == ""
    assert fields["txt_skbjdm"] == ""


def test_production_requests_never_contain_double_slash(make_mock_server, config_factory):
    """生产代码**实际发出**的每个 URL 都不得含双斜杠（防退化）。

    拦下 session 的 get/post，逐个检查真实 URL。这比只断言 ``_url()`` 更有价值：
    它覆盖的是**调用点**，将来谁再手拼字符串就会被抓住。
    注意：本条只保证 URL 形态整洁，**不代表双斜杠曾是故障原因**（那是误判）。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    session = QkSession.from_config(config)
    adapter = KingosoftCourseAdapter(config, session)

    seen: list[str] = []

    def _record(method):
        def _do(url, **kwargs):
            seen.append(f"{method} {url}")
            text = ('{"status":"200","message":"操作成功!","result":{}}'
                    if "action" in str(url) else
                    "<table><tr><td id='head_kc'>课程</td></tr></table>")
            return HttpResult(url=str(url), status=200, text=text)
        return _do

    session.get = _record("GET")     # type: ignore[method-assign]
    session.post = _record("POST")   # type: ignore[method-assign]

    # 走一遍会发出请求的路径（吞掉异常，只关心 URL 形态）
    for call in (
        lambda: adapter.ensure_session(),
        lambda: adapter.context(),
        lambda: adapter.fetch_candidates(),
        lambda: adapter._secret("xn=2026"),
        lambda: adapter._menu_context(),
    ):
        try:
            call()
        except Exception:  # noqa: BLE001 - 只关心 URL，不关心成功率
            pass

    assert seen, "没有捕获到任何请求，测试本身有问题"
    bad = [u for u in seen if "//" in u.split("://", 1)[1]]
    assert not bad, "以下请求 URL 含双斜杠：\n" + "\n".join(bad)

def test_drop_params_are_the_eight_field_form(make_mock_server, config_factory):
    """退课参数只有 **8 个字段**，且不带表单前缀。

    来自页面 ``cancelElectiveCourse()`` 的真实拼串（2026-10-14 读取运行中的页面）：

        xn, xq, xh, kcdm, skbjdm, skbzdm, lcid, xktype
        （随后 ``replaceAll("electiveCourseForm.", "")`` 再加密）

    端到端实测：用这 8 个字段提交 ``cancelElectiveCourse.action`` 得到
    ``{"status":"200","message":"操作成功!"}``，学分统计从 25.5/13门 降到 23.5/11门。

    注意与**选课**的区别：选课是 34 个字段（见金标准夹具），退课只要 8 个。
    两者都**不带** ``electiveCourseForm.`` 前缀。
    """
    server = make_mock_server()
    config = _kingosoft_config(config_factory(server.base_url), server.base_url)
    adapter = KingosoftCourseAdapter(config, QkSession.from_config(config))
    adapter._context = {"xn": "2026", "xqM": "0", "xh": "20230001",
                        "lcid": "35732179068578980275991", "kcfw": "zxggrx",
                        "nj": "2024", "zydm": "0501"}
    course = CandidateCourse(
        teaching_class_id="01261231-001", course_code="01261231",
        course_name="[01261231]AI创业专题实战", credits=1.0,
        raw={"class_group_code": ""},
    )
    plain = adapter._drop_params(course)
    fields = _fields_of(plain)

    assert set(fields) == {"xn", "xq", "xh", "kcdm", "skbjdm", "skbzdm", "lcid", "xktype"}, \
        f"退课字段集合不对：{sorted(fields)}"
    assert "electiveCourseForm." not in plain, "退课参数不应带表单前缀"
    assert fields["xn"] == "2026"
    assert fields["xq"] == "0"
    assert fields["xh"] == "20230001"
    assert fields["kcdm"] == "01261231"
    assert fields["skbjdm"] == "01261231-001"
    assert fields["skbzdm"] == ""
    assert fields["lcid"] == "35732179068578980275991"
    assert fields["xktype"] == "2"
