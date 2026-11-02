// 端到端：真的拉起 Python daemon，走一遍「插件 → daemon → 手机不在线」的链路。
// 不需要手机，也不需要 ComfyUI 那种外部服务。用法：
//   node scripts/e2e_local.mjs [python解释器路径]
// 默认用 AstrBot 实例的 venv（它自带 aiohttp）；换了机器就传一个装了 aiohttp 的解释器。

import { mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { apply } from '../lib/index.js'

const python =
  process.argv[2] ||
  'C:/Users/YUZURU/.astrbot_launcher/instances/9040990f-1e86-46fc-85a6-aee6cd7aa47c/venv/Scripts/python.exe'

if (!existsSync(python)) {
  console.error(`❌ 找不到 python：${python}\n传一个装了 aiohttp 的解释器路径进来：node scripts/e2e_local.mjs <python>`)
  process.exit(2)
}

const dataDir = join(tmpdir(), `dp-e2e-${process.pid}`)
const port = 8799
mkdirSync(dataDir, { recursive: true })
// 先把端口钉住，daemon 起来后会自己补 token
writeFileSync(join(dataDir, 'dp.json'), JSON.stringify({ listen_port: port }, null, 2), 'utf8')

const tools = new Map()
const ctx = {
  logger: { info: (m) => console.log(`  · ${m}`) },
  tools: {
    register(definition) {
      tools.set(definition.name, definition)
      return () => tools.delete(definition.name)
    },
  },
  on(event, handler) {
    if (event === 'dispose') ctx.dispose = handler
  },
  dispose: null,
}

let passed = 0
let failed = 0
const check = (label, fn) => {
  try {
    fn()
    passed += 1
    console.log(`  ✅ ${label}`)
  } catch (err) {
    failed += 1
    console.log(`  ❌ ${label} — ${err.message}`)
  }
}

console.log(`python: ${python}`)
console.log(`data:   ${dataDir}`)
apply(ctx, { python, port, dataDir, autostart: true, startTimeoutMs: 25000 })

console.log('== 真 daemon ==')
const status = await tools.get('phone_status').execute({})
console.log(`  返回：${status.text.split('\n')[0]}`)
check('daemon 起来了并回答了 phone_status', () => {
  if (!/手机离线|手机在线/.test(status.text)) throw new Error(`返回里没有状态：${status.text.slice(0, 120)}`)
})
check('daemon 生成了 dp.json 与 local_token', () => {
  const cfg = JSON.parse(readFileSync(join(dataDir, 'dp.json'), 'utf8'))
  if (!cfg.local_token) throw new Error('dp.json 里没有 local_token')
})

const stop = await tools.get('phone_stop').execute({})
check('phone_stop 能清队列', () => {
  if (!/急停|清空|🛑/.test(stop.text)) throw new Error(`返回不对：${stop.text.slice(0, 120)}`)
})

const bad = await tools.get('phone_tap').execute({ x: 'abc', y: 1 })
check('参数不合法时不崩、给出可读结果', () => {
  if (typeof bad.text !== 'string' || bad.text.length === 0) throw new Error('返回空')
})

// 关掉：dispose 应该带走子进程
try {
  ctx.dispose?.()
} catch (err) {
  console.log(`  · dispose 报错（不影响结论）：${err.message}`)
}
// Windows 上子进程的文件句柄要松一会儿才放得掉，清不掉不算失败
await new Promise((r) => setTimeout(r, 1200))
try {
  rmSync(dataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 })
} catch (err) {
  console.log(`  · 临时目录没清掉（不影响结论）：${err.code || err.message}`)
}

console.log('')
console.log(`通过 ${passed} 项，失败 ${failed} 项`)
process.exitCode = failed === 0 ? 0 : 1
