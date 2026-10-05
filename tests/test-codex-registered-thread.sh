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

# A profile's CODEX_HOME: the rollout is looked for there, not in ~/.codex.
cxhome="$TEST_ROOT/cxhome"
mkdir -p "$cxhome/sessions/2026/10/03"
touch "$cxhome/sessions/2026/10/03/rollout-2026-10-03T21-00-00-$gone.jsonl"
[ "$(codex_registered_thread stale "$cxhome")" = "$gone" ] \
  || { echo "FAIL: rollout under the session's CODEX_HOME not found" >&2; exit 1; }
[ -z "$(codex_registered_thread codex "$cxhome")" ] \
  || { echo "FAIL: ~/.codex rollout must not satisfy a different CODEX_HOME" >&2; exit 1; }

printf 'not json' > "$state/codex-threads.json"
expect codex ""

# start_session wiring: a non-RC session with no rollout marker (empty
# codex_rollout_uuid) must resume the registered thread, and a marker hit
# must still win over the registration.
sed -n '/^codex_rollout_uuid() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/rollout.sh"
[ -s "$TEST_ROOT/rollout.sh" ] || { echo "codex_rollout_uuid not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/rollout.sh"
GREP=grep
printf '{"codex":{"thread":"%s"}}' "$live" > "$state/codex-threads.json"

target_for() {
  t="$(codex_rollout_uuid "$1")"
  [ -n "$t" ] || t="$(codex_registered_thread "$2")"
  printf '%s' "$t"
}
[ "$(target_for boxid-nomarker codex)" = "$live" ] \
  || { echo "FAIL: marker-less session did not fall back to registration" >&2; exit 1; }
[ -z "$(target_for boxid-nomarker unregistered)" ] \
  || { echo "FAIL: unregistered marker-less session must start fresh" >&2; exit 1; }
marked=01a10360-0000-7000-8000-000000000001
printf 'agent-box session boxid-marked\n' \
  > "$sessions/rollout-2026-10-03T20-00-00-$marked.jsonl"
[ "$(target_for boxid-marked codex)" = "$marked" ] \
  || { echo "FAIL: marker must win over registration" >&2; exit 1; }

# The marker is looked for under the session's CODEX_HOME too.
pmarked=01a10361-0000-7000-8000-000000000002
printf 'agent-box session boxid-profile\n' \
  > "$cxhome/sessions/2026/10/03/rollout-2026-10-03T22-00-00-$pmarked.jsonl"
[ "$(codex_rollout_uuid boxid-profile "$cxhome")" = "$pmarked" ] \
  || { echo "FAIL: marker under the session's CODEX_HOME not found" >&2; exit 1; }
[ -z "$(codex_rollout_uuid boxid-profile)" ] \
  || { echo "FAIL: a CODEX_HOME marker must not match ~/.codex" >&2; exit 1; }
[ "$(codex_rollout_uuid boxid-marked)" = "$marked" ] \
  || { echo "FAIL: no CODEX_HOME must still default to ~/.codex" >&2; exit 1; }

# The fallback itself must stay wired into start_session's non-RC branch.
grep -qF 'codex_target="$(codex_rollout_uuid "$bid" "$cxhome")"' "$SUPERVISOR" \
  || { echo "FAIL: start_session no longer passes CODEX_HOME to codex_rollout_uuid" >&2; exit 1; }
grep -qF '|| codex_target="$(codex_registered_thread "$sname" "$cxhome")"' "$SUPERVISOR" \
  || { echo "FAIL: start_session no longer falls back to codex_registered_thread" >&2; exit 1; }

echo "codex_registered_thread: ok"
