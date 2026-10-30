'use strict';
// 升级后一键重打本机 WorkDaddy 补丁（A1/B1/C1 + B2）。
// 默认只预览；确认无误后加 --apply 落盘。幂等：已有补丁标记就跳过。
// 用法：
//   node apply-patches.mjs            # 预览
//   node apply-patches.mjs --apply    # 执行
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

const HOME = process.env.USERPROFILE || 'C:/Users/Administrator';
const SCRIPTS = (() => {
  const arg = process.argv.find(a => a.startsWith('--scripts='));
  return arg ? arg.slice('--scripts='.length) : path.join(HOME, 'AppData/Local/Programs/WorkDaddy/scripts');
})();
const DATA_DIR = path.join(HOME, 'AppData/Roaming/WorkDaddy');
const BACKUPS = path.join(DATA_DIR, 'backups/patches');
const NODE_EXE = path.join(HOME, 'AppData/Local/Programs/WorkDaddy/scripts/runtime/node/node.exe');
const MANIFEST = 'C:/Program Files/WorkBuddy/resources/install-manifest.json';
const BINARY = ['C:', 'Program Files', 'WorkBuddy', 'WorkBuddy.exe'].join(String.fromCharCode(92));

const APPLY = process.argv.includes('--apply');
// --only=c1 只打指定项（逗号分隔，可写 a1,b1,c1,b2）；不传则全打
const ONLY = ((process.argv.find(a => a.startsWith('--only=')) || '').slice('--only='.length))
  .split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
const want = tag => !ONLY.length || ONLY.includes(tag);
const stamp = (() => {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
})();

const A1_MARK = 'target && target.nodeType && root && root.contains(target)';
const A1_OLD_LINES = [
  '      function onWindowChange(event) {',
  '        if (event && root && root.contains(event.target)) { positionFlyouts(); return; }',
  '        position(); scheduleActive();',
  '      }',
];
const A1_NEW_LINES = [
  '      function onWindowChange(event) {',
  '        // resize 事件的 target 是 window，不是 Node；直接传进 contains() 会抛',
  '        // TypeError（WorkBuddy 5.9.2 下每次改窗口尺寸都记一条未捕获异常）。',
  '        try {',
  '          var target = event && event.target;',
  '          if (target && target.nodeType && root && root.contains(target)) { positionFlyouts(); return; }',
  '        } catch (_) {}',
  '        position(); scheduleActive();',
  '      }',
];

const B1_MARK = "div.cr-input-box'";
const B1_OLD_LINES = ["    var selector = '[class*=\"_inputBox_\"]';"];
const B1_NEW_LINES = [
  '    // WorkBuddy 5.9.2 已移除 _inputBox_ 类，输入框容器改用 div.cr-input-box；',
  '    // 两个都试，避免官方改名后这条路径直接失效。',
  '    var selector = \'[class*="_inputBox_"],div.cr-input-box\';',
];

// C1：切号同步遇到「会话仍在运行」时先等待，不再立刻抛错。
// 原实现一见 Working / Planning 就抛，导致整轮账号切换中止，并把失败记录挂到
// 账号名下形成屏障，后续切号全被拦（本机 10-25 至 10-30 共 16 次）。
// 这里改成轮询等待：默认最多 60 秒，每 2 秒复查一次，超时才抛原错误。
const C1_MARK = '[wbg-patch C1]';
const C1_START_LINE = 'async function assertSessionSyncIdle(sessionIds = []) {';
const C1_END_LINE = 'async function assertAccountSwitchIdle() {';
const C1_NEW_LINES = [
  '// [wbg-patch C1] 会话忙碌时先轮询等待空闲，超时才报错（原逻辑立即抛错，会中止整轮切号）。',
  'const SESSION_SYNC_IDLE_WAIT_MS = 60000;',
  'const SESSION_SYNC_IDLE_POLL_MS = 2000;',
  'async function sessionSyncBusy(sessionIds = []) {',
  "  if (PROFILE.kind === 'codebuddy') {",
  '    const sessions = (await codeBuddyNative.sessionItems()).map(item => JSON.parse(item.value));',
  "    return sessions.some(s => (!sessionIds.length || sessionIds.includes(s.conversationId)) && ['Working', 'Planning'].includes(s.status));",
  '  }',
  "  if (PROFILE.kind !== 'workbuddy') return false;",
  "  if (!cdp.connected) throw new Error('无法确认会话状态，请连接 WorkBuddy 后重试');",
  "  const ids = Array.isArray(sessionIds) ? sessionIds.map(id => String(id || '').trim()).filter(Boolean) : [];",
  '  const expression = ids.length',
  '    ? "typeof window.__wbsSessionsBusy === \'function\' ? window.__wbsSessionsBusy(" + JSON.stringify(ids) + ") : (typeof window.__wbsAnySessionBusy === \'function\' ? window.__wbsAnySessionBusy() : null)"',
  '    : "typeof window.__wbsAnySessionBusy === \'function\' ? window.__wbsAnySessionBusy() : null";',
  "  const result = await cdpSend('Runtime.evaluate', { expression, returnByValue: true });",
  '  const busy = result && result.result && result.result.value;',
  "  if (result.exceptionDetails || typeof busy !== 'boolean') throw new Error('无法确认会话状态，请等待面板加载后重试');",
  '  return busy;',
  '}',
  'async function assertSessionSyncIdle(sessionIds = []) {',
  '  const startedAt = Date.now();',
  '  let announced = false;',
  '  for (;;) {',
  '    const busy = await sessionSyncBusy(sessionIds);',
  '    if (!busy) {',
  "      if (announced) log('[wbg-patch C1] 会话已空闲，继续同步（等待 ' + Math.round((Date.now() - startedAt) / 1000) + ' 秒）');",
  '      return;',
  '    }',
  "    if (Date.now() - startedAt >= SESSION_SYNC_IDLE_WAIT_MS) throw new Error('当前账号有会话仍在运行，请等待完成或停止后再同步');",
  '    if (!announced) {',
  '      announced = true;',
  "      log('[wbg-patch C1] 会话仍在运行，等待空闲后再同步（最多 ' + Math.round(SESSION_SYNC_IDLE_WAIT_MS / 1000) + ' 秒）');",
  '    }',
  '    await sleep(Math.min(SESSION_SYNC_IDLE_POLL_MS, Math.max(200, SESSION_SYNC_IDLE_WAIT_MS - (Date.now() - startedAt))));',
  '  }',
  '}',
];

const results = [];
const sha256 = buf => createHash('sha256').update(buf).digest('hex');

function backup(file, tag) {
  fs.mkdirSync(BACKUPS, { recursive: true });
  const base = path.basename(file);
  const ext = path.extname(base);
  const stem = base.slice(0, -ext.length);
  const dest = path.join(BACKUPS, `${stem}_${tag}前_${stamp}${ext}`);
  fs.copyFileSync(file, dest);
  return dest;
}

function patchFile(name, oldLines, newLines, marker) {
  const file = path.join(SCRIPTS, name);
  if (!fs.existsSync(file)) {
    results.push({ name, state: '缺失', detail: file });
    return;
  }
  const src = fs.readFileSync(file, 'utf8');
  // 保留文件原有行尾：上游 inject.js 是 CRLF，workbuddy-compat.js 也是 CRLF
  const eol = /\r\n/.test(src) ? '\r\n' : '\n';
  const oldText = oldLines.join(eol);
  const newText = newLines.join(eol);
  if (src.includes(marker)) {
    results.push({ name, state: '已是最新', detail: '补丁标记存在' });
    return;
  }
  if (!src.includes(oldText)) {
    results.push({ name, state: '需手工', detail: '未找到待替换片段，可能上游版本已变' });
    return;
  }
  if (!APPLY) {
    results.push({ name, state: '待打', detail: '预览模式，加 --apply 执行' });
    return;
  }
  const bak = backup(file, '修改');
  fs.writeFileSync(file, src.replace(oldText, newText), 'utf8');
  try {
    execFileSync(NODE_EXE, ['--check', file], { stdio: 'pipe' });
    results.push({ name, state: '已打', detail: `语法通过；备份 ${path.basename(bak)}；sha256 ${sha256(fs.readFileSync(file)).slice(0, 12)}` });
  } catch (e) {
    fs.copyFileSync(bak, file);
    results.push({ name, state: '回滚', detail: '改后语法校验失败，已用备份还原：' + String(e.stderr || e.message).slice(0, 120) });
  }
}

// A1 / B1
if (want('a1')) patchFile('inject.js', A1_OLD_LINES, A1_NEW_LINES, A1_MARK);
if (want('b1')) patchFile('workbuddy-compat.js', B1_OLD_LINES, B1_NEW_LINES, B1_MARK);

// C1：整段替换 assertSessionSyncIdle（daemon.js 是纯 LF）
function patchRange(name, startLine, endLine, newLines, marker) {
  const file = path.join(SCRIPTS, name);
  if (!fs.existsSync(file)) {
    results.push({ name, state: '缺失', detail: file });
    return;
  }
  const src = fs.readFileSync(file, 'utf8');
  if (src.includes(marker)) {
    results.push({ name, state: '已是最新', detail: '补丁标记存在' });
    return;
  }
  const startAt = src.indexOf(startLine);
  const endAt = startAt < 0 ? -1 : src.indexOf(endLine, startAt);
  if (startAt < 0 || endAt < 0) {
    results.push({ name, state: '需手工', detail: '未找到函数边界，可能上游版本已变' });
    return;
  }
  if (!APPLY) {
    results.push({ name, state: '待打', detail: '预览模式，加 --apply 执行' });
    return;
  }
  const eol = /\r\n/.test(src) ? '\r\n' : '\n';
  const next = src.slice(0, startAt) + newLines.join(eol) + eol + src.slice(endAt);
  const bak = backup(file, '修改');
  fs.writeFileSync(file, next, 'utf8');
  try {
    execFileSync(NODE_EXE, ['--check', file], { stdio: 'pipe' });
    results.push({ name, state: '已打', detail: `语法通过；备份 ${path.basename(bak)}；sha256 ${sha256(fs.readFileSync(file)).slice(0, 12)}` });
  } catch (e) {
    fs.copyFileSync(bak, file);
    results.push({ name, state: '回滚', detail: '改后语法校验失败，已用备份还原：' + String(e.stderr || e.message).slice(0, 120) });
  }
}

if (want('c1')) patchRange('daemon.js', C1_START_LINE, C1_END_LINE, C1_NEW_LINES, C1_MARK);

// B2：目标档案版本跟随安装包
if (want('b2')) try {
  const wbVersion = JSON.parse(fs.readFileSync(MANIFEST, 'utf8')).appVersion;
  const targetPath = path.join(DATA_DIR, 'workbuddy-target.json');
  const cur = fs.existsSync(targetPath) ? JSON.parse(fs.readFileSync(targetPath, 'utf8')) : null;
  if (cur && String(cur.version) === String(wbVersion)) {
    results.push({ name: 'workbuddy-target.json', state: '已是最新', detail: `version=${wbVersion}` });
  } else if (!APPLY) {
    results.push({ name: 'workbuddy-target.json', state: '待改', detail: `当前=${cur ? cur.version : '缺失'} → ${wbVersion}` });
  } else {
    const t = require(path.join(SCRIPTS, 'workbuddy-target.js'));
    if (cur) backup(targetPath, '修改');
    const target = t.buildOfficialTargetFromBinary({ binary: BINARY, profileId: 'workbuddy-cn', version: wbVersion, platform: 'win32' });
    t.writeWorkBuddyTarget({ dataDir: DATA_DIR, profileId: 'workbuddy-cn', target, platform: 'win32' });
    results.push({ name: 'workbuddy-target.json', state: '已改', detail: `version=${wbVersion}` });
  }
} catch (e) {
  results.push({ name: 'workbuddy-target.json', state: '失败', detail: e.message });
}

// 旁证：WB-GUARD 的 r6 补丁是否也在（不自动打，交给 panel-patches/apply.py）
const guard = [];
for (const f of ['inject.js', 'daemon.js']) {
  try {
    const s = fs.readFileSync(path.join(SCRIPTS, f), 'utf8');
    guard.push(`${f}:${/\[wbg-patch r\d+\]/.test(s) ? '有' : '无'}`);
  } catch (_) {
    guard.push(`${f}:?`);
  }
}

const pad = (s, n) => { let w = 0; for (const ch of String(s)) w += /[\u4e00-\u9fa5]/.test(ch) ? 2 : 1; return String(s) + ' '.repeat(Math.max(1, n - w)); };
console.log('WorkDaddy 补丁重打   ' + new Date().toLocaleString('zh-CN') + (APPLY ? '   [执行]' : '   [预览]'));
console.log('-'.repeat(74));
for (const r of results) console.log('[' + r.state + '] ' + pad(r.name, 26) + r.detail);
console.log('-'.repeat(74));
console.log('WB-GUARD r6 标记：' + guard.join('  ') + '（丢失时先跑 panel-patches 的 apply.py，再跑本脚本）');
if (!APPLY) console.log('以上为预览，未改任何文件。确认后加 --apply。');
