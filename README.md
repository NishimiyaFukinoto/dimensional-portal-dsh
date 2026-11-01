# 次元门 · Dimensional Portal（DSH 版）

让 **DSH 里的 AI** 直接操作你的安卓手机：看屏幕、点按滑动、输入文字、打开应用、找字点击。

和「次元门（AstrBot 插件版）」的关系：手机端脚本、协议、控制台完全同源，只是把
「QQ → NapCat → AstrBot」这一整条聊天链路换成了「DSH 会话 → MCP」——**没有 QQ，没有
NapCat，没有第二个模型**，大脑就是正在跟你说话的这个 DSH。

```
DSH 会话 ──MCP(stdio)──> tools/dp_mcp.py ──HTTP(回环)──> tools/dp_daemon.py(:8766)
                                                              ↑ 长轮询
                                                        手机 · AutoX.js 脚本
```

---

## 它现在能做什么

| 能力 | 工具 | 说明 |
| --- | --- | --- |
| 看屏幕 | `phone_see` | 截图存到 `data/shots/`，返回路径；AI 自己把图读出来看 |
| 读控件 | `phone_ui` | 返回界面文字 / 坐标 / 可点状态，敏感界面也能用 |
| 点 | `phone_tap` `phone_long_press` | 真实像素坐标 |
| 划 | `phone_swipe` | 滚动、翻页、返回手势 |
| 打字 | `phone_type` | 失败自动退剪贴板粘贴 |
| 按键 | `phone_key` | back / home / recents / enter / delete / 音量 |
| 开应用 | `phone_open_app` | 应用名或包名，含黑名单复查 |
| 找字点 | `phone_find_tap` | 比算坐标稳 |
| 急停 | `phone_stop` | 清空还没下发的任务 |
| 看状态 | `phone_status` | 在线、电量、分辨率、权限 |

控制台（浏览器）：`http://<本机IP>:8766/console?k=<admin_token>`，能看设备状态、调安全策略、翻审计日志、
逐个决定「哪些应用可以被碰」（应用访问），
调安全策略、翻操作审计、急停。

---

## 五分钟跑起来

### 1. 电脑侧：起 daemon

依赖 `aiohttp` 与 `mcp`（Python 3.10+）。如果你机器上有 AstrBot 的 venv，它已经两样都有：

```bash
"C:\Users\<你>\.astrbot_launcher\instances\<id>\venv\Scripts\python.exe" tools/dp_daemon.py
```

第一次运行会生成 `dp.json`，里面自动带了 `token` 与 `local_token`，屏幕上也打印出来：

```
 手机连这个 http://192.168.1.10:8766
 token      3Xk…（抄到手机脚本里）
 控制台     http://192.168.1.10:8766/console?k=…
```

### 2. 手机侧：装 AutoX.js + 跑脚本

1. 手机装 [AutoX.js](https://github.com/kkevsekk1/AutoX)（免费、开源、要无障碍，不要 root）。
2. 打开 `phone/dimensional_portal_agent.js`，把顶部两处改掉：
   - `BASES` 里填电脑地址（有 Tailscale 就填 `100.x` 开头那个，再加一条局域网地址做备份）；
   - `TOKEN` 填 `dp.json` 里的 `token`。
3. 运行脚本，授权「屏幕录制（整个屏幕）」+ 无障碍 + 悬浮窗。
4. 保持运行（通知栏有「脚本运行中」），并把 AutoX.js 加进电池白名单。

看到控制台里手机变绿、出现机型/电量，就连上了。

### 3. 把工具接进 DSH

用 `dsh-skill-mcp-panel` 加一个 stdio MCP（把 `<python>` 换成装了 `mcp` 的解释器，
`<仓库路径>` 换成本仓库绝对路径）：

```bash
dsh-panel mcp add --name dp-phone --stdio \
  --command "<python>" \
  --args <仓库路径>/tools/dp_mcp.py \
  --profile desktop
```

加完在 DSH 里说一句「看看我手机现在什么界面」，它就会调 `phone_see`，
把截图路径读出来看，然后接着 `phone_tap` / `phone_type`。

---

## 配置（dp.json）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `token` | 自动生成 | 手机 ↔ 电脑共享密钥，必须和手机脚本一致；为空拒绝启动 |
| `listen_host` `listen_port` | `0.0.0.0` `8766` | 要能被手机访问到 |

> 默认用 **8766** 而不是 8765：AstrBot 版插件默认占 8765，两个版本可以同时跑（手机连哪个由脚本里的地址决定）。
| `local_token` | 自动生成 | 本机 MCP 进程调 daemon 用，只认回环地址 |
| `admin_token` | 自动生成 | 控制台密钥；填 `off` 彻底关闭控制台 |
| `app_mode` | `blocklist` | 应用访问模式：`blocklist` 默认放行只禁个别的，`allowlist` 只放行名单里 |
| `app_blocked` | `[]` | 禁止访问的包名数组（blocklist 模式生效） |
| `app_allowed` | `[]` | 允许访问的包名数组（allowlist 模式生效） |
| `debug_token` | 空 | 非空才开 `POST /debug/submit`，平时别开 |
| `rate_limit_per_minute` | 20 | 每分钟最多下发多少动作 |
| `task_timeout_ms` | 20000 | 单个动作默认超时 |
| `offline_after_seconds` | 40 | 多久没心跳算离线 |
| `screenshot_quality` | 55 | 截图 JPEG 质量 |
| `max_screenshot_kb` | 1500 | 单张截图上限 |
| `allow_screenshot_sensitive` | false | 敏感界面（微信/银行/设置…）是否允许截图 |
| `allow_sensitive_write` | false | 敏感界面是否允许点击/输入 |
| `sensitive_packages` | 内置表 | 覆盖默认的敏感包名列表 |
| `package_blacklist` | 内置表 | 禁止操作的包名 |

`allowed_user_ids`（AstrBot 版的 QQ 白名单）在这里**没有意义**，读到会被忽略。

---

## 安全设计

- **不开放公网**：daemon 只监听你指定的地址，手机走局域网/Tailscale 连进来；
  没有 QQ、没有中转服务器，画面不会出你的机器。
- **双密钥**：手机用 `token`；本机 MCP 通道另外要 `local_token`，且**只接受回环地址**。
- **动作层策略**：限流、包名黑名单、敏感界面拦截（默认不截图、不允许写操作）、
  审计日志 `data/audit.jsonl`（5 MB 滚动）。
- **截图落地**：截图只写 `data/shots/`（保留 1 小时），不再推给任何第三方。

### 已知限制

- 锁屏状态下无法解锁（脚本没有锁屏密码）。
- 微信 / 部分银行 App 会屏蔽无障碍，读不到控件。
- 少数自绘输入框 `setText` 会失效（已退剪贴板粘贴，仍可能被拒）。
- 脚本被系统杀掉要手动在 AutoX.js 里再点一次「运行」（可配合电池白名单与自启）。
- 手机上**没有模型**，也不该有：理解画面靠 DSH 里的模型读截图。

---

## 目录

```
bridge/          协议 / 队列 / HTTP 服务 / 控制台网页
safety/          安全策略（限流、黑名单、敏感界面、审计）
dp/              配置读写 + 服务主体（手机队列 + 11 个工具方法）
phone/           手机端 AutoX.js 脚本（纯 ES5，无依赖）
tools/           dp_daemon.py（常驻）/ dp_mcp.py（MCP 工具层）
tests/           smoke_test.py（纯逻辑）/ live_server.py（假手机端到端）
docs/            INTRO.md（项目介绍）、PROTOCOL.md（协议）、TODO.md（待办与降延迟优化）
```

跑测试：

```bash
<python> tests/smoke_test.py     # 27 项，不联网
<python> tests/live_server.py    # 21 项，起真 HTTP + 假手机
```

---

## 和 AstrBot 版的差别

| | AstrBot 插件版 | DSH 版（本仓库） |
| --- | --- | --- |
| 入口 | QQ 群/私聊 | DSH 会话 |
| 大脑 | AstrBot 里的模型 | DSH 里的模型 |
| 截图去向 | 发到 QQ | 存本机 `data/shots/` |
| 权限控制 | QQ 号白名单 + 工具级屏蔽 | 本机回环 + `local_token` |
| 配置 | AstrBot 插件面板 | `dp.json` |
| 控制台 | 有 | 有（同款） |

---

## License

MIT
