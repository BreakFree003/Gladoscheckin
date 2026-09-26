# Glados自动签到

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

#### 我的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

#### 我的优惠码（9折）：([DEVILSTORE](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应你账号所在站点的cookie值中的有效部分（获取方式如下）

- 在**你注册的那个站点**（[glados.cloud](https://glados.cloud) 或 [railgun.info](https://railgun.info)）的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > **每个站点各有一套会话字段（2026-09 起）：**
  >
  > | 你的账号在 | `GLADOS_COOKIES` 里要带的字段 | 参考格式 |
  > |---|---|---|
  > | GLaDOS (`glados.cloud`) | `gld:sess` + `gld:sess.sig` | `gld:sess=eyJ1c2...; gld:sess.sig=xxxx` |
  > | Railgun (`railgun.info`) | `koa:sess` + `koa:sess.sig` | `koa:sess=eyJ1c2...; koa:sess.sig=xxxx` |
  > | 两个站点都有账号 | 两对都带（用 `;` 连接） | `koa:sess=...; koa:sess.sig=...; gld:sess=...; gld:sess.sig=...` |
  >
  > ⚠️ 只要**有一对完整**就能签到：脚本会把同一份 Cookie 依次发给两个域名，
  > 不持有你账号的那个域名返回 `code -2` 是正常现象。
  > 最简单的做法是在 `Network` 里右键 `Cookie` → 复制完整值，不要手工挑字段。

![图片加载失败](imgs/3.png)

- 多账号请在 `COOKIES` 中 添加多个 `cookies` 中间使用 `&`连接即可。（例如： `c1&c3&c3...`）

3. 配置积分兑换策略（非必须）

- 添加1个`repository secret`，命名为`GLADOS_EXCHANGE_PLAN`，配置自动兑换积分策略：

| 值 | 积分要求 | 兑换天数 |
|---|---------|---------|
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天 (默认) |

> 不配置时默认为 `plan500`，即积分达到 500 时自动兑换 100 天

4. 手机推送（非必须）

- 添加1个`repository secret`，命名为`PUSHDEER_SENDKEY`，其值对应 PushDeer key: ([获取地址](https://www.pushdeer.com/product.html))。

5. User-Agent（**强烈建议配置**）

GLaDOS 从 2026-09 起会校验「签到请求的平台」和「登录时浏览器的平台」是否一致，**对不上就会返回
`code 4 Automated check-in detected`**，表现为 Actions 里一直签到失败，但 `status`/`points` 接口都正常，
很容易被误以为是 Cookie 坏了。

- 添加1个`repository secret`，命名为`GLADOS_USER_AGENT`
- 在**登录 GLaDOS 的那个浏览器**里按 `F12` → `Console`，执行：

  ```js
  navigator.userAgent
  ```

- 把输出的完整字符串原样粘贴为 secret 的值

> 不配置时会使用脚本内置的 macOS Chrome UA。同一次实测中：macOS UA 可以签到，
> Windows / Linux / iPhone UA 一律被判定为自动签到（只改 Chrome 版本号无效）。
> 所以**如果你是在 Windows 或 Linux 上登录的，这一项必须配置**。
>
> 平台为什么是关键：账号页面「登录设备」列表调用的 `/api/user/sessions` 里，
> `device` 字段只取 `macOS` / `Windows` / `Linux` / `Android` / `iOS` / `Bot` / `Other`，
> 服务端就是拿它和签到请求的平台比对的（详见下面「签到请求和网页点签到的一致性」）。

### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 文件结构

```shell
│  checkin.py	# 签到脚本
│  logging_config.py	# 日志配置
│
├─.github
│  └─workflows
│          gladosCheck.yml	# Actions 配置文件
│
└─tests
       test_checkin.py	# 测试
       fixtures
              browser_checkin_request.json	# 真机抓包的签到请求, 作为请求形态的期望值
```

## 更新日志

- **2026-01**: 重构代码，添加log输出方便定位，支持新版网址，支持配置积分兑换策略。
- **2026-04**: 优化代码逻辑，优化日志输出，支持[新版域名](https://railgun.info) ，在 GLADOS_COOKIES 中添加新版域名下的 cookies 即可使用。
- **2026-09**: GLaDOS 连续改了两处：
  1. 会话 Cookie 拆成两套：`glados.cloud` 用 `gld:sess` / `gld:sess.sig`，
     `railgun.info` 用 `koa:sess` / `koa:sess.sig`。只在一个站点注册时，只复制
     那个站点的那一对即可；两个站点都有账号时两对都带上。
  2. 新增反自动化校验：签到请求的平台必须与登录浏览器一致，否则返回
     `code 4 Automated check-in detected`（回复里还会带 `reason: device-mismatch`
     和 `loginDevice`/`currentDevice`，站点自己也是让用户重新登录来重置设备）。
  3. 脚本的请求形态与网页端逐项对齐（做法见下面「签到请求和网页点签到的一致性」）：
     站点 console 包里签到走的是 `axios.post("/user/checkin", {token: location.hostname})`
     （baseURL 为 `/api`），所以脚本现在发**紧凑 JSON 请求体**
     （`{"token":"<域名>"}`、`{"planType":...}`，与浏览器的 `JSON.stringify` 逐字节一致）、
     `content-type: application/json;charset=UTF-8`，并且**不再自己造 `referer`**——
     签到页带 `<meta name="referrer" content="no-referrer">`，浏览器根本不发这个头。

  脚本现在会在加载阶段校验 Cookie 字段、在认证失败 (code -2) 与被判定为自动签到 (code 4) 时
  分别给出可操作提示，并在**账号在所有域名上都失败时返回非 0 退出码**，
  让 Actions 真正变红，不再出现“工作流成功但没签到”的假绿。


## 签到请求和网页点签到的一致性

被判定为自动签到 (code 4) 的根因是「请求长得不像本人浏览器点的」。这里的请求形态是拿真机
抓包逐项对齐的，期望值存在 [`tests/fixtures/browser_checkin_request.json`](tests/fixtures/browser_checkin_request.json)：
2026-09-26 用 CDP 记录本机 Chrome 154 (macOS) 在 `https://glados.cloud/console/checkin`
点击「签到」时发出的那一次请求（含浏览器自动加的 `sec-ch-ua*` / `sec-fetch-*` 等头）。

| 项目 | 浏览器的签到请求 | 脚本 |
|---|---|---|
| 方法 / 路径 | `POST /api/user/checkin` | 一致 |
| 请求体 | `{"token":"glados.cloud"}`（24 字节，无空格） | 一致 |
| `content-type` | `application/json;charset=UTF-8` | 一致 |
| `accept` | `application/json, text/plain, */*` | 一致 |
| `origin` | `https://glados.cloud` | 一致 |
| `user-agent` | 登录时那个浏览器的 UA | 一致（默认就是它，可用 `GLADOS_USER_AGENT` 覆盖） |
| `referer` | **没有**（页面是 `no-referrer`） | 不发 |
| `sec-ch-ua*` / `sec-fetch-*` / `accept-language` / `dnt` | 浏览器进程自动加 | 刻意不伪造 |

最后一行是故意的：伪造 `sec-ch-ua-platform` 这类值，一旦用户的实际浏览器平台和
`GLADOS_USER_AGENT` 对不上，反而制造出「UA 与 client hints 打架」这种更像机器人的特征；
实测缺了这些头服务端照样接受（返回 `code 1`）。

服务端判「自动签到」的依据，是比对**登录设备平台**和**本次请求的平台**：账号页面调用的
`/api/user/sessions` 里 `device` 只会是 `macOS` / `Windows` / `Linux` / `Android` / `iOS` /
`Bot` / `Other` 之一，正是从 User-Agent 里解析出来的。所以只要 `GLADOS_USER_AGENT` 是
**当初登录的那个浏览器**的 `navigator.userAgent`，平台就对得上。

想自己复核：在浏览器里点一次签到，`F12` → `Network` → 右键该请求 → `Copy as cURL`，
再和 `tests/test_checkin.py` 里 `test_checkin_wire_request_matches_the_captured_browser_request`
与 `test_checkin_sends_no_header_the_browser_never_sends` 的断言对一遍即可。


## 问题排查与定位
- 大家可以通过查询 actions 中的 running checkin 日志快速定位问题，有其他问题提交issue。

  <img width="1684" height="844" alt="image" src="https://github.com/user-attachments/assets/45348a5f-43e4-45f5-8fdf-ce84d343b30d" />

### 常见报错对照

| 日志现象 | 原因 | 处理方式 |
|---|---|---|
| `Cookie[n] 没有一对完整的会话字段` | Cookie 里 `gld:sess`/`gld:sess.sig` 与 `koa:sess`/`koa:sess.sig` 两对都不完整 | 回到你账号所在站点的 `F12` → `Network`，右键 `Cookie` 复制完整值；只复制其中一个站点的那一对也够用 |
| `认证失败 (code -2, message: 没有权限)` | Cookie 不完整或已过期（约 30 天），或复制的是另一个站点的 Cookie | 重新登录账号所在站点复制完整 Cookie；本行前面会提示该域名需要哪一对字段 |
| `签到被判定为自动签到` / `code 4 Automated check-in detected` | 请求的 User-Agent 平台与登录浏览器不一致（日志里会跟着打印服务端给的 `reason` / `loginDevice` / `currentDevice`） | 按上面第 5 步配置 `GLADOS_USER_AGENT` 为你的 `navigator.userAgent`；`currentDevice` 就是这次请求被识别成的平台，`loginDevice` 是登录时那个 |
| `任务 x/y: 🍪[n] on 🌐[railgun.info] ❌ { code : -2, message : No permission }` | 该账号不在 railgun.info，属正常现象 | 无需处理，只看有账号的那个域名是否签到成功 |
| Actions 变红，总结里 `失败2` | 该账号所有域名都没签到成功 | 按上面的提示依次检查 Cookie 和 `GLADOS_USER_AGENT` |


### 退出码约定

| 退出码 | 含义 |
|---|---|
| `0` | 所有账号至少在其中一个域名上签到成功 / 今日已签到 |
| `1` | 有账号在所有域名上都未签到成功 → Actions 变红 |
| `2` | 配置错误，例如未设置 `GLADOS_COOKIES` |

### 本地自测

```shell
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
# 单元测试 + 真实接口端到端测试
./.venv/bin/python -m pytest tests/ -q
# 用你自己的 Cookie 本地跑一次（不会打印 Cookie 值）
# glados.cloud 账号用 gld:sess/gld:sess.sig，railgun.info 账号用 koa:sess/koa:sess.sig
GLADOS_COOKIES='gld:sess=...; gld:sess.sig=...' \
GLADOS_USER_AGENT='粘贴你浏览器的 navigator.userAgent' \
./.venv/bin/python checkin.py; echo "exit=$?"
```

> `重复签到` 也是成功（当天已经签过），日志里的 `code : 1 ... observation logged` 表示服务端接受了这次请求。

## 声明

本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份







