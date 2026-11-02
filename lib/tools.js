// 次元门 DSH 插件 · 11 个工具的定义。
//
// 每个工具只做两件事：把参数打包成 daemon 认识的形状，把 daemon 回的文本交回去。
// 真正的逻辑（安全策略、队列、审计）都在 daemon 里，和 AstrBot 版共用一套。

/** 工具统一返回 { text }，render 里再变成一段 text block。 */
function textResult(text) {
  return { text: String(text ?? '') }
}

const output = {
  schema: {
    type: 'object',
    properties: { text: { type: 'string' } },
    required: ['text'],
    additionalProperties: false,
  },
  render(_args, value) {
    return [{ type: 'text', text: value?.text ?? '' }]
  },
}

/**
 * @param {(tool: string, args?: object, timeoutMs?: number) => Promise<{ok: boolean, text?: string, error?: string}>} call
 */
export function createTools(call) {
  /** 把一次转发压成文字；失败也返回文字，让模型能读懂并自救。 */
  const run = async (tool, args, timeoutMs) => {
    const res = await call(tool, args, timeoutMs)
    if (!res.ok) return textResult(`❌ ${res.error || '调用失败'}`)
    return textResult(res.text)
  }

  return [
    {
      name: 'phone_status',
      description:
        '查看手机连接状态：是否在线、电量、分辨率、当前应用、无障碍/截图权限。不需要手机在线也能调用；掉线时用它排查（会告诉你该检查什么）。',
      parameters: { type: 'object', properties: {}, required: [] },
      output,
      timeoutMs: 20000,
      async execute() {
        return run('phone_status', {}, 20000)
      },
    },
    {
      name: 'phone_see',
      description:
        '看一眼手机现在显示什么：截一张图保存到本机，返回图片路径和界面文字。这是最常用的第一个动作——拿到路径后用 read 工具把图读出来看，再用 phone_tap 点坐标。敏感界面（微信/支付宝/银行/设置）默认不截图，只返回控件文字。',
      parameters: {
        type: 'object',
        properties: {
          with_ui: { type: 'boolean', description: '是否同时返回界面文字摘要，默认 true' },
        },
        required: [],
      },
      output,
      timeoutMs: 120000,
      async execute(args) {
        return run('phone_see', { with_ui: args?.with_ui !== false }, 90000)
      },
    },
    {
      name: 'phone_ui',
      description:
        '读手机当前界面的控件树（文字、坐标、是否可点），不截图。适合看不清截图、或敏感界面不方便截图时用。每行给出中心坐标 @(x,y)，可以直接拿去 phone_tap。',
      parameters: {
        type: 'object',
        properties: {
          max_nodes: { type: 'number', description: '最多显示多少个节点，默认 80' },
        },
        required: [],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        const n = Number(args?.max_nodes ?? 80)
        return run('phone_ui', { max_nodes: Number.isFinite(n) ? Math.trunc(n) : 80 }, 45000)
      },
    },
    {
      name: 'phone_tap',
      description: '点击屏幕上一个点。坐标是真实像素，原点在左上角；取自 phone_see 的截图或 phone_ui 的 @(x,y)。',
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'number', description: '横坐标像素' },
          y: { type: 'number', description: '纵坐标像素' },
        },
        required: ['x', 'y'],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        return run('phone_tap', { x: Math.trunc(Number(args.x)), y: Math.trunc(Number(args.y)) }, 45000)
      },
    },
    {
      name: 'phone_long_press',
      description: '长按一个点（默认 800ms），常用于弹出菜单、拖动前抓取。',
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'number', description: '横坐标像素' },
          y: { type: 'number', description: '纵坐标像素' },
          duration_ms: { type: 'number', description: '按住时长，默认 800' },
        },
        required: ['x', 'y'],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        return run(
          'phone_long_press',
          {
            x: Math.trunc(Number(args.x)),
            y: Math.trunc(Number(args.y)),
            duration_ms: Math.trunc(Number(args.duration_ms ?? 800)),
          },
          45000,
        )
      },
    },
    {
      name: 'phone_swipe',
      description: '滑动/滚动屏幕：从 (x1,y1) 划到 (x2,y2)。向上划 = 看下面的内容。',
      parameters: {
        type: 'object',
        properties: {
          x1: { type: 'number', description: '起点横坐标' },
          y1: { type: 'number', description: '起点纵坐标' },
          x2: { type: 'number', description: '终点横坐标' },
          y2: { type: 'number', description: '终点纵坐标' },
          duration_ms: { type: 'number', description: '滑动时长，默认 300' },
        },
        required: ['x1', 'y1', 'x2', 'y2'],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        return run(
          'phone_swipe',
          {
            x1: Math.trunc(Number(args.x1)),
            y1: Math.trunc(Number(args.y1)),
            x2: Math.trunc(Number(args.x2)),
            y2: Math.trunc(Number(args.y2)),
            duration_ms: Math.trunc(Number(args.duration_ms ?? 300)),
          },
          45000,
        )
      },
    },
    {
      name: 'phone_type',
      description:
        '往当前焦点输入框里打字。先 phone_tap 点一下输入框，再调这个。setText 注入失败时手机端会自动退回剪贴板粘贴。',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: '要输入的文字' },
          submit: { type: 'boolean', description: '输完是否立刻回车提交（搜索框/聊天框常用），默认 false' },
        },
        required: ['text'],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        return run('phone_type', { text: String(args.text ?? ''), submit: args.submit === true }, 45000)
      },
    },
    {
      name: 'phone_key',
      description:
        '按系统键：back（返回）/ home（回桌面）/ recents（最近任务）/ enter / delete / volume_up / volume_down。卡在某个页面时用 back 退出。',
      parameters: {
        type: 'object',
        properties: {
          key: { type: 'string', description: '按键名，默认 back' },
        },
        required: [],
      },
      output,
      timeoutMs: 45000,
      async execute(args) {
        return run('phone_key', { key: String(args?.key ?? 'back') }, 30000)
      },
    },
    {
      name: 'phone_open_app',
      description:
        '打开手机上的一个应用。可以给应用名（中文名如「微信」「相机」）或包名（com.tencent.mm）。会等一会儿再返回前台包名，用来确认真的打开了。',
      parameters: {
        type: 'object',
        properties: {
          name: { type: 'string', description: '应用名或包名' },
        },
        required: ['name'],
      },
      output,
      timeoutMs: 90000,
      async execute(args) {
        return run('phone_open_app', { name: String(args.name ?? '') }, 60000)
      },
    },
    {
      name: 'phone_find_tap',
      description:
        '在屏幕上找一段文字并点它（比算坐标稳）。找不到会列出当前界面有哪些文字。',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: '要找的文字，比如「设置」「发送」「允许」' },
          index: { type: 'number', description: '有多个匹配时点第几个，从 0 开始' },
          exact: { type: 'boolean', description: 'true = 文字完全相等；false = 包含即可' },
        },
        required: ['text'],
      },
      output,
      timeoutMs: 60000,
      async execute(args) {
        return run(
          'phone_find_tap',
          {
            text: String(args.text ?? ''),
            index: Math.trunc(Number(args.index ?? 0)),
            exact: args.exact === true,
          },
          45000,
        )
      },
    },
    {
      name: 'phone_stop',
      description: '急停：清空所有还没发出去的任务。用户说「停下」「别动了」时立刻调用。',
      parameters: { type: 'object', properties: {}, required: [] },
      output,
      timeoutMs: 30000,
      async execute() {
        return run('phone_stop', {}, 20000)
      },
    },
  ]
}
