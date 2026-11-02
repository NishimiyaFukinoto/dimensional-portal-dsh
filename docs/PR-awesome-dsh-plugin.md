# 给 awesome-dsh-plugin 的 PR（现成文案）

> 目标仓库：https://github.com/awesome-dsh-plugin/awesome-dsh-plugin
> 做法：Fork → 在它的插件列表里加一条（照抄相邻条目的格式）→ 提 PR。
> 合并后站点与 [dshmarket](https://github.com/dsh-market/dsh-market) 通常一天内自动收录。

---

## 前置：包得先在 npm 上

```bash
cd F:/大肥鱼/dimensional-portal-dsh
npm login                     # 需要你的 npm 账号
npm pack --dry-run            # 先看一遍：29 个文件、不含 dp.json / *.local.js
npm publish --access public
```

包名 `dsh-dimensional-portal@0.2.0`（2026-11-02 查过 registry：未被占用）。

---

## PR 标题

```
Add dsh-dimensional-portal: drive a real Android phone from DSH
```

## PR 正文

```markdown
### What it is

`dsh-dimensional-portal` lets the DSH agent operate a real Android phone:
screenshots, taps, swipes, typing, opening apps, finding text on screen.

The phone runs a small AutoX.js script (no root) that **long-polls your machine** —
no public port, no relay server, no cloud. The screenshot lands in `data/shots/` on
your own disk and is read by the agent; nothing is uploaded anywhere.

11 tools: `phone_status` `phone_see` `phone_ui` `phone_tap` `phone_long_press`
`phone_swipe` `phone_type` `phone_key` `phone_open_app` `phone_find_tap` `phone_stop`.

### Why it is safe by default

- **Loopback only for the tool channel**: the daemon accepts phone traffic on your LAN /
  Tailscale, while the local tool channel needs a second token (`local_token`) **and** a
  loopback source address.
- **Sensitive screens are off by default**: WeChat / Alipay / banking / system settings are
  not captured and not clickable until you explicitly flip a switch.
- **Auditable**: every action is one line in `audit.jsonl`; a browser console shows status,
  per-app allow/block lists, and a stop button.
- **No model on the phone**: the agent in DSH reads the screenshot; the phone only executes.

### Runtime

Needs Python 3.10+ with `aiohttp` (the bundled daemon). The plugin auto-starts it and
stores `dp.json`, screenshots, audit log under `~/.dsh/data/dimensional-portal/`.
A sidebar panel shows the phone's connect address, token, console link and passwords.

- Repo: https://github.com/<你的用户名>/dimensional-portal-dsh
- npm: dsh-dimensional-portal
- License: MIT
```

---

## 要填的条目字段

| 字段 | 值 |
| --- | --- |
| `name` | `dsh-dimensional-portal` |
| `repository` | `https://github.com/<你的用户名>/dimensional-portal-dsh` |
| `description.zh` | 让 DSH 里的 Agent 直接操作你的安卓手机：看屏幕、点按滑动、打字、开应用、按文字找控件。手机端是 AutoX.js 脚本，主动长轮询你的电脑，不开公网端口、不经过任何中转；截图只落在本机，敏感界面默认不截、不允许点击。11 个工具，附带浏览器控制台与应用访问白名单。 |
| `description.en` | Let the DSH agent drive a real Android phone: screenshots, taps, swipes, typing, app launching. The phone long-polls your machine — no public port, no relay. Screenshots stay local; sensitive screens are not captured or clicked by default. |
| `category` | `remote`（移动端/远程） |
| `keywords` | `android`, `phone`, `automation`, `autox` |
| `engines.dsh` | `>=0.1.2-alpha.4 <0.3.0-0` |

---

## 提交前最后一眼

- [ ] `npm pack --dry-run` 里没有 `dp.json`、`*.local.js`、`__pycache__`
- [ ] `node scripts/plugin_smoke.mjs` 14 项全绿
- [ ] `node scripts/client_smoke.mjs` 7 项全绿
- [ ] README 里贴了手机端配置步骤（BASES / TOKEN / 三个权限）
- [ ] 仓库是 public，license 是 MIT
