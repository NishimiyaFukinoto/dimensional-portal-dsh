# 上架 DSH 插件市场

目标：在 DSH 的 **插件市场（Plugins → Plugin Market）** 里搜得到、点一下就装。

---

## 市场的收录机制（先说清楚，免得白折腾）

市场（`dshmarket`）自己不接受投稿，它读的是精选目录
[awesome-dsh-plugin/awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin)：

```
npm 上的插件包  ──┐
                  ├─→ awesome-dsh-plugin 列表（人工审核的 PR）──→ index.json ──→ 市场里的一张卡片
GitHub 仓库     ──┘
```

所以「能被看见」要同时满足两件事：

1. **有一个能装的包**：npm 上的 `dsh-*` 包，或 GitHub 仓库里带 `dsh` 字段的包（市场会去读它的 package.json）。
2. **在目录里有一条**：向 awesome-dsh-plugin 提 PR，加一条条目。合并后站点和市场通常一天内自动收录。

> 收录 ≠ 背书。第三方代码，装之前自己看一眼。

---

## 本仓库已经准备好的东西

| 文件 | 作用 |
| --- | --- |
| `package.json` | `name` / `version` / `keywords` 含 `dsh-plugin` / `engines.dsh` / `dsh.bundle.patch` |
| `cordis.patch.yml` | 把插件插进 profile 的层栈（`id: dimensional-portal`） |
| `lib/index.js` | 宿主侧入口：确保 daemon 在跑，注册 11 个 `phone_*` 工具 |
| `lib/tools.js` | 11 个工具定义（名称、描述、参数 schema、超时） |
| `lib/daemon.js` | 找 python、拉起 `tools/dp_daemon.py`、读写 `dp.json`、HTTP 转发 |
| `scripts/plugin_smoke.mjs` | 不需要真机的插件层冒烟（假 daemon，8 项断言） |
| `tools/ dp/ bridge/ safety/ phone/` | 和 AstrBot 版共用的 daemon 与手机脚本，随包一起发 |

装好之后 DSH 里会出现 11 个工具：`phone_status` `phone_see` `phone_ui` `phone_tap`
`phone_long_press` `phone_swipe` `phone_type` `phone_key` `phone_open_app` `phone_find_tap` `phone_stop`。

**运行时依赖**：Python 3.10+ 和一个 `aiohttp`。插件会自动找 `python`，找不到会给出可读的错误；
`dp.json`、截图、审计日志都落在插件目录的 `data/` 下（可用 `dataDir` 改）。

---

## 三步上架

### ① 检查打包内容

```bash
node scripts/plugin_smoke.mjs        # 8 项，必须全绿
npm pack --dry-run                   # 看看到底会发哪些文件
```

### ② 发到 npm（需要账号）

```bash
npm login
npm publish --access public
```

包名 `dsh-dimensional-portal` 目前空着（2026-11-02 查过，registry 返回 404）。

### ③ 向 awesome-dsh-plugin 提 PR

在 [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 的列表里加**一条**，
按该仓库现有条目的格式写（它是一份人工整理的列表，照抄相邻条目的字段即可）。要准备的内容：

```yaml
name: dsh-dimensional-portal
title: <你的GitHub用户名>/dimensional-portal-dsh
description_zh: >-
  让 DSH 里的 Agent 直接操作你的安卓手机：看屏幕、点按滑动、打字、开应用、按文字找控件。
  手机端是 AutoX.js 脚本，主动长轮询你的电脑，不开公网端口、不经过任何中转服务器；
  截图只落在本机，敏感界面默认不截、不允许点击。共 11 个工具，附带浏览器控制台与应用访问白名单。
description_en: >-
  Let the DSH agent drive a real Android phone: screenshots, taps, swipes, typing, app launching.
  The phone runs an AutoX.js script that long-polls your machine — no public port, no relay server.
  Screenshots stay local; sensitive screens are not captured or clicked by default.
repository: https://github.com/<你>/dimensional-portal-dsh
category: remote          # 也可用 tools
keywords: [android, phone, automation, autox]
engines.dsh: ">=0.1.2-alpha.4 <0.3.0-0"
```

技术口径（写进 PR 描述里，审核的人会看）：

- **不联网作案**：daemon 只监听本机/Tailscale，手机主动连进来；没有云端。
- **双密钥**：手机用 `token`，本机工具通道另用 `local_token`，且**只接受回环地址**。
- **默认最保守**：敏感界面（微信/支付宝/银行/设置）默认不截图、不允许写操作，需要显式打开开关。
- **可审计**：每个动作一行 `audit.jsonl`，控制台能翻；`phone_stop` 一键清队列。

### ④（可选）GitHub Release 兜底

市场也认 GitHub 上的包：打一个 `npm pack` 出来的 `.tgz` 挂到 Release，
条目的 `source` 写成那个 tarball 链接，就不依赖 npm 了。

---

## 市场卡片的几个字段真实从哪来

| 市场里显示 | 来源 |
| --- | --- |
| 插件名 | `package.json` 的 `name` |
| 一行简介（中/英） | 目录条目里的 `description.zh` / `description.en` |
| 兼容性标签 | `package.json` 的 `engines.dsh`（或 peerDependencies 里的 `@deepseek-ai/dsh-*`） |
| 分类 | 目录条目里的 `metadata.category` |
| 星数/更新 | GitHub 仓库 + npm 版本 |

---

## 还没做的

- **客户端面板**：现在只有工具，没有 DSH 侧边栏卡片。手机控制台还是浏览器开
  `http://<本机>:8766/console?k=…`。要做成插件面板需要 `dsh.client` 字段 + 前端 bundle。
- **截图预览**：`phone_see` 返回路径，让 Agent 自己 `read`。市场里同类插件常见做法是
  直接把图片塞进工具结果，这个要等工具返回值支持 media block 再改。
- **真机回归**：手机换上 8766 的脚本后，跑一遍 `phone_see → phone_tap → phone_type`。
