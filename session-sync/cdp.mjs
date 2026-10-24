// CDP 客户端：连接 WorkDaddy 渲染进程执行 JS 或截图
// 用法：
//   node cdp.mjs list                    列出所有页面（多窗口时用来挑目标）
//   node cdp.mjs eval "document.title"
//   node cdp.mjs shot out.png
//   node cdp.mjs click 60 152
//   node cdp.mjs run step.js out.png
// 前提：客户端需以远程调试端口启动（默认 9222，可用环境变量 WD_CDP_PORT 覆盖）
// 多窗口：客户端可能同时有多个 page（主窗口 + 文档窗口），默认取第一个匹配项，
// 会打到「不是你要的那个窗口」上——表现为「页面明明有元素，探测结果却是空的」。
// 用 WD_CDP_TARGET=<id 前缀|URL 片段> 指定。
import fs from 'node:fs';
import path from 'node:path';

// 端口解析：环境变量 -> 客户端自写的 cdp-port.json -> 9222（1.2.17 起端口由客户端自选）
function resolveCdpPort() {
  if (process.env.WD_CDP_PORT) return String(process.env.WD_CDP_PORT);
  const f = path.join(process.env.APPDATA || '', 'WorkDaddy', 'cdp-port.json');
  try {
    const j = JSON.parse(fs.readFileSync(f, 'utf8'));
    const p = (j && typeof j === 'object' && !Array.isArray(j)) ? j.port : j;
    if (p) return String(p);
  } catch (_) {}
  return '9222';
}

const PORT = resolveCdpPort();
const [, , cmd, arg] = process.argv;

let list;
try {
  list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
} catch (e) {
  console.error(`无法连接调试端口 ${PORT}：${e.message}`);
  process.exit(2);
}

if (cmd === 'list') {
  list.forEach(t => console.log([t.type, (t.id || '').slice(0, 8),
    (t.title || '').slice(0, 24), (t.url || '').slice(0, 60)].join(' | ')));
  process.exit(0);
}

const want = process.env.WD_CDP_TARGET;
let page;
if (want) {
  page = list.find(t => t.type === 'page' && ((t.id || '').startsWith(want) || (t.url || '').includes(want)));
  if (!page) {
    console.error(`WD_CDP_TARGET=${want} 没匹配到页面，用 list 看可选值`);
    process.exit(1);
  }
} else {
  page = list.find(t => t.type === 'page' && /app\.asar\/renderer/.test(t.url || ''))
      || list.find(t => t.type === 'page' && /WorkBuddy/i.test(t.title || ''))
      || list.find(t => t.type === 'page');
}
if (!page) { console.error('没有可用页面'); process.exit(1); }


const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params }));
});
await new Promise(r => ws.onopen = r);

if (cmd === 'shot') {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const out = arg || 'shot.png';
  fs.writeFileSync(out, Buffer.from(r.result.data, 'base64'));
  console.log('saved', out);
} else if (cmd === 'click') {
  const x = Number(arg), y = Number(process.argv[4]);
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', {
      type, x, y, button: 'left', clickCount: 1, buttons: type === 'mousePressed' ? 1 : 0,
    });
  }
  console.log('clicked', x, y);
} else if (cmd === 'run') {
  const code = fs.readFileSync(arg, 'utf8');
  const r = await send('Runtime.evaluate', {
    expression: code, returnByValue: true, awaitPromise: true, userGesture: true,
  });
  console.log(JSON.stringify(r.result && r.result.result ? r.result.result.value : r, null, 1).slice(0, 2000));
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(process.argv[4] || 'run.png', Buffer.from(shot.result.data, 'base64'));
} else if (cmd === 'eval') {
  const r = await send('Runtime.evaluate', {
    expression: arg, returnByValue: true, awaitPromise: true, userGesture: true,
  });
  console.log(JSON.stringify(r.result, null, 1).slice(0, 4000));
}
ws.close();
