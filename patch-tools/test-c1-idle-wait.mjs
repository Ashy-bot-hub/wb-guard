'use strict';
// C1 补丁行为测试：从 daemon.js 里抽出已打补丁的函数原文，注入桩件后跑五种场景。
// 只读 daemon.js，不改任何文件。用法：node patch-tools/test-c1-idle-wait.mjs
import fs from 'node:fs';
import path from 'node:path';

const HOME = process.env.USERPROFILE || 'C:/Users/Administrator';
const SCRIPTS = (() => {
  const arg = process.argv.find(a => a.startsWith('--scripts='));
  return arg ? arg.slice('--scripts='.length) : path.join(HOME, 'AppData/Local/Programs/WorkDaddy/scripts');
})();

const src = fs.readFileSync(path.join(SCRIPTS, 'daemon.js'), 'utf8');
const startAt = src.indexOf('// [wbg-patch C1]');
const endAt = src.indexOf('async function assertAccountSwitchIdle() {');
if (startAt < 0 || endAt < 0) {
  console.error('未找到 C1 补丁段落，先跑 apply-patches.mjs --only=c1 --apply');
  process.exit(1);
}
const block = src.slice(startAt, endAt);

const factory = new Function('PROFILE', 'cdp', 'cdpSend', 'codeBuddyNative', 'log', 'sleep', 'Date',
  block + '\nreturn { assertSessionSyncIdle, sessionSyncBusy, SESSION_SYNC_IDLE_WAIT_MS, SESSION_SYNC_IDLE_POLL_MS };');

const WAIT = 60000;
const POLL = 2000;
const BUSY_MSG = '当前账号有会话仍在运行，请等待完成或停止后再同步';
const T0 = 1_000_000;

let failures = 0;
const ok = (name, cond, detail = '') => {
  console.log(`${cond ? '[通过]' : '[失败]'} ${name}${detail ? '  ' + detail : ''}`);
  if (!cond) failures++;
};

function build({ busySeq = [false], codebuddySeq = ['Idle'], kind = 'workbuddy', connected = true }) {
  const logs = [];
  let clock = T0;
  let busyCalls = 0;
  let cdCalls = 0;
  const cdpSend = async () => {
    const v = busySeq[Math.min(busyCalls, busySeq.length - 1)];
    busyCalls++;
    return { result: { value: v } };
  };
  const codeBuddyNative = {
    sessionItems: async () => {
      const status = codebuddySeq[Math.min(cdCalls, codebuddySeq.length - 1)];
      cdCalls++;
      return [{ value: JSON.stringify({ conversationId: 's1', status }) }];
    },
  };
  const api = factory(
    { kind },
    { connected },
    cdpSend,
    codeBuddyNative,
    (...a) => logs.push(a.join(' ')),
    async ms => { clock += ms; },
    { now: () => clock },
  );
  return { api, logs, waited: () => clock - T0, calls: () => busyCalls };
}

// 场景一：一开始就空闲，应立刻返回，不写等待日志
{
  const t = build({ busySeq: [false] });
  await t.api.assertSessionSyncIdle(['a']);
  ok('空闲直通：不等待、不记日志', t.calls() === 1 && t.logs.length === 0, `探测 ${t.calls()} 次，日志 ${t.logs.length} 条`);
}

// 场景二：忙碌两轮后转空闲，应等待并继续
{
  const t = build({ busySeq: [true, true, false] });
  await t.api.assertSessionSyncIdle(['a']);
  ok('短时忙碌：等待后放行', t.calls() === 3 && t.waited() === POLL * 2, `等待 ${t.waited()} 毫秒，探测 ${t.calls()} 次`);
  ok('短时忙碌：留下开始与恢复两条日志', t.logs.length === 2 && t.logs[0].includes('等待空闲') && t.logs[1].includes('会话已空闲'));
}

// 场景三：持续忙碌，应在超时后抛原错误文案
{
  const t = build({ busySeq: [true] });
  let error = null;
  try { await t.api.assertSessionSyncIdle(['a']); } catch (e) { error = e; }
  ok('持续忙碌：超时报错且文案不变', !!error && error.message === BUSY_MSG, error ? error.message : '未抛错');
  ok('持续忙碌：等待时长卡在阈值', t.waited() >= WAIT && t.waited() < WAIT + POLL + 1, `等待 ${t.waited()} 毫秒，阈值 ${WAIT}`);
  ok('持续忙碌：只记一条开始日志', t.logs.length === 1, `日志 ${t.logs.length} 条`);
}

// 场景四：codebuddy 档案下按会话状态忙碌，同样走等待分支
{
  const t = build({ kind: 'codebuddy', codebuddySeq: ['Working', 'Idle'] });
  await t.api.assertSessionSyncIdle([]);
  ok('codebuddy 档案：忙碌时同样等待后放行', t.waited() === POLL && t.logs.length === 2, `等待 ${t.waited()} 毫秒，日志 ${t.logs.length} 条`);
}

// 场景五：CDP 未连接属硬错误，不应进入等待
{
  const t = build({ busySeq: [false], connected: false });
  let error = null;
  try { await t.api.assertSessionSyncIdle(['a']); } catch (e) { error = e; }
  ok('未连接 CDP：直接报错不进等待', !!error && error.message.includes('无法确认会话状态') && t.calls() === 0, error ? error.message : '未抛错');
}

console.log('-'.repeat(60));
console.log(`补丁段字节数 ${Buffer.byteLength(block, 'utf8')}；阈值 ${WAIT} 毫秒、轮询 ${POLL} 毫秒；失败 ${failures} 项`);
process.exit(failures ? 1 : 0);
