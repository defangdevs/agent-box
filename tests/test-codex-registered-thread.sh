#!/usr/bin/env bash
# A prompt-less codex session has no rollout marker, so the supervisor falls
# back to the thread a WhatsApp registration bound to it (issue #825).
set -euo pipefail

SUPERVISOR=${1:?usage: test-codex-registered-thread.sh PATH/TO/supervisor.sh}
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export HOME="$TEST_ROOT/home"
JQ=jq
FIND=find
state="$HOME/.local/state/local-whatsapp"
sessions="$HOME/.codex/sessions/2026/10/03"
mkdir -p "$state" "$sessions"

sed -n '/^codex_registered_thread() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/function.sh"
[ -s "$TEST_ROOT/function.sh" ] || { echo "codex_registered_thread not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/function.sh"

live=01a10354-8d36-7ad3-b474-03a4198adfd5
gone=01a10359-177f-70e2-9646-c6d97dd8fef4
touch "$sessions/rollout-2026-10-03T19-53-55-$live.jsonl"

expect() {
  got="$(codex_registered_thread "$1")"
  [ "$got" = "$2" ] || { echo "FAIL: '$1' -> '$got', want '$2'" >&2; exit 1; }
}

# No registration file: nothing to follow.
expect codex ""

printf '{"codex":{"thread":"%s"},"stale":{"thread":"%s"},"bad":{"thread":"../x"},"short":{"thread":"abc"}}' \
  "$live" "$gone" > "$state/codex-threads.json"

expect codex "$live"          # registered and its rollout exists
expect stale ""               # registered, rollout gone: start fresh instead
expect bad ""                 # not a UUID: never reaches `codex resume`
expect short ""
expect other ""               # unregistered session
expect "" ""

printf 'not json' > "$state/codex-threads.json"
expect codex ""

echo "codex_registered_thread: ok"
