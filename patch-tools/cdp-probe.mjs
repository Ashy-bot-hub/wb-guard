import fs from 'node:fs';
import path from 'node:path';

const LIST = 'http://127.0.0.1:9222/json/list';
const res = await fetch(LIST);
const targets = await res.json();
const page = targets.find(t => t.type === 'page' && /WorkBuddy/i.test(t.title)) || targets.find(t => t.type === 'page');
if (!page) { console.error('no page target'); process.exit(2); }

const exprFile = process.argv[2];
if (!exprFile) { console.error('usage: node cdp-probe.mjs <exprfile>'); process.exit(2); }
const expression = fs.readFileSync(exprFile, 'utf8');

const WS_PATH = 'C:/Users/Administrator/AppData/Local/Programs/WorkDaddy/scripts/node_modules/ws/index.js';
const { default: WebSocket } = await import('file:///' + WS_PATH);

const ws = new WebSocket(page.webSocketDebuggerUrl, { perMessageDeflate: false, maxPayload: 64 * 1024 * 1024 });
let id = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const msgId = ++id;
  pending.set(msgId, { resolve, reject });
  ws.send(JSON.stringify({ id: msgId, method, params }));
});
ws.on('message', raw => {
  let msg; try { msg = JSON.parse(raw.toString()); } catch { return; }
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  }
});
ws.on('error', e => { console.error('ws error', e.message); process.exit(3); });
ws.on('open', async () => {
  try {
    await send('Runtime.enable');
    const out = await send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: false,
    });
    if (out.exceptionDetails) {
      console.log(JSON.stringify({ ok: false, exception: out.exceptionDetails.exception && out.exceptionDetails.exception.description || out.exceptionDetails.text }, null, 2));
    } else {
      const v = out.result && out.result.value;
      console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 2));
    }
  } catch (e) {
    console.error('eval failed:', e.message);
  } finally {
    ws.close();
  }
});
