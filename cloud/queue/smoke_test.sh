#!/usr/bin/env bash
# Smoke test for the GMP job queue — runs anywhere (sqlite backend), no ssh.
# Proves: token auth, mixed-priority claim order, complete, fail + retry law,
# and the router (llm stub done / not-wired stub failed permanently).
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-C:/Python313/python.exe}"
TOKEN="smoke-token-123"
BASE="http://127.0.0.1:8901"
export QUEUE_TOKEN="$TOKEN"

rm -f test.db test.db-wal test.db-shm

say() { echo "[smoke] $*"; }
jid() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print((d.get('job') or {}).get('id',''))"; }
field() { "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

say "starting queue_api --backend sqlite"
"$PY" queue_api.py --backend sqlite >api.log 2>&1 &
API_PID=$!

for i in $(seq 1 20); do
  curl -s "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.3
done

say "--- health (no token needed) ---"
curl -s "$BASE/health"; echo

say "--- auth check: GET /jobs without token (expect 403) ---"
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/jobs"

say "--- enqueue 3 jobs of mixed priority (enqueue order: low, high, mid) ---"
curl -s -X POST "$BASE/jobs" -H "X-Token: $TOKEN" \
  -d '{"type":"deploy","payload":{"slug":"sonar"},"priority":9}'; echo
curl -s -X POST "$BASE/jobs" -H "X-Token: $TOKEN" \
  -d '{"type":"llm","payload":{"prompt":"design boss fight","max_tokens":512},"priority":1}'; echo
curl -s -X POST "$BASE/jobs" -H "X-Token: $TOKEN" \
  -d '{"type":"gate","payload":{"game":"octogram-arcade","suites":7},"priority":5}'; echo

say "--- claim x3: priority order must be 1, then 5, then 9 ---"
J_HIGH=$(curl -s -X POST "$BASE/jobs/claim" -H "X-Token: $TOKEN" -d '{"types":["llm","gate","deploy"]}' | jid)
J_MID=$(curl -s -X POST "$BASE/jobs/claim" -H "X-Token: $TOKEN" -d '{"types":["llm","gate","deploy"]}' | jid)
J_LOW=$(curl -s -X POST "$BASE/jobs/claim" -H "X-Token: $TOKEN" -d '{"types":["llm","gate","deploy"]}' | jid)
say "claim order ids: $J_HIGH (p1) -> $J_MID (p5) -> $J_LOW (p9)"

say "--- complete the priority-1 llm job (id=$J_HIGH) ---"
curl -s -X POST "$BASE/jobs/$J_HIGH/result" -H "X-Token: $TOKEN" \
  -d '{"result":{"ok":true}}'; echo

say "--- fail the gate job once (id=$J_MID) -> retry law requeues it ---"
curl -s -X POST "$BASE/jobs/$J_MID/result" -H "X-Token: $TOKEN" \
  -d '{"error":"gate wall red: run_tests 3 failed"}'; echo

say "--- re-claim (gate comes back) and fail again -> failed permanently ---"
curl -s -X POST "$BASE/jobs/claim" -H "X-Token: $TOKEN" -d '{"types":["gate"]}' | jid
curl -s -X POST "$BASE/jobs/$J_MID/result" -H "X-Token: $TOKEN" \
  -d '{"error":"gate wall red again: run_tests 3 failed"}'; echo

say "--- list all statuses (llm=done, deploy still running, gate=failed) ---"
curl -s "$BASE/jobs?limit=10" -H "X-Token: $TOKEN" \
  | "$PY" -c "import json,sys; [print(f\"id={j['id']} type={j['type']} prio={j['priority']} status={j['status']} attempts={j['attempts']} error={j.get('error')}\") for j in json.load(sys.stdin)['jobs']]"

say "--- resolve the p9 deploy job (id=$J_LOW) it was left running by the claim demo ---"
curl -s -X POST "$BASE/jobs/$J_LOW/result" -H "X-Token: $TOKEN" \
  -d '{"result":{"deployed":"https://retromonkey.com.au/games/sonar/"}}'; echo

say "--- router --once on a fresh critique job -> not-wired stub, failed permanently ---"
curl -s -X POST "$BASE/jobs" -H "X-Token: $TOKEN" \
  -d '{"type":"critique","payload":{"slug":"slime-line","frames":20},"priority":2}'; echo
"$PY" router.py --once; say "router exit=$?"

say "--- router --once on a fresh llm job -> real stub logs payload shape -> done ---"
curl -s -X POST "$BASE/jobs" -H "X-Token: $TOKEN" \
  -d '{"type":"llm","payload":{"prompt":"critique slime line","rung":"glm-5.3"},"priority":3}'; echo
"$PY" router.py --once; say "router exit=$?"

say "--- router --once with nothing queued ---"
"$PY" router.py --once; say "router exit=$?"

say "--- final state ---"
curl -s "$BASE/health"; echo
curl -s "$BASE/jobs?limit=10" -H "X-Token: $TOKEN" \
  | "$PY" -c "import json,sys; [print(f\"id={j['id']} type={j['type']} status={j['status']} attempts={j['attempts']}\") for j in json.load(sys.stdin)['jobs']]"

say "--- router --once against a dead API (expect exit 1) ---"
QUEUE_TOKEN="$TOKEN" "$PY" router.py --once --api http://127.0.0.1:8999; say "router exit=$?"

kill "$API_PID" 2>/dev/null
wait "$API_PID" 2>/dev/null
say "queue_api stopped. test.db left in place for inspection."
