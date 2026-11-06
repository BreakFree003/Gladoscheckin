# GLaDOS 自动签到

> 优惠码 **`PORTALGUN`**，购买套餐可享 **20% OFF**（八折）

个人自用：用 GitHub Actions 每天定时跑一次 `checkin.py`，给 GLaDOS（`glados.cloud`）
签到。**签到失败时让作业变红，靠 GitHub 的失败邮件收到通知。**

| 文件 | 作用 |
|---|---|
| `checkin.py` | 签到脚本（唯一入口） |
| `logging_config.py` | 日志配置 |
| `.github/workflows/gladosCheck.yml` | 定时任务 |
| `tests/test_checkin.py` | 签到脚本的测试 |
| `tests/test_workflow_gate.py` | workflow 里「今天是否已签到」那一步的测试 |

## 它每天是怎么跑的

**一天 4 个槽位，北京时间 00:07 / 06:23 / 12:41 / 18:13。**

之所以要 4 个，是因为 GitHub 的 `schedule` 是 best-effort：文档明确说高负载时会延迟
甚至丢弃，而且 2026-08 底起还有大范围漂移。本仓库实测：计划的时间点，实际会迟到 3~10
小时；同一份配置用「手动触发」则是秒级启动。所以用多个槽位互相兜底。

**一天只需要成功一次，第一个成功的槽位签到，后面的槽位直接跳过**，不会重复签到。
判据不是额外存的状态，而是 GitHub 自己的运行记录：北京时间「今天」本工作流有没有
成功运行过。脚本是 fail-closed 的（只有**真签到成功**才退出 0：`code 0` 且分数不低于
正常下限、`code 1` 重复签到、并且签到前的协议比对与接口响应的契约都过了），所以「今天有成功运行」
等价于「今天已经签到成功」。被服务端降级成 1 分、前端协议变了、或者接口改版停机的那天会变红，
后续槽位继续重试。

两个细节：

- **手动触发（Actions 页面点 Run workflow）不跳过**，方便排查问题时立刻跑一次。
- 查询运行记录失败时一律**回退为照常签到** —— 宁可多签一次，也不会静默漏签。

另外还有一个收尾步骤：`liskin/gh-workflow-keepalive` 防止 GitHub 因长期不活动自动停用
定时任务（它靠 API 重新启用工作流，所以 workflow 里要 `permissions: actions: write`）。

## 配置

在仓库 `Settings` → `Secrets and variables` → `Actions` 里添加：

| Secret | 必填 | 说明 |
|---|---|---|
| `GLADOS_COOKIES` | 是 | 账号的会话 Cookie，见下 |
| `GLADOS_USER_AGENT` | **强烈建议** | 登录浏览器的 `navigator.userAgent`，见下 |

### GLADOS_COOKIES

在 `glados.cloud` 的签到页面按 `F12` → `Network` → 刷新 → 点第一个请求 →
`Request Headers` 里的 `Cookie` → 右键复制完整值。

2026-09 起站点把会话拆成了两个字段，**两个都要有，缺一个就不能签到**：

| 字段 | 说明 |
|---|---|
| `gld:sess` | 会话 ID |
| `gld:sess.sig` | 会话签名（漏掉它是最常见的复制错误） |

直接复制完整的 `Cookie` 值即可，不用手工挑字段：脚本只认这两个，多余的（`theme`、
`_ga` 之类）会被忽略；但只抄了半对的话，加载阶段就会告警。

> 完整的 Cookie 里通常还带着 `koa:sess` / `koa:sess.sig` 等旧字段——那是站点自己
> 下发的（和 `gld:*` 同时出现），不是别的站点，脚本不检查、留着无所谓。但**只有**
> 这类旧字段而没有 `gld:*` 的话，加载阶段会告警。

只支持**一个账号**。以前用 `&` 把多个 Cookie 拼在一起的写法已经去掉了——整个值会
被当成一份 Cookie 发给站点，认不出来就是签到失败（作业变红，不会静默放过）。

### GLADOS_USER_AGENT

GLaDOS 从 2026-09 起会校验「签到请求的平台」与「登录时浏览器的平台」是否一致，
**对不上就返回 `code 4 Automated check-in detected`**，表现为一直签到失败，但积分接口照常返回，
很容易误判成 Cookie 坏了。

在**登录 GLaDOS 的那个浏览器**里按 `F12` → `Console`，执行 `navigator.userAgent`，
把输出原样粘成这个 secret 的值。

> 不配置时会用脚本内置的 macOS Chrome UA。同一次实测里 macOS UA 能签到，
> Windows / Linux / iPhone UA 一律被判为自动签到（只改 Chrome 版本号无效）。
> 所以**在 Windows 或 Linux 上登录的话，这一项必须配**。

### 兑换

积分够就自动兑换成天数，固定用 `plan500`（500 积分换 100 天），没有开关。

**积分没到门槛时不会发兑换请求**，也就不会有任何报错——每天拿一个明知道会被拒的
请求去换一句 `Not enough points` 没有意义，余额是本脚本自己刚查过的。所以结果行里
只会带一句门槛：

```
✅ 签到成功, 获得 13 积分, 总 497 积分, 未到 500 兑换门槛
```

积分够了并且真的兑换成功时，同一行的末尾变成 `兑换成功: plan500`
（它会真的扣掉 500 积分，所以这一行无条件输出，不会被任何开关关掉）。

> 之所以只有 `plan500`：服务端在积分不够时自己回过 `Need 500`，这是唯一被真实验证过
> 的门槛。站点说明里还有 `plan100` / `plan200`，但从没在这个脚本上验证过，所以连同
> `GLADOS_EXCHANGE_PLAN` 这个开关一起去掉了。

## 日志怎么看

就一份日志，没有级别开关。正常一次运行 4 行左右，每行都在说不同的事：

```
开始签到: 兑换 plan500 (需 500 积分)
User-Agent: 内置默认 (可用 GLADOS_USER_AGENT 覆盖为登录浏览器的 UA)
✅ 签到成功, 获得 13 积分, 总 497 积分, 未到 500 兑换门槛
签到完成 (退出码 0)
```

- 第 3 行是结果行：`✅ 签到成功` / `🔄 重复签到` / `⚠️ 签到只拿到 N 分（低于正常下限 2）`
  / `❌ 签到失败`。
- 最后一行是判定行，带退出码；失败时它是 `ERROR` 级别，作业变红。
- **成功的请求不会逐条记录**：那些内容结果行里已经有了。
- **失败一定有原因**：认证失败带服务端原话、`code 4` 带 `loginDevice` /
  `currentDevice` 对比、网络错误带异常、没见过的响应直接打原始 JSON ——
  这些都不需要额外开什么开关。
- 请求头从不进日志，Cookie 值不会出现在任何日志里（有测试守着）。

## 通知与退出码

脚本不做任何推送。通知链路只有一条：**签到失败 → 非 0 退出码 → 作业变红 →
GitHub 发失败邮件**。所以别关掉 Actions 的失败通知：

- 仓库页右上角 `Watch` → 至少勾上 `Actions`
- `Settings` → `Notifications` → `Actions` 段选 **Send notifications for failed workflows only**

| 退出码 | 含义 |
|---|---|
| `0` | 签到成功 / 今日已签到 |
| `1` | 签到失败 → 作业变红 |
| `2` | 配置错误，例如没设 `GLADOS_COOKIES` |

判定是 fail-closed 的：只有 `code 0`（签到成功，且这次拿到的分数不低于正常下限）和
`code 1`（重复签到）算通过；认证失败、反自动化拦截、网络失败、**被降级成低分**、
**接口形状变了**、**签到前的协议比对没过**、未预期异常**全部**返回 1。中途异常也一样算失败 ——
宁可多签一次，也绝不静默漏签。

「签到成功但只给 1 分」也在这条线上：站点不会为它返回错误，唯一的信号就是分数本身。
历史 51 条流水（2026-09-20 ~ 11-07）里正常下限是 7（Cable 质量差的那三天），常见
12~14，唯一的异常值就是降级那两天的 1 —— 门槛取 `MIN_EXPECTED_POINTS = 2`，
贴着 1，不会误伤单日 Cable 波动。**这种降级以前是「✅ 签到成功」的绿勾，现在是红灯。**

## 出问题了怎么看

去 Actions 里点开最近一次 `auto check` 运行（文件是 `.github/workflows/gladosCheck.yml`），
看 `Running checkin` 那一步的日志。

| 日志现象 | 原因 | 处理 |
|---|---|---|
| `Cookie 缺少会话字段 gld:sess.sig` | 只抄了半对，或复制时被截断 | 回签到页重新复制完整 Cookie |
| `认证失败 (code -2, message: 没有权限)` | Cookie 不完整或已过期（**服务端不会续期**，实测 14~30 天） | 按 [docs/cookie-refresh.md](docs/cookie-refresh.md) 刷新：从浏览器取 cookie → 只读验证 → 写 secret → 触发一次；紧跟着的那行会写清需要哪几个字段 |
| `签到被判定为自动签到` / `code 4` | 请求的平台和登录浏览器不一致 | 按上文配置 `GLADOS_USER_AGENT`；日志里会打印服务端给的 `reason` / `loginDevice` / `currentDevice` |
| `⚠️ 签到只拿到 1 分 (低于正常下限 2)` | 服务端认了签到（`code 0`），但按「未验证」降级记分 | 先看是否仍能取到 challenge、proof 是否按 `SHA-256(nonce + '.' + 盐)` 算出；若站点又加了新环节，抓一次网页端签到请求对照上文「观察挑战」 |
| `站点签到协议变了, 已暂停签到` | 签到页面/JS 里的协议与基线对不上，脚本没提交签到 | 看紧跟着的几行 ERROR，它逐条写了哪个字段从什么变成了什么；改 `site_protocol.EXPECTED` 与对应实现后重新打开工作流。详见上文「签到前的协议比对」 |
| `接口 … 的响应形状变了 (契约不符)` | 站点改了字段名或类型，脚本已停机 | 那一行带着完整原始响应，照它改 `CONTRACTS` 与解析逻辑，再打开工作流 |
| 作业变红，末尾 `签到失败 (退出码 1)` | 这次签到没成功 | 紧跟着的那行会写下一步；再按上面依次检查 Cookie 和 `GLADOS_USER_AGENT` |

日志时间戳是 **UTC**（runner 的时区），和 GitHub 日志每行自带的时间前缀一致。
北京时间 = UTC + 8。

## 本地跑

```shell
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt

# 单元测试 + 真实接口端到端测试
./.venv/bin/python -m pytest tests/ -q

# 用你自己的 Cookie 本地跑一次（不会打印 Cookie 值）
GLADOS_COOKIES='gld:sess=...; gld:sess.sig=...' \
GLADOS_USER_AGENT='粘贴你浏览器的 navigator.userAgent' \
./.venv/bin/python checkin.py; echo "exit=$?"
```

注意 `重复签到` 也是成功：站点回的是 `code 1`，表示今天这一次它已经记过了；
退出码仍然是 `0`，不会把作业染红。

## 为什么请求要长得像浏览器

被判定为自动签到（`code 4`）的根因是「请求不像本人浏览器点的」。这里不是猜的，是拿真机
抓包逐项对齐的 —— 期望值存在 [`tests/fixtures/browser_checkin_request.json`](tests/fixtures/browser_checkin_request.json)：
2026-11-06 用 CDP 记录本机 Chrome 156（macOS）在 `https://glados.cloud/console/checkin`
点「签到」时发出的那两次请求。

| 项目 | 浏览器 | 脚本 |
|---|---|---|
| 方法 / 路径 | `GET /api/user/checkin/challenge` → `POST /api/user/checkin` | 一致 |
| 请求体 | `{"token":"glados.cloud","nonce":…,"proof":…}`（142 字节，无空格） | 一致 |
| `content-type` | `application/json;charset=UTF-8` | 一致 |
| `accept` | `application/json, text/plain, */*` | 一致 |
| `origin` | `https://glados.cloud` | 一致 |
| `user-agent` | 登录时那个浏览器 | 一致（默认内置，可用 `GLADOS_USER_AGENT` 覆盖） |
| `referer` | **没有**（签到页是 `no-referrer`） | 不发 |
| `sec-ch-ua*` / `sec-fetch-*` / `accept-language` / `dnt` | 浏览器进程自动加 | 刻意不伪造 |

最后一行是故意的：伪造 `sec-ch-ua-platform` 这类值，一旦和用户真实浏览器对不上，反而制造出
「UA 与 client hints 打架」这种更像机器人的特征；实测缺了这些头服务端照样接受。

服务端判定依据是**登录设备平台**：账号页面的 `/api/user/sessions` 里 `device` 只会是
`macOS` / `Windows` / `Linux` / `Android` / `iOS` / `Bot` / `Other`，正是从 User-Agent
解析出来的。所以只要 `GLADOS_USER_AGENT` 是当初登录那个浏览器的 `navigator.userAgent`，
平台就对得上。

## 签到前的「观察挑战」

2026-11-05 起签到收益从 12~17 掉到 1 分，原因是站点加了一道 attestation。前端代码里叫
`checkinProof`，位置在 console 包的 `main~20e9f87d` 模块 613：

```js
salt = [103,108,97,100,111,115,45,111,98,115,101,114,118,101,45,118,49]  // "glados-observe-v1"
proof = hex(SHA-256(nonce + "." + salt))
```

流程（与 `main~189dec1b` 的 `loadChallenge` / `solveChallenge` 逐行对齐）：

1. `GET /api/user/checkin/challenge` 拿 `{nonce, ttl}`（`ttl` 实测 300000ms）；
2. **至少停 2.5 秒**才提交 —— 前端写死 `if (Date.now() - at < 2500) await sleep(2500 - …)`；
3. `POST /api/user/checkin`，体是 `{token, nonce, proof}`。

不带 proof 的 POST 一样返回 `code 0`，但服务端当成「未验证」只给 1 分，Cable / Streak
都不发。所以脚本现在是 **challenge 拿不到就判签到失败**（fail-closed），让当天下一个 cron
槽重试，而不是记一笔看着成功的 1 分。

算法本身没有秘密（nonce 由服务端发、盐是前端常量），这一步防的是「不知道要算」。

## 签到前的协议比对（改了就先停）

契约守卫是**事后**才报警的：响应回来了才发现形状不对，可那时签到已经提交、分已经按新
规则算完了。2026-11-05 那次就是这样 —— 站点把签到改成 `nonce` + `proof`，接口照样回
`code 0`，只是分数降成 1，连丢两天才被注意到。

所以真正的第一道关口在**提交签到之前**：脚本先抓一次签到页面
（`/console/checkin`）和它加载的 JS，从里面把签到协议抽出来，跟自己认识的基线比。
抽的是语义，不是文件名：

| 字段 | 含义 | 基线 |
|---|---|---|
| `endpoints` | 签到流程调用的接口 | `/user/checkin`、`/user/checkin/challenge` |
| `salt` | `proof` 的盐值 | `glados-observe-v1` |
| `digest` | `proof` 的摘要算法 | `SHA-256` |
| `post_fields` | 签到请求体里的字段 | `nonce`、`proof`、`token` |
| `min_delay_ms` | 拿到 nonce 后最少等多久才提交 | `2500` |
| `challenge_ttl_ms` | 挑战有效期 | `300000` |

对不上就在**发签到请求之前**停下：退出码 1，一行一条说明哪个字段从什么变成了什么，
例如

```
站点签到协议变了, 已暂停签到 (没有提交, 今天的分数未动):
  - 签到调用的接口 变了: 原来是 ['/user/checkin', '/user/checkin/challenge'], 现在是 ['/user/checkin']
```

看清改了什么、决定要不要适配，再改 `site_protocol.EXPECTED` 与对应实现，然后重新打开
工作流。**这天不签到，只是丢掉一次机会；猜着签，丢的是分。**

为什么不会因为发版误报：2026-11-06 与 11-07 两份真实构建，32 个 chunk 的文件名
全不同（`main~189dec1b.806cceb9.chunk.js` → `main~21b3c6c3.91594a3a.chunk.js`）、
webpack 模块编号也从 613 变成 614，但上面六个字段抽出来**逐字相同**。两份构建的源码
片段就存在 `tests/fixtures/` 里，测试直接拿它们当期望值。

四个刻意的取舍：

- **不比对 bundle 的文件名或 hash。** 每次发版必变，而协议通常一个字节都没动。
- **多出来的接口或字段不算变化。** 站点加功能是常态；判据是「脚本依赖的每一项都还在」。
- **抓不到页面就放行。** 「这次没抓到」不等于「协议变了」，为一次网络抖动拦下签到，
  等于让抖动吃掉一天的分。但页面 200 却一个 JS 都解析不出来 —— 那是页面结构变了，算变化。
- **抓 JS 用 8 线程。** 32 个 chunk 串行要 20 秒，8 线程 2~3 秒（2026-11-07 实测）。

## 接口变了就停机（契约守卫）

站点每次改接口，脚本有两种结局：要么报错，要么**安静地算出错的数**。后者才可怕 ——
`data.get("points", 0)` 在字段改名后照样返回 0，「签到成功 0 分」还会退出 0。

所以脚本把真正依赖的字段写成了契约（`checkin.py` 里的 `CONTRACTS`）：

| 端点 | code | 读取的字段 | 期望类型 | 依据 |
|---|---|---|---|---|
| `GET /api/user/checkin/challenge` | `0` | `nonce` | 字符串 | 实测 `"nonce":"0b1e882c…"` |
| `POST /api/user/checkin` | `0` | `points` | 整数 | 2026-11-07 实跑日志「获得 10 积分」 |
| `GET /api/user/points` | `0` | `points` | 字符串 | 实测 `"450.0000000000000000"` |

对不上就**停机**：退出码 1，并在 ERROR 里带上**完整原始响应**，例如

```
接口 points 的响应形状变了 (契约不符): 缺少字段 points。原始响应: {"code": 0, "balance": "450.0000000000000000"}
```

那一行就是证据本身 —— 不需要再复现一次就知道它改成了什么。照它改 `CONTRACTS` 和解析逻辑，
再打开工作流即可（`.github/workflows/gladosCheck.yml` 不用改，它只认退出码）。

这是第二道关口：它守的是**响应**的形状，报警时签到已经提交了。真正「改了就先停」的那道
在上面「签到前的协议比对」——它读的是前端实现，能在提交之前拦住。

两个刻意的取舍：

- **服务端加字段不算违约。** 契约只守脚本真正读取的字段。
- **重复签到（`code 1`）不设契约。** 那一支一个字段都不读，分数直接记 0；给它加约束
  只是替站点加要求。

## 说明

本项目基于 [Devilstore/Glados-Railgun-checkin](https://github.com/Devilstore/Glados-Railgun-checkin)
修改，遵循仓库内的 [LICENSE](LICENSE)。
