/**
 * 次元门 · 客户端面板（手写 bundle，不经过构建）。
 *
 * 宿主用 window.__ModuleLoader__.load 加载它，然后调 apply(ctx)。
 * 这里做两件事：注册一个 `main` 面板（侧栏点「次元门」打开），
 * 以及那条侧栏行本身。数据来自服务端路由 GET /dimensional-portal/info。
 */
window.__ModuleLoader__.load({
  id: 'dimensional-portal',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')
    const h = React.createElement

    const PANEL_ID = 'dimensional-portal'
    const NS = 'dimensional-portal'
    const INFO_URL = '/dimensional-portal/info'

    const C = {
      text: 'var(--dsh-text-primary, #e6e8eb)',
      dim: 'var(--dsh-text-secondary, #9aa4ad)',
      line: 'var(--dsh-border-subtle, rgba(255,255,255,0.12))',
      card: 'var(--dsh-surface-raised, rgba(255,255,255,0.04))',
      accent: 'var(--dsh-accent, #5b8def)',
    }

    const box = {
      padding: '28px 32px',
      maxWidth: 860,
      margin: '0 auto',
      color: C.text,
      fontFamily: 'inherit',
      fontSize: 14,
      lineHeight: 1.65,
    }
    const card = {
      border: `1px solid ${C.line}`,
      background: C.card,
      borderRadius: 12,
      padding: '16px 18px',
      marginBottom: 14,
    }
    const label = { color: C.dim, width: 116, flex: '0 0 116px', fontSize: 13 }
    const row = { display: 'flex', gap: 12, alignItems: 'flex-start', padding: '7px 0' }
    const mono = {
      fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
      fontSize: 13,
      wordBreak: 'break-all',
    }
    const btn = {
      marginLeft: 8,
      padding: '2px 9px',
      borderRadius: 6,
      border: `1px solid ${C.line}`,
      background: 'transparent',
      color: C.dim,
      cursor: 'pointer',
      fontSize: 12,
    }
    const link = { color: C.accent, textDecoration: 'underline', cursor: 'pointer' }

    function useInfo() {
      const [state, setState] = React.useState({ loading: true, data: null, error: '' })
      const load = React.useCallback(() => {
        setState((s) => ({ ...s, loading: true }))
        fetch(INFO_URL, { cache: 'no-store', credentials: 'same-origin' })
          .then((r) => r.json())
          .then((data) => setState({ loading: false, data, error: data?.ok === false ? String(data.error || '读取失败') : '' }))
          .catch((err) => setState({ loading: false, data: null, error: String(err?.message || err) }))
      }, [])
      React.useEffect(() => {
        load()
      }, [load])
      return [state, load]
    }

    function Copy({ value, label: text }) {
      const [done, setDone] = React.useState(false)
      if (!value) return null
      return h(
        'button',
        {
          style: btn,
          title: '复制',
          onClick: () => {
            try {
              navigator.clipboard.writeText(value)
              setDone(true)
              setTimeout(() => setDone(false), 1200)
            } catch (e) {
              // 老浏览器没有 clipboard，就不提示
            }
          },
        },
        done ? '已复制' : text || '复制',
      )
    }

    function Field({ name, children }) {
      return h('div', { style: row }, h('div', { style: label }, name), h('div', { style: { flex: 1, minWidth: 0 } }, children))
    }

    function Panel() {
      const [state, reload] = useInfo()
      const d = state.data || {}
      const online = d.online === true

      const status = state.error
        ? h('span', { style: { color: '#e0774f' } }, '读取失败')
        : state.loading
          ? h('span', { style: { color: C.dim } }, '读取中…')
          : h(
              'span',
              { style: { color: online ? '#3fb950' : '#c98500' } },
              online ? '● 连接正常' : '● daemon 未运行',
            )

      return h(
        'div',
        { style: box },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 4 } },
          h('div', { style: { fontSize: 19, fontWeight: 600 } }, '次元门'),
          status,
          h('div', { style: { flex: 1 } }),
          h('button', { style: btn, onClick: reload }, state.loading ? '…' : '刷新'),
        ),
        h('div', { style: { color: C.dim, fontSize: 13, marginBottom: 18 } },
          '让 DSH 里的 Agent 操作你的安卓手机。手机端脚本主动连回这台电脑，不开公网端口。',
        ),

        state.error ? h('div', { style: { ...card, color: '#e0774f' } }, state.error) : null,

        h('div', { style: card },
          h('div', { style: { fontWeight: 600, marginBottom: 8 } }, '手机接这里'),
          h(Field, { name: '手机端地址' },
            h('span', { style: mono }, d.phoneBase || '—'),
            h(Copy, { value: d.phoneBase }),
          ),
          h(Field, { name: '手机 token' },
            h('span', { style: mono }, d.token || '—'),
            h(Copy, { value: d.token }),
          ),
          h(Field, { name: '填到手机脚本' },
            h('span', { style: { ...mono, color: C.dim } }, "BASES 填上面的地址，TOKEN 填上面的 token；改完在 AutoX.js 里重新运行脚本。"),
          ),
        ),

        h('div', { style: card },
          h('div', { style: { fontWeight: 600, marginBottom: 8 } }, '控制台'),
          h(Field, { name: '打开控制台' },
            d.consoleUrl
              ? h('a', { href: d.consoleUrl, target: '_blank', rel: 'noreferrer', style: link }, d.consoleUrl)
              : h('span', { style: C.dim }, 'daemon 起来后才有'),
            h(Copy, { value: d.consoleUrl }),
          ),
          h(Field, { name: '控制台口令' },
            h('span', { style: mono }, d.adminToken || '—'),
            h(Copy, { value: d.adminToken }),
          ),
          d.consolePassword
            ? h(Field, { name: '登录口令' }, h('span', { style: mono }, d.consolePassword), h(Copy, { value: d.consolePassword }))
            : null,
          d.readonlyPassword
            ? h(Field, { name: '只读口令' }, h('span', { style: mono }, d.readonlyPassword), h(Copy, { value: d.readonlyPassword }))
            : null,
          h('div', { style: { color: C.dim, fontSize: 12, marginTop: 6 } },
            '口令在 dp.json 里；控制台链接里的 k= 就是控制台口令，等于钥匙，别转发给别人。',
          ),
        ),

        h('div', { style: card },
          h('div', { style: { fontWeight: 600, marginBottom: 8 } }, '本机'),
          h(Field, { name: 'daemon' }, h('span', { style: mono }, d.daemon || '—')),
          h(Field, { name: '配置文件' }, h('span', { style: mono }, d.configPath || '—'), h(Copy, { value: d.configPath })),
          h(Field, { name: '数据目录' }, h('span', { style: mono }, d.dataDir || '—'), h(Copy, { value: d.dataDir })),
          d.note ? h(Field, { name: '提示' }, h('span', { style: { color: C.dim } }, d.note)) : null,
        ),
      )
    }

    /** 侧栏那一行的图标：一扇门。 */
    function RowIcon(owner = {}) {
      const size = owner.size || 16
      return h(
        'svg',
        { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true },
        h('path', {
          d: 'M6 4.5h12v15H6z',
          stroke: 'currentColor',
          strokeWidth: 1.6,
          strokeLinejoin: 'round',
        }),
        h('path', { d: 'M6 19.5c0-2 2.7-3.2 6-3.2s6 1.2 6 3.2', stroke: 'currentColor', strokeWidth: 1.4 }),
        h('circle', { cx: 15.4, cy: 11.2, r: 1.1, fill: 'currentColor' }),
      )
    }

    function apply(ctx) {
      const disposers = []
      try {
        disposers.push(
          ctx.slots.register(
            { name: 'main', key: PANEL_ID, locale: NS, inject: () => ({}) },
            () => h(Panel),
          ),
        )
      } catch (err) {
        console.warn('[dimensional-portal] 注册面板失败', err)
      }
      let rowMounted = false
      const mountRow = () => {
        if (rowMounted) return
        rowMounted = true
        try {
          disposers.push(
            ctx.slots.register(
              { name: 'sidebar.panellist', id: PANEL_ID, order: 2, label: () => '次元门', locale: NS },
              (owner) => h(RowIcon, owner),
            ),
          )
        } catch (err) {
          console.warn('[dimensional-portal] 注册侧栏行失败', err)
        }
      }
      // 先试着直接挂（槽位已经存在的情况）；宿主晚一点才声明 `sidebar.panellist` 时，
      // 下面这个 inject 会再补一次（mountRow 里有幂等保护）。
      mountRow()
      try {
        ctx.slots.inject('sidebar.panellist', mountRow)
      } catch {
        // 没有这个槽位就不挂，面板仍然可用
      }
      try {
        ctx.effect?.(() => () => {
          for (const dispose of disposers) {
            try {
              if (typeof dispose === 'function') dispose()
            } catch {
              // ignore
            }
          }
        })
      } catch {
        // 没有 effect 就算了
      }
    }

    exports.apply = apply
    exports.inject = []
    return module.exports
  },
})
