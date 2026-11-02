// 客户端面板冒烟：不需要浏览器。
// 用一个假的 window.__ModuleLoader__ 把 client.js 装进去，再拿假的 ctx 调 apply，
// 看它有没有把「面板」和「侧栏行」注册上。

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'client.js'), 'utf8')

let loaded = null
const reactStub = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  useState: (v) => [v, () => {}],
  useEffect: () => {},
  useCallback: (fn) => fn,
  Fragment: 'Fragment',
}

const sandbox = {
  window: {
    __ModuleLoader__: {
      load({ id, factory }) {
        loaded = { id, exports: factory((name) => (name === 'react' ? reactStub : {})) }
      },
    },
  },
  console,
  fetch: async () => ({ json: async () => ({ ok: true }) }),
  navigator: { clipboard: { writeText: () => {} } },
  setTimeout,
}
vm.createContext(sandbox)
vm.runInContext(source, sandbox)

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

check('bundle 用 dimensional-portal 这个 id 注册', () => assert.equal(loaded?.id, 'dimensional-portal'))
check('导出了 apply', () => assert.equal(typeof loaded.exports.apply, 'function'))

const slots = []
const ctx = {
  slots: {
    register(spec, render) {
      slots.push({ spec, render })
      return () => {}
    },
    inject() {
      // 真实宿主会等 slot 出现再回调；这里直接当作已就绪
      return () => {}
    },
  },
  effect() {},
}

loaded.exports.apply(ctx)

const main = slots.find((s) => s.spec.name === 'main')
const row = slots.find((s) => s.spec.name === 'sidebar.panellist')
check('注册了 main 面板', () => assert.ok(main, '没有 main'))
check('面板 key 与侧栏行 id 对得上', () => assert.equal(main.spec.key, row?.spec.id))
check('侧栏行有中文名', () => assert.equal(row.spec.label(), '次元门'))
check('侧栏行能渲染出图标', () => {
  const el = row.render({ size: 18 })
  assert.equal(typeof el.type, 'function', '侧栏行应该渲染一个组件')
  const svg = el.type({ size: 18 })
  assert.equal(svg.type, 'svg')
})
check('面板能渲染成 React 元素', () => {
  const el = main.render()
  assert.ok(el, '面板渲染返回空')
})

console.log('')
console.log(`通过 ${passed} 项，失败 ${failed} 项`)
process.exitCode = failed === 0 ? 0 : 1
