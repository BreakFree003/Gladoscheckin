import requests
import json
import os
import sys
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass
from logging_config import init_logger


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2


class LogEmoji:
    """日志里的状态标记。

    只挂在"结果"上, 不给每一行都配一个 —— 行首的级别 (INFO/WARNING/ERROR) 已经
    说明了性质, 再补一个 ℹ️ 只是噪音, 而且各行的 emoji 间距还不一致。
    """

    SUCCESS = "✅"
    REPEAT = "🔄"
    FAIL = "❌"


"""唯一的站点。

这个脚本只服务 glados.cloud 一个站点: 会话字段是 gld:sess 与 gld:sess.sig
两个, 缺任何一个都不能签到 (2026-09-26 起站点把会话拆成了这两个字段)。"""
DOMAIN = "glados.cloud"
COOKIE_KEYS: Tuple[str, ...] = ("gld:sess", "gld:sess.sig")

"""认证失败时服务端可能返回的关键字。

中文是 glados.cloud 实测文案; 英文没有已知来源 (上游两站点时代留下的兜底), 留着
只是防止服务端换文案时把权限错误误判成别的错误。"""
PERMISSION_ERROR_HINTS: Tuple[str, ...] = ("没有权限", "no permission")

"""GLaDOS 判定「自动签到」时返回的 code 与关键字。

2026-09 实测: 同一份 Cookie, User-Agent 平台对不上登录浏览器时,
/api/user/checkin 返回 code 4「Automated check-in detected」, 而积分接口照常返回,
很容易被误判成 Cookie 失效。"""
AUTOMATION_ERROR_CODE = 4
AUTOMATION_ERROR_HINTS: Tuple[str, ...] = ("automated check-in detected",)

"""进程退出码: 0 全部账号签到成功/重复签到; 1 有账号签到失败; 2 配置错误 (无 Cookie)"""
EXIT_OK = 0
EXIT_CHECKIN_FAILED = 1
EXIT_CONFIG_ERROR = 2


def parse_cookie_keys(cookie: str) -> List[str]:
    """解析 Cookie 字符串里出现的字段名。只返回字段名, 不返回字段值, 避免泄露凭据。"""
    keys: List[str] = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        keys.append(part.split("=", 1)[0].strip())
    return keys


def missing_cookie_keys(cookie: str) -> List[str]:
    """返回 COOKIE_KEYS 中在这份 Cookie 里缺失的字段名。"""
    present = set(parse_cookie_keys(cookie))
    return [key for key in COOKIE_KEYS if key not in present]


def is_permission_error(code: int, message: str) -> bool:
    """判断接口响应是否为认证/权限失败 (Cookie 缺失、不完整或已失效)。"""
    if code != CheckinStatus.FAILURE.value:
        return False
    lowered = (message or "").lower()
    return any(hint in lowered for hint in PERMISSION_ERROR_HINTS)


def is_automation_blocked(code: int, message: str) -> bool:
    """判断签到是否被 GLaDOS 的反自动化校验拦下 (code 4)。"""
    lowered = (message or "").lower()
    return code == AUTOMATION_ERROR_CODE or any(
        hint in lowered for hint in AUTOMATION_ERROR_HINTS
    )


def log_method(func):
    """异常兜底装饰器: 把 API 方法的异常记进日志, 并返回该方法对应的失败默认值。

    注意这里只兜底、不改判成败: 返回的默认值都会被上层判成失败, 不会制造假绿。
    """

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"API {method_name} 执行失败: {e}")

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": ""},
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                error_template = DEFAULT_ERRORS[method_name]
                if isinstance(error_template, dict):
                    error_result = error_template.copy()
                    error_result["message"] = f"执行失败: {e}"
                    return error_result
                return error_template
            raise

    return wrapper


class Config:
    """应用配置"""

    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_USER_AGENT = "GLADOS_USER_AGENT"

    """默认 User-Agent。

GLaDOS 的反自动化校验会比对「签到请求的平台」与「登录时浏览器的平台」:
2026-09 实测同一份 Cookie 下, macOS UA 可以签到, Windows / Linux / iPhone UA
一律返回 code 4「Automated check-in detected」(改动 Chrome 版本号无影响)。
因此这里默认给一个 macOS 桌面 Chrome UA, 并用 GLADOS_USER_AGENT 覆盖成
你自己浏览器的 navigator.userAgent 才是最稳的做法。"""
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"

    """兑换计划与它需要的积分。

只有 plan500 一个: 服务端自己回过 "Need 500", 是唯一验证过的门槛。
plan100 / plan200 来自站点说明但从未在真实接口上验证过, 而 GLADOS_EXCHANGE_PLAN
这个开关除了把门槛改小之外没有别的用处, 所以连同它一起去掉了。"""
    EXCHANGE_PLAN = "plan500"
    EXCHANGE_PLAN_POINTS = 500

    def __init__(self):
        self.cookie: str = ""
        self.user_agent: str = self.DEFAULT_USER_AGENT
        self._load_config()

    def _load_config(self) -> None:
        """加载配置, 并把生效的配置打成启动日志 (一行, 便于事后对照)。"""
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        user_agent_env: Optional[str] = os.environ.get(self.ENV_USER_AGENT)

        # 只支持一个账号。以前用 "&" 拼接多个账号的写法已去掉 (见 README):
        # 整个值当成一份 Cookie, 里面若还带着 "&" 只会让站点认不出来 -> 变红。
        self.cookie = raw_cookies_env.strip() if raw_cookies_env else ""

        logger.info(f"开始签到: 兑换 {self.EXCHANGE_PLAN} (需 {self.EXCHANGE_PLAN_POINTS} 积分)")

        if user_agent_env and user_agent_env.strip():
            self.user_agent = user_agent_env.strip()
            logger.info(f"User-Agent: 用 {self.ENV_USER_AGENT} 指定的值")
        else:
            logger.info(f"User-Agent: 内置默认 (可用 {self.ENV_USER_AGENT} 覆盖为登录浏览器的 UA)")

        self._validate_cookie()

    def _validate_cookie(self) -> None:
        """校验 Cookie 结构, 只输出字段名与数量, 不输出凭据本身。

        只报问题: 字段齐全属于正常情况, 不值得在正常路径上空占一行。
        """
        if not self.cookie:
            return

        missing = missing_cookie_keys(self.cookie)
        if not missing:
            return

        present = parse_cookie_keys(self.cookie)
        logger.warning(
            f"Cookie 缺少会话字段 {'/'.join(missing)} "
            f"(当前字段: {', '.join(present) if present else '无'}); "
            f"{DOMAIN} 需要 {' 与 '.join(COOKIE_KEYS)} 两个字段, "
            f"缺 .sig 大多是复制时被截断了, 请重新复制完整 Cookie 更新 {self.ENV_COOKIES}"
        )


class API:
    """API 调用"""

    CHECKIN_URL = "/api/user/checkin"
    POINTS_URL = "/api/user/points"
    EXCHANGE_URL = "/api/user/exchange"

    """POST 的 content-type, 与站点前端 axios 发出的一致 (带 charset, 无空格)。"""
    CONTENT_TYPE_JSON = "application/json;charset=UTF-8"

    def __init__(self, user_agent: str = Config.DEFAULT_USER_AGENT):
        self.user_agent: str = user_agent
        self.headers: Dict[str, str] = self._get_headers()
        self._auth_error_reported: bool = False
        self._automation_error_reported: bool = False
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def close(self) -> None:
        """关闭 session。只有 with 语句会调用它, 那时 __init__ 必然已经跑完。"""
        try:
            self.session.close()
        except Exception as e:
            logger.error(f"关闭 session 时发生错误: {e}")

    def __enter__(self):
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头, 逐字对齐「网页上点签到」时浏览器发出的头。

        站点 console 包里 `axios.defaults.baseURL="/api"` 且
        `axios.post("/user/checkin", {token: location.hostname})`, axios 自己只设置
        accept(默认值)与 content-type(POST)。其余由浏览器生成。

        2026-09-26 用 CDP 抓了本机 Chrome 154 (macOS) 在 /console/checkin 点「签到」
        的真实请求 (见 tests/fixtures/browser_checkin_request.json), 结论:
        - accept 就是 `application/json, text/plain, */*`;
        - 页面 /console 带 `<meta name="referrer" content="no-referrer">`,
          所以浏览器**没有**发 Referer —— 这里也就不能自己造一个;
        - sec-ch-ua* / sec-fetch-* / accept-language / dnt 是浏览器进程生成的头,
          脚本不伪造 (实测缺了它们服务端照样返回 code 1, 而伪造的
          sec-ch-ua-platform 会和用户自定义的 GLADOS_USER_AGENT 自相矛盾)。"""
        return {
            "origin": f"https://{DOMAIN}",
            "accept": "application/json, text/plain, */*",
            "user-agent": self.user_agent,
        }

    def _log(self, level: str, message: str) -> None:
        """统一的 API 层日志。

        这里只输出有信息量的事件 (失败)。成功的请求不逐条回显 —— 跑完会有一行结果。
        """
        if level == "info":
            logger.info(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "error":
            logger.error(message)

    def _get_full_url(self, path: str) -> str:
        """获取完整 URL"""
        return f"https://{DOMAIN}{path}"

    def _report_auth_error(self, endpoint: str, message: str) -> None:
        """认证失败时输出一次可操作的提示, 避免每个接口重复刷屏。"""
        if self._auth_error_reported:
            return
        self._auth_error_reported = True
        self._log(
            "error",
            f"{endpoint} 认证失败 (code -2, message: {message}): Cookie 无效、已过期或不完整",
        )

    def _report_automation_block(self, payload: Dict) -> None:
        """被判定为自动签到 (code 4) 时输出一次可操作的提示。

        站点前端在 code 4 且 reason == "device-mismatch" 时会弹出「设备不一致,
        请重新登录」的对话框, 并把服务端给的 loginDevice / currentDevice 显示出来;
        脚本这边同样把这两个值打出来, 直接指出是哪台「设备」对不上。"""
        if self._automation_error_reported:
            return
        self._automation_error_reported = True

        details = [
            f"{key}: {payload[key]}"
            for key in ("reason", "loginDevice", "currentDevice")
            if payload.get(key)
        ]
        detail_text = f" 服务端返回 {'; '.join(details)}。" if details else " "

        self._log(
            "error",
            f"签到被判定为自动签到 (message: {payload.get('message', '')})。{detail_text}"
            f"GLaDOS 比对的是「登录时的设备平台」与「这次请求声明的平台」, 而脚本能声明平台的"
            f"只有 User-Agent (当前 [{self.user_agent}])。请在登录那个浏览器的控制台执行 "
            f"navigator.userAgent, 把完整值设为 {Config.ENV_USER_AGENT} "
            "(Windows / Linux / iPhone 的 UA 实测都会被拦下)",
        )

    def _serialize_post_body(self, data: Optional[Dict]) -> bytes:
        """按 axios 的方式序列化 JSON 请求体。

        axios 用 `JSON.stringify` 的紧凑格式 (`{"token":"glados.cloud"}`), 而
        requests 的 `json=` 走 `json.dumps` 默认分隔符, 会多出空格
        (`{"token": "glados.cloud"}`)。这里对齐成浏览器那一份字节。"""
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _make_request(self, url: str, method: str, data: Optional[Dict] = None, cookies: str = "") -> Optional[requests.Response]:
        """发送 HTTP 请求。

        请求体与 POST 的 content-type 与网页端逐字一致: 站点前端走 axios,
        请求体是紧凑 JSON, content-type 为 `application/json;charset=UTF-8`。
        GET 请求则**不带** content-type —— 浏览器的 GET 也不带。

        只有失败才记日志: 网络错误与非 2xx 都会带出原始响应。成功的请求一条不记 ——
        每个账号结束后有一行结果, 而请求头从不进日志 (Cookie 值不落盘)。
        """
        path = url.removeprefix(f"https://{DOMAIN}")
        body = self._serialize_post_body(data).decode("utf-8") if data else ""
        session_headers = self.headers.copy()
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                session_headers["content-type"] = self.CONTENT_TYPE_JSON
                response = self.session.post(url, headers=session_headers, data=body.encode("utf-8"), timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", f"不支持的 HTTP 方法: {method}")
                return None

            if not response.ok:
                self._log("warning", f"请求 {path} 失败: HTTP {response.status_code}, 响应内容: {response.text}")
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", f"请求 {path} 时发生网络错误: {e}")
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        """获取签到数据: 站点前端发的就是 {token: location.hostname}"""
        return {"token": DOMAIN}

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到"""
        url = self._get_full_url(self.CHECKIN_URL)
        checkin_data = self._get_checkin_data()
        response = self._make_request(url, "POST", checkin_data, cookies)

        result = {
            "status": "签到失败",
            "points": "0",
            "message": "",
            "code": CheckinStatus.FAILURE,
        }

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "无消息字段")
            points = str(data.get("points", 0))

            if code == CheckinStatus.SUCCESS.value:
                result["code"] = CheckinStatus.SUCCESS
                result["status"] = "签到成功"
                result["points"] = points
                result["message"] = message
            elif code == CheckinStatus.REPEAT.value:
                result["code"] = CheckinStatus.REPEAT
                result["status"] = "重复签到"
                result["points"] = "0"
                result["message"] = message
            else:
                # 已知原因 (权限/反自动化) 各自有专门的一行解释, 只有其他 code
                # 才需要在这里留下原始 code 与 message。
                if is_permission_error(code, message):
                    self._report_auth_error("checkin", message)
                elif is_automation_blocked(code, message):
                    self._report_automation_block(data)
                else:
                    # 已知原因各有专门的一行解释; 剩下的只有原始响应能说明问题
                    # (服务端加字段时, 这里就能看见)。
                    self._log("error", f"签到失败: {response.text}")
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
                result["message"] = message
        else:
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["message"] = "网络请求失败"

        return result

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int]:
        """获取总积分。第二个返回值是给兑换门槛用的数字。"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "")
            points = data.get("points", None)

            if points is not None:
                points_int = int(float(points))
                return f"{points_int} 积分", points_int

            if is_permission_error(code, message):
                self._report_auth_error("points", message)
            else:
                self._log("warning", f"读取总积分失败: {response.text}")
            return "None 积分", 0

        return "None 积分", 0

    @log_method
    def exchange(self, cookies: str, plan: str) -> str:
        """执行兑换。

        调用方只在积分达到门槛时才调用这里: 积分不够时服务端只会回
        "Not enough points", 每天发一次请求、再报一次错没有意义。
        """
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "未知错误")

            if code == CheckinStatus.SUCCESS.value:
                # 兑换成功不在这里记: 调用方的结果行一定会带上"兑换成功: plan500",
                # 而且是无条件输出 —— 它会真扣掉 500 积分, 不能只在某处悄悄发生。
                return f"兑换成功: {plan}"

            self._log("error", f"兑换失败: {plan} (code {code}, message: {message})")
            if is_permission_error(code, message):
                self._report_auth_error("exchange", message)
            return f"兑换失败: {message}"

        return "兑换失败"


@dataclass()
class CheckinResult:
    """一次签到的结果"""

    status: str = "签到失败"
    points: str = "0"
    points_total: str = "None"
    exchange: str = ""
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败

    @property
    def passed(self) -> bool:
        """签到成功与重复签到都算通过, 其余一律算失败 (fail-closed)。"""
        return self.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT)

    def describe(self) -> str:
        """把结果拼成一行: 状态 + 本次获得 + 总积分 + 兑换情况。"""
        emoji = {
            CheckinStatus.SUCCESS: LogEmoji.SUCCESS,
            CheckinStatus.REPEAT: LogEmoji.REPEAT,
            CheckinStatus.FAILURE: LogEmoji.FAIL,
        }[self.code]

        line = f"{emoji} {self.status}"
        if self.code is CheckinStatus.FAILURE:
            return line

        # 重复签到时没有"获得"可言, 硬写一句"获得 0 积分"看着像 bug。
        earned = f"获得 {self.points} 积分, " if self.code is CheckinStatus.SUCCESS else ""
        return f"{line}, {earned}总 {self.points_total}, {self.exchange}"


def run_checkin(config: Config) -> CheckinResult:
    """一次完整的签到: 签到 -> 查总积分 -> 达标才兑换。"""
    result = CheckinResult()

    with API(user_agent=config.user_agent) as api:
        # 1. 签到
        checkin_result = api.checkin(config.cookie)
        result.status = checkin_result["status"]
        result.code = checkin_result.get("code", CheckinStatus.FAILURE)
        result.points = checkin_result.get("points", "0")

        # 2. 总积分
        points_str, points_num = api.get_points(config.cookie)
        result.points_total = points_str

        # 3. 兑换: 积分没到门槛就不发这个请求。服务端对积分不够只会回
        #    "Not enough points", 每天发一次、再报一次错既没用又像是故障。
        #    这里只写门槛, 不重复余额 —— 结果行里刚打过总积分。
        if points_num < config.EXCHANGE_PLAN_POINTS:
            result.exchange = f"未到 {config.EXCHANGE_PLAN_POINTS} 兑换门槛"
        else:
            result.exchange = api.exchange(config.cookie, config.EXCHANGE_PLAN)

    return result


# 初始化日志
logger = init_logger()


def main() -> int:
    """主函数, 返回进程退出码 (0 签到成功 / 1 签到失败 / 2 配置错误)。

    日志约定: 跑完打一行结果, 这里只收尾 —— 失败时给出下一步, 最后一行带退出码。
    通知方式: 靠退出码让 GitHub Actions 变红, 由 GitHub 发失败邮件, 脚本不做推送。
    """
    exit_code = EXIT_OK
    next_step = ""

    try:
        config = Config()

        if not config.cookie:
            logger.error(f"未找到有效的 Cookie, 请设置 {Config.ENV_COOKIES}")
            exit_code = EXIT_CONFIG_ERROR
        else:
            result = run_checkin(config)
            line = result.describe()
            if result.passed:
                logger.info(line)
            else:
                logger.warning(line)

            if not result.passed:
                exit_code = EXIT_CHECKIN_FAILED
                next_step = (
                    f"请检查 Cookie 是否完整/过期 ({DOMAIN} 需要 {' 与 '.join(COOKIE_KEYS)} 两个字段), "
                    f"或签到被判定为自动签到 (code 4, 需设置 {Config.ENV_USER_AGENT})"
                )

    except Exception as e:
        logger.error(f"执行过程中发生未预期的错误: {e}")
        exit_code = EXIT_CHECKIN_FAILED

    if exit_code == EXIT_OK:
        logger.info(f"签到完成 (退出码 {exit_code})")
    else:
        logger.error(f"签到失败 (退出码 {exit_code})")
    if next_step:
        logger.error(next_step)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
