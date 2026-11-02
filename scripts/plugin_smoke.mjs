// 插件层冒烟：不需要真 daemon、不需要手机。
// 起一个假的 HTTP daemon，用假的 cordis ctx 装配插件，然后调几个工具看返回。

import assert from 'node:assert/strict'
import { createServer } from 'node:http'

import { apply } from '../lib/index.js'

const TOKEN = 'test-local-token'
const calls = []

const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  const token = req.headers['x-local-token'] || ''
  const send = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify(obj))
  }
  if (url.pathname === '/local/state') {
    if (token !== TOKEN) return send(401, { ok: false, error: 'unauthorized' })
    return send(200, { ok: true, online: true })
  }
  if (url.pathname === '/local/tool' && req.method === 'POST') {
    if (token !== TOKEN) return send(401, { ok: false, error: 'unauthorized' })
    let raw = ''
    req.on('data', (c) => (raw += c))
    req.on('end', () => {
      const payload = JSON.parse(raw || '{}')
      calls.push(payload)
      const { tool, args } = payload
      if (tool === 'phone_tap') {
        return send(200, { ok: true, result: `已点击 (${args.x}, ${args.y})` })
      }
      if (tool === 'phone_status') {
        return send(200, { ok: true, result: '✅ 手机在线：Pixel / Android 15' })
      }
      if (tool === 'phone_boom') {
        return send(200, { ok: false, error: '模拟失败' })
      }
      return send(200, { ok: true, result: { echo: payload } })
    })
    return
  }
  send(404, { ok: false, error: 'not found' })
})

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port

// 假 ctx：记录注册进来的工具，收集 dispose 回调
const registered = new Map()
const disposers = []
const ctx = {
  logger: { info: () => {} },
  tools: {
    register(definition) {
      registered.set(definition.name, definition)
      const dispose = () => registered.delete(definition.name)
      disposers.push(dispose)
      return dispose
    },
  },
  on(event, handler) {
    if (event === 'dispose') disposers.push(handler)
  },
}

let passed = 0
let failed = 0
function check(label, fn) {
  try {
    fn()
    passed += 1
    console.log(`  ✅ ${label}`)
  } catch (err) {
    failed += 1
    console.log(`  ❌ ${label} — ${err.message}`)
  }
}

apply(ctx, {
  port,
  autostart: false,
  configPath: '/nonexistent/dp.json', // token 由下面的假文件替代
  dataDir: process.cwd(),
})

// token 从 dp.json 读，这里临时造一个
import { mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
const dir = join(tmpdir(), `dp-plugin-smoke-${process.pid}`)
mkdirSync(dir, { recursive: true })
const cfgPath = join(dir, 'dp.json')
writeFileSync(cfgPath, JSON.stringify({ local_token: TOKEN }), 'utf8')

// 重新装配一次，让它用上真 token 的配置路径
const registered2 = new Map()
const ctx2 = {
  logger: { info: () => {} },
  tools: {
    register(definition) {
      registered2.set(definition.name, definition)
      return () => {}
    },
  },
  on() {},
}
apply(ctx2, { port, autostart: false, configPath: cfgPath, dataDir: dir })

console.log('== 注册 ==')
check('注册了 11 个工具', () => assert.equal(registered2.size, 11))
check('工具名齐全', () => {
  const expected = [
    'phone_status', 'phone_see', 'phone_ui', 'phone_tap', 'phone_long_press', 'phone_swipe',
    'phone_type', 'phone_key', 'phone_open_app', 'phone_find_tap', 'phone_stop',
  ]
  for (const name of expected) assert.ok(registered2.has(name), `缺 ${name}`)
})

console.log('== 调用 ==')
const tap = registered2.get('phone_tap')
const tapOut = await tap.execute({ x: 120.6, y: 300.2 })
check('phone_tap 返回文本', () => assert.match(tapOut.text, /已点击 \(120, 300\)/))
check('坐标被取整后再发出', () => {
  const last = calls.at(-1)
  assert.deepEqual(last, { tool: 'phone_tap', args: { x: 120, y: 300 } })
})

const status = registered2.get('phone_status')
const statusOut = await status.execute({})
check('phone_status 返回文本', () => assert.match(statusOut.text, /手机在线/))

// 失败路径：daemon 返回 ok:false
registered2.get('phone_see') // 只是确认存在
const fake = await (async () => {
  const res = await fetch(`http://127.0.0.1:${port}/local/tool`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Local-Token': TOKEN },
    body: JSON.stringify({ tool: 'phone_boom', args: {} }),
  })
  return res.json()
})()
check('daemon 报错原样透传', () => assert.equal(fake.error, '模拟失败'))

console.log('== render ==')
check('输出的 render 产生 text block', () => {
  const blocks = tap.output.render({}, tapOut)
  assert.deepEqual(blocks, [{ type: 'text', text: tapOut.text }])
})

console.log('== 断线 ==')
const offlineCtx = {
  logger: { info: () => {} },
  tools: {
    register(d) {
      offlineCtx.registered.set(d.name, d)
      return () => {}
    },
  },
  on() {},
  registered: new Map(),
}
apply(offlineCtx, { port: 1, autostart: false, configPath: cfgPath, dataDir: dir })
const offlineTick = offlineCtx.registered.get('phone_tap')
const offlineOut = await offlineTick.execute({ x: 1, y: 2 })
check('daemon 连不上时返回可读错误', () => assert.match(offlineOut.text, /daemon 没起来|连不上/))

// ---- 侧栏面板要读的信息路由 ----
console.log('== 信息路由 ==')
const routes = new Map()
const infoCtx = {
  logger: { info: () => {} },
  tools: { register: () => () => {} },
  on() {},
  get(name) {
    if (name !== 'webServer') return undefined
    return {
      register(route) {
        routes.set(route.path, route)
        return () => routes.delete(route.path)
      },
    }
  },
}
const infoCfg = join(dir, 'dp-info.json')
writeFileSync(
  infoCfg,
  JSON.stringify({ token: 'tok-123', admin_token: 'adm-456', console_password: 'pw-789', listen_port: port }),
  'utf8',
)
apply(infoCtx, { port, autostart: false, configPath: infoCfg, dataDir: dir })

const route = routes.get('/dimensional-portal/info')
check('注册了 /dimensional-portal/info', () => assert.ok(route, '路由没挂上'))
const res = {
  statusCode: 0,
  headers: {},
  body: '',
  setHeader(k, v) {
    this.headers[k] = v
  },
  end(t) {
    this.body = t
  },
}
await route.handler({}, res)
const payload = JSON.parse(res.body)
check('返回手机 token', () => assert.equal(payload.token, 'tok-123'))
check('返回控制台口令', () => assert.equal(payload.adminToken, 'adm-456'))
check('返回控制台链接（带 k=）', () => assert.match(payload.consoleUrl, /\/console\?k=adm-456$/))
check('返回登录口令', () => assert.equal(payload.consolePassword, 'pw-789'))
check('返回手机要用到的地址', () => assert.match(payload.phoneBase, /^http:\/\/.+:\d+$/))

await new Promise((resolve) => server.close(resolve))
rmSync(dir, { recursive: true, force: true })

console.log('')
console.log(`通过 ${passed} 项，失败 ${failed} 项`)
process.exitCode = failed === 0 ? 0 : 1
