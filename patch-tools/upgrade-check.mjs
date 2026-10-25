'use strict';
// WorkDaddy × WorkBuddy 升级体检（D 项）
// 用途：WorkBuddy 或 WorkDaddy 升级后 3 分钟内跑一次，确认补丁与注入仍然成立。
// 用法：node upgrade-check.mjs      退出码 0=全部通过，1=有失败项
import fs from 'node:fs';
import path from 'node:path';

const HOME = process.env.USERPROFILE || 'C:/Users/Administrator';
const WB_RES = 'C:/Program Files/WorkBuddy/resources';
const WD_SCRIPTS = path.join(HOME, 'AppData/Local/Programs/WorkDaddy/scripts');
const WD_DATA = path.join(HOME, 'AppData/Roaming/WorkDaddy');
const NODE_EXE = path.join(WD_SCRIPTS, 'runtime/node/node.exe');
const DAEMON_LOG = path.join(WD_DATA, 'daemon.log');
const TARGET_JSON = path.join(WD_DATA, 'workbuddy-target.json');
const INJECT_JS = path.join(WD_SCRIPTS, 'inject.js');
const COMPAT_JS = path.join(WD_SCRIPTS, 'workbuddy-compat.js');

const rows = [];
const add = (name, ok, detail) => rows.push({ name, ok, detail });

// 1) WorkBuddy 版本
let wbVersion = 'unknown';
try {
  wbVersion = JSON.parse(fs.readFileSync(path.join(WB_RES, 'install-manifest.json'), 'utf8')).appVersion || 'unknown';
  add('WorkBuddy 版本可读', true, wbVersion);
} catch (e) {
  add('WorkBuddy 版本可读', false, e.message);
}

// 2) 目标档案版本是否与安装一致
try {
  const t = JSON.parse(fs.readFileSync(TARGET_JSON, 'utf8'));
  const ok = String(t.version || '') === String(wbVersion);
  add('workbuddy-target.json 版本同步', ok, `target=${t.version || '空'} / 实际=${wbVersion}`);
} catch (e) {
  add('workbuddy-target.json 版本同步', false, e.message);
}

// 3) 补丁是否还在（上游更新会整体覆盖 scripts/）
try {
  const inject = fs.readFileSync(INJECT_JS, 'utf8');
  add('补丁 A1 onWindowChange 守卫', inject.includes('target && target.nodeType && root && root.contains(target)'), 'inject.js');
  const compat = fs.readFileSync(COMPAT_JS, 'utf8');
  add('补丁 B1 输入框选择器兼容', compat.includes('div.cr-input-box'), 'workbuddy-compat.js');
} catch (e) {
  add('补丁文件可读', false, e.message);
}

// 4) 语法自检
try {
  const { execFileSync } = await import('node:child_process');
  execFileSync(NODE_EXE, ['--check', INJECT_JS], { stdio: 'pipe' });
  execFileSync(NODE_EXE, ['--check', COMPAT_JS], { stdio: 'pipe' });
  add('脚本语法自检', true, 'node --check 通过');
} catch (e) {
  add('脚本语法自检', false, String(e.stderr || e.message).slice(0, 200));
}

// 5) 渲染进程注入与关键选择器（需要 WorkBuddy 以 CDP 端口运行）
const PROBE = `(function(){
  var out={};
  out.ua=navigator.userAgent;
  var sel={'.wbs-root':'.wbs-root','FAB':'.wbs-fab','面板':'.wbs-panel',
    '消息列表':'.cr-message-list','输入框':'div.cr-input-box','工具条':'.cr-input-toolbar__right',
    '发送按钮':'.cr-send-button','会话项':'.conversation-item','侧栏卡片':'.cb-agent-card'};
  out.sel={};
  for(var k in sel){out.sel[k]=document.querySelectorAll(sel[k]).length;}
  var c=window.__wbsWorkBuddyCompat;
  out.compat=!!c;
  try{out.toolbar=(c.findComposerToolbar(document)||{}).kind||null;}catch(e){out.toolbar='ERR';}
  try{out.queue=!!c.findQueueAdapter(document);}catch(e){out.queue='ERR';}
  try{out.controllers=(c.findConversationControllers(document)||[]).length;}catch(e){out.controllers='ERR';}
  try{out.sessions=c.findConversationListRecords(document).length;}catch(e){out.sessions='ERR';}
  return JSON.stringify(out);
})()`;

async function cdpProbe() {
  let targets;
  try {
    const res = await fetch('http://127.0.0.1:9222/json/list', { signal: AbortSignal.timeout(3000) });
    targets = await res.json();
  } catch (e) {
    return { error: 'CDP 不可达（WorkBuddy 未以远程调试端口启动）：' + e.message };
  }
  const page = targets.find(t => t.type === 'page' && /WorkBuddy/i.test(t.title));
  if (!page) return { error: '未找到 WorkBuddy 页面目标' };
  const { default: WebSocket } = await import('file:///' + path.join(WD_SCRIPTS, 'node_modules/ws/index.js').replace(/\\/g, '/'));
  return await new Promise((resolve) => {
    const ws = new WebSocket(page.webSocketDebuggerUrl, { perMessageDeflate: false });
    const timer = setTimeout(() => { try { ws.close(); } catch (_) {} resolve({ error: 'CDP 探测超时' }); }, 8000);
    ws.on('open', () => ws.send(JSON.stringify({
      id: 1, method: 'Runtime.evaluate',
      params: { expression: PROBE, returnByValue: true, awaitPromise: true },
    })));
    ws.on('message', (raw) => {
      let msg; try { msg = JSON.parse(raw.toString()); } catch { return; }
      if (msg.id !== 1) return;
      clearTimeout(timer);
      try { ws.close(); } catch (_) {}
      const v = msg.result && msg.result.result && msg.result.result.value;
      resolve(v ? JSON.parse(v) : { error: '探测返回空' });
    });
    ws.on('error', (e) => { clearTimeout(timer); resolve({ error: 'WebSocket 错误：' + e.message }); });
  });
}

const probe = await cdpProbe();
if (probe.error) {
  // CDP 不可达时区分两种情况：客户端没开，或客户端被「裸启动」了（无调试端口）。
  let running = false;
  try {
    const { execFileSync } = await import('node:child_process');
    const tl = execFileSync('tasklist', ['/FI', 'IMAGENAME eq WorkBuddy.exe', '/FO', 'CSV', '/NH'], { stdio: 'pipe' }).toString();
    running = /WorkBuddy\.exe/i.test(tl);
  } catch (_) {}
  const hint = running
    ? 'WorkBuddy 正在运行但没有调试端口（多为从桌面直启，未走 WorkDaddy 启动器）。请完全退出后用 WorkDaddy 启动器重开。'
    : 'WorkBuddy 未运行，请用 WorkDaddy 启动器启动后再体检。';
  add('渲染进程注入', false, probe.error);
  add('调试端口诊断', false, hint);
  for (const name of ['输入框/工具条识别', '队列适配器', '会话与控制器', 'WorkBuddy 运行版本']) add(name, false, '未测（CDP 不可达）');
} else {
  add('渲染进程注入', probe.sel['.wbs-root'] > 0 && !!probe.compat, `root=${probe.sel['.wbs-root']} compat=${probe.compat}`);
  add('输入框/工具条识别', probe.sel['输入框'] > 0 && probe.toolbar === 'modern', `box=${probe.sel['输入框']} toolbar=${probe.toolbar}`);
  add('队列适配器', probe.queue === true, String(probe.queue));
  add('会话与控制器', probe.controllers > 0 && probe.sessions > 0, `controllers=${probe.controllers} sessions=${probe.sessions}`);
  add('WorkBuddy 运行版本', String(probe.ua || '').includes('WorkBuddy/' + wbVersion), (String(probe.ua || '').match(/WorkBuddy\/[\d.]+/) || ['未识别'])[0]);
}

// 6) 崩溃回归：补丁后是否还出现 onWindowChange 报错
// 注意：只有注入实际挂载时这条才有效；无注入时进程内根本没有该监听，不能算通过。
const injected = !probe.error && probe.sel && probe.sel['.wbs-root'] > 0;
try {
  if (!injected) {
    add('resize 崩溃回归', false, '未测（当前无注入，进程内不存在该监听）');
  } else {
    const injectMtime = fs.statSync(INJECT_JS).mtime;
    const lines = fs.readFileSync(DAEMON_LOG, 'utf8').split('\n').filter(l => /onWindowChange/.test(l) && /crash:error/.test(l));
    const last = lines.length ? lines[lines.length - 1] : '';
    const m = /\[(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\]/.exec(last);
    const lastTime = m ? new Date(m[1]) : null;
    const stale = lastTime && lastTime > injectMtime;
    add('resize 崩溃回归', !stale, lastTime ? `最近一次 ${lastTime.toISOString()}` : '日志内无记录');
  }
} catch (e) {
  add('resize 崩溃回归', false, e.message);
}

// 7) 近 24 小时「有内容」的崩溃（排除 WorkBuddy 自身上报的 crash:error:null 噪声）
try {
  const cutoff = Date.now() - 24 * 3600 * 1000;
  const injectMtime = fs.existsSync(INJECT_JS) ? fs.statSync(INJECT_JS).mtime : new Date(0);
  let meaningful = 0;
  let afterPatch = 0;
  let latest = null;
  for (const line of fs.readFileSync(DAEMON_LOG, 'utf8').split('\n')) {
    if (!/crash:error:/.test(line)) continue;
    if (/crash:error:null\b/.test(line)) continue;          // 空上报，官方 crash 探针噪声
    if (/wbg-filter-probe/.test(line)) continue;            // WB-GUARD 自检探针
    const m = /\[(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\]/.exec(line);
    if (!m) continue;
    const t = new Date(m[1]);
    if (t.getTime() < cutoff) continue;
    meaningful++;
    latest = m[1];
    if (t > injectMtime) afterPatch++;
  }
  add('近 24h 有效崩溃', afterPatch === 0,
    meaningful === 0 ? '0 条' : `${meaningful} 条，补丁后新增 ${afterPatch} 条${latest ? '，最近 ' + latest : ''}`);
} catch (e) {
  add('近 24h 有效崩溃', false, e.message);
}

// 8) WB-GUARD 的 r6 补丁标记（与本项目补丁互不覆盖，但升级会一起丢）
try {
  const marks = ['inject.js', 'daemon.js'].map(f => {
    try { return /\[wbg-patch r\d+\]/.test(fs.readFileSync(path.join(WD_SCRIPTS, f), 'utf8')) ? `${f}:有` : `${f}:无`; }
    catch { return `${f}:?`; }
  });
  add('WB-GUARD r6 标记', !marks.some(m => m.endsWith(':无')), marks.join(' '));
} catch (e) {
  add('WB-GUARD r6 标记', false, e.message);
}

// 输出
const pad = (s, n) => { let w = 0; for (const ch of String(s)) w += /[\u4e00-\u9fa5]/.test(ch) ? 2 : 1; return String(s) + ' '.repeat(Math.max(1, n - w)); };
console.log('WorkDaddy 升级体检   ' + new Date().toLocaleString('zh-CN'));
console.log('WorkBuddy 版本：' + wbVersion + '    WorkDaddy 脚本目录：' + WD_SCRIPTS);
console.log('-'.repeat(78));
for (const r of rows) console.log((r.ok ? '[通过] ' : '[失败] ') + pad(r.name, 30) + r.detail);
console.log('-'.repeat(78));
const failed = rows.filter(r => !r.ok);
console.log(failed.length ? `结论：${failed.length} 项未通过` : '结论：全部通过');

// 9) 留档：按天追加到 backups/patches/checks/<yyyy-mm-dd>.json
try {
  const dir = path.join(WD_DATA, 'backups/patches/checks');
  fs.mkdirSync(dir, { recursive: true });
  const d = new Date();
  const p2 = n => String(n).padStart(2, '0');
  const day = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
  const file = path.join(dir, `${day}.json`);
  let history = [];
  if (fs.existsSync(file)) {
    try { history = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { history = []; }
    if (!Array.isArray(history)) history = [];
  }
  history.push({
    at: d.toISOString(),
    workbuddy: wbVersion,
    passed: rows.filter(r => r.ok).length,
    failed: failed.map(r => r.name),
    rows,
  });
  fs.writeFileSync(file, JSON.stringify(history, null, 2), 'utf8');
  console.log('已留档：' + file);
} catch (e) {
  console.log('留档失败（不影响体检结论）：' + e.message);
}

process.exit(failed.length ? 1 : 0);
