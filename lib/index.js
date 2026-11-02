// 次元门 DSH 插件 · 宿主侧入口。
//
// 它做的事情很窄：确保本机 daemon 在跑，然后把 11 个 phone_* 工具注册进 Agent。
// 真正的手机逻辑（队列、长轮询、安全策略、审计、控制台）都还在 Python daemon 里，
// 和 AstrBot 插件版共用同一份实现——所以两个版本的行为永远一致。

import { existsSync, mkdirSync, realpathSync } from 'node:fs'
import { homedir, networkInterfaces } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  callDaemon,
  detectPython,
  isDaemonUp,
  readConfig,
  readLocalToken,
  startDaemon,
  waitForDaemon,
} from './daemon.js'
import { createTools } from './tools.js'

export const name = 'dimensional-portal'

/** 工具注册表；缺了它 cordis 会因为 inject 不满足而不激活本插件。 */
export const inject = ['tools']

const DEFAULTS = {
  /** daemon 的监听地址（daemon 只应该被本机访问）。 */
  host: '127.0.0.1',
  /** daemon 的端口；AstrBot 版占 8765，这里默认 8766。 */
  port: 8766,
  /** 指定 python 解释器；空 = 自动找（需要能 import aiohttp）。 */
  python: '',
  /** daemon 没在跑时是否自动拉起。 */
  autostart: true,
  /** 运行数据目录（dp.json / 截图 / 审计日志都放这里）；空 = 插件目录下的 data/。 */
  dataDir: '',
  /** dp.json 的路径；空 = <dataDir>/dp.json。 */
  configPath: '',
  /** 等 daemon 上线的上限（毫秒）。 */
  startTimeoutMs: 20000,
}

/** 猜一个手机能访问到的本机 IPv4（跳过回环和虚拟网卡里明显不是局域网的）。 */
function lanAddress() {
  try {
    for (const list of Object.values(networkInterfaces())) {
      for (const info of list || []) {
        if (info.family !== 'IPv4' || info.internal) continue
        const ip = info.address
        if (ip.startsWith('169.254.')) continue
        return ip
      }
    }
  } catch {
    // 拿不到就返回空，面板会退回 127.0.0.1
  }
  return ''
}

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Partial<typeof DEFAULTS>} [config]
 */
export function apply(ctx, config = {}) {
  const cfg = { ...DEFAULTS, ...(config || {}) }
  // pnpm 可能把包拷进 node_modules，realpath 一下，保证拿到的是包真正所在的目录
  const pkgRoot = realpathSync(fileURLToPath(new URL('..', import.meta.url)))
  // 仓库里已经有 dp.json（开发态/手工跑过）= 沿用它的配置和 data/；
  // 从 npm 装出来的包没有这个文件 = 数据放进 DSH home，升级插件不会把它删掉。
  const legacyConfig = join(pkgRoot, 'dp.json')
  const hasLegacy = existsSync(legacyConfig)
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
  const dataDir = cfg.dataDir || (hasLegacy ? join(pkgRoot, 'data') : join(dshHome, 'data', 'dimensional-portal'))
  const configPath =
    cfg.configPath ||
    (cfg.dataDir ? join(cfg.dataDir, 'dp.json') : hasLegacy ? legacyConfig : join(dataDir, 'dp.json'))
  const script = join(pkgRoot, 'tools', 'dp_daemon.py')

  mkdirSync(dataDir, { recursive: true })

  let child = null
  let starting = null
  const log = (msg) => {
    try {
      ctx.logger?.info?.(`[dimensional-portal] ${msg}`)
    } catch {
      // 宿主没有 logger 就安静点，不要把工具调用带崩
    }
  }

  /** dp.json 里的 listen_port 才是 daemon 真正监听的端口；没配就用插件配置的。 */
  function activePort() {
    const port = Number(readConfig(configPath).listen_port)
    return Number.isFinite(port) && port > 0 ? port : cfg.port
  }

  /** 保证 daemon 在线；返回是否可用。并发调用共用同一次启动。 */
  async function ensureDaemon() {
    if (await isDaemonUp(cfg.host, activePort())) return true
    if (!cfg.autostart) return false
    if (starting) return starting
    starting = (async () => {
      const python = detectPython(cfg.python)
      if (!python) {
        log('找不到 python 解释器：装上 Python 3.10+（或配置 python 路径）后重试')
        return false
      }
      if (!existsSync(script)) {
        log(`插件里找不到 daemon 脚本：${script}`)
        return false
      }
      log(`拉起 daemon：${python} ${script}`)
      const started = startDaemon({
        python,
        script,
        cwd: pkgRoot,
        env: {
          DP_CONFIG: configPath,
          DP_DATA_DIR: join(dataDir, 'runtime'),
          DP_LOG_FILE: join(dataDir, 'daemon.log'),
        },
        onLog: log,
      })
      if (started.error) {
        log(started.error)
        return false
      }
      child = started.child
      const up = await waitForDaemon({ host: cfg.host, port: activePort(), timeoutMs: cfg.startTimeoutMs })
      if (!up) log('daemon 没能在限定时间内起来，看看 data/daemon.log')
      return up
    })()
    try {
      return await starting
    } finally {
      starting = null
    }
  }

  /** 工具层唯一的出口：POST /local/tool，把结果压成文本。 */
  async function call(tool, args, timeoutMs) {
    const up = await ensureDaemon()
    if (!up) {
      return {
        ok: false,
        error:
          `本机 daemon 没起来（${cfg.host}:${activePort()}）。` +
          '装上 Python 3.10+ 和 aiohttp 后重试，或者先手动跑 tools/dp_daemon.py。',
      }
    }
    const res = await callDaemon({
      host: cfg.host,
      port: activePort(),
      token: readLocalToken(configPath),
      method: 'POST',
      path: '/local/tool',
      body: { tool, args: args ?? {} },
      timeoutMs: timeoutMs ?? 60000,
    })
    if (!res.ok) return { ok: false, error: res.error }
    const data = res.data
    if (!data?.ok) {
      const reason = String(data?.error || '调用失败')
      if (reason === 'unauthorized') {
        return {
          ok: false,
          error:
            `${cfg.host}:${activePort()} 上已经有一个 daemon，但它的 local_token 和本插件读到的对不上。` +
            '多半是旧实例（手工跑的，或者 AstrBot 版）还占着端口：先把它停掉，再让插件自己拉起。',
        }
      }
      return { ok: false, error: reason }
    }
    const result = data.result
    return {
      ok: true,
      text: typeof result === 'string' ? result : JSON.stringify(result, null, 2),
    }
  }

  const disposers = createTools(call).map((definition) => ctx.tools.register(definition))
  log(`已注册 ${disposers.length} 个 phone_* 工具`)

  /** 给侧栏面板读的一份"连接信息"：地址、状态、手机 token、控制台链接与口令。 */
  async function buildInfo() {
    const up = await isDaemonUp(cfg.host, activePort())
    const conf = readConfig(configPath)
    const port = activePort()
    const lan = lanAddress()
    const reachable = lan || cfg.host
    const adminToken = String(conf.admin_token || '')
    const consolePassword = String(conf.console_password || '')
    return {
      ok: true,
      online: up,
      daemon: `http://${cfg.host}:${port}`,
      phoneBase: `http://${reachable}:${port}`,
      lanIP: lan,
      port,
      token: String(conf.token || ''),
      adminToken,
      consolePassword,
      readonlyPassword: String(conf.readonly_password || ''),
      consoleUrl: adminToken ? `http://${reachable}:${port}/console?k=${adminToken}` : `http://${reachable}:${port}/console`,
      dataDir,
      configPath,
      note: up ? '' : 'daemon 还没起来：调一次 phone_status 会把它拉起，或者看 data/daemon.log。',
    }
  }

  const webDisposers = []
  try {
    const webServer = ctx.get('webServer')
    if (webServer?.register) {
      webDisposers.push(
        webServer.register({
          kind: 'exact',
          path: '/dimensional-portal/info',
          handler: async (_request, response) => {
            let body
            try {
              body = await buildInfo()
            } catch (err) {
              body = { ok: false, error: String(err?.message || err) }
            }
            const text = JSON.stringify(body)
            response.statusCode = 200
            response.setHeader('Content-Type', 'application/json; charset=utf-8')
            response.setHeader('Cache-Control', 'no-store')
            response.end(text)
          },
        }),
      )
    }
  } catch (err) {
    log(`没挂上信息路由（宿主没有 webServer）：${err?.message || err}`)
  }

  ctx.on('dispose', () => {
    for (const dispose of webDisposers) {
      try {
        dispose()
      } catch {
        // ignore
      }
    }
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        // 宿主正在卸载，忽略
      }
    }
    if (child) {
      try {
        child.kill()
      } catch {
        // 已经退了
      }
      child = null
    }
  })
}
