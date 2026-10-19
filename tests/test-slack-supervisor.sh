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
v=$(sed -n "s/^$2=//p" "$STORE") || exit 1
[ -n "$v" ] || exit 1
printf '%s' "$v"
EOF2
cat > "$TEST_ROOT/bin/agent-box-slack" <<'EOF2'
#!/bin/sh
printf '%s\t%s\t%s\n' "$*" "$SLACK_APP_TOKEN" "$SLACK_BOT_TOKEN" >> "$CALLS"
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF2
chmod +x "$TEST_ROOT/bin/"*
AGENT_BOX_SLACK_BIN="$TEST_ROOT/bin/agent-box-slack"
AGENT_BOX_ENVSTORE_BIN="$TEST_ROOT/bin/envstore"
slack_pid=""
slack_last_start=0

# No store, then one token: nothing starts.
: > "$STORE"
supervise_slack
echo 'SLACK_APP_TOKEN=xapp-1' > "$STORE"
supervise_slack
[ ! -e "$CALLS" ]

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
for _ in $(seq 1 50); do
  kill -0 "$started_pid" 2>/dev/null || break
  sleep 0.02
done
! kill -0 "$started_pid" 2>/dev/null

echo "slack supervisor: OK"
