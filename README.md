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
成功运行过。脚本是 fail-closed 的（只有签到成功 `code 0` / 重复签到 `code 1` 才退出 0），
所以「今天有成功运行」等价于「今天已经签到成功」。

两个细节：

- **手动触发（Actions 页面点 Run workflow）不跳过**，方便排查问题时立刻跑一次。
- 查询运行记录失败时一律**回退为照常签到** —— 宁可多签一次，也不会静默漏签。

另外工作流还有两个收尾步骤：`liskin/gh-workflow-keepalive` 防止 GitHub 因长期不活动
自动停用定时任务；`Mattraks/delete-workflow-runs` 保留 30 天运行记录（排查漂移要看
连续多天的实际触发时间，删太早会失去证据）。

## 配置

在仓库 `Settings` → `Secrets and variables` → `Actions` 里添加：

| Secret | 必填 | 说明 |
|---|---|---|
| `GLADOS_COOKIES` | 是 | 账号的会话 Cookie，见下 |
| `GLADOS_USER_AGENT` | **强烈建议** | 登录浏览器的 `navigator.userAgent`，见下 |
| `GLADOS_EXCHANGE_PLAN` | 否 | 积分兑换策略，默认 `plan500` |
| `GLADOS_VERBOSE` | 否 | `true` / `false`，默认 `false` |

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

多账号之间用 `&` 连接：`c1&c2&c3`，每个账号各自签到一次。

### GLADOS_USER_AGENT

GLaDOS 从 2026-09 起会校验「签到请求的平台」与「登录时浏览器的平台」是否一致，
**对不上就返回 `code 4 Automated check-in detected`**，表现为一直签到失败，但
`status` / `points` 接口都正常，很容易误判成 Cookie 坏了。

在**登录 GLaDOS 的那个浏览器**里按 `F12` → `Console`，执行 `navigator.userAgent`，
把输出原样粘成这个 secret 的值。

> 不配置时会用脚本内置的 macOS Chrome UA。同一次实测里 macOS UA 能签到，
> Windows / Linux / iPhone UA 一律被判为自动签到（只改 Chrome 版本号无效）。
> 所以**在 Windows 或 Linux 上登录的话，这一项必须配**。

### GLADOS_EXCHANGE_PLAN

积分够就自动兑换成天数，不配置时默认 `plan500`：

| 值 | 需要积分 | 兑换天数 |
|---|---|---|
| `plan100` | 100 | 10 天 |
| `plan200` | 200 | 30 天 |
| `plan500` | 500 | 100 天（默认） |

**积分没到门槛时不会发兑换请求**，也就不会有任何报错——每天拿一个明知道会被拒的
请求去换一句 `Not enough points` 没有意义，余额是本脚本自己刚查过的。所以默认日志
里平常看不到兑换相关的内容，这是正常的。

积分够了并且兑换成功时，日志里会无条件留一行（与是否开 verbose 无关，因为它会真的
扣掉几百积分）：

```
🍪[1] ✅ plan500 兑换成功 (code 0, message: ...)
```

> 表中"兑换天数"三行来自站点说明，代码里只知道"需要多少积分"（500 那行已由服务端
> 自己的报错 `Need 500` 验证过）。

## 通知与退出码

脚本不做任何推送。通知链路只有一条：**签到失败 → 非 0 退出码 → 作业变红 →
GitHub 发失败邮件**。所以别关掉 Actions 的失败通知：

- 仓库页右上角 `Watch` → 至少勾上 `Actions`
- `Settings` → `Notifications` → `Actions` 段选 **Send notifications for failed workflows only**

| 退出码 | 含义 |
|---|---|
| `0` | 所有账号都签到成功 / 今日已签到 |
| `1` | 有账号签到失败 → 作业变红 |
| `2` | 配置错误，例如没设 `GLADOS_COOKIES` |

判定是 fail-closed 的：只有 `code 0`（签到成功）和 `code 1`（重复签到）算通过；
认证失败、反自动化拦截、网络失败、未预期异常**全部**返回 1。某个账号连结果都没跑出来
（中途异常）同样算失败 —— 宁可多签一次，也绝不静默漏签。

## 出问题了怎么看

去 Actions 里点开最近一次 `gladosCheck` 运行，看 `Running checkin` 那一步的日志。

| 日志现象 | 原因 | 处理 |
|---|---|---|
| `Cookie[n] 缺少会话字段 gld:sess.sig` | 只抄了半对，或复制时被截断 | 回签到页重新复制完整 Cookie |
| `认证失败 (code -2, message: 没有权限)` | Cookie 不完整或已过期（约 30 天） | 重新登录复制；这行会提示需要哪几个字段 |
| `签到被判定为自动签到` / `code 4` | 请求的平台和登录浏览器不一致 | 按上文配置 `GLADOS_USER_AGENT`；日志里会打印服务端给的 `reason` / `loginDevice` / `currentDevice` |
| 作业变红，总结里 `失败1` | 该账号没签到成功 | 按上面依次检查 Cookie 和 `GLADOS_USER_AGENT` |

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

注意 `重复签到` 也是成功：`code : 1 ... observation logged` 表示服务端接受了这次请求。

## 为什么请求要长得像浏览器

被判定为自动签到（`code 4`）的根因是「请求不像本人浏览器点的」。这里不是猜的，是拿真机
抓包逐项对齐的 —— 期望值存在 [`tests/fixtures/browser_checkin_request.json`](tests/fixtures/browser_checkin_request.json)：
2026-09-26 用 CDP 记录本机 Chrome 154（macOS）在 `https://glados.cloud/console/checkin`
点「签到」时发出的那一次请求。

| 项目 | 浏览器 | 脚本 |
|---|---|---|
| 方法 / 路径 | `POST /api/user/checkin` | 一致 |
| 请求体 | `{"token":"glados.cloud"}`（24 字节，无空格） | 一致 |
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

## 说明

本项目基于 [Devilstore/Glados-Railgun-checkin](https://github.com/Devilstore/Glados-Railgun-checkin)
修改，遵循仓库内的 [LICENSE](LICENSE)。
