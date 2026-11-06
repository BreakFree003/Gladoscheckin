import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import requests

import site_protocol
from logging_config import init_logger


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2
    # 服务端回 code 0、但给的分数低于正常下限。站点自己的 code 里没有这一档
    # (它只回 0/1/-2/4), 2 不会与任何一个撞上, 所以借它表示「成功了但不对劲」。
    DEGRADED = 2


class LogEmoji:
    """日志里的状态标记。

    只挂在"结果"上, 不给每一行都配一个 —— 行首的级别 (INFO/WARNING/ERROR) 已经
    说明了性质, 再补一个 ℹ️ 只是噪音, 而且各行的 emoji 间距还不一致。
    """

    SUCCESS = "✅"
    REPEAT = "🔄"
    DEGRADED = "⚠️"
    FAIL = "❌"


"""唯一的站点。

这个脚本只服务 glados.cloud 一个站点: 会话字段是 gld:sess 与 gld:sess.sig
两个, 缺任何一个都不能签到 (2026-09-26 起站点把会话拆成了这两个字段)。"""
DOMAIN = "glados.cloud"
COOKIE_KEYS: Tuple[str, ...] = ("gld:sess", "gld:sess.sig")

"""认证失败时服务端可能返回的关键字。

中文是 glados.cloud 实测文案; 英文没有实测来源 (上游两站点时代留下的兜底)。
保留它是为了不在"服务端换文案"这件事上做假设 —— 认不出来时权限错误会被归到
"没见过响应"那一类, 虽然照样报红, 但可读性差一截。"""
PERMISSION_ERROR_HINTS: Tuple[str, ...] = ("没有权限", "no permission")

"""GLaDOS 判定「自动签到」时返回的 code 与关键字。

2026-09 实测: 同一份 Cookie, User-Agent 平台对不上登录浏览器时,
/api/user/checkin 返回 code 4「Automated check-in detected」, 而积分接口照常返回,
很容易被误判成 Cookie 失效。"""
AUTOMATION_ERROR_CODE = 4
AUTOMATION_ERROR_HINTS: Tuple[str, ...] = ("automated check-in detected",)

"""签到前的「观察挑战」(2026-11 起站点新增的门槛)。

前端在点签到后会先 GET /api/user/checkin/challenge 拿一个一次性 nonce, 停顿至少
2.5 秒, 再用 SHA-256 把 nonce 与固定盐值算成 proof 一起 POST 上去。没带 proof 的
签到不再报错, 而是被当成「未验证」降级发放 (2026-11-05/06 连续两天实测只有 1 分,
此前稳定 12~17 分), 所以这个挑战不做, 签到就没有意义。

盐值是前端常量里的一串 char code:
  [103,108,97,100,111,115,45,111,98,115,101,114,118,101,45,118,49]
  → "glados-observe-v1"
proof 算法: hex(SHA-256(nonce + "." + salt)), 小写十六进制 64 字符。"""
CHALLENGE_SALT = "glados-observe-v1"

"""拿到 nonce 后必须停顿的最短时间。

前端 solveChallenge 里写死 `if ((now - at) < 2500) await sleep(2500 - (now - at))`,
服务端按这个间隔区分「真人点了一下」和「脚本秒交」。停顿不够不会被拒, 只会白白
浪费一次 nonce, 所以这里跟着前端一起停。"""
CHALLENGE_MIN_DELAY_SECONDS = 2.5

"""一次正常签到至少该拿到多少分。低于它, 这次签到算失败。

为什么要这个数: 站点把「没做挑战的签到」降级成 1 分时, 响应仍然是 code 0 —— 光看
code 会把降级当成成功, 白丢一天的分还以为一切正常 (2026-11-05/06 就是这样过去的)。
站点没有给出任何语义字段说明「这是降级分」, 只能按数值判断。

取 2 的依据是 51 条历史流水 (2026-09-20 ~ 2026-11-07): 正常值下限是 7 (Cable 质量
差的 09-20 / 09-25 / 10-28 三天), 常见 12~14, 唯一的异常值是降级那两天的 1。阈值
贴着 1 取 2, 差一天的 Cable 分也不会被误判。"""
MIN_EXPECTED_POINTS = 2

"""接口响应的字段契约: (端点, code) -> {脚本读取的字段: 期望类型}。

为什么要有: 站点把字段改名或换类型时, `data.get("points", 0)` 会安静地返回默认值,
于是「签到成功 0 分」照样退出 0。这类静默失败比报错难查得多, 所以宁可停机。

只声明脚本真正读取的字段, 并且类型都取自实测响应:
- challenge 成功: {"code":0,"nonce":"…","ttl":300000} → nonce 是字符串
- checkin code 0: 本次获得的分数是**整数** (2026-11-07 实跑 log 打出「获得 10 积分」,
  旧代码是 str(data["points"]) 直出, 所以服务端给的就是 10 而不是 "10.000…")
- points 成功: points 是 "450.0000000000000000" 这样的**字符串**

重复签到 (code 1) 不列契约: 那两个分支一个字段都不读 (分数直接记 0), 要求它带
points 只是替站点加约束 —— 契约只该守住脚本真正依赖的东西。

服务端新增字段不算违约 —— 那不影响签到, 而每次发版都报一次警只会训练人忽略红色
通知。没在这里声明的 code 也不校验, 交给各自的失败分支去解释。"""
CONTRACTS: Dict[Tuple[str, int], Dict[str, type]] = {
    ("checkin/challenge", 0): {"nonce": str},
    ("checkin", 0): {"points": int},
    ("points", 0): {"points": str},
}

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


def _type_name(expected: type) -> str:
    """把契约里的类型写成人看得懂的名字。"""
    return {str: "字符串", int: "整数", list: "数组", dict: "对象"}.get(expected, expected.__name__)


def _describe_type(value) -> str:
    """描述实际值的类型, int 要排除 bool —— JSON 里 true 也是 int 的实例。"""
    if isinstance(value, bool):
        return "布尔值"
    return _type_name(type(value))


def contract_violations(endpoint: str, code: int, data: Dict) -> List[str]:
    """校验一个响应是否满足 CONTRACTS 里声明的字段契约, 返回违约说明 (空表示合格)。

    只查「脚本要用的字段在不在、类型对不对」。服务端加字段不算违约。
    """
    expected = CONTRACTS.get((endpoint, code))
    if expected is None:
        return []

    problems: List[str] = []
    for field, field_type in expected.items():
        if field not in data:
            problems.append(f"缺少字段 {field}")
            continue
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, field_type):
            problems.append(
                f"字段 {field} 应为{_type_name(field_type)}, 实际是{_describe_type(value)}"
            )
    return problems


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
                "checkin": {"status": "签到失败", "points": "0", "reason": "request"},
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                return DEFAULT_ERRORS[method_name]
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
    CHALLENGE_URL = "/api/user/checkin/challenge"
    PAGE_PATH = "/console/checkin"

    """抓 JS 的并发数: 32 个 chunk 串行要 20 秒, 8 线程 2~3 秒 (2026-11-07 实测)。"""
    SCRIPT_WORKERS = 8

    """POST 的 content-type, 与站点前端 axios 发出的一致 (带 charset, 无空格)。"""
    CONTENT_TYPE_JSON = "application/json;charset=UTF-8"

    def __init__(self, user_agent: str = Config.DEFAULT_USER_AGENT):
        self.user_agent: str = user_agent
        self.headers: Dict[str, str] = self._get_headers()
        self._auth_error_reported: bool = False
        self._automation_error_reported: bool = False
        # 挑战失败的具体原因 (challenge/request/contract/auth/unknown), 给上层选提示用
        self._challenge_failure: str = "challenge"
        # 查总积分失败的原因 (request/contract/unavailable); 空串表示没失败
        self._points_failure: str = ""
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def close(self) -> None:
        """关闭 session。只有 with 语句会调用它, 那时 __init__ 必然已经跑完。"""
        try:
            self.session.close()
        except Exception as e:
            logger.error(f"关闭 session 时发生错误: {e}")

    @property
    def points_failure(self) -> str:
        """上一次 get_points 的失败原因 (空串表示成功)。"""
        return self._points_failure

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
        """统一的 API 层日志, 只有 warning / error 两种调用。

        只输出有信息量的事件 (失败); 成功的请求不逐条回显 —— 跑完会有一行结果。
        """
        if level == "warning":
            logger.warning(message)
        else:
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

    def _report_contract_violation(self, endpoint: str, response: requests.Response, problems: List[str]) -> None:
        """接口形状对不上时, 把「期望什么、实际收到什么」一次说清。

        关键在于带上**完整原始响应**: 只报「字段 points 类型不对」还是得自己去复现
        一次才知道它改成了什么; 而把 raw body 逐字打出来, 日志本身就是证据 ——
        停机之后直接看这一行就能决定要不要适配。"""
        self._log(
            "error",
            f"接口 {endpoint} 的响应形状变了 (契约不符): {'; '.join(problems)}。"
            f"原始响应: {response.text}",
        )

    def _serialize_post_body(self, data: Optional[Dict]) -> bytes:
        """按 axios 的方式序列化 JSON 请求体。

        axios 用 `JSON.stringify` 的紧凑格式 (`{"token":"glados.cloud","nonce":…}`), 而
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

    def _protocol_drift(self) -> List[str]:
        """签到**之前**先比一次前端协议, 返回差异说明 (空列表表示一成不变)。

        为什么要提前: 2026-11-05 站点给签到加了 nonce/proof, 接口照样回 code 0, 只是
        把分数降成 1 分。等到响应回来才发现, 分已经丢了 —— 而且是丢了两天才被注意到。
        协议本身是公开的, 签到页面加载的 bundle 里就有完整实现, 所以在提交签到**之前**
        先抓一次页面与 JS 比对, 变了就停下。

        为什么不会因为发版误报: 两份相隔一天、32 个 chunk 的构建 hash 全不同、模块编号
        也从 613 变成 614, 但抽出的六个字段逐字相同 (见 site_protocol 的模块说明)。

        取不到页面时只警告并放行 —— 「这次没抓到」不等于「协议变了」, 为此拦下签到
        等于让一次网络抖动吃掉一天的分。但页面 200 却抽不出协议, 算变了。
        """
        page_url = self._get_full_url(self.PAGE_PATH)
        response = self._make_request(page_url, "GET")
        if not response:
            self._log("warning", "没取到签到页面, 跳过协议比对 (这不代表协议变了)")
            return []

        srcs = site_protocol.parse_script_srcs(response.text)
        if not srcs:
            self._log("error", "签到页面里一个 JS 都没解析到, 页面结构可能变了")
            return ["签到页面里没有解析到任何 JS 脚本"]

        sources = self._fetch_scripts(srcs)
        if not sources:
            self._log("warning", "签到页面的 JS 一个都没抓到, 跳过协议比对")
            return []

        return site_protocol.drift(site_protocol.EXPECTED, site_protocol.fingerprint(sources))

    def _fetch_scripts(self, srcs: List[str]) -> Dict[str, str]:
        """并发抓 JS 源码, 返回「路径 -> 源码」。抓不到的直接跳过。

        单个 chunk 抓失败不算变化: 协议跨两个 chunk, 缺一个只会让对应字段抽空, 而
        抽空本身会被 drift 报成「变了」—— 那是一条假警报。所以这里把抓不到的路径数
        出来, 由调用方在日志里看见。
        """
        def fetch(src: str) -> Tuple[str, Optional[str]]:
            response = self._make_request(self._get_full_url(src), "GET")
            return src, (response.text if response else None)

        with ThreadPoolExecutor(max_workers=self.SCRIPT_WORKERS) as pool:
            results = list(pool.map(fetch, srcs))

        sources = {src: text for src, text in results if text is not None}
        if len(sources) != len(srcs):
            self._log("warning", f"有 {len(srcs) - len(sources)} 个 JS 没抓到, 协议比对可能不完整")
        return sources

    def _fetch_challenge(self, cookies: str) -> Optional[Dict]:
        """取一次「观察挑战」的 nonce。拿不到就返回 None, 由调用方决定拦下签到。

        站点对未带 Cookie 的挑战请求直接回 code -2, 所以这里复用 _report_auth_error,
        Cookie 失效时只输出一行可操作的提示, 不会因为多调一个接口而刷屏。

        失败原因写进 self._challenge_failure, 让上层能给出对症的下一步 ——
        「Cookie 过期」和「挑战接口改版」需要完全不同的动作。"""
        self._challenge_failure = "challenge"
        url = self._get_full_url(self.CHALLENGE_URL)
        response = self._make_request(url, "GET", cookies=cookies)
        if not response:
            self._challenge_failure = "request"
            return None

        data = response.json()
        code = data.get("code", -2)

        problems = contract_violations("checkin/challenge", code, data)
        if problems:
            self._report_contract_violation("checkin/challenge", response, problems)
            self._challenge_failure = "contract"
            return None

        if code == CheckinStatus.SUCCESS.value and data.get("nonce"):
            return data

        message = data.get("message", "无消息字段")
        if is_permission_error(code, message):
            self._report_auth_error("checkin/challenge", message)
            self._challenge_failure = "auth"
        else:
            self._log("error", f"取签到挑战失败: {response.text}")
            self._challenge_failure = "unknown"
        return None

    def _compute_proof(self, nonce: str) -> str:
        """按前端的算法把 nonce 算成 proof: hex(SHA-256(nonce + "." + salt))。"""
        return hashlib.sha256(f"{nonce}.{CHALLENGE_SALT}".encode("utf-8")).hexdigest()

    def _wait_out_challenge(self) -> None:
        """拿到 nonce 后停顿到前端要求的最短间隔, 再提交。"""
        time.sleep(CHALLENGE_MIN_DELAY_SECONDS)

    def _get_checkin_data(self, challenge: Dict) -> Dict[str, str]:
        """拼签到请求体。

        2026-11-06 抓包得到的字段顺序是 token → nonce → proof; 前端用
        `Object.assign({token: hostname}, {nonce, proof})`, 所以 token 在最前。
        顺序不影响服务端, 但字节对齐才能让抓包对照有意义。"""
        nonce = challenge["nonce"]
        return {"token": DOMAIN, "nonce": nonce, "proof": self._compute_proof(nonce)}

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到: 先取挑战 → 停顿 → 带上 nonce + proof 提交。

        不取挑战直接 POST 也能「成功」, 但服务端会把它当成未验证的签到只给 1 分
        (2026-11-05/06 连续两天实测), 且 code 仍是 0, 工作流那边看不出异常。所以挑战
        失败时宁可判成签到失败、让当天的下一个 cron 槽重试, 也不要记一笔假成功。

        同理, code 0 但分数低于 MIN_EXPECTED_POINTS 也判失败 (DEGRADED): 分数是脚本
        唯一能看见的「这次签到到底算不算数」的证据。

        第一步是协议比对, 它发生在提交签到**之前** —— 站点改了签到规则时, 抢在丢分
        之前停下, 而不是签完再从分数上倒推。"""
        url = self._get_full_url(self.CHECKIN_URL)

        result = {
            "status": "签到失败",
            "points": "0",
            "code": CheckinStatus.FAILURE,
            "reason": "",
        }

        drift = self._protocol_drift()
        if drift:
            self._log("error", "站点签到协议变了, 已暂停签到 (没有提交, 今天的分数未动):")
            for problem in drift:
                self._log("error", f"  - {problem}")
            result["status"] = "站点签到协议变了, 已暂停签到"
            result["reason"] = "protocol"
            return result

        challenge = self._fetch_challenge(cookies)
        if not challenge:
            # 具体原因已经在 _fetch_challenge 里输出过了
            self._log("error", "未取到签到挑战, 本次签到作废 (不带 proof 的签到只会拿到 1 分)")
            result["reason"] = self._challenge_failure
            return result

        self._wait_out_challenge()
        checkin_data = self._get_checkin_data(challenge)
        response = self._make_request(url, "POST", checkin_data, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "无消息字段")

            # 形状对不上就先停下: 后面的 code/points 读取全部建立在它成立之上。
            problems = contract_violations("checkin", code, data)
            if problems:
                self._report_contract_violation("checkin", response, problems)
                result["reason"] = "contract"
                return result

            if code == CheckinStatus.SUCCESS.value:
                points = data["points"]
                result["points"] = str(points)
                if points < MIN_EXPECTED_POINTS:
                    result["code"] = CheckinStatus.DEGRADED
                    result["status"] = f"签到只拿到 {points} 分 (低于正常下限 {MIN_EXPECTED_POINTS})"
                    result["reason"] = "degraded"
                else:
                    result["code"] = CheckinStatus.SUCCESS
                    result["status"] = "签到成功"
            elif code == CheckinStatus.REPEAT.value:
                result["code"] = CheckinStatus.REPEAT
                result["status"] = "重复签到"
                result["points"] = "0"
            else:
                # 已知原因 (权限/反自动化) 各自有专门的一行解释, 只有其他 code
                # 才需要在这里留下原始 code 与 message。
                if is_permission_error(code, message):
                    self._report_auth_error("checkin", message)
                    result["reason"] = "auth"
                elif is_automation_blocked(code, message):
                    self._report_automation_block(data)
                    result["reason"] = "automation"
                else:
                    # 已知原因各有专门的一行解释; 剩下的只有原始响应能说明问题
                    # (服务端加字段时, 这里就能看见)。
                    self._log("error", f"签到失败: {response.text}")
                    result["reason"] = "unknown"
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
        else:
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["reason"] = "request"

        return result

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int]:
        """获取总积分。第二个返回值是给兑换门槛用的数字。"""
        self._points_failure = ""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if not response:
            self._points_failure = "request"
            return "None 积分", 0

        data = response.json()
        code = data.get("code", -2)
        message = data.get("message", "")

        # points 在实测里是 "450.0000000000000000" 这样的字符串; 变成数字就说明
        # 契约变了, 而 int(float(...)) 在别的类型上会静默算出别的数。
        problems = contract_violations("points", code, data)
        if problems:
            self._report_contract_violation("points", response, problems)
            self._points_failure = "contract"
            return "None 积分", 0

        points = data.get("points", None)

        if points is not None:
            points_int = int(float(points))
            return f"{points_int} 积分", points_int

        if is_permission_error(code, message):
            self._report_auth_error("points", message)
        else:
            self._log("warning", f"读取总积分失败: {response.text}")
        self._points_failure = "unavailable"
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
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, 2: 降级, -2: 失败
    reason: str = ""  # 失败/降级的原因, 决定 main() 给出哪一条下一步提示

    @property
    def passed(self) -> bool:
        """只有签到成功与重复签到算通过, 降级与失败一律算失败 (fail-closed)。

        降级 (code 0 但分数低于正常下限) 必须算失败: 站点不会为它返回错误, 唯一的
        信号就是分数本身, 放过它就等于每天白丢分还以为一切正常。"""
        return self.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT)

    def describe(self) -> str:
        """把结果拼成一行: 状态 + 本次获得 + 总积分 + 兑换情况。"""
        emoji = {
            CheckinStatus.SUCCESS: LogEmoji.SUCCESS,
            CheckinStatus.REPEAT: LogEmoji.REPEAT,
            CheckinStatus.DEGRADED: LogEmoji.DEGRADED,
            CheckinStatus.FAILURE: LogEmoji.FAIL,
        }[self.code]

        line = f"{emoji} {self.status}"
        if self.code is CheckinStatus.FAILURE:
            return line

        # 重复签到时没有"获得"可言, 硬写一句"获得 0 积分"看着像 bug; 降级的分数
        # 已经写在状态里了, 再补一次就成了"只拿到 1 分, 获得 1 积分"。
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
        result.reason = checkin_result.get("reason", "")

        # 协议对不上时签到根本没提交, 后面的余额查询与兑换都不该继续: 用户要的是
        # 「有变化就先停下」, 停下来的意思就是不碰账户。
        if result.reason == "protocol":
            return result

        # 2. 总积分
        points_str, points_num = api.get_points(config.cookie)
        result.points_total = points_str

        # 查总积分时撞上契约变更, 说明站点接口已经动过。这时候即便签到那一步看着
        # 成功, 也不能算通过 —— 用户要的是「有变化就停下、先报错」, 而不是带着一个
        # 自己都没法确认的状态继续跑到兑换。签到本身的结果仍留在 status 里。
        if api.points_failure == "contract":
            result.code = CheckinStatus.FAILURE
            result.reason = "contract"
            result.status = f"签到结果无法确认 (积分接口形状不符): {result.status}"
            return result

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


"""按失败原因给出下一步。每一种都指向一个具体动作, 不含「请重试」这种废话。"""
NEXT_STEPS: Dict[str, str] = {
    "protocol": (
        "前端签到协议变了, 脚本在提交签到之前就停了 (今天的分没动)。"
        "上面几行 ERROR 逐条写明哪个字段从什么变成了什么; "
        "改 site_protocol.EXPECTED 与 checkin.py 的对应实现, 并抓一次网页端签到请求复核"
    ),
    "contract": (
        "站点接口的响应形状变了, 脚本已停机 (没有继续猜着签)。"
        "上一条 ERROR 里有完整原始响应, 照着它改 CONTRACTS 与解析逻辑再打开工作流"
    ),
    "degraded": (
        "签到被服务端降级记分 (code 0 但分数低于正常下限)。"
        "先看是否仍能取到 challenge、proof 是否按 SHA-256(nonce + '.' + 盐) 算出; "
        "若站点又加了新环节, 抓一次网页端签到请求对照"
    ),
    "challenge": (
        "没取到「观察挑战」的 nonce, 所以没提交签到。"
        "若 Cookie 未过期, 多半是 /api/user/checkin/challenge 变了"
    ),
    "auth": (
        f"请检查 Cookie 是否完整/过期 ({DOMAIN} 需要 {' 与 '.join(COOKIE_KEYS)} 两个字段)"
    ),
    "automation": (
        f"签到被判定为自动签到 (code 4), 需把登录浏览器的 navigator.userAgent 设为 "
        f"{Config.ENV_USER_AGENT}"
    ),
    "request": "签到请求没有成功发出或返回非 2xx, 先看上面那行原始响应",
    "unknown": "签到返回了没见过的 code, 先看上一条 ERROR 里的原始响应",
}


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
                next_step = NEXT_STEPS.get(result.reason) or (
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
