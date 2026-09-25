import requests
import json
import os
import sys
import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from pypushdeer import PushDeer
from logging_config import init_logger


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2


class ExchangePlan(Enum):
    """兑换计划"""

    PLAN100 = "plan100"
    PLAN200 = "plan200"
    PLAN500 = "plan500"


class APIEndpoint(Enum):
    """API端点"""

    CHECKIN = "/api/user/checkin"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    PENDING = "⏳"
    CHECKIN = "🎫"
    STATUS = "📊"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️ "
    ERROR = "🔴"
    INFO = "ℹ️ "


"""GLaDOS 自 2026-09 起同时下发两套会话 Cookie。

只带 koa:sess / koa:sess.sig 的旧 Cookie 会被服务端拒绝, 所有接口统一返回
code -2「没有权限」, 表现为 Actions 里签到全部失败。"""
COOKIE_BASE_KEYS: Tuple[str, ...] = ("koa:sess", "koa:sess.sig")
COOKIE_EXTRA_KEYS: Tuple[str, ...] = ("gld:sess", "gld:sess.sig")

"""认证失败时服务端返回的关键字 (中英文站点各一份)"""
PERMISSION_ERROR_HINTS: Tuple[str, ...] = ("没有权限", "no permission")

"""GLaDOS 判定「自动签到」时返回的 code 与关键字。

2026-09 实测: 同一份 Cookie, User-Agent 平台对不上登录浏览器时,
/api/user/checkin 返回 code 4「Automated check-in detected」, 而
status/points 等接口照常工作, 很容易被误判成 Cookie 失效。"""
AUTOMATION_ERROR_CODE = 4
AUTOMATION_ERROR_HINTS: Tuple[str, ...] = ("automated check-in detected",)

"""进程退出码: 0 全部账号成功; 1 有账号在所有域名上都失败; 2 配置错误 (无 Cookie)"""
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


def missing_cookie_keys(cookie: str, keys: Tuple[str, ...]) -> List[str]:
    """返回 keys 中在 Cookie 里缺失的字段名。"""
    present = set(parse_cookie_keys(cookie))
    return [key for key in keys if key not in present]


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
    """日志装饰器"""

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji_map = {
            "checkin": LogEmoji.CHECKIN,
            "get_status": LogEmoji.STATUS,
            "get_points": LogEmoji.POINTS,
            "exchange": LogEmoji.EXCHANGE,
        }
        emoji = emoji_map.get(method_name, LogEmoji.INFO)
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {LogEmoji.ERROR} {method_name} 执行失败: {e}")

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": ""},
                "get_status": ("None 天", -2),
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

    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
    ENV_VERBOSE = "GLADOS_VERBOSE"
    ENV_USER_AGENT = "GLADOS_USER_AGENT"

    """默认 User-Agent。

GLaDOS 的反自动化校验会比对「签到请求的平台」与「登录时浏览器的平台」:
2026-09 实测同一份 Cookie 下, macOS UA 可以签到, Windows / Linux / iPhone UA
一律返回 code 4「Automated check-in detected」(改动 Chrome 版本号无影响)。
因此这里默认给一个 macOS 桌面 Chrome UA, 并用 GLADOS_USER_AGENT 覆盖成
你自己浏览器的 navigator.userAgent 才是最稳的做法。"""
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"

    """默认兑换计划"""
    DEFAULT_EXCHANGE_PLAN = "plan500"

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    """默认域名"""
    DOMAINS = ["glados.cloud", "railgun.info"]

    """兑换计划列表"""
    EXCHANGE_PLANS = {
        ExchangePlan.PLAN100.value: 100,
        ExchangePlan.PLAN200.value: 200,
        ExchangePlan.PLAN500.value: 500,
    }

    def __init__(self):
        self.push_key: str = ""
        self.cookies_list: List[str] = []
        self.exchange_plan: str = self.DEFAULT_EXCHANGE_PLAN
        self.verbose: bool = self.DEFAULT_VERBOSE
        self.user_agent: str = self.DEFAULT_USER_AGENT
        self._load_config()

    def _load_config(self) -> None:
        """加载配置"""
        push_key_env: Optional[str] = os.environ.get(self.ENV_PUSH_KEY)
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        exchange_plan_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLAN)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)
        user_agent_env: Optional[str] = os.environ.get(self.ENV_USER_AGENT)

        if not push_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_PUSH_KEY}' 未设置。")
            self.push_key = ""
        else:
            self.push_key = push_key_env

        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        if not exchange_plan_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 未设置，将使用默认兑换计划 {self.DEFAULT_EXCHANGE_PLAN}。")
            self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN
        else:
            if exchange_plan_env in self.EXCHANGE_PLANS:
                self.exchange_plan = exchange_plan_env
                logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换计划: {self.exchange_plan}")
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将使用默认兑换计划 {self.DEFAULT_EXCHANGE_PLAN}。")
                self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie 用于签到。")
        self._validate_cookies()
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_EXCHANGE_PLAN}: {self.exchange_plan}。")

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")

        if user_agent_env and user_agent_env.strip():
            self.user_agent = user_agent_env.strip()
            logger.info(f"{LogEmoji.INFO} 使用 {self.ENV_USER_AGENT} 指定的 User-Agent。")
        else:
            logger.info(
                f"{LogEmoji.INFO} 未设置 {self.ENV_USER_AGENT}, 使用默认 {self.user_agent}。"
                "若签到被判定为自动签到 (code 4), 请把它设为你浏览器的 navigator.userAgent。"
            )

    def _validate_cookies(self) -> None:
        """校验 Cookie 结构, 只输出字段名与数量, 不输出凭据本身。"""
        for idx, cookie in enumerate(self.cookies_list, 1):
            missing_base = missing_cookie_keys(cookie, COOKIE_BASE_KEYS)
            if missing_base:
                logger.warning(
                    f"{LogEmoji.WARNING} Cookie[{idx}] 缺少 {', '.join(missing_base)}，"
                    f"请重新从浏览器复制完整 Cookie 更新 {self.ENV_COOKIES}。"
                )
                continue

            missing_extra = missing_cookie_keys(cookie, COOKIE_EXTRA_KEYS)
            if missing_extra:
                logger.warning(
                    f"{LogEmoji.WARNING} Cookie[{idx}] 缺少 {', '.join(missing_extra)}："
                    "GLaDOS 已改为同时校验 koa:sess/koa:sess.sig 与 gld:sess/gld:sess.sig，"
                    "只剩前两项时所有接口都会返回 code -2「没有权限」，签到必然失败。"
                    f"请重新登录后复制完整 Cookie 更新 {self.ENV_COOKIES}。"
                )
            else:
                logger.info(f"{LogEmoji.INFO} Cookie[{idx}] 关键字段完整 ({len(parse_cookie_keys(cookie))} 项)。")


class API:
    """API 调用"""

    CHECKIN_URL = APIEndpoint.CHECKIN.value
    STATUS_URL = APIEndpoint.STATUS.value
    POINTS_URL = APIEndpoint.POINTS.value
    EXCHANGE_URL = APIEndpoint.EXCHANGE.value

    def __init__(
        self,
        domain: str,
        cookie_index: int = 0,
        verbose: bool = False,
        user_agent: str = Config.DEFAULT_USER_AGENT,
    ):
        self.domain: str = domain
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        self.user_agent: str = user_agent
        self.headers: Dict[str, str] = self._get_headers()
        self._auth_error_reported: bool = False
        self._automation_error_reported: bool = False
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def __del__(self):
        """关闭 session"""
        self.close()

    def close(self) -> None:
        """关闭 session"""
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        return {
            "origin": f"https://{self.domain}",
            "user-agent": self.user_agent,
        }

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        log_message = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {emoji} {message}"

        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _get_full_url(self, path: str) -> str:
        """获取完整 URL"""
        return f"https://{self.domain}{path}"

    def _report_auth_error(self, endpoint: str, message: str) -> None:
        """认证失败时输出一次可操作的提示, 避免每个接口重复刷屏。"""
        if self._auth_error_reported:
            return
        self._auth_error_reported = True
        self._log(
            "error",
            LogEmoji.ERROR,
            f"{endpoint} 认证失败 (code -2, message: {message})：Cookie 无效、已过期或不完整。"
            "GLaDOS 现要求 Cookie 同时包含 koa:sess、koa:sess.sig、gld:sess、gld:sess.sig；"
            "请重新登录并复制完整 Cookie 更新 GLADOS_COOKIES。",
            force=True,
        )

    def _report_automation_block(self, message: str) -> None:
        """被判定为自动签到 (code 4) 时输出一次可操作的提示。"""
        if self._automation_error_reported:
            return
        self._automation_error_reported = True
        self._log(
            "error",
            LogEmoji.ERROR,
            f"签到被判定为自动签到 (message: {message})："
            "GLaDOS 会校验签到请求的平台是否与登录浏览器一致。"
            f"当前 User-Agent 为 [{self.user_agent}]，"
            "实测 Windows / Linux / iPhone UA 均会被拦下 (改 Chrome 版本号无效)。"
            "请在浏览器控制台执行 navigator.userAgent 取得完整值，"
            "再把它设为 GLADOS_USER_AGENT。",
            force=True,
        )

    def _make_request(self, url: str, method: str, data: Optional[Dict] = None, cookies: str = "") -> Optional[requests.Response]:
        """发送 HTTP 请求"""
        session_headers = self.headers.copy()
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                response = self.session.post(url, headers=session_headers, data=data, timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            if not response.ok:
                self._log("warning", LogEmoji.WARNING, f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text}", force=True)
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        """获取签到数据"""
        return {"token": self.domain}

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
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
                result["code"] = CheckinStatus.SUCCESS
                result["status"] = "签到成功"
                result["points"] = points
                result["message"] = message
            elif code == CheckinStatus.REPEAT.value:
                self._log("info", LogEmoji.REPEAT, f"{{ code : {code}, message : {message} }}", force=True)
                result["code"] = CheckinStatus.REPEAT
                result["status"] = "重复签到"
                result["points"] = "0"
                result["message"] = message
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                if is_permission_error(code, message):
                    self._report_auth_error("checkin", message)
                elif is_automation_blocked(code, message):
                    self._report_automation_block(message)
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
                result["message"] = message
        else:
            self._log("warning", LogEmoji.WARNING, "签到失败", force=True)
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["message"] = "网络请求失败"

        return result

    @log_method
    def get_status(self, cookies: str) -> Tuple[str, int]:
        """获取状态"""

        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "")
            left_days = data.get("data", {}).get("leftDays", None)

            if left_days is not None:
                left_days_int = int(float(left_days))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, leftDays : {left_days_int} 天}}")
                return f"{left_days_int} 天", code
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, leftDays : {left_days} 天}}", force=True)
                if is_permission_error(code, message):
                    self._report_auth_error("status", message)
                return "None 天", code
        else:
            self._log("warning", LogEmoji.WARNING, "获取状态失败", force=True)
            return "None 天", -2

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int]:
        """获取积分"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "")
            points = data.get("points", None)

            if points is not None:
                points_int = int(float(points))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points_int} 积分}}")
                points_str = f"{points_int} 积分"
                points_num = points_int
                return points_str, points_num
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, points : {points} 积分}}", force=True)
                if is_permission_error(code, message):
                    self._report_auth_error("points", message)
                return "None 积分", 0
        else:
            self._log("warning", LogEmoji.WARNING, "获取积分失败", force=True)
            return "None 积分", 0

    @log_method
    def exchange(self, cookies: str, plan: str, required_points: int) -> str:
        """执行兑换"""
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "未知错误")

            if code == 0:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, message : {message} }}")
                return f"兑换成功: {plan}"
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                if is_permission_error(code, message):
                    self._report_auth_error("exchange", message)
                return f"兑换失败: {message}"
        else:
            self._log("warning", LogEmoji.WARNING, "兑换失败", force=True)
            return "兑换失败"


@dataclass()
class CheckinResult:
    """签到结果"""

    cookie_index: int
    domain: str
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        result_dict = asdict(self)
        return result_dict


class PushService:
    """推送服务"""

    def __init__(self, config: Optional[Config] = None):
        self.config = config

    @property
    def push_key(self) -> str:
        """推送密钥, 配置缺失时视为未设置。"""
        return getattr(self.config, "push_key", "") or ""

    def send(self, title: str, content: str) -> bool:
        """发送推送"""
        if not self.push_key:
            logger.info(f"{LogEmoji.WARNING} 未设置推送密钥，跳过推送通知。")
            return False

        try:
            pushdeer = PushDeer(pushkey=self.push_key)
            pushdeer.send_text(title, desp=content)
            logger.info(f"{LogEmoji.SUCCESS} 推送通知发送成功。")
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} 发送推送通知失败: {e}")
            return False


class Checker:
    """签到"""

    def __init__(self, config: Config):
        self.config = config
        self.results = []

    def _log(self, cookie_idx: int, domain: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}")

    def checkin_all(self):
        """执行所有签到任务"""
        cookie_count = len(self.config.cookies_list)
        domain_count = len(self.config.DOMAINS)
        total_tasks = cookie_count * domain_count
        task_idx = 0

        logger.info(f"{LogEmoji.INFO} 共 {cookie_count} 个 Cookie, {domain_count} 个域名, 共 {total_tasks} 个任务")

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            logger.info(f"{LogEmoji.START} ========== 开始处理 Cookie {cookie_idx} ==========")

            for domain in self.config.DOMAINS:
                task_idx += 1
                logger.info(f"{LogEmoji.INFO} ----- 任务 {task_idx}/{total_tasks}: {LogEmoji.COOKIE}[{cookie_idx}] on {LogEmoji.DOMAIN}[{domain}] -----")

                result = self._checkin_on_domain(cookie, cookie_idx, domain)
                self.results.append(result)

                result_message = f"结果: {result.status}"
                if result.code == CheckinStatus.SUCCESS:
                    if self.config.verbose:
                        result_message = f"结果: {result.status}, 获得 {result.points} 积分, 剩余 {result.days}, 总 {result.points_total}, {result.exchange}"
                    self._log(cookie_idx, domain, LogEmoji.SUCCESS, result_message, force=True)
                else:
                    self._log(cookie_idx, domain, LogEmoji.WARNING, result_message, force=True)

    def _checkin_on_domain(self, cookie: str, cookie_idx: int, domain: str) -> CheckinResult:
        result = CheckinResult(cookie_idx, domain)

        with API(domain, cookie_idx, verbose=self.config.verbose, user_agent=self.config.user_agent) as api:
            # 1. 获取状态
            self._log(cookie_idx, domain, LogEmoji.STATUS, "查询剩余天数")
            days_str, status_code = api.get_status(cookie)
            result.days = days_str

            # 2. 签到
            self._log(cookie_idx, domain, LogEmoji.CHECKIN, "执行签到")
            checkin_result = api.checkin(cookie)
            result.status = checkin_result["status"]
            result.code = checkin_result.get("code", CheckinStatus.FAILURE)

            # 3. 获取积分
            self._log(cookie_idx, domain, LogEmoji.POINTS, "查询总积分")
            points_str, points_num = api.get_points(cookie)
            result.points_total = points_str

            # 4. 执行兑换
            required_points = self.config.EXCHANGE_PLANS.get(self.config.exchange_plan, 500)
            self._log(
                cookie_idx,
                domain,
                LogEmoji.EXCHANGE,
                f"开始兑换 {self.config.exchange_plan} (需要 {required_points} 积分)",
            )
            result.exchange = api.exchange(cookie, self.config.exchange_plan, required_points)

        return result

    def get_results(self) -> List[Dict[str, str]]:
        """获取所有结果"""
        return [result.to_dict() for result in self.results]

    def failed_cookie_indexes(self) -> List[int]:
        """返回在所有域名上都未签到成功/重复的 Cookie 序号。

        同一个 Cookie 会被依次发往 glados.cloud 与 railgun.info, 通常只有其中一个
        站点持有该账号, 另一个必然返回 code -2。因此以「该 Cookie 是否至少在一个
        域名上成功」作为账号维度的成功判据, 避免把正常现象当成失败。
        """
        succeeded = {
            result["cookie_index"]
            for result in self.get_results()
            if result["code"] in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT)
        }
        return [
            idx
            for idx in range(1, len(self.config.cookies_list) + 1)
            if idx not in succeeded
        ]

    def format_results(self) -> Tuple[str, str, str]:
        """格式化结果"""
        results = self.get_results()

        success_count = sum(1 for r in results if r["code"] == CheckinStatus.SUCCESS)
        repeat_count = sum(1 for r in results if r["code"] == CheckinStatus.REPEAT)
        fail_count = sum(1 for r in results if r["code"] == CheckinStatus.FAILURE)

        title = f"GLaDOS 签到, 成功{success_count}, 失败{fail_count}, 重复{repeat_count}"

        send_content_lines = []
        log_content_lines = []
        for i, res in enumerate(results, 1):
            line = f"#{i} P:{res['points']} 剩余:{res['days']} 总积分:{res['points_total']} | {res['status']} | {res['exchange']}"
            send_content_lines.append(line)

            if self.config.verbose:
                log_line = line
            else:
                log_line = f"#{i} {res['status']}"
            log_content_lines.append(log_line)

        content = "\n".join(send_content_lines)
        log_content = "\n".join(log_content_lines)
        return title, content, log_content


# 初始化日志
logger = init_logger()


def main() -> int:
    """主函数, 返回进程退出码 (0 成功 / 1 签到失败 / 2 配置错误)。"""
    exit_code = EXIT_OK
    config: Optional[Config] = None

    try:
        # 1. 加载配置
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            title, content = "# 未找到 cookies!", ""
            exit_code = EXIT_CONFIG_ERROR
        else:
            # 2. 执行签到
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            checker = Checker(config)
            checker.checkin_all()

            # 3. 格式化结果
            logger.info(f"{LogEmoji.START} 步骤 3: 格式化结果")
            title, content, log_content = checker.format_results()
            logger.info(f"\n{LogEmoji.END}========== 签到总结 ==========\n{title}\n{log_content}")

            failed_indexes = checker.failed_cookie_indexes()
            if failed_indexes:
                exit_code = EXIT_CHECKIN_FAILED
                logger.error(
                    f"{LogEmoji.ERROR} Cookie "
                    f"{', '.join(f'[{idx}]' for idx in failed_indexes)} "
                    "在所有域名上都未签到成功, 请检查 Cookie 是否完整/过期 "
                    "(GLaDOS 现要求 koa:sess、koa:sess.sig、gld:sess、gld:sess.sig)，"
                    "或签到被判定为自动签到 (code 4, 需把 GLADOS_USER_AGENT 设为浏览器 "
                    "navigator.userAgent)。"
                )

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        title, content, log_content = "# 脚本执行出错", str(e), str(e)
        exit_code = EXIT_CHECKIN_FAILED

    # 4. 发送推送
    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    push_service = PushService(config)
    push_service.send(title, content)
    logger.info(f"{LogEmoji.END} 签到完成 (退出码 {exit_code})")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
