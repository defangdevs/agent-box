#!/usr/bin/env bash
# The supervisor must start WhatsApp through the release-pinned helper.
set -euo pipefail

SUPERVISOR=${1:?usage: test-whatsapp-supervisor.sh PATH/TO/supervisor.sh}
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export HOME="$TEST_ROOT/home"
export CALLS="$TEST_ROOT/calls"
mkdir -p "$HOME/.local/state/local-whatsapp" "$TEST_ROOT/bin"

sed -n '/^supervise_whatsapp() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/function.sh"
# shellcheck source=/dev/null
. "$TEST_ROOT/function.sh"

cat > "$TEST_ROOT/bin/agent-box-session" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$TEST_ROOT/bin/agent-box-session"

cat > "$TEST_ROOT/bin/agent-box-whatsapp" <<'EOF'
#!/bin/sh
printf '%s\t%s\t%s\n' "$*" "${LOCAL_WHATSAPP_SESSION_BIN:-}" \
  "${LOCAL_WHATSAPP_CODEX_BIN:-}" >> "$CALLS"
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF
chmod +x "$TEST_ROOT/bin/agent-box-whatsapp"

cat > "$TEST_ROOT/bin/codex" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$TEST_ROOT/bin/codex"

export PATH="$TEST_ROOT/bin:$PATH"
AGENT_BOX_WHATSAPP_BIN="$TEST_ROOT/bin/missing"
whatsapp_pid=""
whatsapp_last_start=0

agent_bin() {
  [ "$1" = codex ] || return 1
  printf '%s\n' "$TEST_ROOT/bin/codex"
}

# Neither an absent ready marker nor a missing pinned helper starts anything.
supervise_whatsapp
[ ! -e "$CALLS" ]
touch "$HOME/.local/state/local-whatsapp/ready"
supervise_whatsapp || true
[ ! -e "$CALLS" ]

AGENT_BOX_WHATSAPP_BIN="$TEST_ROOT/bin/agent-box-whatsapp"
supervise_whatsapp
for _ in $(seq 1 50); do
  [ -s "$CALLS" ] && break
  sleep 0.02
done
grep '^serve' "$CALLS" >/dev/null
grep "$TEST_ROOT/bin/agent-box-session" "$CALLS" >/dev/null
grep "$TEST_ROOT/bin/codex" "$CALLS" >/dev/null
[ "$(wc -l < "$CALLS")" -eq 1 ]

# A live helper is retained; removing the marker stops it.
started_pid=$whatsapp_pid
supervise_whatsapp
[ "$(wc -l < "$CALLS")" -eq 1 ]
rm "$HOME/.local/state/local-whatsapp/ready"
supervise_whatsapp
[ -z "$whatsapp_pid" ]
for _ in $(seq 1 50); do
  kill -0 "$started_pid" 2>/dev/null || break
  sleep 0.02
done
! kill -0 "$started_pid" 2>/dev/null

echo "whatsapp supervisor: OK"
