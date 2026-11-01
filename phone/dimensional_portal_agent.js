/**
 * 次元门 —— AutoX.js 执行器
 * ================================================
 * 手机这端只做三件事：
 *   1. 每 10 秒 POST /hb 报一次心跳（设备信息、电量、前台应用、权限状态）
 *   2. 不停 GET /poll 长轮询要任务
 *   3. 执行任务，POST /result 回传结果
 *
 * 没有模型，没有 AI，只是一个听话的手。大脑在电脑上的 AstrBot 里。
 *
 * 用法：
 *   1. AutoX.js 里新建脚本，把本文件内容粘进去（或把本文件拷到手机后用 AutoX.js 打开）
 *   2. 改下面「配置区」的 BASE 和 TOKEN，TOKEN 必须和插件配置里的一致
 *   3. 运行 → 首次会弹「屏幕录制」授权，点「立即开始」
 *   4. 确认无障碍服务已开启（AutoX.js 会自动检查）
 *   5. 保持脚本运行（通知栏有「脚本运行中」），建议把 AutoX.js 加到电池白名单
 *
 * 协议见 docs/PROTOCOL.md。
 */

// ============================================================ 配置区
// ↓↓↓ 这三处必须改成你自己的，否则跑不起来 ↓↓↓

// 电脑的地址：优先走 Tailscale（手机不在同一 WiFi、甚至用流量也能连），
// 连不上时自动退回局域网地址。换电脑/换网络只改这个数组，第一个填 Tailscale 的 100.x。
// 怎么查电脑的 100.x：电脑上执行 "C:\Program Files\Tailscale\tailscale.exe" ip -4
var BASES = [
    "http://100.x.y.z:8766",       // ← 改成电脑的 Tailscale 地址
    "http://192.168.1.10:8766"     // ← 改成电脑的局域网地址（可选，删掉也行）
];
var baseIndex = 0;
var BASE = BASES[baseIndex];

/** 连续连不上时切到下一个地址（用尽后回到第一个）。 */
function nextBase() {
    if (BASES.length < 2) return false;
    baseIndex = (baseIndex + 1) % BASES.length;
    BASE = BASES[baseIndex];
    log("切换服务器 → " + BASE);
    return true;
}
// 必须和插件配置里的 token 一字不差（在 AstrBot 插件配置里自己填一个别人猜不到的）
var TOKEN = "改成你的token";

var AGENT_VERSION = "0.3.0";
var HEARTBEAT_MS = 10000;   // 心跳间隔
var POLL_WAIT_S = 12;       // 长轮询挂起秒数（服务端上限 30）
var POLL_TIMEOUT_MS = 25000;// 单次 poll 的网络读超时
var MAX_UI_NODES = 500;     // 一次最多抓多少节点

// ============================================================ 基础工具

function s(v) {
    // Java 的 CharSequence / null 统一转成 JS 字符串
    if (v === null || v === undefined) return "";
    var str = String(v);
    // 有些控件把文字设成了 Java 的 null，转出来是 "null"，别让模型当真
    if (str === "null") return "";
    return str;
}

function nowMs() {
    return new Date().getTime();
}

function log(msg) {
    console.log("[dimensional-portal] " + msg);
}

function sleepSafe(ms) {
    try { sleep(ms); } catch (e) { }
}

function readText(stream) {
    var reader = new java.io.BufferedReader(new java.io.InputStreamReader(stream, "UTF-8"));
    var sb = new java.lang.StringBuilder();
    var line;
    while ((line = reader.readLine()) !== null) {
        sb.append(line);
        sb.append("\n");
    }
    reader.close();
    return String(sb.toString());
}

/**
 * 同步 HTTP 请求，返回 {code, body, json}。
 * 用原生 HttpURLConnection：可控超时，不依赖 auto.js 的 http 模块。
 */
function httpJson(method, path, bodyObj, timeoutMs) {
    var url = null;
    var conn = null;
    try {
        url = new java.net.URL(BASE + path);
        conn = url.openConnection();
        conn.setConnectTimeout(8000);
        conn.setReadTimeout(timeoutMs || 20000);
        conn.setRequestProperty("X-Token", TOKEN);
        conn.setRequestProperty("Accept", "application/json");
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");

        if (method === "POST") {
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            var payload = JSON.stringify(bodyObj === undefined ? {} : bodyObj);
            // 注意：Rhino 里 JS 字符串没有 getBytes()（那是 java.lang.String 的方法），
            // 直接 payload.getBytes() 会报 "Cannot find function getBytes"。
            // 用 OutputStreamWriter 按 UTF-8 写，绕开字节转换。
            var writer = new java.io.OutputStreamWriter(conn.getOutputStream(), "UTF-8");
            writer.write(payload);
            writer.flush();
            writer.close();
        } else {
            conn.setRequestMethod("GET");
        }

        var code = conn.getResponseCode();
        var stream = (code >= 400) ? conn.getErrorStream() : conn.getInputStream();
        var text = stream ? readText(stream) : "";
        var parsed = null;
        try { parsed = JSON.parse(text); } catch (e2) { parsed = null; }
        return { code: code, body: text, json: parsed };
    } catch (e) {
        return { code: -1, body: s(e), json: null };
    } finally {
        try { if (conn) conn.disconnect(); } catch (e3) { }
    }
}

// ============================================================ 设备信息

var DEVICE_ID = (function () {
    try {
        var id = device.getAndroidId();
        if (id) return String(id).substring(0, 16);
    } catch (e) { }
    return "phone-1";
})();

var captureReady = false;

/** 申请/检查截图权限。AutoX.js 会弹系统「屏幕录制」对话框。 */
function ensureCapture(force) {
    if (captureReady && !force) return true;
    try {
        if (typeof requestScreenCapture === "function") {
            var ok = requestScreenCapture(false);
            captureReady = (ok !== false);
        } else {
            captureReady = true; // 有些版本没有这个函数，直接试 captureScreen
        }
    } catch (e) {
        captureReady = false;
        log("申请截图权限失败: " + s(e));
    }
    return captureReady;
}

var WIN_TYPE_APPLICATION = 1;
var WIN_TYPE_INPUT_METHOD = 2;
var WIN_TYPE_SYSTEM = 3;
var SELF_PKG = "org.autojs.autoxjs.v6";

/**
 * 列出所有窗口：[{root, pkg, type, focused, active, order, hasRoot, bounds, layer}]。
 * root 可能为 null（PopupWindow / 系统浮层常见），这类窗口拿不到界面树，
 * 但仍要列出来做诊断（例如长按图标弹出的菜单，WindowManager 里有、无障碍里没 root）。
 */
function appRoots() {
    var results = [];
    if (!auto.service) return results;
    var windows = null;
    try { windows = auto.service.getWindows(); } catch (e) { windows = null; }
    if (!windows) return results;
    var count = 0;
    try { count = windows.size(); } catch (e2) { count = 0; }
    for (var i = 0; i < count; i++) {
        var w = null;
        try { w = windows.get(i); } catch (e3) { w = null; }
        if (!w) continue;
        var type = -1;
        try { type = w.getType(); } catch (e4) { type = -1; }
        var root = null;
        try { root = w.getRoot(); } catch (e5) { root = null; }
        var pkg = "";
        if (root) {
            try { pkg = String(root.getPackageName() || ""); } catch (e6) { pkg = ""; }
        }
        var focused = false;
        try { focused = !!w.isFocused(); } catch (e7) { focused = false; }
        var active = false;
        try { active = !!w.isActive(); } catch (e8) { active = false; }
        var bounds = null;
        try {
            var rect = new android.graphics.Rect();
            w.getBoundsInScreen(rect);
            bounds = { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
        } catch (e9) { bounds = null; }
        var layer = -1;
        try { layer = w.getLayer(); } catch (ea) { layer = -1; }
        results.push({
            root: root, pkg: pkg, type: type,
            focused: focused, active: active, order: i,
            hasRoot: !!root, bounds: bounds, layer: layer
        });
    }
    return results;
}

/**
 * 给窗口打分，越大越像「用户现在正看着的窗口」。
 * 有焦点的应用窗口最高；输入法直接排除；拿不到 root 的窗口只用于诊断；
 * 窗口顺序只当同分时的兜底。
 */
function windowScore(w) {
    if (w.type === WIN_TYPE_INPUT_METHOD) return -1000;
    var score = 0;
    if (w.type === WIN_TYPE_APPLICATION) score += 50;
    else if (w.type === WIN_TYPE_SYSTEM) score -= 50;
    if (w.focused) score += 200;
    if (w.active) score += 60;
    if (!w.root) score -= 400;
    return score - w.order;
}

/** 按分数从高到低排好的窗口列表；调用方负责 recycle。 */
function sortedWindows() {
    var wins = appRoots();
    wins.sort(function (a, b) { return windowScore(b) - windowScore(a); });
    return wins;
}

/** 回收一组窗口的 root。 */
function recycleAll(wins) {
    if (!wins) return;
    for (var i = 0; i < wins.length; i++) {
        try { wins[i].root.recycle(); } catch (e) { }
    }
}

/** 当前前台包名：取分数最高的非输入法窗口；都拿不到才回退 currentPackage()。 */
function currentPkg() {
    var wins = [];
    try {
        wins = sortedWindows();
        for (var i = 0; i < wins.length; i++) {
            if (!wins[i].root || windowScore(wins[i]) <= -1000) continue;
            if (wins[i].pkg) {
                var pkg = wins[i].pkg;
                recycleAll(wins);
                return pkg;
            }
        }
        recycleAll(wins);
    } catch (e) {
        recycleAll(wins);
    }
    try {
        var p = currentPackage();
        return p ? String(p) : "";
    } catch (e3) {
        return "";
    }
}

function buildHeartbeat() {
    var battery = -1, charging = false, screenOn = false;
    try { battery = device.getBattery(); } catch (e) { }
    try { charging = device.isCharging(); } catch (e) { }
    try { screenOn = device.isScreenOn(); } catch (e) { }
    return {
        device_id: DEVICE_ID,
        device: s(device.brand) + " " + s(device.model),
        android: s(device.release),
        w: device.width,
        h: device.height,
        battery: battery,
        charging: charging,
        screen_on: screenOn,
        cur_pkg: currentPkg(),
        accessibility: (auto.service !== null && auto.service !== undefined),
        screen_capture: captureReady,
        overlay: !!overlayGuard,
        overlay_note: overlayNote,
        agent_version: AGENT_VERSION,
        protocol: 1,
        ts: nowMs()
    };
}

// ============================================================ 无障碍节点

function rectOf(node) {
    var rect = new android.graphics.Rect();
    try {
        node.getBoundsInScreen(rect);
    } catch (e) {
        rect.set(0, 0, 0, 0);
    }
    return rect;
}

/**
 * 按层（BFS）收集节点，输出扁平数组（带中心坐标，模型可以直接拿去点）。
 * 用 BFS 而不是递归 DFS：弹窗/长按菜单这类东西往往在浅层，DFS 会先一头扎进
 * 背景列表里，max_nodes 一截断就把弹窗内容全丢了（vivo 桌面长按菜单就是这样）。
 */
function collectNodes(root, maxDepth, out, limit) {
    if (!root) return;
    var depthLimit = maxDepth || 40;
    var queue = [{ node: root, depth: 0 }];
    var head = 0;
    while (head < queue.length && out.length < limit) {
        var item = queue[head];
        head++;
        var node = item.node;
        var depth = item.depth;
        if (!node || depth > depthLimit) continue;

        var rect = rectOf(node);
        var left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
        var text = s(node.getText ? node.getText() : "");
        var desc = s(node.getContentDescription ? node.getContentDescription() : "");
        var cls = s(node.getClassName ? node.getClassName() : "");
        var vid = "";
        try { vid = s(node.getViewIdResourceName()); } catch (e) { }
        var clickable = false, editable = false, selected = false, enabled = true;
        try { clickable = node.isClickable(); } catch (e) { }
        try { editable = node.isEditable(); } catch (e) { }
        try { selected = node.isSelected(); } catch (e) { }
        try { enabled = node.isEnabled(); } catch (e) { }
        var visible = false;
        try { visible = node.isVisibleToUser(); } catch (e) { }

        // 只输出「有信息量」的节点：纯容器（无文字/无描述/不可点/不可输入）跳掉，
        // 否则 max_nodes 会被一串空 FrameLayout 吃掉，模型拿不到真正能点的地方。
        var informative = !!(text || desc || clickable || editable || selected);
        if (visible && (right > left) && (bottom > top) && informative) {
            out.push({
                cls: cls,
                text: text.substring(0, 120),
                desc: desc.substring(0, 80),
                id: vid ? String(vid).split("/").pop() : "",
                clickable: clickable,
                editable: editable,
                selected: selected,
                enabled: enabled,
                depth: depth,
                left: left, top: top, right: right, bottom: bottom,
                cx: Math.round((left + right) / 2),
                cy: Math.round((top + bottom) / 2)
            });
        }

        if (depth >= depthLimit) continue;
        var count = 0;
        try { count = node.getChildCount(); } catch (e) { count = 0; }
        for (var i = 0; i < count; i++) {
            if (out.length >= limit) break;
            var child = null;
            try { child = node.getChild(i); } catch (e) { child = null; }
            if (child) queue.push({ node: child, depth: depth + 1 });
        }
    }
}

function rootNode() {
    if (!auto.service) throw new Error("无障碍服务没开，请在系统设置里启用 AutoX.js 的无障碍服务");
    var wins = sortedWindows();
    for (var i = 0; i < wins.length; i++) {
        if (!wins[i].root || windowScore(wins[i]) <= -1000) continue;
        var chosen = wins[i].root;
        for (var k = 0; k < wins.length; k++) {
            if (k !== i) {
                try { wins[k].root.recycle(); } catch (e1) { }
            }
        }
        return chosen;
    }
    recycleAll(wins);
    var root = null;
    try { root = auto.service.getRootInActiveWindow(); } catch (e3) { root = null; }
    if (!root) throw new Error("拿不到当前窗口的界面树（可能是系统弹窗、锁屏，或当前应用屏蔽了无障碍）");
    return root;
}

function dumpUi(limit) {
    var cap = limit || MAX_UI_NODES;
    var wins = sortedWindows();
    var chosen = null;
    var out = [];
    for (var i = 0; i < wins.length; i++) {
        if (!wins[i].root || windowScore(wins[i]) <= -1000) continue;
        var tmp = [];
        try {
            collectNodes(wins[i].root, 0, tmp, cap);
        } catch (e1) {
            tmp = [];
        }
        if (tmp.length) {
            chosen = wins[i];
            out = tmp;
            break;
        }
    }
    // 有些弹窗（长按菜单、PopupWindow）在无障碍里根本没有 root，
    // 界面树读不到，但屏幕上确实有一层东西。这种情况给模型一个提示，
    // 让它改用截图看（phone_see）而不是继续在旧树里找按钮。
    var ghost = null;
    for (var g = 0; g < wins.length; g++) {
        var gw = wins[g];
        if (gw === chosen || gw.root || gw.type === WIN_TYPE_INPUT_METHOD) continue;
        var b = gw.bounds;
        if (!b || b.right <= b.left || b.bottom <= b.top) continue;
        var fullScreen = (b.right - b.left) >= device.width - 8
            && (b.bottom - b.top) >= device.height - 8;
        if (!fullScreen) {
            ghost = { type: gw.type, bounds: b, focused: gw.focused, active: gw.active };
            break;
        }
    }
    for (var k = 0; k < wins.length; k++) {
        if (wins[k] !== chosen) {
            try { wins[k].root.recycle(); } catch (e2) { }
        }
    }
    if (chosen) {
        try { chosen.root.recycle(); } catch (e3) { }
    } else {
        var root = rootNode();
        try {
            collectNodes(root, 0, out, cap);
        } finally {
            try { root.recycle(); } catch (e4) { }
        }
    }
    return {
        pkg: chosen && chosen.pkg ? chosen.pkg : currentPkg(),
        w: device.width,
        h: device.height,
        win: chosen
            ? { pkg: chosen.pkg, type: chosen.type, focused: chosen.focused, active: chosen.active }
            : null,
        // 屏幕上有无障碍读不到的一层（多半是刚弹出的菜单/对话框），建议改用截图确认
        popup_maybe: ghost,
        nodes: out
    };
}

/** 找焦点所在的输入框节点。 */
function focusedEditable(root) {
    function walk(node, depth) {
        if (!node || depth > 40) return null;
        var editable = false;
        try { editable = node.isEditable(); } catch (e) { }
        var focused = false;
        try { focused = node.isFocused(); } catch (e) { }
        if (editable && focused) return node;
        var count = 0;
        try { count = node.getChildCount(); } catch (e) { return null; }
        for (var i = 0; i < count; i++) {
            var child = null;
            try { child = node.getChild(i); } catch (e) { child = null; }
            var hit = walk(child, depth + 1);
            if (hit) return hit;
        }
        return null;
    }
    return walk(root, 0);
}

// ============================================================ 动作实现

/**
 * 拿一张可用的截图位图。
 * AutoX 的 captureScreen() 会复用同一个 Bitmap 实例：一旦被 recycle 过，
 * 下一次就抛 java.lang.IllegalStateException: image has been recycled。
 * 所以这里既不复用也不回收，拿到的若是已回收的实例就重新申请一次录屏权限再抓。
 */
function grabScreen() {
    for (var attempt = 0; attempt < 3; attempt++) {
        var img = null;
        try { img = captureScreen(); } catch (e) { img = null; }
        if (img && !img.isRecycled()) return img;
        try { ensureCapture(true); } catch (e2) { }
        sleepSafe(500);
    }
    return null;
}

function actScreen(job) {
    var quality = job.params && job.params.quality ? job.params.quality : 55;
    var img = grabScreen();
    if (!img) throw new Error("截图失败：没有屏幕录制权限，请在 AutoX.js 里重新运行脚本并允许「屏幕录制」");

    var dir = context.getExternalFilesDir(null).getAbsolutePath();
    var path = dir + "/dimensional_portal_shot.jpg";
    images.save(img, path, "jpg", quality);

    var b64 = "";
    var stream = null;
    try {
        stream = new java.io.FileInputStream(path);
        var bos = new java.io.ByteArrayOutputStream();
        var buf = java.lang.reflect.Array.newInstance(java.lang.Byte.TYPE, 8192);
        var n;
        while ((n = stream.read(buf)) > 0) bos.write(buf, 0, n);
        stream.close();
        stream = null;
        b64 = String(android.util.Base64.encodeToString(bos.toByteArray(), android.util.Base64.NO_WRAP));
        bos.close();
    } finally {
        try { if (stream) stream.close(); } catch (e) { }
        try { new java.io.File(path).delete(); } catch (e) { }
    }

    captureReady = true;
    return {
        image_b64: b64,
        w: device.width,
        h: device.height,
        pkg: currentPkg(),
        format: "jpg",
        quality: quality
    };
}

function actUi(job) {
    var limit = (job.params && job.params.max_nodes) ? job.params.max_nodes : MAX_UI_NODES;
    return dumpUi(limit);
}

function actTap(job) {
    var x = Math.round(job.params.x), y = Math.round(job.params.y);
    var ok = click(x, y);
    sleepSafe(300);
    return { clicked: [x, y], ok: ok !== false, pkg: currentPkg() };
}

function actLongPress(job) {
    var x = Math.round(job.params.x), y = Math.round(job.params.y);
    var duration = job.params.duration ? job.params.duration : 800;
    press(x, y, duration);
    sleepSafe(300);
    return { pressed: [x, y], duration: duration, pkg: currentPkg() };
}

function actSwipe(job) {
    var p = job.params;
    var duration = p.duration ? p.duration : 300;
    swipe(p.x1, p.y1, p.x2, p.y2, duration);
    sleepSafe(400);
    return { swiped: [p.x1, p.y1, p.x2, p.y2], duration: duration, pkg: currentPkg() };
}

function actText(job) {
    var text = s(job.params.text);
    var method = "";

    // 方案 1：Direct 通过无障碍给焦点输入框 setText（最可靠，不需要输入法）
    try {
        var root = rootNode();
        var node = focusedEditable(root);
        if (node) {
            var args = new android.os.Bundle();
            args.putCharSequence(
                android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                new java.lang.String(text)
            );
            var okSet = node.performAction(
                android.view.accessibility.AccessibilityNodeInfo.ACTION_SET_TEXT, args);
            if (okSet) method = "setText";
        }
        try { root.recycle(); } catch (e) { }
    } catch (e) {
        log("setText 失败: " + s(e));
    }

    // 方案 2：全局 setText(text)
    if (!method && typeof setText === "function") {
        try {
            setText(text);
            method = "setText(global)";
        } catch (e2) { }
    }

    // 方案 3：剪贴板 + 长按粘贴（最后兜底，可能失败）
    if (!method) {
        try {
            setClip(text);
            var root2 = rootNode();
            var node2 = focusedEditable(root2);
            if (node2) {
                node2.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_FOCUS);
                sleepSafe(200);
                node2.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_PASTE);
                method = "paste";
            }
            try { root2.recycle(); } catch (e3) { }
        } catch (e4) { }
    }

    if (!method) throw new Error("没找到可输入的焦点输入框，请先 tap 一下输入框再输入");

    sleepSafe(400);

    var submitted = false;
    if (job.params.submit) {
        // 找「搜索/发送/确定」这类按钮点掉，比模拟回车稳
        var words = ["搜索", "发送", "确定", "完成", "搜索一下", "Send", "Search", "Go", "Done"];
        try {
            var res = findAndTapText(words, 0);
            submitted = res.found;
        } catch (e5) { }
    }

    return { method: method, submitted: submitted, pkg: currentPkg() };
}

function actKey(job) {
    var name = s(job.params.name).toLowerCase();
    var done = true;
    if (name === "back") back();
    else if (name === "home") home();
    else if (name === "recents") recents();
    else if (name === "volume_up") volumeUp();
    else if (name === "volume_down") volumeDown();
    else if (name === "delete") {
        // 无障碍没有「删除键」，直接把输入框文字清掉
        var root = rootNode();
        var node = focusedEditable(root);
        if (!node) {
            try { root.recycle(); } catch (e) { }
            throw new Error("当前没有焦点输入框，删除键不可用");
        }
        var args = new android.os.Bundle();
        args.putCharSequence(
            android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
            new java.lang.String("")
        );
        node.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        try { root.recycle(); } catch (e) { }
    } else if (name === "enter") {
        // 没有系统回车注入权限，改成点提交按钮
        var words2 = ["搜索", "发送", "确定", "完成", "Send", "Search", "Go", "Done"];
        var r2 = findAndTapText(words2, 0);
        if (!r2.found) {
            done = false;
            throw new Error("找不到可以点的提交按钮（搜索/发送/确定），这版还不支持纯回车键");
        }
    } else {
        throw new Error("不认识的按键: " + name);
    }
    sleepSafe(400);
    return { key: name, ok: done, pkg: currentPkg() };
}

/**
 * vivo/iQOO 的「应用跳转」拦截：AutoX 想打开第三方应用时，系统会弹
 * com.vivo.appfilter 的对话框（"Autox.js v6 想要打开 XX"），不点掉就停在原地。
 * 这里自动点「始终打开」，一次之后同一个目标应用就不再问。
 * 返回 true 表示点掉了一个拦截框。
 */
function handleJumpPromptIfShown() {
    var p = "";
    try { p = currentPkg(); } catch (e) { p = ""; }
    var likely = p.indexOf("appfilter") >= 0 || p === "com.vivo.appfilter";
    if (!likely) {
        // 包名不一定看得到（系统对话框有时不在无障碍窗口里），退回按文字判断
        var probe = null;
        try { probe = findTextNodes(["始终打开"], false); } catch (e2) { probe = null; }
        if (!probe || !probe.hits.length) return false;
        click(probe.hits[0].cx, probe.hits[0].cy);
        sleepSafe(1000);
        return true;
    }
    var res = null;
    try { res = findTextNodes(["始终打开"], false); } catch (e3) { res = null; }
    if (!res || !res.hits.length) return false;
    click(res.hits[0].cx, res.hits[0].cy);
    sleepSafe(1000);
    return true;
}

/** 等前台包名变化：变了就返回新包名，没变返回空串。中途顺手处理 vivo 跳转框。 */
function waitForegroundChange(before, tries, intervalMs) {
    var handledJump = false;
    for (var i = 0; i < tries; i++) {
        sleepSafe(intervalMs);
        var now = "";
        try { now = currentPkg(); } catch (e) { now = ""; }
        if (now && now !== before && now.indexOf("appfilter") < 0) return now;
        if (!handledJump) {
            if (handleJumpPromptIfShown()) {
                handledJump = true;
                continue;
            }
        }
    }
    return "";
}

function launchTarget(pkg, name) {
    if (pkg && pkg.indexOf(".") >= 0) {
        var ok = app.launchPackage(pkg);
        if (!ok) ok = app.launch(pkg);
        return ok;
    }
    if (name) {
        var ok2 = app.launchApp(name);
        if (!ok2) ok2 = app.launchPackage(name);
        return ok2;
    }
    throw new Error("没给应用名或包名");
}

/** 把自己（AutoX）顶到前台：有些应用（微信是典型）拒绝被后台应用唤起。 */
function bringSelfToFront() {
    try { app.launchPackage(SELF_PKG); } catch (e) { }
    sleepSafe(800);
}

/** 前台包名是不是目标应用（也接受子包，例如 com.tencent.mm 的 :push 子进程不算）。 */
function looksLikeTarget(after, pkg) {
    if (!after) return false;
    if (!pkg) return after !== SELF_PKG;
    return after === pkg || after.indexOf(pkg + ".") === 0;
}

function actOpenApp(job) {
    var pkg = s(job.params.package);
    var name = s(job.params.name);
    var target = pkg || name;
    var before = currentPkg();
    var launched = launchTarget(pkg, name);

    // launchXxx 返回 true 只说明"意图发出去了"：Android 10+ 会拦后台启动，
    // vivo 还会弹"应用跳转"确认框，微信这类应用自己也会拒绝后台唤起，
    // 所以必须看一眼前台包名，否则会出现"说打开了、其实没动"的假成功。
    var after = waitForegroundChange(before, 3, 500);
    var bounced = false;
    if (!after) {
        // 兜底：先把自己顶到前台，再唤起目标（微信在后台唤起时会直接被丢弃）
        bringSelfToFront();
        bounced = true;
        var again = launchTarget(pkg, name);
        launched = launched || again;
        after = waitForegroundChange(before, 8, 600);
    }
    if (!after) {
        var cur = "";
        try { cur = currentPkg(); } catch (e2) { cur = ""; }
        after = cur;
    }

    // 诊断：被拦住时把当时能看到的界面文字带回去，方便判断是不是 vivo 的跳转框
    var seen = null;
    if (!looksLikeTarget(after, pkg)) {
        try {
            var dump = dumpUi(30);
            var texts = [];
            for (var di = 0; di < dump.nodes.length; di++) {
                var lab = dump.nodes[di].text || dump.nodes[di].desc;
                if (lab) texts.push(lab);
            }
            seen = {
                pkg: dump.pkg,
                win: dump.win,
                popup_maybe: dump.popup_maybe,
                texts: texts.slice(0, 15)
            };
        } catch (e3) { seen = null; }
    }

    var warning = "";
    var looksRight = looksLikeTarget(after, pkg);
    if (launched === false) {
        warning = "系统没接受启动请求：「" + target + "」可能没装，或应用名不对";
    } else if (after && after.indexOf("appfilter") >= 0) {
        warning = "被 vivo 的「应用跳转」确认框拦住了（没找到可点的「始终打开」）。请在屏幕上手动点一下「始终打开」";
    } else if (!looksRight && (after === before || after === SELF_PKG)) {
        warning = "启动请求发出去了，但前台还是 " + after + "。常见原因：" +
            "①这个应用拒绝被别的应用唤起（微信常见）；" +
            "②vivo 的「后台启动应用」限制（i管家 → 权限管理 → 后台弹出界面 里允许 AutoX.js）";
    }
    return {
        launched: launched !== false,
        pkg: after,
        target: target,
        bounced: bounced,
        seen: seen,
        warning: warning
    };
}

/** 在界面树里找文字，返回匹配节点数组。 */
function findTextNodes(words, exact) {
    var root = rootNode();
    var all = [];
    try {
        collectNodes(root, 0, all, MAX_UI_NODES);
    } finally {
        try { root.recycle(); } catch (e) { }
    }
    var hits = [];
    for (var i = 0; i < all.length; i++) {
        var node = all[i];
        var label = node.text || node.desc || "";
        if (!label) continue;
        for (var w = 0; w < words.length; w++) {
            var target = s(words[w]);
            if (!target) continue;
            var matched = exact ? (label === target) : (label.indexOf(target) >= 0);
            if (matched) {
                hits.push({ label: label, cx: node.cx, cy: node.cy, clickable: node.clickable });
                break;
            }
        }
    }
    return { hits: hits, texts: all };
}

function findAndTapText(words, index) {
    var res = findTextNodes(words, false);
    if (!res.hits.length) {
        var uniq = [];
        var seen = {};
        for (var i = 0; i < res.texts.length; i++) {
            var t = (res.texts[i].text || res.texts[i].desc || "").substring(0, 40);
            if (t && !seen[t]) {
                seen[t] = 1;
                uniq.push(t);
                if (uniq.length >= 30) break;
            }
        }
        return { found: false, texts: uniq };
    }
    var hit = res.hits[Math.min(index || 0, res.hits.length - 1)];
    click(hit.cx, hit.cy);
    return {
        found: true,
        matched: hit.label,
        cx: hit.cx,
        cy: hit.cy,
        total: res.hits.length,
        pkg: currentPkg()
    };
}

function actFindText(job) {
    var text = s(job.params.text);
    var index = job.params.index ? job.params.index : 0;
    var exact = !!job.params.exact;
    var root = rootNode();
    var all = [];
    try {
        collectNodes(root, 0, all, MAX_UI_NODES);
    } finally {
        try { root.recycle(); } catch (e) { }
    }
    var hits = [];
    for (var i = 0; i < all.length; i++) {
        var node = all[i];
        var label = node.text || node.desc || "";
        if (!label) continue;
        var matched = exact ? (label === text) : (label.indexOf(text) >= 0);
        if (matched) hits.push({ label: label, cx: node.cx, cy: node.cy });
    }
    if (!hits.length) {
        var uniq = [];
        var seen = {};
        for (var j = 0; j < all.length; j++) {
            var t = (all[j].text || all[j].desc || "").substring(0, 40);
            if (t && !seen[t]) {
                seen[t] = 1;
                uniq.push(t);
                if (uniq.length >= 20) break;
            }
        }
        return { found: false, texts: uniq, pkg: currentPkg() };
    }
    var hit = hits[Math.min(index, hits.length - 1)];
    var tapped = false;
    if (job.params.tap !== false) {
        click(hit.cx, hit.cy);
        tapped = true;
        sleepSafe(300);
    }
    return {
        found: true,
        matched: hit.label,
        cx: hit.cx,
        cy: hit.cy,
        tapped: tapped,
        total: hits.length,
        pkg: currentPkg()
    };
}

function actWait(job) {
    var ms = job.params.ms ? job.params.ms : 1000;
    // 回传夹取后的真实等待时长，别把「想等 60s、实际只等 30s」说成已等 60s。
    var waited = Math.min(ms, 30000);
    sleepSafe(waited);
    return { waited_ms: waited, pkg: currentPkg() };
}

function actStatus(job) {
    return buildHeartbeat();
}

/** 诊断用：列出当前所有窗口的分数，排查「UI 树取错窗口」这类问题。 */
function actWindows(job) {
    var wins = sortedWindows();
    var out = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var children = -1;
        if (w.root) {
            try { children = w.root.getChildCount(); } catch (e) { children = -1; }
        }
        out.push({
            pkg: w.pkg,
            type: w.type,
            focused: w.focused,
            active: w.active,
            order: w.order,
            score: windowScore(w),
            has_root: w.hasRoot,
            layer: w.layer,
            bounds: w.bounds,
            children: children
        });
    }
    recycleAll(wins);
    return { windows: out, pkg: currentPkg(), w: device.width, h: device.height };
}

// ============================================================ 任务执行

// ------------------------------------------------------------ 应用列表

// 给控制台「应用访问」用：把手机上装了什么报给电脑，人再逐个决定放不放行。
// 优先用 app.getInstalledApps()（带名字）；这台机器上如果不可用，退回 pm list。
function actApps(job) {
    // 应用清单要「全」：系统应用（计算器、设置、相机）也必须在内，否则按名字拦截会漏。
    // getInstalledApps 只给一部分（这台机器上只有 115 个用户应用），所以始终用 pm list packages 兜底合并。
    var out = [];
    var seen = {};
    var labels = {};
    var i;

    // 系统应用集合：pm list packages -s 列出来的就是系统签名的
    var sysSet = {};
    try {
        var sysRes = shell("pm list packages -s", false);
        var sysLines = s(sysRes).split(String.fromCharCode(10));
        for (var si = 0; si < sysLines.length; si++) {
            var sl = s(sysLines[si]).trim();
            var sidx = sl.indexOf("package:");
            if (sidx >= 0) sysSet[sl.substring(sidx + 8).trim()] = 1;
        }
    } catch (eSys) {
        log("读系统应用清单失败：" + eSys);
    }

    function push(pkg, label) {
        pkg = s(pkg).trim();
        if (!pkg || seen[pkg]) return;
        seen[pkg] = 1;
        out.push({ package: pkg, name: s(label).trim() || pkg, system: sysSet[pkg] === 1 });
    }

    // 1) 快速拿到「包名 → 中文名」的映射（这一步可能不全，但名字质量最好）
    try {
        if (typeof app.getInstalledApps === "function") {
            var list = app.getInstalledApps() || [];
            for (i = 0; i < list.length; i++) {
                var it = list[i] || {};
                var pkg = s(it.packageName || it.package || it.pkg).trim();
                var label = s(it.label || it.name || it.appName).trim();
                if (!pkg) continue;
                if (label) labels[pkg] = label;
                push(pkg, label);
            }
        }
    } catch (e) {
        log("getInstalledApps 不可用：" + e);
    }

    // 2) 无条件再扫一遍全部包（含系统应用），补上第 1 步漏掉的
    try {
        // 第二个参数 false = 用 shell 用户执行；true 会走 su，没 root 直接抛 IOException
        var res = shell("pm list packages", false);
        var text = s(res);
        var lines = text.split(String.fromCharCode(10));
        for (i = 0; i < lines.length; i++) {
            var line = s(lines[i]).trim();
            if (!line) continue;
            var idx = line.indexOf("package:");
            if (idx < 0) continue;
            var pkg2 = line.substring(idx + 8).trim();
            if (seen[pkg2]) continue;
            var label2 = labels[pkg2] || "";
            if (!label2) {
                try { label2 = s(app.getAppName(pkg2)); } catch (e2) { label2 = ""; }
            }
            push(pkg2, label2);
        }
    } catch (e3) {
        if (out.length === 0) throw new Error("读不到应用列表：" + e3);
        log("pm list packages 失败，只用已读到的 " + out.length + " 个：" + e3);
    }

    out.sort(function (a, b) {
        return s(a.name).localeCompare(s(b.name), "zh");
    });
    return { apps: out, total: out.length, w: device.width, h: device.height };
}

function actIcon(job) {
    // 单个应用图标：64x64 PNG 的 base64。按需取，避免一次传 500 张。
    var pkg = s(job.params && (job.params.package || job.params.pkg)).trim();
    if (!pkg) throw new Error("缺少 package");
    var size = Number(job.params && job.params.size) || 64;
    if (size < 24) size = 24;
    if (size > 192) size = 192;

    var drawable = null;
    // 1) AutoX 自带的 app.getIcon（有些机型返回 null）
    try {
        drawable = app.getIcon(pkg);
    } catch (e1) {
        drawable = null;
    }
    // 2) 直接问 PackageManager，这条路更靠谱（自适应图标也能画）
    if (!drawable) {
        try {
            var pm = context.getPackageManager();
            drawable = pm.getApplicationIcon(pkg);
        } catch (e2) {
            drawable = null;
        }
    }
    if (!drawable) return { package: pkg, found: false };

    var bmp = android.graphics.Bitmap.createBitmap(size, size, android.graphics.Bitmap.Config.ARGB_8888);
    var canvas = new android.graphics.Canvas(bmp);
    drawable.setBounds(0, 0, size, size);
    drawable.draw(canvas);
    var out = new java.io.ByteArrayOutputStream();
    bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
    var bytes = out.toByteArray();
    var b64 = android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP);
    try { bmp.recycle(); } catch (eR) { }
    return { package: pkg, found: true, size: size, png_b64: b64 };
}

function executeJob(job) {
    var action = s(job.action);
    log("执行 " + job.id + " " + action);
    try {
        var data = null;
        if (action === "screen") data = actScreen(job);
        else if (action === "ui") data = actUi(job);
        else if (action === "tap") data = actTap(job);
        else if (action === "long_press") data = actLongPress(job);
        else if (action === "swipe") data = actSwipe(job);
        else if (action === "text") data = actText(job);
        else if (action === "key") data = actKey(job);
        else if (action === "open_app") data = actOpenApp(job);
        else if (action === "find_text") data = actFindText(job);
        else if (action === "wait") data = actWait(job);
        else if (action === "status") data = actStatus(job);
        else if (action === "windows") data = actWindows(job);
        else if (action === "apps") data = actApps(job);
        else if (action === "icon") data = actIcon(job);
        else if (action === "camera_photo" || action === "camera_video"
            || action === "download_apk" || action === "install_apk") {
            throw new Error("动作 " + action + " 还没实现（M3/M4 里程碑）");
        } else {
            throw new Error("未知动作: " + action);
        }
        return { op_ok: true, data: data, error: null };
    } catch (e) {
        return { op_ok: false, data: null, error: s(e && e.message ? e.message : e) };
    }
}

function reportResult(job, res) {
    var body = {
        id: job.id,
        ok: res.op_ok,
        data: res.data,
        error: res.error,
        finished_at: nowMs()
    };
    var r = httpJson("POST", "/result", body, 20000);
    if (r.code !== 200) {
        log("回传结果失败 " + job.id + " code=" + r.code + " " + r.body.substring(0, 200));
    }
    return r.code === 200;
}

// ============================================================ 主循环

function sendHeartbeat() {
    var r = httpJson("POST", "/hb", buildHeartbeat(), 15000);
    if (r.code === 200) return true;
    log("心跳失败 code=" + r.code + " " + s(r.body).substring(0, 160));
    return false;
}

var overlayGuard = null;
var overlayNote = "";

/**
 * 创建 1x1 透明的悬浮窗"守卫"。
 * 为什么需要它：Android 10+ 禁止后台应用启动 Activity，官方豁免条件之一是
 * "应用持有 SYSTEM_ALERT_WINDOW 权限并且当前确实有悬浮窗在显示"。
 * 没有这层窗时，AutoX 在后台调 app.launchPackage() 会被系统静默丢弃
 * （返回 true 但前台不变），微信这类应用尤其明显。
 */
function createOverlayGuard() {
    // 直接用 WindowManager 加一个 1x1 的 TYPE_APPLICATION_OVERLAY 窗口。
    // 不走 floaty：AutoX 6 的 floaty.rawWindow 在这个脚本里抛 NPE
    // （RhinoJavaScriptEngine.getRuntime() on a null object）。
    try {
        var wm = context.getSystemService(android.content.Context.WINDOW_SERVICE);
        var params = new android.view.WindowManager.LayoutParams();
        params.type = 2038;   // TYPE_APPLICATION_OVERLAY
        params.width = 1;
        params.height = 1;
        params.format = android.graphics.PixelFormat.TRANSLUCENT;
        params.gravity = 51;  // 左上角
        params.flags = android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            | android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
        params.alpha = 0.01;  // 不是 0，免得被判定成"不可见窗口"
        var view = new android.view.View(context);
        wm.addView(view, params);
        overlayNote = "ok(WindowManager)";
        return { wm: wm, view: view, params: params };
    } catch (e1) {
        overlayNote = "WindowManager 失败: " + s(e1 && e1.message ? e1.message : e1);
    }
    try {
        var v2 = new android.widget.FrameLayout(context);
        var win = floaty.rawWindow(v2);
        try { win.setSize(1, 1); } catch (e3) { }
        try { win.setPosition(0, 0); } catch (e4) { }
        overlayNote += " | ok(floaty)";
        return win;
    } catch (e2) {
        overlayNote += " | floaty 失败: " + s(e2 && e2.message ? e2.message : e2);
        return null;
    }
}

function main() {
    log("启动 v" + AGENT_VERSION + " → " + BASE);
    toast("次元门 已启动");

    try {
        if (typeof auto !== "undefined" && auto.waitFor) auto.waitFor();
    } catch (e) {
        log("auto.waitFor 异常: " + s(e));
    }
    try { auto.setMode("fast"); } catch (e2) { }

    if (overlayGuard) {
        log("悬浮窗守卫在工作（后台提起应用靠它）");
    } else {
        log("没有悬浮窗守卫：后台启动应用可能被系统拦（" + overlayNote + "）");
    }

    // 先要一次截图权限（会弹系统对话框），失败也不阻塞，等真正截图时再要
    try { ensureCapture(false); } catch (e3) { }

    var since = 0;
    var failStreak = 0;

    while (true) {
        // 1) 心跳
        try {
            sendHeartbeat();
        } catch (e) {
            log("心跳异常: " + s(e));
        }

        // 2) 长轮询拿任务
        var resp = httpJson("GET", "/poll?since=" + since + "&wait=" + POLL_WAIT_S, null, POLL_TIMEOUT_MS);

        if (resp.code === 401) {
            log("❌ token 不匹配：检查脚本里的 TOKEN 和插件配置是否一致");
            sleepSafe(5000);
            continue;
        }
        if (resp.code !== 200 || !resp.json) {
            failStreak++;
            log("连不上(" + failStreak + ") code=" + resp.code + " → " + BASE);
            // 连着三次连不上，换下一个地址试试（Tailscale ↔ 局域网 互为备份）
            if (failStreak % 3 === 0) nextBase();
            sleepSafe(Math.min(2000 + failStreak * 1000, 8000));
            continue;
        }

        failStreak = 0;
        var payload = resp.json;
        if (payload.server_seq !== undefined && payload.server_seq !== null) {
            since = payload.server_seq;
        }

        var jobs = payload.jobs || [];
        for (var i = 0; i < jobs.length; i++) {
            var job = jobs[i];
            var res = executeJob(job);
            reportResult(job, res);
        }
    }
}

// 悬浮窗守卫必须在脚本主线程创建：floaty 依赖 Rhino 运行时上下文，
// 在 threads.start 的子线程里调用会抛 NullPointerException。
try {
    overlayGuard = createOverlayGuard();
    log(overlayGuard
        ? "悬浮窗守卫已创建（后台提起应用靠它）"
        : "悬浮窗守卫创建失败，后台可能提不起应用：" + overlayNote);
} catch (eOvTop) {
    overlayNote = "顶层创建异常: " + s(eOvTop);
    log("悬浮窗守卫异常：" + overlayNote);
}

// AutoX.js 里直接运行会进入 main；用 threads.start 是为了让 UI 线程不被卡住
threads.start(function () {
    try {
        main();
    } catch (e) {
        log("主循环崩了: " + s(e) + "\n" + (e && e.stack ? e.stack : ""));
        toast("手机执行器异常退出，请查看日志");
    }
});
