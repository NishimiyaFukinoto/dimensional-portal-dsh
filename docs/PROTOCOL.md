# 协议说明（手机 ⇄ daemon）

> 版本：`PROTOCOL_VERSION = 1`。手机端脚本 `phone/dimensional_portal_agent.js` 与
> 本目录的 `bridge/` 一一对应，改字段两边要一起改。

## 1. 拓扑

```
DSH 会话（AI）
   ↓ MCP stdio（tools/dp_mcp.py）
本机 HTTP 127.0.0.1:8765（tools/dp_daemon.py → dp.service.PhoneService → bridge/server.py）
   ↑ 手机主动长轮询（AutoX.js 脚本，无障碍服务执行）
手机 Android
```

- **手机在 CGNAT / 移动网络后面，永远不做服务端**：它只主动连电脑。
- 电脑不需要公网 IP；要远程用就走 Tailscale（或任何让手机能访问到 8765 的通道）。
- 同一个 daemon 可以接多台手机（按 `device_id` 区分），但默认只用最新心跳的那台。

## 2. 认证

| 谁 | 用什么 | 说明 |
| --- | --- | --- |
| 手机 | `X-Token: <token>` 或 `?token=` | `dp.json` 的 `token`，空则 daemon 拒绝启动 HTTP |
| MCP 进程 | `X-Local-Token: <local_token>` | 只允许**回环地址**访问，自动生成 |
| 浏览器控制台 | `?k=<admin_token>` | 填 `off` 可彻底关闭控制台 |
| 调试 | `X-Debug-Token: <debug_token>` | 非空才注册 `POST /debug/submit` |

比较全部用常量时间（`hmac.compare_digest` 语义），错误 token 一律 `401`，不回显原因。

## 3. 端点

### 手机用的（需要 `X-Token`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/hb` | 心跳，10 秒一次；返回服务端时间、队列长度、轮询间隔 |
| GET | `/poll?since=N&wait=S` | 长轮询取任务，最长阻塞 `wait` 秒（默认 12，上限 30） |
| POST | `/result` | 回传任务结果 |
| GET | `/healthz` | 活着的证明，不需要 token，只回 `{"ok":true,"service":...}` |

### 本机 MCP 用的（只允许回环 + `X-Local-Token`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/local/submit` | `{"action","params","timeout_ms"}` → `{"ok":true,"result":…}` |
| POST | `/local/tool` | `{"name":"phone_tap","args":{…}}`，`name` 只允许 `phone_` 前缀 |
| GET | `/local/state` | 设备快照：`online/device/queue/last_heartbeat_age/uptime_s/stats` |
| POST | `/local/stop` | 急停：清空所有排队任务，返回 `{"ok":true,"dropped":N}` |

### 控制台用的（`?k=<admin_token>`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/console` | 单文件页面；本机不带 `k` 会自动 302 补上 |
| GET | `/admin/state` | 设备状态 + 可编辑配置 + `editable` 区间 + 最新 20 条审计 |
| POST | `/admin/config` | 改配置，改完立刻生效（不用重载） |
| GET | `/admin/audit?limit=&offset=` | 审计分页，新的在前 |
| POST | `/admin/stop` | 清空排队任务 |
| GET | `/console-assets/{name}` | 只放行 `bg-wide.jpg` / `bg-tall.jpg` 两张背景图 |

## 4. 心跳体

```json
{
  "device_id": "7d21aecedc7d7473",
  "device": "vivo V2171A",
  "android": "15",
  "w": 1080, "h": 2400,
  "battery": 98, "charging": true,
  "cur_pkg": "com.android.systemui",
  "accessibility": true,
  "screen_capture": true,
  "overlay": true,
  "overlay_note": "ok(WindowManager)",
  "agent_version": "0.3.0",
  "queue_depth": 0
}
```

- `overlay` 是「后台启动第三方应用」的豁免条件，为 `false` 时 `open_app` 在后台大概率被系统丢掉。
- 服务端超过 `offline_after_seconds`（默认 40 秒）没收到心跳就算离线，工具直接拒绝下发。

## 5. 任务体

```json
{ "id": "t_1a2b3c4d5e6f", "action": "tap", "params": {"x": 540, "y": 1200},
  "timeout_ms": 20000, "created_at": 1761996094.12 }
```

语义 **at-most-once**：手机取走任务后就从队列里删掉；`/result` 回来晚了（超时之后）直接丢弃。
任务在队列里超过 90 秒还没被取走会自动过期。

## 6. 动作表

| action | params | 返回（`data`） |
| --- | --- | --- |
| `screen` | `quality`(1–95) `max_edge`(可选，长边像素) | `image_b64`、`w`、`h`、`pkg`、`size` |
| `ui` | `max_nodes`(默认 80) | `nodes`、`win`、`popup_maybe` |
| `tap` | `x` `y` | `pkg` |
| `long_press` | `x` `y` `duration`(默认 800) | `pkg` |
| `swipe` | `x1` `y1` `x2` `y2` `duration`(默认 300) | `pkg` |
| `text` | `text` `submit`(bool) | `ok`、`method`（`setText` 或 `clipboard`） |
| `key` | `name`（back/home/recents/enter/delete/volume_up/volume_down） | `ok` |
| `open_app` | `package` 或 `name` | `launched`、`pkg`、`target`、`bounced`、`warning`、`seen` |
| `find_text` | `text` `index` `exact` `tap` | `found`、`x`、`y`、`text`、`tapped` |
| `wait` | `ms` | `ok` |
| `windows` | — | 所有窗口的 `pkg`/`type`/`focused`/`active`/`order` |

### `open_app` 的返回怎么读

- `launched: true, bounced: false` → 成功。
- `launched: true, bounced: true, warning: "..."` → 被系统弹了「要打开 XX 吗」确认框，需要再点一次。
- `launched: false, warning: "..."` → 没起来（包名不存在 / 被系统拦 / 微信这类拒绝被后台唤起）。
- `seen` 里列出桌面上扫到的相关包名，方便判断「用户说的应用到底装没装」。

### 前台窗口怎么判定（避开厂商手势条）

遍历 `getWindows()` 给每个窗口打分：输入法 `−1000`（直接排除）、`focused +200`、`active +60`、
应用窗口(type=1) `+50`、系统窗口(type=3) `−50`、拿不到 root `−400`、再减窗口顺序。
之所以不直接用 `currentPackage()`：vivo 的上滑手势条（`com.vivo.upslide`，type=3）会抢到前台。

### UI 树的采集规则

- **广度优先（BFS）**，深度上限 40：长按菜单这类弹窗挂在很深的层，深度优先会被桌面背景吃光 `max_nodes`。
- 只输出「有信息量」的节点：`text/desc/clickable/editable/selected` 至少一个非空。
- 不可见、零尺寸的节点直接跳过。
- 每个节点带 `depth`，`win` 说明这份树是从哪个窗口读的。

## 7. 结果体

```json
{ "id": "t_1a2b3c4d5e6f", "ok": true, "data": { ... }, "error": null }
```

`ok` 必须是**真布尔**（不是 `"true"`）；`ok=false` 时 `error` 写人话。

## 8. 错误码

| 情况 | 返回 |
| --- | --- |
| token 不对 | `401 {"ok":false}` |
| 手机离线 | 工具层直接拒绝，不排队 |
| 队列满 / 限流 | 工具层拒绝，文案里带「每分钟最多 N 次」 |
| 超时 | 工具层 `{"ok":false,"error":"手机在 N 秒内没有回传结果"}` |
| 包名在黑名单 | 直接拒绝并写审计 `deny_blacklist` |
| 敏感界面 + 未开开关 | 拒绝截图/写操作，写审计 `deny_screenshot` / `deny_sensitive_write` |

## 9. 延迟构成（排查用）

一次动作 ≈ **2 个 RTT**（下发 + 回传）+ 手机端执行时间 + 截图传输时间。
所以：局域网 2–10ms 基本无感；Tailscale 中继 60–150ms 有明显顿感；截图 150KB 在慢链路上要 1–2 秒。
控制长轮询的 `POLL_WAIT_S` 默认 12 秒，既保证「有任务立刻返回」，又低于各种反向代理的 30–100 秒超时。
