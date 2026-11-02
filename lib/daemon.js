// 次元门 DSH 插件 · 与本机 daemon 打交道的底层工具。
//
// 架构：DSH 插件 ──HTTP(回环, X-Local-Token)──> dp_daemon.py(:8766) <──长轮询── 手机
// 这里只负责：找 python、拉起 daemon、读写 dp.json 里的 local_token、发请求。

import { spawn, spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'

/** 读 dp.json 里的 local_token；文件不在/格式坏就给空串。 */
export function readLocalToken(configPath) {
  try {
    if (!existsSync(configPath)) return ''
    const raw = JSON.parse(readFileSync(configPath, 'utf8'))
    return typeof raw?.local_token === 'string' ? raw.local_token : ''
  } catch {
    return ''
  }
}

export function readConfig(configPath) {
  try {
    if (!existsSync(configPath)) return {}
    return JSON.parse(readFileSync(configPath, 'utf8'))
  } catch {
    return {}
  }
}

function baseUrl(host, port) {
  return `http://${host}:${port}`
}

/** 一个很轻的 GET，用来判断 daemon 是否已经活着。 */
export async function isDaemonUp(host, port, timeoutMs = 1200) {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  try {
    const res = await fetch(`${baseUrl(host, port)}/local/state`, {
      headers: { 'X-Local-Token': 'ping' },
      signal: ac.signal,
    })
    // 401 也算活着（只是 token 不对），只要不是连接失败。
    return res.status === 200 || res.status === 401 || res.status === 403
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

/** 统一请求：返回 { ok, data, error }。 */
export async function callDaemon({ host, port, token, method = 'GET', path, body, timeoutMs = 60000 }) {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  try {
    const res = await fetch(`${baseUrl(host, port)}${path}`, {
      method,
      headers: {
        'X-Local-Token': token || '',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json; charset=utf-8' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: ac.signal,
    })
    const text = await res.text()
    let data
    try {
      data = JSON.parse(text)
    } catch {
      return { ok: false, error: `daemon 返回的不是 JSON（HTTP ${res.status}）：${text.slice(0, 200)}` }
    }
    if (!res.ok && !data?.error) {
      return { ok: false, error: `HTTP ${res.status}：${text.slice(0, 200)}` }
    }
    return { ok: true, data }
  } catch (err) {
    const reason = err?.name === 'AbortError' ? `超时（${timeoutMs}ms）` : String(err?.cause?.code || err?.message || err)
    return {
      ok: false,
      error:
        `连不上本机 daemon（${baseUrl(host, port)}）：${reason}。` +
        '确认 daemon 在跑，或把插件的 autostart 打开。',
    }
  } finally {
    clearTimeout(timer)
  }
}

/** 依次试几个候选解释器，返回第一个能跑的。 */
export function detectPython(preferred) {
  const candidates = []
  if (preferred) candidates.push(preferred)
  if (process.platform === 'win32') {
    candidates.push('python', 'python3', 'py')
  } else {
    candidates.push('python3', 'python')
  }
  for (const bin of candidates) {
    try {
      const probe = spawnSync(bin, ['--version'], { encoding: 'utf8', timeout: 8000 })
      if (probe.status === 0) return bin
    } catch {
      // 试下一个
    }
  }
  return ''
}

/**
 * 拉起 daemon。返回 { child } 或 { error }。
 * env 里带上 DP_CONFIG / DP_DATA_DIR / DP_LOG_FILE，把运行时数据放进插件的数据目录。
 */
export function startDaemon({ python, script, cwd, env, onLog }) {
  if (!existsSync(script)) {
    return { error: `插件里找不到 daemon 脚本：${script}` }
  }
  let child
  try {
    child = spawn(python, [script], {
      cwd,
      env: { ...process.env, ...env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
  } catch (err) {
    return { error: `启动 daemon 失败：${err?.message || err}` }
  }
  const relay = (chunk) => {
    const text = String(chunk).trim()
    if (text) onLog?.(text)
  }
  child.stdout?.on('data', relay)
  child.stderr?.on('data', relay)
  child.on('error', (err) => onLog?.(`[daemon] 进程错误：${err?.message || err}`))
  return { child }
}

/** 轮询等 daemon 上线（第一次启动要生成 dp.json，可能几百毫秒）。 */
export async function waitForDaemon({ host, port, timeoutMs = 20000, intervalMs = 400 }) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await isDaemonUp(host, port)) return true
    await new Promise((r) => setTimeout(r, intervalMs))
  }
  return false
}
