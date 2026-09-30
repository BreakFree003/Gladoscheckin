import requests
import json
import os
import sys
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
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
    WARNING = "⚠️"
    SEND = "→"
    RECV = "←"


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
/api/user/checkin 返回 code 4「Automated check-in detected」, 而
status/points 等接口照常工作, 很容易被误判成 Cookie 失效。"""
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
            logger.error(f"[{self.cookie_index}] API {method_name} 执行失败: {e}")

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

    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_VERBOSE = "GLADOS_VERBOSE"
    ENV_USER_AGENT = "GLADOS_USER_AGENT"

    """默认 User-Agent。

GLaDOS 的反自动化校验会比对「签到请求的平台」与「登录时浏览器的平台」:
2026-09 实测同一份 Cookie 下, macOS UA 可以签到, Windows / Linux / iPhone UA
一律返回 code 4「Automated check-in detected」(改动 Chrome 版本号无影响)。
因此这里默认给一个 macOS 桌面 Chrome UA, 并用 GLADOS_USER_AGENT 覆盖成
你自己浏览器的 navigator.userAgent 才是最稳的做法。"""
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"

    """默认是否输出详细日志"""
    DEFAULT_VERBOSE = False

    """兑换计划与它需要的积分。

只有 plan500 一个: 服务端自己回过 "Need 500", 是唯一验证过的门槛。
plan100 / plan200 来自站点说明但从未在真实接口上验证过, 而 GLADOS_EXCHANGE_PLAN
这个开关除了把门槛改小之外没有别的用处, 所以连同它一起去掉了。"""
    EXCHANGE_PLAN = "plan500"
    EXCHANGE_PLAN_POINTS = 500

    def __init__(self):
        self.cookies_list: List[str] = []
        self.verbose: bool = self.DEFAULT_VERBOSE
        self.user_agent: str = self.DEFAULT_USER_AGENT
        self._load_config()

    def _load_config(self) -> None:
        """加载配置, 并把生效的配置打成启动日志 (一行, 便于事后对照)。"""
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)
        user_agent_env: Optional[str] = os.environ.get(self.ENV_USER_AGENT)

        if raw_cookies_env:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置, 但未包含任何有效的 Cookie。")

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(
                    f"环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效, "
                    f"按 {self.DEFAULT_VERBOSE} 处理。"
                )

        logger.info(
            f"开始签到: {len(self.cookies_list)} 个账号, "
            f"兑换 {self.EXCHANGE_PLAN} (需 {self.EXCHANGE_PLAN_POINTS} 积分), "
            f"详细日志 {'开' if self.verbose else '关'}"
        )

        if user_agent_env and user_agent_env.strip():
            self.user_agent = user_agent_env.strip()
            logger.info(f"User-Agent: 用 {self.ENV_USER_AGENT} 指定的值")
        else:
            logger.info(f"User-Agent: 内置默认 (可用 {self.ENV_USER_AGENT} 覆盖为登录浏览器的 UA)")

        self._validate_cookies()

    def _validate_cookies(self) -> None:
        """校验 Cookie 结构, 只输出字段名与数量, 不输出凭据本身。

        字段齐全属于正常情况, 只写详细日志; 缺字段才是要看的那条。
        """
        for idx, cookie in enumerate(self.cookies_list, 1):
            missing = missing_cookie_keys(cookie)
            if not missing:
                if self.verbose:
                    logger.info(f"[{idx}] Cookie 会话字段完整 ({len(parse_cookie_keys(cookie))} 项)")
                continue

            present = parse_cookie_keys(cookie)
            logger.warning(
                f"[{idx}] Cookie 缺少会话字段 {'/'.join(missing)} "
                f"(当前字段: {', '.join(present) if present else '无'}); "
                f"{DOMAIN} 需要 {' 与 '.join(COOKIE_KEYS)} 两个字段, "
                f"缺 .sig 大多是复制时被截断了, 请重新复制完整 Cookie 更新 {self.ENV_COOKIES}"
            )


class API:
    """API 调用"""

    CHECKIN_URL = "/api/user/checkin"
    STATUS_URL = "/api/user/status"
    POINTS_URL = "/api/user/points"
    EXCHANGE_URL = "/api/user/exchange"

    """POST 的 content-type, 与站点前端 axios 发出的一致 (带 charset, 无空格)。"""
    CONTENT_TYPE_JSON = "application/json;charset=UTF-8"

    def __init__(
        self,
        cookie_index: int = 0,
        verbose: bool = False,
        user_agent: str = Config.DEFAULT_USER_AGENT,
    ):
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
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

    def _log(self, level: str, message: str, force: bool = False) -> None:
        """统一日志输出方法。

        行首的 `[N]` 是账号序号 (多个账号时才分得清), 详细日志以外的行平时不出现。
        """
        if not (force or self.verbose):
            return

        log_message = f"[{self.cookie_index}] {message}"
        if level == "info":
            logger.info(log_message)
        elif level == "warning":
            logger.warning(log_message)
        elif level == "error":
            logger.error(log_message)

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
            force=True,
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
            force=True,
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

        线上流量只在这里记一次 (详细日志): 请求记方法/路径/请求体, 响应记状态码与
        响应体。各接口方法因此不用再各自回显一遍原始 JSON。Cookie 不在记录范围内
        —— 请求头从不进日志。
        """
        path = url.removeprefix(f"https://{DOMAIN}")
        body = self._serialize_post_body(data).decode("utf-8") if data else ""
        session_headers = self.headers.copy()
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                session_headers["content-type"] = self.CONTENT_TYPE_JSON
                self._log("info", f"{LogEmoji.SEND} POST {path} {body}")
                response = self.session.post(url, headers=session_headers, data=body.encode("utf-8"), timeout=(60, 120))
            elif method.upper() == "GET":
                self._log("info", f"{LogEmoji.SEND} GET {path}")
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", f"不支持的 HTTP 方法: {method}", force=True)
                return None

            self._log("info", f"{LogEmoji.RECV} {response.status_code} {response.text}")
            if not response.ok:
                self._log("warning", f"请求 {path} 失败: HTTP {response.status_code}, 响应内容: {response.text}", force=True)
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", f"请求 {path} 时发生网络错误: {e}", force=True)
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
                    self._log("error", f"签到失败: code {code}, message: {message}", force=True)
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
    def get_status(self, cookies: str) -> Tuple[str, int]:
        """获取剩余天数。第二个返回值只是给详细日志用的原始 code。"""
        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "")
            left_days = data.get("data", {}).get("leftDays", None)

            if left_days is not None:
                return f"{int(float(left_days))} 天", code

            # 权限问题的解释统一交给 _report_auth_error 打一次, 这里不再重说一遍。
            if is_permission_error(code, message):
                self._report_auth_error("status", message)
            else:
                self._log("warning", f"读取剩余天数失败: code {code}, message: {message}", force=True)
            return "None 天", code

        return "None 天", -2

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
                self._log("warning", f"读取总积分失败: code {code}, message: {message}", force=True)
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
                # 兑换会真扣掉几百积分, 是"改变账号状态"的操作, 不管有没有开详细日志
                # 都必须留痕 —— 否则成功时反而在日志里什么都看不到。
                self._log("info", f"兑换成功: {plan} (code {code}, message: {message})", force=True)
                return f"兑换成功: {plan}"

            self._log("error", f"兑换失败: {plan} (code {code}, message: {message})", force=True)
            if is_permission_error(code, message):
                self._report_auth_error("exchange", message)
            return f"兑换失败: {message}"

        return "兑换失败"


@dataclass()
class CheckinResult:
    """单个账号的签到结果"""

    cookie_index: int
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        return asdict(self)


class Checker:
    """签到一个或多个账号"""

    def __init__(self, config: Config):
        self.config = config
        self.results: List[CheckinResult] = []

    def _log(self, cookie_idx: int, message: str, level: str = "info", verbose_only: bool = False) -> None:
        """统一的账号级日志。

        结果行一定要输出 (不能被详细日志开关藏起来), 过程性细节走 verbose_only。
        """
        if verbose_only and not self.config.verbose:
            return

        log_message = f"[{cookie_idx}] {message}"
        if level == "warning":
            logger.warning(log_message)
        else:
            logger.info(log_message)

    def checkin_all(self) -> None:
        """依次签到每个账号, 每个账号跑完就打一行结果。"""
        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            result = self._checkin_account(cookie, cookie_idx)
            self.results.append(result)
            self._log(
                cookie_idx,
                self._describe(result),
                level="warning" if result.code is CheckinStatus.FAILURE else "info",
            )

    @staticmethod
    def _describe(result: CheckinResult) -> str:
        """把单个账号的结果拼成一行: 状态 + 本次获得/剩余/总积分 + 兑换情况。"""
        emoji = {
            CheckinStatus.SUCCESS: LogEmoji.SUCCESS,
            CheckinStatus.REPEAT: LogEmoji.REPEAT,
            CheckinStatus.FAILURE: LogEmoji.FAIL,
        }[result.code]

        line = f"{emoji} {result.status}"
        if result.code is CheckinStatus.FAILURE:
            return line
        return f"{line}, 获得 {result.points} 积分, 剩余 {result.days}, 总 {result.points_total}, {result.exchange}"

    def _checkin_account(self, cookie: str, cookie_idx: int) -> CheckinResult:
        """一个账号的完整流程: 查状态 -> 签到 -> 查积分 -> 达标才兑换。"""
        result = CheckinResult(cookie_idx)

        with API(cookie_idx, verbose=self.config.verbose, user_agent=self.config.user_agent) as api:
            # 1. 剩余天数 (只进详细日志的结果行, 默认日志里靠签到结果说话)
            result.days, _ = api.get_status(cookie)

            # 2. 签到
            checkin_result = api.checkin(cookie)
            result.status = checkin_result["status"]
            result.code = checkin_result.get("code", CheckinStatus.FAILURE)
            result.points = checkin_result.get("points", "0")

            # 3. 总积分
            points_str, points_num = api.get_points(cookie)
            result.points_total = points_str

            # 4. 兑换: 积分没到门槛就不发这个请求。服务端对积分不够只会回
            #    "Not enough points", 每天发一次、再报一次错既没用又像是故障。
            required_points = self.config.EXCHANGE_PLAN_POINTS
            if points_num < required_points:
                result.exchange = f"未兑换 (积分 {points_num}/{required_points})"
                self._log(
                    cookie_idx,
                    f"积分 {points_num}/{required_points}, 未到 {self.config.EXCHANGE_PLAN} 门槛, 跳过兑换",
                    verbose_only=True,
                )
            else:
                result.exchange = api.exchange(cookie, self.config.EXCHANGE_PLAN)

        return result

    def get_results(self) -> List[Dict[str, str]]:
        """获取所有结果"""
        return [result.to_dict() for result in self.results]

    def failed_cookie_indexes(self) -> List[int]:
        """返回未签到成功 (也没重复签到) 的 Cookie 序号。

        判据是「该账号有没有拿到成功或重复签到的结果」, 并且以配置里的账号数为准:
        某个账号没有结果 (例如中途异常) 也算失败 —— 宁可报红, 也不能静默漏签。
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

    def format_results(self) -> str:
        """汇总一行: 成功/失败/重复 各多少个账号。"""
        results = self.get_results()
        success = sum(1 for r in results if r["code"] == CheckinStatus.SUCCESS)
        repeat = sum(1 for r in results if r["code"] == CheckinStatus.REPEAT)
        fail = sum(1 for r in results if r["code"] == CheckinStatus.FAILURE)
        return f"成功 {success}, 失败 {fail}, 重复 {repeat}"


# 初始化日志
logger = init_logger()


def main() -> int:
    """主函数, 返回进程退出码 (0 签到成功 / 1 签到失败 / 2 配置错误)。

    日志约定: 每个账号跑完打一行结果 (在上面的 checkin_all 里), 这里只收尾 ——
    失败时给出下一步, 最后无论成败都打一行带汇总与退出码的判定行。
    通知方式: 靠退出码让 GitHub Actions 变红, 由 GitHub 发失败邮件, 脚本不做推送。
    """
    exit_code = EXIT_OK
    summary = ""
    next_step = ""

    try:
        config = Config()

        if not config.cookies_list:
            logger.error(f"未找到有效的 Cookie, 请设置 {Config.ENV_COOKIES}")
            exit_code = EXIT_CONFIG_ERROR
        else:
            checker = Checker(config)
            checker.checkin_all()
            summary = checker.format_results()

            failed_indexes = checker.failed_cookie_indexes()
            if failed_indexes:
                exit_code = EXIT_CHECKIN_FAILED
                summary = f"{summary}, 失败账号 {', '.join(f'[{idx}]' for idx in failed_indexes)}"
                next_step = (
                    f"请检查 Cookie 是否完整/过期 ({DOMAIN} 需要 {' 与 '.join(COOKIE_KEYS)} 两个字段), "
                    f"或签到被判定为自动签到 (code 4, 需设置 {Config.ENV_USER_AGENT})"
                )

    except Exception as e:
        logger.error(f"执行过程中发生未预期的错误: {e}")
        exit_code = EXIT_CHECKIN_FAILED

    detail = f": {summary}" if summary else ""
    if exit_code == EXIT_OK:
        logger.info(f"签到完成{detail} (退出码 {exit_code})")
    else:
        logger.error(f"签到失败{detail} (退出码 {exit_code})")
    if next_step:
        logger.error(next_step)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
