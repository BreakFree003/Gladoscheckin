"""checkin.py 的测试。

覆盖的失败模式 (来自 2026-09-24 起线上 Actions 日志与上游 issue #37):
- GLaDOS 自 2026-09 起要求 Cookie 同时包含 koa:sess/koa:sess.sig 与
  gld:sess/gld:sess.sig；只有前两项时所有接口返回 code -2「没有权限」。
- 旧版本脚本把失败咽掉, 进程退出码始终为 0, 于是 Actions 显示绿色但实际没签到。
- 同一个 Cookie 会依次请求 glados.cloud 与 railgun.info, 通常只有其中一个站点
  持有该账号, 另一个必然返回 -2；这属于正常现象, 不应判定为账号失败。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import checkin  # noqa: E402  (需要先注入仓库根目录到 sys.path)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_SENTINEL = "SENTINEL_VALUE_MUST_NOT_BE_LOGGED"

STALE_TWO_COOKIE = (
    f"koa:sess={COOKIE_SENTINEL}_sess; koa:sess.sig={COOKIE_SENTINEL}_sig"
)
FULL_FOUR_COOKIE = (
    f"koa:sess={COOKIE_SENTINEL}_sess; koa:sess.sig={COOKIE_SENTINEL}_sig; "
    f"gld:sess={COOKIE_SENTINEL}_gsess; gld:sess.sig={COOKIE_SENTINEL}_gsig"
)


# --------------------------------------------------------------------------
# Cookie 结构解析
# --------------------------------------------------------------------------


def test_parse_cookie_keys_returns_field_names_without_values():
    """需求: 解析只给出字段名, 任何日志路径都不得泄露 Cookie 值。"""
    keys = checkin.parse_cookie_keys(FULL_FOUR_COOKIE)

    assert keys == ["koa:sess", "koa:sess.sig", "gld:sess", "gld:sess.sig"]
    assert all(COOKIE_SENTINEL not in key for key in keys)


def test_parse_cookie_keys_tolerates_missing_spaces_and_trailing_semicolon():
    """失败模式: 用户从浏览器复制的 Cookie 分隔符不规范时不应解析错位。"""
    keys = checkin.parse_cookie_keys("koa:sess=a;koa:sess.sig=b; gld:sess=c;;")

    assert keys == ["koa:sess", "koa:sess.sig", "gld:sess"]


def test_missing_cookie_keys_flags_stale_two_cookie_format():
    """失败模式: 2026-09 变更前的两字段 Cookie 必须被判定为缺少 gld:sess 两项。"""
    assert checkin.missing_cookie_keys(STALE_TWO_COOKIE, checkin.COOKIE_BASE_KEYS) == []
    assert checkin.missing_cookie_keys(STALE_TWO_COOKIE, checkin.COOKIE_EXTRA_KEYS) == [
        "gld:sess",
        "gld:sess.sig",
    ]
    assert checkin.missing_cookie_keys(FULL_FOUR_COOKIE, checkin.COOKIE_EXTRA_KEYS) == []


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
    ],
)
def test_is_permission_error_matches_real_api_responses(code, message, expected):
    """期望值来自线上日志里服务端真实返回的 code/message 组合。"""
    assert checkin.is_permission_error(code, message) is expected


# --------------------------------------------------------------------------
# 配置期 Cookie 校验 (只警告, 不泄露凭据)
# --------------------------------------------------------------------------


def _config_with_cookie(monkeypatch, cookie: str) -> checkin.Config:
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, cookie)
    monkeypatch.delenv(checkin.Config.ENV_PUSH_KEY, raising=False)
    return checkin.Config()


def test_config_warns_about_missing_gld_cookies_without_leaking_values(monkeypatch, caplog):
    """失败模式: 旧的两字段 Cookie 在加载阶段就要给出可操作警告, 且不打印 Cookie 值。"""
    with caplog.at_level("WARNING"):
        _config_with_cookie(monkeypatch, STALE_TWO_COOKIE)

    text = caplog.text
    assert "gld:sess" in text
    assert "gld:sess.sig" in text
    assert "GLADOS_COOKIES" in text
    assert COOKIE_SENTINEL not in text


def test_config_accepts_complete_four_cookie_without_warning(monkeypatch, caplog):
    """需求: 新格式 Cookie 不应产生任何针对 Cookie 的警告。"""
    with caplog.at_level("WARNING"):
        config = _config_with_cookie(monkeypatch, FULL_FOUR_COOKIE)

    assert config.cookies_list == [FULL_FOUR_COOKIE]
    assert "gld:sess" not in caplog.text
    assert "没有权限" not in caplog.text


def test_config_warns_when_base_cookie_is_malformed(monkeypatch, caplog):
    """失败模式: 完全不像 Cookie 的输入要提示缺少 koa:sess 字段。"""
    with caplog.at_level("WARNING"):
        _config_with_cookie(monkeypatch, f"gld:sess={COOKIE_SENTINEL}")

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
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, FULL_FOUR_COOKIE)

    assert checkin.main() == checkin.EXIT_OK


def test_main_returns_1_when_checkin_fails_on_all_domains(monkeypatch, _stub_api, caplog):
    """失败模式: 所有域名都失败时必须返回非 0, 让 Actions 变红而不是假绿。"""
    monkeypatch.setenv(checkin.Config.ENV_COOKIES, FULL_FOUR_COOKIE)
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

    result = api.checkin(FULL_FOUR_COOKIE)

    assert result["status"] == "签到失败"
    assert result["points"] == "0"


# --------------------------------------------------------------------------
# 端到端: 真实服务端 + 真实脚本进程
# --------------------------------------------------------------------------


def _run_checkin(env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop(checkin.Config.ENV_COOKIES, None)
    env.pop(checkin.Config.ENV_PUSH_KEY, None)
    env.update(env_overrides)

    return subprocess.run(
        [sys.executable, "checkin.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_e2e_stale_two_cookie_against_live_api_exits_nonzero_with_actionable_log():
    """端到端: 用 2026-09 之前格式的 Cookie 打真实接口, 必须失败并给出可操作提示。

    对应线上故障: 2026-09-24 08:52Z 之前同一份 Cookie 还能签到成功, 14:32Z 起
    glados.cloud 全接口返回 code -2「没有权限」。
    """
    proc = _run_checkin({checkin.Config.ENV_COOKIES: STALE_TWO_COOKIE})

    assert proc.returncode == checkin.EXIT_CHECKIN_FAILED, proc.stdout + proc.stderr
    assert "gld:sess" in proc.stderr or "gld:sess" in proc.stdout
    assert "认证失败" in proc.stderr
    assert "没有权限" in proc.stderr
    assert COOKIE_SENTINEL not in proc.stdout
    assert COOKIE_SENTINEL not in proc.stderr


def test_e2e_missing_cookie_env_exits_with_config_error():
    """端到端: 未配置 Cookie 的 Actions 运行必须直接失败。"""
    proc = _run_checkin({})

    assert proc.returncode == checkin.EXIT_CONFIG_ERROR, proc.stdout + proc.stderr
    assert "未找到有效的 Cookie" in proc.stderr
