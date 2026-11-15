#!/usr/bin/env bash
# sweep_session_tmp removes the scratch dir (TMPDIR) of a session the registry
# no longer lists, and nothing else.
set -euo pipefail

SUPERVISOR=${1:?usage: test-session-tmp.sh PATH/TO/supervisor.sh}
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

sed -n '/^sweep_session_tmp() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/function.sh"
# shellcheck source=/dev/null
. "$TEST_ROOT/function.sh"

NL="
"
JQ=jq
REGISTRY_FILE="$TEST_ROOT/sessions.json"
SESSION_TMP_DIR="$TEST_ROOT/tmp"
mkdir -p "$SESSION_TMP_DIR/live" "$SESSION_TMP_DIR/gone" "$SESSION_TMP_DIR/bad name" "$TEST_ROOT/outside"
touch "$SESSION_TMP_DIR/gone/c.md" "$TEST_ROOT/outside/keep"
ln -s "$TEST_ROOT/outside" "$SESSION_TMP_DIR/link"
echo '{"sessions":{"live":{}}}' > "$REGISTRY_FILE"

sweep_session_tmp
[ -d "$SESSION_TMP_DIR/live" ]
[ ! -e "$SESSION_TMP_DIR/gone" ]
[ -d "$SESSION_TMP_DIR/bad name" ]
[ -L "$SESSION_TMP_DIR/link" ] && [ -e "$TEST_ROOT/outside/keep" ]

# An unreadable registry is not "no sessions": nothing is removed.
mkdir "$SESSION_TMP_DIR/gone2"
echo '{not json' > "$REGISTRY_FILE"
sweep_session_tmp
[ -d "$SESSION_TMP_DIR/gone2" ] && [ -d "$SESSION_TMP_DIR/live" ]

# An empty registry IS an answer: the last session's dir goes.
echo '{"sessions":{}}' > "$REGISTRY_FILE"
sweep_session_tmp
[ ! -e "$SESSION_TMP_DIR/live" ] && [ ! -e "$SESSION_TMP_DIR/gone2" ]
# A symlinked root is never traversed.
mkdir -p "$TEST_ROOT/real/gone"
echo '{"sessions":{}}' > "$REGISTRY_FILE"
SESSION_TMP_DIR="$TEST_ROOT/rootlink"
ln -s "$TEST_ROOT/real" "$SESSION_TMP_DIR"
sweep_session_tmp
[ -d "$TEST_ROOT/real/gone" ]
SESSION_TMP_DIR="$TEST_ROOT/missing"
sweep_session_tmp
echo "session tmp sweep: ok"
