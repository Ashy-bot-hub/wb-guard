// 面板真实 UI 点删：全流程用真实鼠标事件，逐步截图并断言「两步确认」。
// 用法：node ui_delete.mjs <会话标题> <输出前缀>
import fs from 'node:fs';

const TITLE = process.argv[2] || 'ZZ-UI点删验证2-临时会话';
const OUT = process.argv[3] || 'run';
const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
const want = process.env.WD_CDP_TARGET || '';
const page = want
  ? list.find(t => t.type === 'page' && ((t.id || '').startsWith(want) || (t.url || '').includes(want)))
  : list.filter(t => t.type === 'page').sort((a, b) => (b.id > a.id ? 1 : -1))[0];
if (!page) { console.error('没有可用页面'); process.exit(1); }
console.log('target =', page.id.slice(0, 8));

const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise(res => { const mid = ++id; pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
await new Promise(r => ws.onopen = r);

const sleep = ms => new Promise(r => setTimeout(r, ms));
const evalJS = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  return r.result?.result?.value;
};
const shot = async (name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(`${OUT}_${name}.png`, Buffer.from(r.result.data, 'base64'));
};
// 真实鼠标：按下 + 抬起，带 userGesture 语义，走的是浏览器事件链而不是 el.click()
const clickXY = async (x, y) => {
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', {
      type, x, y, button: 'left', clickCount: 1, buttons: type === 'mousePressed' ? 1 : 0,
    });
  }
  await sleep(250);
};
// 元素中心点（找不到返回 null）
const center = async (sel) => evalJS(`(() => {
  const e = document.querySelector(${JSON.stringify(sel)});
  if (!e) return null;
  const r = e.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
})()`);
// 按标题在会话面板里定位行，返回复选框中心点
const rowCheck = async (title) => evalJS(`(() => {
  const pane = document.querySelector('.wbs-pane[data-pane="sessions"]');
  if (!pane) return { err: 'no pane' };
  const row = [...pane.querySelectorAll('.wbs-sess-row')].find(r =>
    (r.querySelector('.wbs-sess-title') || {}).textContent === ${JSON.stringify(title)});
  if (!row) return { err: 'row not found' };
  const cb = row.querySelector('.wbs-sess-check');
  const c = cb.getBoundingClientRect();
  return { cb: [Math.round(c.x + c.width / 2), Math.round(c.y + c.height / 2)], cls: cb.className };
})()`);
// 面板可见状态
const panelOpen = async () => evalJS(`(() => {
  const p = document.querySelector('.wbs-panel');
  return p ? getComputedStyle(p).display : 'none';
})()`);
// 弹窗状态：标题 / 主按钮文案 / 是否可见 / 当前 toast
// toast 挂在 #wbs-toast-root 的 shadow root 里，普通 querySelector 抓不到——
// 只查文档树会得到「toasts: []」，看起来像提示没弹出来。
const modalState = async () => evalJS(`(() => {
  const t = document.querySelector('#wbs-sess-modal-title');
  const ok = document.querySelector('#wbs-sess-modal-ok');
  const box = t ? t.getBoundingClientRect() : null;
  const host = document.querySelector('#wbs-toast-root');
  const toasts = host && host.shadowRoot
    ? [...host.shadowRoot.querySelectorAll('.wbs-hot-message')].map(e => (e.textContent || '').trim()).filter(Boolean)
    : [];
  return {
    visible: !!(box && box.width > 0),
    title: t ? t.textContent.trim() : null,
    okText: ok ? ok.textContent.trim() : null,
    toasts,
  };
})()`);

const log = [];

// 1) 打开面板
if (await panelOpen() === 'none') {
  const fab = await center('.wbs-fab');
  if (!fab) { console.error('找不到悬浮入口'); process.exit(1); }
  await clickXY(fab[0], fab[1]);
  await sleep(400);
}
log.push({ step: '打开面板', display: await panelOpen() });

// 2) 切到会话页
const tab = await evalJS(`(() => {
  const t = [...document.querySelectorAll('.wbs-tab')].find(e => e.textContent.trim() === '会话');
  if (!t) return null; const r = t.getBoundingClientRect();
  return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
})()`);
await clickXY(tab[0], tab[1]);
await sleep(900);
await shot('1_sessions');

// 3) 进批量模式（未进时点一次）
const batchVisible = await evalJS(`(() => {
  const b = document.querySelector('#wbs-sess-batchbar');
  return b ? getComputedStyle(b).display : 'none';
})()`);
if (batchVisible === 'none') {
  const b = await center('#wbs-sess-batch');
  if (!b) { console.error('找不到批量操作'); process.exit(1); }
  await clickXY(b[0], b[1]);
  await sleep(400);
}
log.push({ step: '批量模式', bar: await evalJS(`getComputedStyle(document.querySelector('#wbs-sess-batchbar')).display`) });

// 4) 勾选目标行
const rc = await rowCheck(TITLE);
if (!rc || rc.err) { console.error('定位行失败', rc); process.exit(1); }
await clickXY(rc.cb[0], rc.cb[1]);
await sleep(400);
log.push({ step: '勾选', 已选: await evalJS(`document.querySelector('#wbs-sess-batch-count').textContent.trim()`) });
await shot('2_selected');

// 5) 点「删除所有账号同步副本」→ 第一步确认框
const delAll = await center('#wbs-sess-delete-all');
if (!delAll) { console.error('找不到删除所有账号同步副本'); process.exit(1); }
await clickXY(delAll[0], delAll[1]);
await sleep(500);
log.push({ step: '打开确认框', state: await modalState() });
await shot('3_confirm1');

// 6) 第一次点确认：应只换标题 + 出提示，不执行删除
const ok1 = await center('#wbs-sess-modal-ok');
await clickXY(ok1[0], ok1[1]);
await sleep(350);
const s2 = await modalState();
log.push({ step: '第一次确认后', state: s2 });
await shot('4_confirm2_toast');

// 7) 第二次点：真正执行
const ok2 = await center('#wbs-sess-modal-ok');
await clickXY(ok2[0], ok2[1]);
await sleep(1800);
log.push({ step: '第二次确认后', state: await modalState() });
await shot('5_after_delete');
log.push({ step: '列表里还有该行吗', still: await rowCheck(TITLE).then(r => r && r.err ? false : true) });

// 判定：每一步都要对上，缺一条这条链路就没走通
const v1 = log.find(x => x.step === '打开确认框').state;
const v2 = log.find(x => x.step === '第一次确认后').state;
const v3 = log.find(x => x.step === '第二次确认后').state;
const still = log.find(x => x.step === '列表里还有该行吗').still;
const verdict = {
  '首点不改标题': v1.title === '删除所有账号的同源会话？' && v1.okText === '继续确认',
  '首点只换标题不执行': v2.visible === true && v2.title === '再次确认永久删除' && v2.okText === '永久删除',
  '首点给出「再点一次」提示': v2.toasts.some(t => t.includes('再点一次')),
  '第二次才执行并关窗': v3.visible === false,
  '执行后有「已删除」反馈': v3.toasts.some(t => t.includes('已删除')),
  '列表已刷新（行消失）': still === false,
};
console.log(JSON.stringify(log, null, 1));
console.log('\n判定:');
for (const [k, v] of Object.entries(verdict)) console.log((v ? '  [通过] ' : '  [失败] ') + k);
console.log(Object.values(verdict).every(Boolean) ? '\n结论：面板实点删除链路完整' : '\n结论：有环节未通过，见上');
ws.close();

