"""checkin.py 的测试。

覆盖的失败模式 (来自 2026-09-24 起线上 Actions 日志与上游 issue #37):
- GLaDOS 自 2026-09 起把会话 Cookie 拆成了 gld:sess / gld:sess.sig 两个字段,
  缺一个就不能签到 (只抄半对是常见错误, 所以要在加载期就告警)。
- 旧版本脚本把失败咽掉, 进程退出码始终为 0, 于是 Actions 显示绿色但实际没签到。
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

GLADOS_COOKIE = f"gld:sess={COOKIE_SENTINEL}_gsess; gld:sess.sig={COOKIE_SENTINEL}_gsig"
# 只有会话字段的一半 (缺 .sig), 也是从浏览器复制时最常见的截断形态
INCOMPLETE_SESSION_COOKIE = f"gld:sess={COOKIE_SENTINEL}_gsess; theme=dark"
# 站点同源下发的旧字段名: 真实浏览器 Cookie 里 gld:* 与 koa:* 是并存的
# (见 fixtures/browser_checkin_request.json 的 cookie 头), 但脚本只认 gld:*,
# 所以只有旧字段的 Cookie 必须告警
LEGACY_FIELDS_ONLY_COOKIE = f"koa:sess={COOKIE_SENTINEL}_sess; koa:sess.sig={COOKIE_SENTINEL}_sig"
# 完全不像这个站点 Cookie 的输入
NON_COOKIE_INPUT = f"not-a-cookie={COOKIE_SENTINEL}"

# --------------------------------------------------------------------------
# Cookie 结构解析
# --------------------------------------------------------------------------


def test_parse_cookie_keys_returns_field_names_without_values():
    """需求: 解析只给出字段名, 任何日志路径都不得泄露 Cookie 值。"""
    keys = checkin.parse_cookie_keys(GLADOS_COOKIE)

    assert keys == ["gld:sess", "gld:sess.sig"]
    assert all(COOKIE_SENTINEL not in key for key in keys)


def test_parse_cookie_keys_tolerates_missing_spaces_and_trailing_semicolon():
    """失败模式: 用户从浏览器复制的 Cookie 分隔符不规范时不应解析错位。"""
    keys = checkin.parse_cookie_keys("gld:sess=a;gld:sess.sig=b; theme=dark;;")

    assert keys == ["gld:sess", "gld:sess.sig", "theme"]


def test_missing_cookie_keys_reports_the_incomplete_pair():
    """需求: 缺字段诊断只报真正缺的那几个, 不把无关字段算进来。"""
    assert checkin.missing_cookie_keys(GLADOS_COOKIE) == []
    assert checkin.missing_cookie_keys(INCOMPLETE_SESSION_COOKIE) == ["gld:sess.sig"]
    assert checkin.missing_cookie_keys(NON_COOKIE_INPUT) == ["gld:sess", "gld:sess.sig"]


# --------------------------------------------------------------------------
# 认证失败识别
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (-2, "没有权限", True),  # glados.cloud 实测响应
        (-2, "No permission", True),  # 英文文案没实测来源 (上游两站点时代的兜底)
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
    config = _config_with_cookie(monkeypatch, GLADOS_COOKIE)

    assert config.user_agent == checkin.Config.DEFAULT_USER_AGENT
    assert "Windows" not in config.user_agent  # 实测 Windows UA 会被判定为自动签到


def test_config_user_agent_can_be_overridden_by_env(monkeypatch):
    """需求: 登录平台不是 macOS 的用户必须能用 GLADOS_USER_AGENT 覆盖。"""
    config = _config_with_cookie(
        monkeypatch, GLADOS_COOKIE, user_agent="UA_FROM_USER_BROWSER"
    )

    assert config.user_agent == "UA_FROM_USER_BROWSER"


def test_api_sends_the_configured_user_agent(monkeypatch):
    """失败模式: 配置了 UA 但请求仍带旧硬编码 UA, 会继续被 code 4 拦下。"""
    config = _config_with_cookie(monkeypatch, GLADOS_COOKIE)
    config.user_agent = "UA_MUST_REACH_THE_WIRE"

    assert checkin.API(user_agent=config.user_agent).headers["user-agent"] == (
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
    api = checkin.API(user_agent=capture["browser"]["user_agent"])

    recorded = _capture_request(
        monkeypatch,
        {"code": 1, "message": "Today's observation logged."},
        lambda: api.checkin(GLADOS_COOKIE),
    )

    assert recorded["method"] == browser["method"] == "POST"
    assert recorded["path"] == "/api/user/checkin"
    assert recorded["body"].decode() == browser["postData"]
    assert len(recorded["body"]) == browser["contentLength"]
    for name in ("accept", "content-type", "origin", "user-agent", "content-length"):
        assert recorded["headers"][name] == browser["headers"][name], name
    assert recorded["headers"]["cookie"] == GLADOS_COOKIE


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

    api = checkin.API()
    recorded = _capture_request(
        monkeypatch, {"code": 1, "message": "repeat"}, lambda: api.checkin(GLADOS_COOKIE)
    )

    unexpected = set(recorded["headers"]) - browser_headers - client_only
    assert unexpected == set(), f"脚本多发了浏览器没有的头: {sorted(unexpected)}"
    assert "referer" not in recorded["headers"]
    assert "authorization" not in recorded["headers"]


def test_api_exchange_posts_compact_json_plan_type_like_the_web_console(monkeypatch):
    """失败模式: 兑换请求体不是网页端那种紧凑 JSON, 或少了 content-type。"""
    api = checkin.API()

    recorded = _capture_request(
        monkeypatch, {"code": 0, "message": "ok"},
        lambda: api.exchange(GLADOS_COOKIE, "plan500"),
    )

    assert recorded["path"] == "/api/user/exchange"
    assert recorded["body"] == b'{"planType":"plan500"}'
    assert recorded["headers"]["content-type"] == "application/json;charset=UTF-8"


def test_api_get_requests_carry_no_content_type_or_referer(monkeypatch):
    """失败模式: 把 POST 才有的 content-type 也塞给 GET, 与浏览器不一致。"""
    api = checkin.API()

    recorded = _capture_request(
        monkeypatch,
        {"code": 0, "points": 497},
        lambda: api.get_points(GLADOS_COOKIE),
    )

    assert recorded["method"] == "GET"
    assert "content-type" not in recorded["headers"]
    assert "referer" not in recorded["headers"]
    assert recorded["headers"]["cookie"] == GLADOS_COOKIE



# --------------------------------------------------------------------------
# 配置期 Cookie 校验 (只警告, 不泄露凭据)
# --------------------------------------------------------------------------


def _config_with_cookie(monkeypatch, cookie: str, user_agent=None) -> checkin.Config:
    """在干净的环境里加载配置: 可选环境变量一律先清掉, 免得被本机环境干扰断言。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, cookie)
    if user_agent is None:
        monkeypatch.delenv(checkin.Config.ENV_USER_AGENT, raising=False)
    else:
        monkeypatch.setenv(checkin.Config.ENV_USER_AGENT, user_agent)
    return checkin.Config()


def _assert_no_config_warning(caplog, cookie: str) -> None:
    """正常配置在加载期一条告警都不该有, 而且任何级别的日志都不得出现 Cookie 值。

    只设必需的 GLADOS_COOKIES 是最常见的正常状态,
    让正常路径冒 ⚠️ 会训练人忽略警告, 真正的异常反而看不见。
    """
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == [], warnings
    assert cookie not in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


def test_config_accepts_complete_cookie_without_warning(monkeypatch, caplog):
    """需求: 会话字段齐全的正常 Cookie 不得产生告警 (且加载日志里不得出现 Cookie 值)。"""
    with caplog.at_level("INFO"):
        config = _config_with_cookie(monkeypatch, GLADOS_COOKIE)

    assert config.cookie == GLADOS_COOKIE
    _assert_no_config_warning(caplog, GLADOS_COOKIE)


def test_config_accepts_cookie_with_extra_unrelated_fields_without_warning(monkeypatch, caplog):
    """需求: 从浏览器复制出来的 Cookie 常带着 theme 之类的无关字段, 不该因此告警。"""
    cookie = f"{GLADOS_COOKIE}; theme=dark; _ga=GA1.1.123456"
    with caplog.at_level("INFO"):
        config = _config_with_cookie(monkeypatch, cookie)

    assert config.cookie == cookie
    _assert_no_config_warning(caplog, cookie)


def test_config_warns_for_cookie_with_only_legacy_fields(monkeypatch, caplog):
    """失败模式: 只有本站旧字段 (koa:*) 而没有 gld:* 时必须告警。

    真实浏览器 Cookie 里两套字段是并存的, 所以「有 koa:*」本身不代表拿错了;
    判据只有一个 —— 有没有 gld:sess 与 gld:sess.sig。照旧静默通过的话,
    用户只会在签到失败时才发现自己挑错了字段。
    """
    with caplog.at_level("INFO"):
        config = _config_with_cookie(monkeypatch, LEGACY_FIELDS_ONLY_COOKIE)

    assert config.cookie == LEGACY_FIELDS_ONLY_COOKIE
    assert "缺少会话字段" in caplog.text
    assert "gld:sess" in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


def test_config_warns_when_session_fields_are_incomplete_without_leaking_values(monkeypatch, caplog):
    """失败模式: 只抄了半对时必须在加载阶段给出可操作警告, 且不打印 Cookie 值。"""
    with caplog.at_level("INFO"):
        _config_with_cookie(monkeypatch, INCOMPLETE_SESSION_COOKIE)

    text = caplog.text
    assert "缺少会话字段" in text
    assert "gld:sess.sig" in text
    assert "GLADOS_COOKIES" in text
    assert COOKIE_SENTINEL not in text


def test_config_warns_when_input_is_not_a_cookie(monkeypatch, caplog):
    """失败模式: 完全不像 Cookie 的输入同样要提示需要的会话字段名称。"""
    with caplog.at_level("INFO"):
        _config_with_cookie(monkeypatch, NON_COOKIE_INPUT)

    assert "缺少会话字段" in caplog.text
    assert "gld:sess" in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


# --------------------------------------------------------------------------
# 退出码判定 (fail-closed)
# --------------------------------------------------------------------------


def test_main_fails_closed_when_the_checkin_raises(monkeypatch, caplog):
    """最要紧的一条约束: 拿不到"成功/重复"的结果就必须报红。

    宁可多签一次, 也绝不静默漏签。中途异常是这条约束最容易破的地方 —— 异常被
    吞掉、流程照走, 就会静默漏签还显示绿色。
    """
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)

    def boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(checkin, "run_checkin", boom)

    with caplog.at_level("ERROR"):
        exit_code = checkin.main()

    assert exit_code == checkin.EXIT_CHECKIN_FAILED
    assert "boom" in caplog.text


# --------------------------------------------------------------------------
# 主流程退出码: 修复「Action 绿色但没签到」
# --------------------------------------------------------------------------


@pytest.fixture()
def _stub_api(monkeypatch):
    """把 API 层替换成可控结果, 只验证 main() 的退出码与日志。

    state 可改: checkin_code (签到结果)、checkin_points (本次签到拿到的积分)、
    points (总积分余额)、exchange_result (兑换返回)。
    exchange_calls 记录真正发出去的兑换请求, 用来验证"积分不够就别发请求"。
    """
    state = {
        "checkin_code": checkin.CheckinStatus.SUCCESS,
        "checkin_points": "0",
        "points": 500,
        "exchange_result": "兑换成功: plan500",
        "exchange_calls": [],
    }

    monkeypatch.setattr(
        checkin.API, "get_points", lambda self, cookies: (f"{state['points']} 积分", state["points"])
    )

    def fake_exchange(self, cookies, plan):
        state["exchange_calls"].append(plan)
        return state["exchange_result"]

    def fake_checkin(self, cookies):
        code = state["checkin_code"]
        return {
            "status": "签到成功" if code is checkin.CheckinStatus.SUCCESS else "签到失败",
            "points": state["checkin_points"],
            "message": "stub",
            "code": code,
        }

    monkeypatch.setattr(checkin.API, "exchange", fake_exchange)
    monkeypatch.setattr(checkin.API, "checkin", fake_checkin)
    return state


def test_main_returns_0_when_checkin_succeeds(monkeypatch, _stub_api):
    """需求: 账号签到成功时 Actions 应为绿色 (退出码 0)。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)

    assert checkin.main() == checkin.EXIT_OK


def test_main_returns_1_when_checkin_fails(monkeypatch, _stub_api, caplog):
    """失败模式: 账号签到失败时必须返回非 0, 让 Actions 变红而不是假绿。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["checkin_code"] = checkin.CheckinStatus.FAILURE

    with caplog.at_level("ERROR"):
        exit_code = checkin.main()

    assert exit_code == checkin.EXIT_CHECKIN_FAILED
    assert "请检查 Cookie 是否完整/过期" in caplog.text


def test_main_returns_2_when_cookie_env_is_missing(monkeypatch, _stub_api):
    """失败模式: 没配置 GLADOS_COOKIES 属于配置错误, 必须显式失败。"""
    monkeypatch.delenv(checkin.Config.ENV_COOKIES, raising=False)

    assert checkin.main() == checkin.EXIT_CONFIG_ERROR


def test_result_line_says_what_happened_and_where_the_account_stands(monkeypatch, _stub_api, caplog):
    """需求: 结果行是每天唯一要看的那一行, 它得同时说清「这次签到发生了什么」和
    「离兑换还差多少」—— 后者的具体数字由这一行负责, 别处都没有。

    两个都回归过:
    - 「获得 N 积分」曾是 CheckinResult.points 从未被赋值, 恒为"获得 0 积分";
    - 「总 N 积分」曾在 52 条测试全绿的情况下被一次误提交从结果行里删掉
      (2026-09-30, 见 a68d159 / 2638edc) —— 结果行少打余额, 连测试带 CI 都不会响。
    """
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["checkin_points"] = "13"
    _stub_api["points"] = 497

    with caplog.at_level("INFO"):
        exit_code = checkin.main()

    assert exit_code == checkin.EXIT_OK
    assert "获得 13 积分" in caplog.text
    assert "总 497 积分" in caplog.text
    # 门槛进度也用同一条结果行交代, 期望值按配置拼, 免得改了门槛这里还绿着。
    assert f"未到 {checkin.Config.EXCHANGE_PLAN_POINTS} 兑换门槛" in caplog.text


def test_a_successful_checkin_logs_neither_the_request_nor_the_cookie(monkeypatch, caplog):
    """需求: 日志要短, 且不泄密。

    不逐条回显请求/响应, 是用户明确提过的要求 (旧日志跑一次 18 行); Cookie 值不落盘
    是安全约束 —— 请求头从不进日志, 这两条都得有人守着。
    """
    api = checkin.API()

    with caplog.at_level("INFO"):
        _capture_request(
            monkeypatch,
            {"code": 1, "message": "Today's observation logged."},
            lambda: api.checkin(GLADOS_COOKIE),
        )

    assert "Today's observation logged." not in caplog.text, caplog.text
    assert GLADOS_COOKIE not in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


# --------------------------------------------------------------------------
# 兑换: 积分到门槛才发请求
# --------------------------------------------------------------------------


def test_no_exchange_request_when_points_are_below_the_plan_threshold(monkeypatch, _stub_api):
    """需求: 积分不够就不要主动兑换。

    线上表现: 余额 497 / plan500 需要 500 时, 服务端每次都回 "Not enough points",
    于是每天多一次注定失败的请求 + 一条看着像故障的报错。积分是本脚本自己刚查过的,
    够不够当场就能判断, 没必要去问服务端。
    """
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["points"] = 497

    assert checkin.main() == checkin.EXIT_OK
    assert _stub_api["exchange_calls"] == []


def test_exchange_request_is_sent_once_points_reach_the_threshold(monkeypatch, _stub_api):
    """边界: 刚好够就必须兑换 —— 门槛判断写成 > 而不是 >= 会永远差一分不兑换。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["points"] = 500

    assert checkin.main() == checkin.EXIT_OK
    assert set(_stub_api["exchange_calls"]) == {"plan500"}


def test_a_successful_exchange_is_visible_in_the_log(monkeypatch, caplog):
    """需求: 兑换成功必须留痕。

    它是一次扣掉 500 积分、改变账号状态的操作, 所以必须无条件留痕 ——
    否则成功兑换这种最该知道的事, 反而在日志里什么都看不到。
    回归的就是"积分够了、兑换也成功了, 但日志里一个字都没有"这个观测盲区。

    留痕的位置是结果行 (无条件输出)。这里只换掉网络层, checkin /
    get_points / exchange / run_checkin / 结果行全部走真代码 —— 把其中任何一层 stub 掉,
    就等于在测 stub 自己。
    """
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)

    def fake_make_request(self, url, method, data=None, cookies=""):
        if url.endswith(checkin.API.POINTS_URL):
            return _FakeResponse({"code": 0, "points": checkin.Config.EXCHANGE_PLAN_POINTS})
        if url.endswith(checkin.API.EXCHANGE_URL):
            return _FakeResponse({"code": 0, "message": "ok"})
        return _FakeResponse({"code": 0, "points": 13, "message": "checkin ok"})

    monkeypatch.setattr(checkin.API, "_make_request", fake_make_request)

    with caplog.at_level("INFO"):
        exit_code = checkin.main()

    assert exit_code == checkin.EXIT_OK
    assert checkin.Config.EXCHANGE_PLAN in caplog.text, caplog.text
    assert "兑换成功" in caplog.text, caplog.text


def test_exchange_failure_does_not_fail_the_run(monkeypatch, _stub_api):
    """需求: 兑换是附加动作, 失败不该让签到运行变红。

    否则"积分不够"这类正常状态会天天把 Actions 染红, 把真正的签到失败淹没。
    """
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["exchange_result"] = "兑换失败: 服务端炸了"

    assert checkin.main() == checkin.EXIT_OK


def test_exchange_uses_the_hardcoded_plan500(monkeypatch, _stub_api):
    """需求: 兑换固定用 plan500 —— 计划选择已经去掉, 别又长出一个开关来。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, GLADOS_COOKIE)
    _stub_api["points"] = checkin.Config.EXCHANGE_PLAN_POINTS

    assert checkin.main() == checkin.EXIT_OK
    assert set(_stub_api["exchange_calls"]) == {checkin.Config.EXCHANGE_PLAN}




def test_api_checkin_reports_failure_when_request_raises(monkeypatch):
    """失败模式: 网络异常被 log_method 兜底后必须仍是「签到失败」而不是成功。"""
    api = checkin.API()

    def boom(*args, **kwargs):
        raise checkin.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(api.session, "post", boom)

    result = api.checkin(GLADOS_COOKIE)

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
        result = api.checkin(GLADOS_COOKIE)

    assert result["status"] == "签到失败"
    assert "GLADOS_USER_AGENT" in caplog.text
    assert "navigator.userAgent" in caplog.text
    assert "loginDevice: macos" in caplog.text
    assert "currentDevice: windows" in caplog.text
    assert "认证失败" not in caplog.text
    assert COOKIE_SENTINEL not in caplog.text


def test_api_checkin_automation_hint_still_works_without_device_fields(monkeypatch, caplog):
    """失败模式: 服务端只回 code 4 不带 reason/设备字段时, 提示不能崩也不能消失。"""
    api = checkin.API()
    monkeypatch.setattr(
        api,
        "_make_request",
        lambda *a, **k: _FakeResponse({"code": 4, "message": "Automated check-in detected."}),
    )

    with caplog.at_level("ERROR"):
        result = api.checkin(GLADOS_COOKIE)

    assert result["status"] == "签到失败"
    assert "签到被判定为自动签到" in caplog.text
    assert "GLADOS_USER_AGENT" in caplog.text


# --------------------------------------------------------------------------
# 端到端: 真实服务端 + 真实脚本进程
# --------------------------------------------------------------------------


def _run_checkin(env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop(checkin.Config.ENV_COOKIES, None)
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
    """端到端: 拿一份无效 Cookie 打真实接口, 必须失败并点名需要的会话字段。

    Cookie 值是可识别的哨兵值而不是真实凭据, 所以这条用例验证的是诊断链路
    (真实网络 + 真实子进程 + 服务端确实返回 code -2 → 退出码 1 + 可操作提示),
    而不是服务端对字段的要求 —— 后者只能靠真实账号的 Cookie 验证。
    对应线上故障: glados.cloud 全接口返回 code -2「没有权限」时 Actions 必须变红。
    """
    proc = _run_checkin({checkin.Config.ENV_COOKIES: GLADOS_COOKIE})

    combined = proc.stdout + proc.stderr
    assert proc.returncode == checkin.EXIT_CHECKIN_FAILED, combined
    assert "认证失败" in proc.stderr
    assert "没有权限" in proc.stderr
    # 失败提示要指名这个站点需要的会话字段
    assert "gld:sess" in combined
    # 必须瞄准 main() 收尾那一句: 只写"签到失败"会被结果行 "❌ 签到失败" 满足,
    # 于是删掉整段收尾也照样绿。
    assert "签到失败 (退出码 1)" in proc.stderr
    assert "请检查 Cookie 是否完整/过期" in proc.stderr
    assert COOKIE_SENTINEL not in combined


def test_e2e_missing_cookie_env_exits_with_config_error():
    """端到端: 未配置 Cookie 的 Actions 运行必须直接失败。"""
    proc = _run_checkin({})

    assert proc.returncode == checkin.EXIT_CONFIG_ERROR, proc.stdout + proc.stderr
    assert "未找到有效的 Cookie" in proc.stderr
