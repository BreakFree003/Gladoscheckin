"""checkin.py 的测试。

覆盖的失败模式 (来自 2026-09-24 起线上 Actions 日志与上游 issue #37):
- GLaDOS 自 2026-09 起把会话 Cookie 拆成两套: glados.cloud 用 gld:sess/gld:sess.sig,
  railgun.info 用 koa:sess/koa:sess.sig (上游 issue #37 评论 5845370610 的实测反馈)。
  只在一个站点注册的用户只能拿到其中一对, 因此「没有一对完整」才是配置错误。
- 旧版本脚本把失败咽掉, 进程退出码始终为 0, 于是 Actions 显示绿色但实际没签到。
- 同一个 Cookie 会依次请求 glados.cloud 与 railgun.info, 通常只有其中一个站点
  持有该账号, 另一个必然返回 -2；这属于正常现象, 不应判定为账号失败。
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import checkin  # noqa: E402  (需要先注入仓库根目录到 sys.path)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_SENTINEL = "SENTINEL_VALUE_MUST_NOT_BE_LOGGED"

GLADOS_SITE_COOKIE = f"gld:sess={COOKIE_SENTINEL}_gsess; gld:sess.sig={COOKIE_SENTINEL}_gsig"
RAILGUN_SITE_COOKIE = f"koa:sess={COOKIE_SENTINEL}_sess; koa:sess.sig={COOKIE_SENTINEL}_sig"
BOTH_SITES_COOKIE = f"{GLADOS_SITE_COOKIE}; {RAILGUN_SITE_COOKIE}"
# 两对会话字段都不完整: 只有 gld:sess (缺 .sig), koa 那对完全没有
INCOMPLETE_SESSION_COOKIE = f"gld:sess={COOKIE_SENTINEL}_gsess; theme=dark"
# 完全不像 Cookie 的输入
NON_COOKIE_INPUT = f"not-a-cookie={COOKIE_SENTINEL}"


# --------------------------------------------------------------------------
# Cookie 结构解析
# --------------------------------------------------------------------------


def test_parse_cookie_keys_returns_field_names_without_values():
    """需求: 解析只给出字段名, 任何日志路径都不得泄露 Cookie 值。"""
    keys = checkin.parse_cookie_keys(BOTH_SITES_COOKIE)

    assert keys == ["gld:sess", "gld:sess.sig", "koa:sess", "koa:sess.sig"]
    assert all(COOKIE_SENTINEL not in key for key in keys)


def test_parse_cookie_keys_tolerates_missing_spaces_and_trailing_semicolon():
    """失败模式: 用户从浏览器复制的 Cookie 分隔符不规范时不应解析错位。"""
    keys = checkin.parse_cookie_keys("koa:sess=a;koa:sess.sig=b; gld:sess=c;;")

    assert keys == ["koa:sess", "koa:sess.sig", "gld:sess"]


def test_complete_cookie_sites_matches_each_site_own_pair():
    """需求: gld 那对属 glados.cloud、koa 那对属 railgun.info, 两对齐全则两个站点都算。

    期望值来自上游 issue #37 评论里两类用户的实测: 单站点用户手里只有一对 Cookie。
    """
    assert checkin.complete_cookie_sites(GLADOS_SITE_COOKIE) == ["glados.cloud"]
    assert checkin.complete_cookie_sites(RAILGUN_SITE_COOKIE) == ["railgun.info"]
    assert checkin.complete_cookie_sites(BOTH_SITES_COOKIE) == [
        "glados.cloud",
        "railgun.info",
    ]


def test_complete_cookie_sites_rejects_partial_or_non_cookie_input():
    """失败模式: 只抄了半对 / 根本没抄会话字段时必须判定为没有可用站点。"""
    assert checkin.complete_cookie_sites(INCOMPLETE_SESSION_COOKIE) == []
    assert checkin.complete_cookie_sites(NON_COOKIE_INPUT) == []


def test_missing_cookie_keys_reports_only_requested_fields():
    """需求: 缺字段诊断只针对给定站点那一对, 不把另一站点的字段算成缺失。"""
    assert checkin.missing_cookie_keys(GLADOS_SITE_COOKIE, checkin.SITE_COOKIE_KEYS["glados.cloud"]) == []
    assert checkin.missing_cookie_keys(GLADOS_SITE_COOKIE, checkin.SITE_COOKIE_KEYS["railgun.info"]) == [
        "koa:sess",
        "koa:sess.sig",
    ]


# --------------------------------------------------------------------------
# 认证失败识别
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (-2, "没有权限", True),  # glados.cloud 实测响应
        (-2, "No permission", True),  # railgun.info 实测响应
        (-2, "NO PERMISSION", True),  # 大小写不敏感
        (1, "Today's observation logged. Return tomorrow for more points.", False),
        (1, "Not enough points. Need 500, have 320.0000000000000000", False),
        (-2, "其他错误", False),  # -2 但不是权限问题, 不做 Cookie 归因
        (0, "Checkin! Got 1 Points", False),
        (4, "Automated check-in detected. Please sign in again to continue.", False),
    ],
)
def test_is_permission_error_matches_real_api_responses(code, message, expected):
    """期望值来自线上日志里服务端真实返回的 code/message 组合。"""
    assert checkin.is_permission_error(code, message) is expected


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (4, "Automated check-in detected. Please sign in again to continue.", True),
        (4, "automated check-in detected", True),  # 大小写不敏感
        (0, "Checkin! Got 7 Points", False),
        (1, "Today's observation logged. Return tomorrow for more points.", False),
        (-2, "没有权限", False),  # 认证失败不是自动化拦截, 不能混为一谈
    ],
)
def test_is_automation_blocked_distinguishes_code_4_from_auth_failure(code, message, expected):
    """code 4 与 code -2 是两种完全不同的故障, 诊断必须区分开。"""
    assert checkin.is_automation_blocked(code, message) is expected


# --------------------------------------------------------------------------
# User-Agent 配置 (2026-09 起 GLaDOS 用它做反自动化校验)
# --------------------------------------------------------------------------


def test_config_uses_default_user_agent_when_env_absent(monkeypatch):
    """需求: 未配置 GLADOS_USER_AGENT 时使用能通过校验的默认 UA。"""
    monkeypatch.delenv(checkin.Config.ENV_USER_AGENT, raising=False)
    config = _config_with_cookie(monkeypatch, BOTH_SITES_COOKIE)

    assert config.user_agent == checkin.Config.DEFAULT_USER_AGENT
    assert "Windows" not in config.user_agent  # 实测 Windows UA 会被判定为自动签到


def test_config_user_agent_can_be_overridden_by_env(monkeypatch):
    """需求: 登录平台不是 macOS 的用户必须能用 GLADOS_USER_AGENT 覆盖。"""
    config = _config_with_cookie(
        monkeypatch, BOTH_SITES_COOKIE, user_agent="UA_FROM_USER_BROWSER"
    )

    assert config.user_agent == "UA_FROM_USER_BROWSER"


def test_api_sends_the_configured_user_agent(monkeypatch):
    """失败模式: 配置了 UA 但请求仍带旧硬编码 UA, 会继续被 code 4 拦下。"""
    config = _config_with_cookie(monkeypatch, BOTH_SITES_COOKIE)
    config.user_agent = "UA_MUST_REACH_THE_WIRE"

    assert checkin.API("glados.cloud", 1, user_agent=config.user_agent).headers["user-agent"] == (
        "UA_MUST_REACH_THE_WIRE"
    )


# --------------------------------------------------------------------------
# 请求形态对齐网页端
#
# 期望值不是从实现反推的, 而是真机抓包: 2026-09-26 用 CDP 记录本机 Chrome 154
# (macOS) 在 https://glados.cloud/console/checkin 点击「签到」发出的那一次请求,
# 存在 tests/fixtures/browser_checkin_request.json 里。
# 站点前端代码 `axios.post("/user/checkin", {token: window.location.hostname})`
# (baseURL=/api) 见 console 包 main~d0ae3f07 / main~189dec1b。
# --------------------------------------------------------------------------

FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures", "browser_checkin_request.json")


def _browser_capture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as fp:
        return json.load(fp)


class _RecordingHandler(BaseHTTPRequestHandler):
    """把收到的请求原样记下来, 再回一份可解析的 JSON 响应。"""

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.recorded.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        payload = json.dumps(self.server.payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # 静音
        pass


def _capture_request(monkeypatch, payload: dict, call) -> dict:
    """把 API 调用真的发到本机 HTTP 服务器, 返回线上缆的 method/path/headers/body。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.daemon_threads = True
    server.recorded = []
    server.payload = payload
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        checkin.API,
        "_get_full_url",
        lambda self, path: f"http://127.0.0.1:{server.server_port}{path}",
    )
    try:
        call()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(server.recorded) == 1, server.recorded
    return server.recorded[0]


def test_checkin_wire_request_matches_the_captured_browser_request(monkeypatch):
    """失败模式: 请求体 / content-type / accept / origin / UA 与网页点签到时不一致。

    期望值逐项来自浏览器抓包 (tests/fixtures/browser_checkin_request.json):
    请求体是紧凑 JSON ``{"token":"glados.cloud"}`` 共 24 字节, content-type 带
    ``charset=UTF-8``, GET 与 POST 都不带 Referer。
    """
    capture = _browser_capture()
    browser = capture["request"]
    api = checkin.API("glados.cloud", 1, user_agent=capture["browser"]["user_agent"])

    recorded = _capture_request(
        monkeypatch,
        {"code": 1, "message": "Today's observation logged."},
        lambda: api.checkin(BOTH_SITES_COOKIE),
    )

    assert recorded["method"] == browser["method"] == "POST"
    assert recorded["path"] == "/api/user/checkin"
    assert recorded["body"].decode() == browser["postData"]
    assert len(recorded["body"]) == browser["contentLength"]
    for name in ("accept", "content-type", "origin", "user-agent", "content-length"):
        assert recorded["headers"][name] == browser["headers"][name], name
    assert recorded["headers"]["cookie"] == BOTH_SITES_COOKIE


def test_checkin_sends_no_header_the_browser_never_sends(monkeypatch):
    """失败模式: 自作聪明地补 Referer / Authorization 之类的头。

    浏览器抓到的那次请求里根本没有 Referer (页面是 ``<meta name="referrer"
    content="no-referrer">``), 旧实现自己造的 ``referer: /console`` 就是纯粹的差异;
    登录页 app.bundle.js 的指纹 Authorization 头也绝不能出现在签到请求里。
    """
    capture = _browser_capture()
    browser_headers = {name.lower() for name in capture["request"]["headers"]}
    # HTTP/1.1 客户端自带、而浏览器走 h2 时不会出现的两个头
    client_only = {"host", "connection"}

    api = checkin.API("glados.cloud", 1)
    recorded = _capture_request(
        monkeypatch, {"code": 1, "message": "repeat"}, lambda: api.checkin(BOTH_SITES_COOKIE)
    )

    unexpected = set(recorded["headers"]) - browser_headers - client_only
    assert unexpected == set(), f"脚本多发了浏览器没有的头: {sorted(unexpected)}"
    assert "referer" not in recorded["headers"]
    assert "authorization" not in recorded["headers"]


def test_api_exchange_posts_compact_json_plan_type_like_the_web_console(monkeypatch):
    """失败模式: 兑换请求体不是网页端那种紧凑 JSON, 或少了 content-type。"""
    api = checkin.API("railgun.info", 1)

    recorded = _capture_request(
        monkeypatch, {"code": 0, "message": "ok"},
        lambda: api.exchange(BOTH_SITES_COOKIE, "plan500", 500),
    )

    assert recorded["path"] == "/api/user/exchange"
    assert recorded["body"] == b'{"planType":"plan500"}'
    assert recorded["headers"]["content-type"] == "application/json;charset=UTF-8"


def test_api_get_requests_carry_no_content_type_or_referer(monkeypatch):
    """失败模式: 把 POST 才有的 content-type 也塞给 GET, 与浏览器不一致。"""
    api = checkin.API("glados.cloud", 1)

    recorded = _capture_request(
        monkeypatch,
        {"code": 0, "data": {"leftDays": "10"}},
        lambda: api.get_status(BOTH_SITES_COOKIE),
    )

    assert recorded["method"] == "GET"
    assert "content-type" not in recorded["headers"]
    assert "referer" not in recorded["headers"]
    assert recorded["headers"]["cookie"] == BOTH_SITES_COOKIE



# --------------------------------------------------------------------------
# 配置期 Cookie 校验 (只警告, 不泄露凭据)
# --------------------------------------------------------------------------


def _config_with_cookie(monkeypatch, cookie: str, user_agent=None) -> checkin.Config:
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, cookie)
    monkeypatch.delenv(checkin.Config.ENV_PUSH_KEY, raising=False)
    if user_agent is None:
        monkeypatch.delenv(checkin.Config.ENV_USER_AGENT, raising=False)
    else:
        monkeypatch.setenv(checkin.Config.ENV_USER_AGENT, user_agent)
    return checkin.Config()


def _assert_no_cookie_warning(caplog, cookie: str) -> None:
    """加载期不应出现任何针对 Cookie 字段的告警 (推送密钥之类的告警不算)。"""
    text = caplog.text
    assert "会话字段" not in text, text
    assert cookie not in text
    assert COOKIE_SENTINEL not in text


def test_config_accepts_glados_only_cookie_without_warning(monkeypatch, caplog):
    """需求: 只在 glados.cloud 注册的用户手里只有 gld 那一对, 不得报缺失告警。

    对应上游 issue #37 评论 5845370610 第 1 条。
    """
    with caplog.at_level("WARNING"):
        config = _config_with_cookie(monkeypatch, GLADOS_SITE_COOKIE)

    assert config.cookies_list == [GLADOS_SITE_COOKIE]
    _assert_no_cookie_warning(caplog, GLADOS_SITE_COOKIE)


def test_config_accepts_railgun_only_cookie_without_warning(monkeypatch, caplog):
    """需求: 只在 railgun.info 注册的用户手里只有 koa 那一对, 不得报缺失告警。

    对应上游 issue #37 评论 5845370610 第 2 条。
    """
    with caplog.at_level("WARNING"):
        config = _config_with_cookie(monkeypatch, RAILGUN_SITE_COOKIE)

    assert config.cookies_list == [RAILGUN_SITE_COOKIE]
    _assert_no_cookie_warning(caplog, RAILGUN_SITE_COOKIE)


def test_config_accepts_both_sites_cookie_without_warning(monkeypatch, caplog):
    """需求: 两个站点都有账号时把两对拼在一起, 同样不应产生告警。"""
    with caplog.at_level("WARNING"):
        config = _config_with_cookie(monkeypatch, BOTH_SITES_COOKIE)

    assert config.cookies_list == [BOTH_SITES_COOKIE]
    _assert_no_cookie_warning(caplog, BOTH_SITES_COOKIE)


def test_config_warns_when_no_complete_session_pair_without_leaking_values(monkeypatch, caplog):
    """失败模式: 连一对完整会话字段都没有时必须在加载阶段给出可操作警告, 且不打印 Cookie 值。"""
    with caplog.at_level("WARNING"):
        _config_with_cookie(monkeypatch, INCOMPLETE_SESSION_COOKIE)

    text = caplog.text
    assert "没有一对完整的会话字段" in text
    assert "gld:sess" in text
    assert "gld:sess.sig" in text
    assert "koa:sess" in text
    assert "koa:sess.sig" in text
    assert "GLADOS_COOKIES" in text
    assert COOKIE_SENTINEL not in text


def test_config_warns_when_input_is_not_a_cookie(monkeypatch, caplog):
    """失败模式: 完全不像 Cookie 的输入同样要提示两套会话字段的名称。"""
    with caplog.at_level("WARNING"):
        _config_with_cookie(monkeypatch, NON_COOKIE_INPUT)

    assert "没有一对完整的会话字段" in caplog.text
    assert "gld:sess" in caplog.text
    assert "koa:sess" in caplog.text


# --------------------------------------------------------------------------
# 账号维度的退出码判定
# --------------------------------------------------------------------------


class _FakeConfig:
    """仅提供 Checker 需要的字段, 避免测试依赖环境变量。"""

    def __init__(self, cookie_count: int):
        self.cookies_list = [f"cookie-{i}" for i in range(cookie_count)]
        self.verbose = False
        self.exchange_plan = "plan500"


def _result(cookie_index: int, domain: str, code: checkin.CheckinStatus) -> checkin.CheckinResult:
    """Checker.results 实际存放的是 CheckinResult 实例 (get_results() 才转成 dict)。"""
    return checkin.CheckinResult(cookie_index, domain, code=code)


def test_failed_cookie_indexes_ignores_domain_that_never_holds_the_account():
    """需求: Cookie1 在 glados.cloud 成功、在 railgun.info 返回 -2 时, 账号视为成功。"""
    checker = checkin.Checker(_FakeConfig(cookie_count=1))
    checker.results = [
        _result(1, "glados.cloud", checkin.CheckinStatus.SUCCESS),
        _result(1, "railgun.info", checkin.CheckinStatus.FAILURE),
    ]

    assert checker.failed_cookie_indexes() == []


def test_failed_cookie_indexes_reports_cookie_failing_on_every_domain():
    """失败模式: 线上 2026-09-24 起两个域名都返回 -2, 必须判定为账号失败。"""
    checker = checkin.Checker(_FakeConfig(cookie_count=2))
    checker.results = [
        _result(1, "glados.cloud", checkin.CheckinStatus.FAILURE),
        _result(1, "railgun.info", checkin.CheckinStatus.FAILURE),
        _result(2, "glados.cloud", checkin.CheckinStatus.REPEAT),
        _result(2, "railgun.info", checkin.CheckinStatus.FAILURE),
    ]

    assert checker.failed_cookie_indexes() == [1]


# --------------------------------------------------------------------------
# 主流程退出码: 修复「Action 绿色但没签到」
# --------------------------------------------------------------------------


@pytest.fixture()
def _stub_api(monkeypatch):
    """把 API 层替换成可控结果, 只验证 main() 的退出码与日志。"""
    state = {"checkin_code": checkin.CheckinStatus.SUCCESS}

    monkeypatch.setattr(checkin.API, "get_status", lambda self, cookies: ("42 天", 0))
    monkeypatch.setattr(checkin.API, "get_points", lambda self, cookies: ("500 积分", 500))
    monkeypatch.setattr(checkin.API, "exchange", lambda self, c, plan, pts: "未兑换")

    def fake_checkin(self, cookies):
        code = state["checkin_code"]
        return {
            "status": "签到成功" if code is checkin.CheckinStatus.SUCCESS else "签到失败",
            "points": "0",
            "message": "stub",
            "code": code,
        }

    monkeypatch.setattr(checkin.API, "checkin", fake_checkin)
    monkeypatch.delenv(checkin.Config.ENV_PUSH_KEY, raising=False)
    return state


def test_main_returns_0_when_one_domain_succeeds(monkeypatch, _stub_api):
    """需求: 只要账号在任一域名上签到成功/Actions 应为绿色 (退出码 0)。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, BOTH_SITES_COOKIE)

    assert checkin.main() == checkin.EXIT_OK


def test_main_returns_1_when_checkin_fails_on_all_domains(monkeypatch, _stub_api, caplog):
    """失败模式: 所有域名都失败时必须返回非 0, 让 Actions 变红而不是假绿。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, BOTH_SITES_COOKIE)
    _stub_api["checkin_code"] = checkin.CheckinStatus.FAILURE

    with caplog.at_level("ERROR"):
        exit_code = checkin.main()

    assert exit_code == checkin.EXIT_CHECKIN_FAILED
    assert "在所有域名上都未签到成功" in caplog.text


def test_main_returns_2_when_cookie_env_is_missing(monkeypatch, _stub_api):
    """失败模式: 没配置 GLADOS_COOKIES 属于配置错误, 必须显式失败。"""
    monkeypatch.delenv(checkin.Config.ENV_COOKIES, raising=False)

    assert checkin.main() == checkin.EXIT_CONFIG_ERROR


def test_push_service_without_config_does_not_raise():
    """失败模式: Config() 抛异常时旧代码用 "" 兜底, 会在推送阶段再抛一次异常。"""
    assert checkin.PushService(None).send("title", "content") is False


def test_api_checkin_reports_failure_when_request_raises(monkeypatch):
    """失败模式: 网络异常被 log_method 兜底后必须仍是「签到失败」而不是成功。"""
    api = checkin.API("glados.cloud", 1)

    def boom(*args, **kwargs):
        raise checkin.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(api.session, "post", boom)

    result = api.checkin(BOTH_SITES_COOKIE)

    assert result["status"] == "签到失败"
    assert result["points"] == "0"


class _FakeResponse:
    """只实现 API 层用到的 json()。"""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_api_checkin_hints_user_agent_when_automation_detected(monkeypatch, caplog):
    """失败模式: 线上 2026-09-25 实测的 code 4 必须提示 GLADOS_USER_AGENT,
    而不是被误判成 Cookie 失效。

    追加需求: 站点前端在 device-mismatch 时会弹出对话框显示服务端给的
    loginDevice / currentDevice; 脚本也要把这两个值打出来, 否则用户只能瞎试 UA。
    """
    api = checkin.API(
        "glados.cloud",
        1,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36",
    )
    monkeypatch.setattr(
        api,
        "_make_request",
        lambda *a, **k: _FakeResponse(
            {
                "code": 4,
                "message": "Automated check-in detected. Please sign in again to continue.",
                "reason": "device-mismatch",
                "loginDevice": "macos",
                "currentDevice": "windows",
            }
        ),
    )

    with caplog.at_level("ERROR"):
        result = api.checkin(BOTH_SITES_COOKIE)

    assert result["status"] == "签到失败"
    assert "GLADOS_USER_AGENT" in caplog.text
    assert "navigator.userAgent" in caplog.text
    assert "loginDevice: macos" in caplog.text
    assert "currentDevice: windows" in caplog.text
    assert "认证失败" not in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


def test_api_checkin_automation_hint_still_works_without_device_fields(monkeypatch, caplog):
    """失败模式: 服务端只回 code 4 不带 reason/设备字段时, 提示不能崩也不能消失。"""
    api = checkin.API("glados.cloud", 1)
    monkeypatch.setattr(
        api,
        "_make_request",
        lambda *a, **k: _FakeResponse({"code": 4, "message": "Automated check-in detected."}),
    )

    with caplog.at_level("ERROR"):
        result = api.checkin(BOTH_SITES_COOKIE)

    assert result["status"] == "签到失败"
    assert "签到被判定为自动签到" in caplog.text
    assert "GLADOS_USER_AGENT" in caplog.text


# --------------------------------------------------------------------------
# 端到端: 真实服务端 + 真实脚本进程
# --------------------------------------------------------------------------


def _run_checkin(env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop(checkin.Config.ENV_COOKIES, None)
    env.pop(checkin.Config.ENV_PUSH_KEY, None)
    env.pop(checkin.Config.ENV_USER_AGENT, None)
    env.update(env_overrides)

    return subprocess.run(
        [sys.executable, "checkin.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_e2e_invalid_cookie_against_live_api_exits_nonzero_with_actionable_log():
    """端到端: 拿一份无效 Cookie 打真实接口, 必须失败并点名该域名需要的会话字段。

    Cookie 值是可识别的哨兵值而不是真实凭据, 所以这条用例验证的是诊断链路
    (真实网络 + 真实子进程 + 服务端确实返回 code -2 → 退出码 1 + 可操作提示),
    而不是服务端对字段的要求 —— 后者只能靠真实账号的 Cookie 验证。
    对应线上故障: glados.cloud 全接口返回 code -2「没有权限」时 Actions 必须变红。
    """
    proc = _run_checkin({checkin.Config.ENV_COOKIES: RAILGUN_SITE_COOKIE})

    combined = proc.stdout + proc.stderr
    assert proc.returncode == checkin.EXIT_CHECKIN_FAILED, combined
    assert "认证失败" in proc.stderr
    assert "没有权限" in proc.stderr
    # 针对上游反馈: glados.cloud 的失败提示要指名 gld:sess/gld:sess.sig
    assert "gld:sess" in combined
    assert "在所有域名上都未签到成功" in proc.stderr
    assert COOKIE_SENTINEL not in combined


def test_e2e_missing_cookie_env_exits_with_config_error():
    """端到端: 未配置 Cookie 的 Actions 运行必须直接失败。"""
    proc = _run_checkin({})

    assert proc.returncode == checkin.EXIT_CONFIG_ERROR, proc.stdout + proc.stderr
    assert "未找到有效的 Cookie" in proc.stderr
