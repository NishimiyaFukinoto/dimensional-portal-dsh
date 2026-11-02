# 待办（按优先级）

## 已完成（2026-11-02）：插件化 + npm 发布 + 目录 PR

- **npm**：`dsh-dimensional-portal@0.2.0` 已发布（https://www.npmjs.com/package/dsh-dimensional-portal）
- **GitHub**：https://github.com/NishimiyaFukinoto/dimensional-portal-dsh（main 已推送）
- **目录 PR**：awesome-dsh-plugin/awesome-dsh-plugin#11392（新增 `data/plugins/NishimiyaFukinoto__dimensional-portal-dsh.yml`）
- 合并后 README 自动重建，dshmarket 一般一天内收录 → 市场里可搜可一键装

## 已完成（2026-11-02）：插件化 + 上架准备

- 仓库本身改成 **DSH 插件包**：`package.json` 的 `dsh.bundle.patch` + `cordis.patch.yml` +
  `lib/index.js`（宿主侧入口）/ `lib/tools.js`（11 个工具定义）/ `lib/daemon.js`（拉起 daemon、转发请求）。
  装了插件就有 11 个 `phone_*` 工具，**不再需要 MCP 那一层**。
- 插件会按需拉起 `tools/dp_daemon.py`（自动找 python），运行数据落在 `<插件>/data/`：
  `dp.json`、`data/shots/`、`audit.jsonl`、`daemon.log`。
- `scripts/plugin_smoke.mjs`：假 daemon + 假 ctx，8 项断言（注册 / 转发 / 取整 / render / 断线提示）。
- `scripts/e2e_local.mjs`：真的拉起 Python daemon 跑一遍（4 项：上线、生成 `local_token`、急停、坏参数不崩），
  跑完 dispose 会把子进程带走。端口取 `dp.json` 里的 `listen_port`，不再写死。
- `.npmignore` + `files` 白名单：`*.local.js`（含真实 token 与地址）、`dp.json`、`data/`、`__pycache__`
  都不进包；`npm pack` 现在 29 个文件 / 477 KB。
- 上架路径写进 `docs/MARKET.md`：市场读的是 awesome-dsh-plugin 目录，要「npm 包 + 目录条目」两件事。

**还差三步才能真的在市场里被看见**（都需要你的账号）：

| 步骤 | 谁做 | 说明 |
| --- | --- | --- |
| `npm login` + `npm publish` | 你（需要 npm 账号） | 包名 `dsh-dimensional-portal` 目前没被占用 |
| 向 awesome-dsh-plugin 提 PR | 你（需要 GitHub） | 条目字段见 `docs/MARKET.md` |
| 装进本机 profile 实测 | 可我做，但要重启 DSH | `dsh plugin add` 后会拉起 daemon，需要 Python + aiohttp |

## 已完成（2026-11-01）

- **应用访问**：手机上报已安装应用（`apps` 动作）→ 控制台「应用访问」卡逐项勾选 → `app_mode` /
  `app_blocked` / `app_allowed` 三项配置 → `_gate` 里按包名拦截（`deny_app` 审计）。
  `open_app` 看目标包名，其余动作看当前前台包名；`back/home/recents` 不受影响（逃生动作）。


> 最后更新：2026-11-01。做完一项就把这一节挪到 `CHANGELOG.md`，别让它烂尾。

---

## P1 · 降延迟优化（用户已确认要做，尚未动手）

一次动作的耗时 ≈ **2 个 RTT**（下发 + 回传）+ 手机端执行 + 截图传输。
走 Tailscale 中继（60–150ms RTT）时，网络部分约 0.2–0.3 秒，**截图传输才是大头**。

### ① 截图降采样（收益最大）

- **现状**：`screen` 动作按 1080×2400、JPEG q55 截图，100–200KB/张，慢网络要 1–2 秒。
- **做法**：手机端截图后先等比缩到长边 720（`max_edge`），质量降到 45；同时给动作加参数
  `screen {quality?: number, max_edge?: number}`，默认值来自 `dp.json` 的
  `screenshot_quality` / 新增 `screenshot_max_edge`。
  涉及 `phone/dimensional_portal_agent.js` 的 `captureScreen` 分支、`dp/service.py` 的 `phone_see`、
  `bridge/protocol.py` 的动作常量与 `dp.json.example`。
- **收益**：截图 100–200KB → 40–60KB，传输 1–2s → 0.3–0.6s。
- **代价**：小字略糊；需要原图时显式传 `max_edge: 0`（表示不缩）。
- **验收**：同一界面截图两次（默认 / 原图），比对文件大小；`phone_see` 返回的 `size` 明显变小，
  `w`/`h` 记录原图尺寸，缩放后尺寸另存 `scaled_w`/`scaled_h`。

### ② 连接复用（keep-alive）

- **现状**：手机端每次请求都新建 `HttpURLConnection`，慢链路每次多付 TCP（+TLS）握手 1–2 RTT。
- **做法**：手机脚本里 `conn.setRequestProperty("Connection", "keep-alive")`，并复用同一个
  `java.net.URL` 连接池（Rhino 下 `HttpURLConnection` 默认走连接池，关键是别在 `finally` 里
  `disconnect()`）；服务端 aiohttp 已默认 `tcp_nodelay`，无需改。
- **收益**：每个动作省 0.1–0.3 秒（Tailscale 中继下更明显）。
- **代价**：极小；注意失败重试时要重建连接，别复用坏连接。
- **验收**：抓手机端日志统计「建连耗时」，优化前 vs 优化后对比；连续 20 次 `phone_status` 总耗时下降。

## P2 · 常连接（长轮询 → WebSocket）—— 暂缓

- **现状**：`GET /poll?since&wait` 每 12 秒一轮，有任务立刻返回。
- **做法**：改用 WebSocket（aiohttp `WebSocketResponse` + 手机端 JSON over WS），保留 `/poll` 作为降级。
- **收益**：省 0.05–0.1 秒 + 省掉每轮重建连接。
- **代价**：手机端要写重连/心跳/半开检测，改动约半天。
- **结论**：收益最小、风险最大，**等 ① ② 做完再评估**。

---

## 其它待办

| 项 | 说明 | 状态 |
| --- | --- | --- |
| 手机端换脚本 | 手机现在跑的是「连 8765（AstrBot 版）」的脚本，DSH 版在 8766；需要用 `tools/make_phone_script.py` 生成的 `phone/dimensional_portal_agent.local.js` | 阻塞在手动替换/adb |
| daemon 开机自启 | 现在靠手动 `python tools/dp_daemon.py`；需要一个 `.cmd` 快捷方式或计划任务 | 未做 |
| 真机联调 | 手机换成 8766 脚本后，跑一遍 `phone_see` → `phone_tap` → `phone_type` | 未做 |
| 独立仓库 | 用户要求 DSH 版单独一个 GitHub 仓库（当前只在本地 git） | 未做 |
| 手机端版本号 | 手机脚本 `AGENT_VERSION` 仍是 `0.3.0`，与插件 0.4.1 并列时容易混 | 待定 |
| AstrBot 版市场审核 | `v0.4.1` 仍 Waiting，通过后要做验收（索引条目 + 下载安装） | 等平台 |

## 真机预览：先搁置（2026-11-02）

代码已合入（`preview_enabled` 默认 `false`），但**真机没验收通过**，暂时关着：

- 现象：`/admin/preview` 返回 502，手机端回 `java.lang.IllegalStateException: image has been recycled`。
- 原因：AutoX 的 `captureScreen()` 复用同一个 Bitmap 实例；脚本保存后 `img.recycle()` 了它，
  第二次截图就抛这个异常（`phone/dimensional_portal_agent.js` 原 `actScreen`）。
- 已改：新增 `grabScreen()`（不复用、不回收；拿到已回收的实例就 `ensureCapture(true)` 再抓一次），
  并用 `tools/make_phone_script.py --bases ...` 重新生成推到手机。**改动未经真机复测。**
- 恢复验证的步骤：确认心跳在线 → 打开 `preview_enabled` → 连续拉两次 `/admin/preview`
  （第二次应为 429 限流）→ 看日志里有没有新的 `preview` 审计记录。

