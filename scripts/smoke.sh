#!/usr/bin/env bash
# Manual end-to-end smoke test against a running service.
#
# Usage:
#   ENVAPI_URL=http://localhost:8008 TOKEN=<keyring user token> ./scripts/smoke.sh
#   ENVAPI_URL=http://localhost:8008 DEV_KEYRING_URL=http://localhost:8001 ./scripts/smoke.sh
#
# With a real keyring, mint TOKEN via POST /v1/auth/service-token {"audience": "environments-api"}
# (and TOKEN2 for a second account to prove isolation). With scripts/dev_keyring.py running,
# set DEV_KEYRING_URL and the script mints both tokens itself.
set -euo pipefail

ENVAPI_URL="${ENVAPI_URL:-http://127.0.0.1:8008}"
DEV_KEYRING_URL="${DEV_KEYRING_URL:-}"
TOKEN="${TOKEN:-}"
TOKEN2="${TOKEN2:-}"

need() { command -v "$1" >/dev/null || { echo "missing $1" >&2; exit 1; }; }
need curl; need python3

j() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }
api() { # method path [json]
  if [ $# -ge 3 ]; then
    curl -s -X "$1" "$ENVAPI_URL$2" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d "$3"
  else
    curl -s -X "$1" "$ENVAPI_URL$2" -H "Authorization: Bearer $TOKEN"
  fi
}
step() { printf '\n== %s\n' "$*"; }
expect() { # description actual expected
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: got '$2', expected '$3'" >&2; exit 1; fi
}

if [ -n "$DEV_KEYRING_URL" ]; then
  TOKEN=$(curl -s -X POST "$DEV_KEYRING_URL/dev/mint" -H 'content-type: application/json' -d '{"account_id":"smoke-a"}' | j 'd["token"]')
  TOKEN2=$(curl -s -X POST "$DEV_KEYRING_URL/dev/mint" -H 'content-type: application/json' -d '{"account_id":"smoke-b"}' | j 'd["token"]')
  curl -s -X PUT "$DEV_KEYRING_URL/dev/credentials/personal/github" -H 'content-type: application/json' -d '{"account_id":"smoke-a","headers":{"Authorization":"Bearer ghp_smoke_secret_value"}}' >/dev/null
fi
[ -n "$TOKEN" ] || { echo "set TOKEN or DEV_KEYRING_URL" >&2; exit 1; }

step "health"
curl -s "$ENVAPI_URL/health/ready" | j '"tier=" + d["sandbox_tier"] + " keyring=" + d["keyring"]["status"]'

step "user token: Authorization: Bearer, or the legacy header on its own"
expect "no token" "$(curl -s -o /dev/null -w '%{http_code}' "$ENVAPI_URL/v1/environments")" "401"
expect "legacy header" "$(curl -s -o /dev/null -w '%{http_code}' "$ENVAPI_URL/v1/environments" -H "X-Keyring-User-Token: $TOKEN")" "200"

step "create environment"
ENV_JSON=$(api POST /v1/environments '{"name":"smoke","credentials":["github"],"labels":{"suite":"smoke"}}')
EID=$(echo "$ENV_JSON" | j 'd["id"]')
echo "$ENV_JSON" | j '"id=" + d["id"] + " tier=" + d["sandbox_tier"] + " workspace=" + d["workspace"]'

step "open shell, echo hello"
SID=$(api POST "/v1/environments/$EID/shells" '{}' | j 'd["id"]')
OUT=$(api POST "/v1/shells/$SID/exec" '{"command":"echo hello","wait_ms":5000}')
expect "output" "$(echo "$OUT" | j 'd["output"]')" "hello"
expect "exit code" "$(echo "$OUT" | j 'd["exit_code"]')" "0"

step "background processes are visible, then killed with the command"
api POST "/v1/shells/$SID/exec" '{"command":"sh -c \"sleep 300 & sleep 300\" & sleep 300"}' >/dev/null
for _ in $(seq 1 50); do
  COUNT=$(api GET "/v1/environments/$EID/processes" | j 'len([p for p in d["processes"] if p["cmdline"].startswith("sleep")])')
  [ "$COUNT" -ge 3 ] && break; sleep 0.1
done
expect "sleepers visible" "$COUNT" "3"
PIDS=$(api GET "/v1/environments/$EID/processes" | j '" ".join(str(p["pid"]) for p in d["processes"] if p["cmdline"].startswith("sleep"))')
api POST "/v1/shells/$SID/signal" '{"signal":"TERM"}' >/dev/null
expect "shell idle after signal" "$(api POST "/v1/shells/$SID/wait" '{"timeout_ms":5000}' | j 'd["status"]')" "idle"
sleep 0.2
LEFT=0; for p in $PIDS; do if [ -d "/proc/$p" ] && [ "$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null)" != "Z" ]; then LEFT=$((LEFT+1)); fi; done
[ -d /proc/1 ] && expect "no sleeper survived (grandchildren included)" "$LEFT" "0"

step "quota is named"
api POST /v1/environments '{"name":"filler-1"}' >/dev/null || true
api POST /v1/environments '{"name":"filler-2"}' >/dev/null || true
api POST /v1/environments '{"name":"filler-3"}' >/dev/null || true
api POST /v1/environments '{"name":"filler-4"}' >/dev/null || true
LIMIT=$(api POST /v1/environments '{"name":"one-too-many"}' | j 'd.get("limit", d.get("id"))')
echo "create beyond quota -> $LIMIT"

step "sandbox behaviour (compare with the tier reported above)"
api POST /v1/exec "{\"environment_id\":\"$EID\",\"command\":\"id -u; cat /etc/shadow >/dev/null 2>&1 && echo SHADOW-READABLE || echo shadow-refused; touch /etc/smoke 2>/dev/null && echo ROOT-WRITABLE || echo root-readonly; ls /proc | grep -c '^[0-9]'\"}" | j 'd["output"]'

step "credential injected and redacted"
expect "redacted" "$(api POST /v1/exec "{\"environment_id\":\"$EID\",\"command\":\"echo \$GITHUB_TOKEN\"}" | j 'd["output"].strip()')" "«redacted:github»"

step "path escape refused"
expect "escape code" "$(api GET "/v1/environments/$EID/files/content?path=../../etc/passwd" | j 'd["code"]')" "path_outside_workspace"

if [ -n "$TOKEN2" ]; then
  step "second account cannot see, poll or delete"
  for path in "/v1/environments/$EID" "/v1/shells/$SID/output"; do
    expect "GET $path as B" "$(curl -s -o /dev/null -w '%{http_code}' "$ENVAPI_URL$path" -H "Authorization: Bearer $TOKEN2")" "404"
  done
  expect "DELETE as B" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$ENVAPI_URL/v1/environments/$EID" -H "Authorization: Bearer $TOKEN2")" "404"
  expect "A and B on one request" "$(curl -s -o /dev/null -w '%{http_code}' "$ENVAPI_URL/v1/environments" -H "Authorization: Bearer $TOKEN" -H "X-Keyring-User-Token: $TOKEN2")" "401"
fi

step "cleanup"
for id in $(api GET "/v1/environments?label=suite=smoke" | j '" ".join(e["id"] for e in d["environments"])'); do api DELETE "/v1/environments/$id" >/dev/null; done
for id in $(api GET "/v1/environments" | j '" ".join(e["id"] for e in d["environments"] if e["name"].startswith("filler-"))'); do api DELETE "/v1/environments/$id" >/dev/null; done
echo
echo "smoke passed. Still to do by hand: restart the service and check the environment returns,"
echo "shells come back dead (service_restarted) and no process from before survives; stop keyring"
echo "and check tokens still verify from the keys already held and /health stays up, while"
echo "credential injection fails with 503 naming keyring."
