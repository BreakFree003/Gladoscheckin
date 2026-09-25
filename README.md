# Glados自动签到

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

#### 我的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

#### 我的优惠码（9折）：([DEVILSTORE](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 在GLaDOS的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > 参考格式（**必须 4 项都带上**）：`koa:sess=eyJ1c2...AwMH0=; koa:sess.sig=xJkO...tnM; gld:sess=eyJ1c2...; gld:sess.sig=xxxx`
  >
  > ⚠️ **2026-09 起 GLaDOS 增加了 `gld:sess` / `gld:sess.sig` 两项**。只复制 `koa:sess`、`koa:sess.sig`
  > 的旧 Cookie 会让所有接口返回 `code -2「没有权限」`，签到全部失败。
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
```

## 更新日志

- **2026-01**: 重构代码，添加log输出方便定位，支持新版网址，支持配置积分兑换策略。
- **2026-04**: 优化代码逻辑，优化日志输出，支持[新版域名](https://railgun.info) ，在 GLADOS_COOKIES 中添加新版域名下的 cookies 即可使用。
- **2026-09**: GLaDOS 增加 `gld:sess` / `gld:sess.sig` 两项 Cookie，旧的两字段 Cookie 全部失效。
  脚本现在会在加载阶段校验 Cookie 字段、在认证失败时给出可操作提示，并在**账号在所有域名上都失败时返回非 0 退出码**，
  让 Actions 真正变红，不再出现“工作流成功但没签到”的假绿。


## 问题排查与定位
- 大家可以通过查询 actions 中的 running checkin 日志快速定位问题，有其他问题提交issue。

  <img width="1684" height="844" alt="image" src="https://github.com/user-attachments/assets/45348a5f-43e4-45f5-8fdf-ce84d343b30d" />

### 常见报错对照

| 日志现象 | 原因 | 处理方式 |
|---|---|---|
| `缺少 gld:sess, gld:sess.sig` | Cookie 是 2026-09 之前的旧格式 | 重新登录后复制完整 Cookie，更新 `GLADOS_COOKIES` |
| `认证失败 (code -2, message: 没有权限)` | Cookie 不完整或已过期（约 30 天） | 同上；确认复制的是 `glados.cloud` 的 Cookie |
| `任务 x/y: 🍪[n] on 🌐[railgun.info] ❌ { code : -2, message : No permission }` | 该账号不在 railgun.info，属正常现象 | 无需处理，只看 `glados.cloud` 那一行是否签到成功 |
| Actions 变红，总结里 `失败2` | 该账号所有域名都没签到成功 | 按上面的提示更新 Cookie |

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
GLADOS_COOKIES='koa:sess=...; koa:sess.sig=...; gld:sess=...; gld:sess.sig=...' ./.venv/bin/python checkin.py; echo "exit=$?"
```

## 声明

本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份







