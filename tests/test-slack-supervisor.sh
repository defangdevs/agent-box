#!/usr/bin/env bash
# The supervisor runs local-slack only while both tokens are in the env store.
set -euo pipefail

SUPERVISOR=${1:?usage: test-slack-supervisor.sh PATH/TO/supervisor.sh}
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/bin"
export CALLS="$TEST_ROOT/calls" STORE="$TEST_ROOT/store"

sed -n '/^supervise_slack() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/function.sh"
# shellcheck source=/dev/null
. "$TEST_ROOT/function.sh"

# A stand-in env store: `get KEY` prints the value of KEY=VALUE from $STORE.
cat > "$TEST_ROOT/bin/envstore" <<'EOF2'
#!/bin/sh
[ "$1" = json ] || exit 2
awk -F= 'BEGIN{printf "{"} NF>1{printf "%s\"%s\":\"%s\"", n++?",":"", $1, $2} END{print "}"}' "$STORE"
EOF2
cat > "$TEST_ROOT/bin/agent-box-slack" <<'EOF2'
#!/bin/sh
printf '%s\t%s\t%s\n' "$*" "$SLACK_APP_TOKEN" "$SLACK_BOT_TOKEN" >> "$CALLS"
trap 'exit 0' TERM INT
while :; do sleep 1 & wait $!; done
EOF2
chmod +x "$TEST_ROOT/bin/"*
AGENT_BOX_SLACK_BIN="$TEST_ROOT/bin/agent-box-slack"
AGENT_BOX_ENVSTORE_BIN="$TEST_ROOT/bin/envstore"
JQ=jq
HOME="$TEST_ROOT/home"
AGENT_BOX_WEBHOOK_SCRIPT=/nonexistent/webhook.py
slack_pid=""
slack_last_start=0

# No store, then one token: nothing starts.
: > "$STORE"
supervise_slack
echo 'SLACK_APP_TOKEN=xapp-1' > "$STORE"
supervise_slack
[ ! -e "$CALLS" ]

# Both tokens but no webhook ingress: nothing starts.
printf 'SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-2\n' > "$STORE"
AGENT_BOX_WEBHOOK_SCRIPT= supervise_slack
[ ! -e "$CALLS" ] && [ -z "$slack_pid" ]

# Both tokens: it starts once, with the tokens in its environment only.
printf 'SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-2\n' > "$STORE"
supervise_slack
for _ in $(seq 1 50); do
  [ -s "$CALLS" ] && break
  sleep 0.02
done
grep -F "$(printf 'serve\txapp-1\txoxb-2')" "$CALLS" >/dev/null
supervise_slack
[ "$(wc -l < "$CALLS")" -eq 1 ]

# Clearing a token stops it.
started_pid=$slack_pid
echo 'SLACK_APP_TOKEN=xapp-1' > "$STORE"
supervise_slack
[ -z "$slack_pid" ]
for _ in $(seq 1 150); do
  kill -0 "$started_pid" 2>/dev/null || break
  sleep 0.02
done
if kill -0 "$started_pid" 2>/dev/null; then
  echo "slack daemon still running after a token was cleared" >&2
  exit 1
fi

# A replaced token restarts it with the new pair.
printf 'SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-2\n' > "$STORE"
slack_last_start=0
supervise_slack
first_pid=$slack_pid
printf 'SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-3\n' > "$STORE"
slack_last_start=0
supervise_slack
[ "$slack_pid" != "$first_pid" ]
for _ in $(seq 1 150); do
  kill -0 "$first_pid" 2>/dev/null || break
  sleep 0.02
done
if kill -0 "$first_pid" 2>/dev/null; then
  echo "old slack daemon survived a token change" >&2
  exit 1
fi
for _ in $(seq 1 50); do
  grep -F xoxb-3 "$CALLS" >/dev/null && break
  sleep 0.02
done
grep -F xoxb-3 "$CALLS" >/dev/null
kill "$slack_pid" 2>/dev/null || true
[ -d "$HOME/.local/state/local-slack" ]

echo "slack supervisor: OK"
