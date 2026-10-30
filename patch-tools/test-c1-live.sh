#!/usr/bin/env bash
# C1 在线验证（会临时拉起守护进程，慎用）：
#   对「状态为运行中」的会话发一次跨账号复制请求，观察是否先等待、超时才报错。
#   补丁生效时：耗时约 60 秒，返回「当前账号有会话仍在运行」，daemon.log 出现 [wbg-patch C1] 行。
#   补丁未生效时：1 秒内直接返回同一错误，日志无 C1 行。
# 前提：本机存在至少一个运行中的会话与第二个账号；脚本不落任何会话副本（守卫拦在前面）。
set -u
APPDIR="$APPDATA/WorkDaddy"
NODE="$LOCALAPPDATA/Programs/WorkDaddy/scripts/runtime/node/node.exe"
DJS="$LOCALAPPDATA/Programs/WorkDaddy/scripts/daemon.js"
TOK=$(tr -d '\r\n' < "$APPDIR/.api-token")

"$NODE" --experimental-sqlite "$DJS" >/dev/null 2>&1 &
DPID=$!
echo "守护进程 PID=$DPID"

PORT=""
for i in $(seq 1 40); do
  PORT=$(python -c "import json;d=json.load(open(r'$APPDIR/ui-port.json'));print(d['port'])" 2>/dev/null || true)
  if [ -n "$PORT" ] && curl -s --noproxy '*' -m 2 -o /dev/null -w '%{http_code}' -H "X-WorkDaddy-Token: $TOK" "http://127.0.0.1:$PORT/api/accounts" | grep -q 200; then
    echo "接口就绪：端口 $PORT（第 $i 次探测）"; break
  fi
  sleep 1
done
[ -z "$PORT" ] && { echo "接口未就绪，终止"; kill $DPID 2>/dev/null; exit 1; }

curl -s --noproxy '*' -m 20 -H "X-WorkDaddy-Token: $TOK" "http://127.0.0.1:$PORT/api/sessions" -o "$USERPROFILE/sess.json"
python - "$USERPROFILE/sess.json" <<'PY'
import json,sys
from collections import Counter
d=json.load(open(sys.argv[1],encoding='utf-8'))
rows=d.get('sessions') if isinstance(d,dict) else d
print('会话总数',len(rows),'状态分布',dict(Counter(r.get('status') for r in rows)))
busy=[r for r in rows if str(r.get('status') or '').lower() in ('working','planning')]
for r in busy[:5]:
    print('BUSY', r.get('id'), r.get('status'), r.get('user_id'), (r.get('custom_title') or r.get('title') or '')[:40])
open(sys.argv[1]+'.busy','w',encoding='utf-8').write('\n'.join(r['id'] for r in busy))
PY

BUSY_ID=$(head -1 "$USERPROFILE/sess.json.busy" 2>/dev/null)
curl -s --noproxy '*' -m 20 -H "X-WorkDaddy-Token: $TOK" "http://127.0.0.1:$PORT/api/accounts" -o "$USERPROFILE/acct.json"
TARGET=$(python - "$USERPROFILE/acct.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
rows=d.get('accounts') if isinstance(d,dict) else d
print(rows[1]['uid'] if len(rows)>1 else '')
PY
)
echo "目标账号 uid=${TARGET:-空}"

if [ -z "$BUSY_ID" ] || [ -z "$TARGET" ]; then
  echo "未发现处于运行中的会话或目标账号，跳过复制验证"
else
  echo "对忙碌会话 $BUSY_ID 发起复制，观察耗时与返回"
  START=$(date +%s)
  RESP=$(curl -s --noproxy '*' -m 120 -w '\nHTTP:%{http_code}' -H "X-WorkDaddy-Token: $TOK" -H 'Content-Type: application/json' \
    -d "{\"ids\":[\"$BUSY_ID\"],\"targetUid\":\"$TARGET\"}" "http://127.0.0.1:$PORT/api/sessions/copy")
  END=$(date +%s)
  echo "耗时 $((END-START)) 秒"
  echo "$RESP"
fi

echo "--- 守护日志中的 C1 记录 ---"
grep -a "wbg-patch C1" "$APPDIR/daemon.log" | tail -5
