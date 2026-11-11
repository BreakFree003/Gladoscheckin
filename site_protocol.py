"""从签到页面加载的 JS 里抽出「签到协议」，并与基线比对。

为什么要有这个模块: 2026-11-05 站点给签到加了 nonce/proof 挑战，可接口照样回
code 0，只是把分数降级成 1 分。脚本从响应里看不出「协议变了」，只能等分数异常
才发现 —— 那时分已经丢了，而且丢了两天才被注意到。协议本身是公开的: 签到页面
加载的 bundle 里就有完整实现，所以在**提交签到之前**先比一次，变了就停下来。

为什么指纹稳定，不会因为发版就误报: 2026-11-06 与 11-07 两次构建（32 个 chunk
全量，所有构建 hash 都不同，模块编号还从 613 变成了 614）抽出的六个字段逐字相同。
webpack 只改文件名与模块号，不改逻辑与字面量。所以这套指纹只在协议真的变时变。
2026-11-12 的反例也印证了这一点: 协议真的变了(请求体多了 tsState), 指纹当场报了出
来, 没有等到分数掉下来才发现。

抽取规则都锚定在语义上，不依赖压缩后的变量名:
- 接口清单: 直接匹配 "/user/checkin" 与 "/user/checkin/xxx" 这样的路径字面量；
- 盐值: 17 字节的 char code 数组（前端用它绕开明文常量）；
- 摘要算法: crypto.subtle.digest 的第一个参数；
- 请求体字段: `.post("/user/checkin", …)` 的实参里的字面量 key，加上
  solveChallenge 通过 `abrupt("return", X)` 交出去的那个对象里的 key;
- 最短停顿: `Date.now() - x.at) < N` 里的 N（`.at` 是对象属性名，不会被混淆）；
- 挑战有效期: `.ttl || N` 里的 N。

请求体字段这一项最容易随前端重构换写法, 所以两条路都认: 2026-11-07 之前 solveChallenge
直接交出对象字面量, 2026-11-12 起改成先拼到变量再交
(`r={nonce:e.t0,proof:e.t1,tsState:e.t2}; ... abrupt("return",r)`)。变量名会被压缩改掉,
不能写死成 `r`, 只能顺着 return 的那个标识符回找它最近一次的 `X={...}` 赋值。同一版里还有
`a.token&&(r.turnstile=a.token)` 这种按条件补字段的写法: turnstile 是可选的, 服务端没
下发验证码密钥时整个字段都不出现, 脚本也不发它, 所以不写进基线。
"""

import re
from typing import Dict, List, Mapping

# 基线: 脚本实现所依据的协议。改这里的每一条都必须同时改 checkin.py。
EXPECTED: Dict[str, List] = {
    "endpoints": ["/user/checkin", "/user/checkin/challenge"],
    "salt": ["glados-observe-v1"],
    "digest": ["SHA-256"],
    "post_fields": ["nonce", "proof", "token", "tsState"],
    "min_delay_ms": [2500],
    "challenge_ttl_ms": [300000],
}

# 每个字段在日志里叫什么。报错要能直接读懂「它改了什么」。
LABELS: Dict[str, str] = {
    "endpoints": "签到调用的接口",
    "salt": "proof 盐值",
    "digest": "proof 摘要算法",
    "post_fields": "签到请求体字段",
    "min_delay_ms": "提交前最短停顿(毫秒)",
    "challenge_ttl_ms": "挑战有效期(毫秒)",
}

_PAIRS = {"(": ")", "{": "}", "[": "]"}
_SCRIPT_SRC = re.compile(r'src="([^"]+\.js)"')
_SALT_ARRAY = re.compile(r"\[((?:\d{1,3},){16}\d{1,3})\]")
_ENDPOINT = re.compile(r'"(/user/checkin(?:/[a-z-]+)?)"')
_DIGEST = re.compile(r'subtle\.digest\("([A-Za-z0-9-]+)"')
_MIN_DELAY = re.compile(r"Date\.now\(\)-[A-Za-z_$][\w$]*\.at\)<(\d+)\)")
_TTL = re.compile(r"\.ttl\|\|(\d+(?:e\d+)?)\)")
_POST_CALL = re.compile(r'\.post\("/user/checkin"')
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_RETURN_CALL = re.compile(r'abrupt\("return"\s*,\s*')
_OBJECT_KEY = re.compile(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*:")


def parse_script_srcs(html: str) -> List[str]:
    """取页面引用的 JS 路径，按出现顺序去重。

    站点是单页应用，签到逻辑散在多个 chunk 里（协议本身跨了两个），所以不能只抓
    「看起来像签到页」的那一个。构建 hash 每次发版都变，但相对路径不变。
    """
    seen: Dict[str, None] = {}
    for src in _SCRIPT_SRC.findall(html):
        seen.setdefault(src, None)
    return list(seen)


def _balanced(text: str, start: int) -> str:
    """text[start] 是开括号，返回与之配对的闭括号之间的文本。"""
    opener = text[start]
    closer = _PAIRS[opener]
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return ""


def _first_opener(text: str, start: int) -> int:
    """从 start 起找第一个开括号的位置，找不到返回 -1。"""
    for i in range(start, len(text)):
        if text[i] in _PAIRS:
            return i
    return -1


def _object_keys(fragment: str) -> set:
    """取一个对象字面量里出现的 key（含首字符就是 key 的情况）。"""
    return set(_OBJECT_KEY.findall(fragment))


def _method_body(blob: str, name: str) -> str:
    """取压缩后 `key:"<name>"` 到下一个 `key:"…"` 之间的方法体。

    用 `key:"…"` 做锚是因为它是对象字面量的属性名，webpack 不会混淆它；模块编号
    和变量名都会变（613→614、w→其他），不能拿来做锚。
    """
    start = blob.find(f'key:"{name}"')
    if start < 0:
        return ""
    end = blob.find('key:"', start + 1)
    return blob[start : end if end > 0 else len(blob)]


def _salts(blob: str) -> List[str]:
    """把 char code 数组还原成字符串。非 UTF-8 的数组（别的常量的 byte）跳过。"""
    found = set()
    for array in _SALT_ARRAY.findall(blob):
        try:
            found.add(bytes(int(code) for code in array.split(",")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    return sorted(found)


def _returned_keys(body: str) -> set:
    """solveChallenge 通过 `abrupt("return", X)` 交出去的对象里的 key。

    两种写法都要认, 站点在 2026-11-12 从第一种换成了第二种:
    - 直接给字面量: `abrupt("return", {nonce: ..., proof: ...})`
    - 先拼到变量再交: `r={nonce: ..., proof: ..., tsState: ...}` 然后
      `abrupt("return", r)`

    第二种的变量名会被压缩改掉, 所以不能写死成 `r`: 顺着 return 的那个标识符,
    回找它最近一次的 `X={...}` 赋值。找到的是「交出去的那一份」, 而不是包里任意
    一个同名变量。
    """
    keys: set = set()
    for call in _RETURN_CALL.finditer(body):
        cursor = call.end()
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor < len(body) and body[cursor] == "{":
            keys |= _object_keys(_balanced(body, cursor))
            continue
        identifier = _IDENT.match(body, cursor)
        if not identifier:
            continue
        assignment = None
        pattern = re.compile(r"\b" + re.escape(identifier.group()) + r"\s*=\s*\{")
        for found in pattern.finditer(body, 0, cursor):
            assignment = found
        if assignment:
            keys |= _object_keys(_balanced(body, assignment.end() - 1))
    return keys


def _post_fields(blob: str) -> List[str]:
    """签到请求体的字段名: POST 实参里的字面量 key + solveChallenge 交出去的对象的 key。"""
    fields = set()
    for match in _POST_CALL.finditer(blob):
        opener = _first_opener(blob, match.end())
        if opener > 0:
            fields |= _object_keys(_balanced(blob, opener))

    # 只在这一个方法体里找: 整个包里 abrupt("return", ...) 有八处, 全抓会把别的
    # 接口的字段混进来。
    fields |= _returned_keys(_method_body(blob, "solveChallenge"))
    return sorted(fields)


def fingerprint(sources: Mapping[str, str]) -> Dict[str, List]:
    """把页面引用的全部 JS 合成一份文本，抽出协议指纹。

    传的是「文件名 -> 源码」而不是单份文本，是因为协议跨 chunk: 签到调用在页面级
    chunk 里，proof 的盐值与算法在另一个 chunk 里。少抓一个就抽不全。

    一个字段都没抽到时，各字段是空列表 —— 调用方据此区分「协议不见了」和「协议变
    了」，两者的处置不同。
    """
    blob = "\n".join(sources.values())

    return {
        "endpoints": sorted(set(_ENDPOINT.findall(blob))),
        "salt": _salts(blob),
        "digest": sorted(set(_DIGEST.findall(blob))),
        "post_fields": _post_fields(blob),
        "min_delay_ms": sorted(
            {int(value) for value in _MIN_DELAY.findall(blob)}
        ),
        "challenge_ttl_ms": sorted(
            {int(float(value)) for value in _TTL.findall(blob)}
        ),
    }


def drift(expected: Mapping[str, List], actual: Mapping[str, List]) -> List[str]:
    """逐字段比对，返回人话形式的差异说明（空列表表示协议没变）。

    只报「脚本依赖的字段」是否还是原样。多出来的接口或字段不算变化 —— 站点加功能
    是常态，每次发版都报红只会训练人忽略红灯。所以判据是「脚本依赖的每一项都还在」，
    而不是「列表逐字相等」。
    """
    problems: List[str] = []
    for field, expected_value in expected.items():
        actual_value = list(actual.get(field, []))
        missing = [value for value in expected_value if value not in actual_value]
        if missing:
            problems.append(
                f"{LABELS.get(field, field)} 变了: "
                f"原来是 {expected_value}, 现在是 {list(actual_value)}"
            )
    return problems
