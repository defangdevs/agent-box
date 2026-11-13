#!/usr/bin/env bash
# A prompt-less codex session has no rollout marker, so the supervisor falls
# back to the thread a WhatsApp registration bound to it (issue #825).
set -euo pipefail

SUPERVISOR=${1:?usage: test-codex-registered-thread.sh PATH/TO/supervisor.sh}
HOOK=${2:-$(dirname "$SUPERVISOR")/claude-session-start-hook.sh}
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export HOME="$TEST_ROOT/home"
JQ=jq
FIND=find
sed -n '/^codex_live_thread() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/live.sh"
. "$TEST_ROOT/live.sh"
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
grep -qF '  sync_codex_threads' "$SUPERVISOR" \
  || { echo "FAIL: reconcile loop no longer publishes Codex threads" >&2; exit 1; }

# Exercise Codex's actual SessionStart payload through the shared recorder,
# then run start_session's actual target-selection block. No kickoff prompt
# or WhatsApp registration is needed to resume the right conversation.
launch=11111111-1111-4111-8111-111111111111
custom_cxhome=$cxhome
sibling_launch=22222222-2222-4222-8222-222222222222
# A real process tree with a Codex-shaped argv exercises owner validation.
cat > "$TEST_ROOT/fake-codex.sh" <<'SH'
if [ "${4:-}" = nested ]; then
  bash -c 'exec -a codex bash "$@"' codex "$0" "$1" "$2" "$3"
else
  printf '{"hook_event_name":"SessionStart","source":"%s","session_id":"%s"}\n' "$3" "$2" \
    | AGENT_BOX_SESSION_ID="$1" bash "$HOOK" codex
fi
:
SH
export HOOK
record_start() {
  bash -c 'export AGENT_BOX_CODEX_PID=$$; exec -a codex bash "$@"' \
    codex "$TEST_ROOT/fake-codex.sh" "$@"
}
record_start "$launch" "$live" startup
record_start "$sibling_launch" "$marked" startup
[[ "$(codex_live_thread "$launch")" = "$live" ]]
[[ "$(codex_live_thread "$sibling_launch")" = "$marked" ]]
[[ -z "$(codex_live_thread ../escape)" ]]
record_start "$launch" "$marked" startup nested
[[ "$(codex_live_thread "$launch")" = "$live" ]] \
  || { echo "FAIL: a nested Codex overwrote its parent's conversation" >&2; exit 1; }
codex_live_thread 33333333-3333-4333-8333-333333333333 2> "$TEST_ROOT/missing.err"
[[ ! -s "$TEST_ROOT/missing.err" ]] \
  || { echo "FAIL: an absent hook record wrote to stderr" >&2; exit 1; }

# A wrapper or test harness may ignore --help. A capability probe must not
# block the supervisor and prevent later sessions from starting.
sed -n '/^      codex_local_arg=""$/,/^      if \[ "$rcdaemon" = true \]; then/p' "$SUPERVISOR" \
  | sed '$d' > "$TEST_ROOT/local-mode.sh"
printf '#!%s\nexec sleep 30\n' "$BASH" > "$TEST_ROOT/hanging-codex"
chmod +x "$TEST_ROOT/hanging-codex"
GREP=grep rcdaemon=false bin="$TEST_ROOT/hanging-codex" \
  timeout 6 bash -c '. "$1"; [ -z "$codex_local_arg" ]' -- "$TEST_ROOT/local-mode.sh"
printf '#!%s\nprintf "%%s\\n" --no-daemon\n' "$BASH" > "$TEST_ROOT/modern-codex"
chmod +x "$TEST_ROOT/modern-codex"
GREP=grep rcdaemon=false bin="$TEST_ROOT/modern-codex" \
  bash -c '. "$1"; [ "$codex_local_arg" = --no-daemon ]' -- "$TEST_ROOT/local-mode.sh"
GREP=grep rcdaemon=true bin="$TEST_ROOT/modern-codex" \
  bash -c '. "$1"; [ -z "$codex_local_arg" ]' -- "$TEST_ROOT/local-mode.sh"

# A rotated thread wins even if the old transcript still has our marker.
printf 'agent-box session %s\n' "$launch" >> "$sessions/rollout-2026-10-03T20-00-00-$marked.jsonl"
sed -n '/^  codex_target=""$/,/^  # Resume on every respawn/p' "$SUPERVISOR" \
  | sed '$d' > "$TEST_ROOT/target.sh"
codex_native_thread() { :; }
resolve_codex_home() { printf '%s' "${test_cxhome:-$HOME/.codex}"; }
agent=codex hasrun=true rcdaemon=false sprofile="" sname=unregistered bid=$launch
. "$TEST_ROOT/target.sh"
[ "$codex_target" = "$live" ] \
  || { echo "FAIL: prompt-less session did not resume its recorded native id" >&2; exit 1; }
record_start "$launch" "$marked" resume
. "$TEST_ROOT/target.sh"
[ "$codex_target" = "$marked" ] \
  || { echo "FAIL: the hook's new conversation was not adopted" >&2; exit 1; }

# A custom CODEX_HOME is honored and a missing rollout is never accepted.
record_start "$launch" "$gone" startup
[ -z "$(codex_live_thread "$launch")" ]
[ "$(codex_live_thread "$launch" "$custom_cxhome")" = "$gone" ]
test_cxhome=$custom_cxhome
. "$TEST_ROOT/target.sh"
[ "$codex_target" = "$gone" ]
unset test_cxhome
record_start "$launch" ../bad startup
[ "$(cat "$HOME/.local/state/agent-box/live-session-id/$launch")" = "$gone" ]
printf '../bad\n' > "$HOME/.local/state/agent-box/live-session-id/$launch"
[ -z "$(codex_live_thread "$launch")" ]

# The resume arm must use the filtering helper rather than appending a
# persisted `resume ID` after its own (#845).
grep -qF 'cmd="$cmd resume"
          codex_autonomy
          append_extra_sans_resume' "$SUPERVISOR" \
  || { echo "FAIL: the codex resume arm must drop a persisted resume from extraArgs" >&2; exit 1; }

# Exercise the helper too: a wiring grep alone would stay green if its jq
# filter kept the duplicate resume or discarded unrelated options. Extract
# the nested function from start_session and run it against complete argv
# shapes, including the final supervisor-owned target.
sed -n '/^  append_extra_sans_resume() {$/,/^  }$/p' "$SUPERVISOR" \
  | sed 's/^  //' > "$TEST_ROOT/append-extra-sans-resume.sh"
[[ -s "$TEST_ROOT/append-extra-sans-resume.sh" ]] \
  || { echo "append_extra_sans_resume not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/append-extra-sans-resume.sh"

expect_resume_command() {
  want=$1
  shift
  sjson="$($JQ -cn --args '{extraArgs: $ARGS.positional}' -- "$@")"
  cmd="codex resume"
  append_extra_sans_resume
  cmd="$cmd -- $live"
  [[ "$cmd" = "$want" ]] \
    || { echo "FAIL: extraArgs $sjson -> '$cmd', want '$want'" >&2; exit 1; }
}

expect_resume_command "codex resume -- $live" resume "$live"
expect_resume_command "codex resume --model gpt-5 -- $live" \
  resume "$live" --model gpt-5
expect_resume_command "codex resume -- $live" resume -- "$live"
expect_resume_command "codex resume --model gpt-5 -- $live" \
  resume -- "$live" --model gpt-5
expect_resume_command "codex resume --model gpt-5 -- $live" --model gpt-5
expect_resume_command "codex resume -- $live"

# Publishing a discovered rollout creates the exact bridge registry shape,
# preserves unrelated manual registrations, and refuses unsafe inputs.
sed -n '/^codex_publish_thread() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/publish.sh"
[ -s "$TEST_ROOT/publish.sh" ] || { echo "codex_publish_thread not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/publish.sh"
rm -f "$state/codex-threads.json"
codex_publish_thread codex "$live"
[ "$($JQ -r '.codex.thread' "$state/codex-threads.json")" = "$live" ] \
  || { echo "FAIL: discovered thread was not published" >&2; exit 1; }
[ "$($JQ -r '.codex.source' "$state/codex-threads.json")" = agent-box ] \
  || { echo "FAIL: automatic registration is not identified" >&2; exit 1; }
[ "$(stat -c %a "$state/codex-threads.json")" = 600 ] \
  || { echo "FAIL: registration is not private" >&2; exit 1; }
$JQ '.manual={thread:"remote-task"}' "$state/codex-threads.json" > "$state/add"
mv "$state/add" "$state/codex-threads.json"
codex_publish_thread second "$gone"
[ "$($JQ -r '.manual.thread' "$state/codex-threads.json")" = remote-task ] \
  || { echo "FAIL: publishing replaced another registration" >&2; exit 1; }
before="$($JQ -c . "$state/codex-threads.json")"
codex_publish_thread '../escape' "$live"
codex_publish_thread codex not-a-uuid
[ "$($JQ -c . "$state/codex-threads.json")" = "$before" ] \
  || { echo "FAIL: unsafe registration input changed state" >&2; exit 1; }

# The reconcile pass covers WhatsApp-selected TUI and subscribed Remote
# Control sessions and keeps all @tsv columns non-empty, so an absent profile
# cannot shift fields.
sed -n '/^sync_codex_threads() {$/,/^}$/p' "$SUPERVISOR" > "$TEST_ROOT/sync.sh"
[ -s "$TEST_ROOT/sync.sh" ] || { echo "sync_codex_threads not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/sync.sh"
REGISTRY_FILE="$TEST_ROOT/sessions.json"
cat > "$REGISTRY_FILE" <<'JSON'
{"sessions":{"tui":{"agent":"codex","remoteControl":false,"whatsapp":true},"remote":{"agent":"codex","remoteControl":true,"profile":"phone","whatsapp":true},"manual-remote":{"agent":"codex","remoteControl":true,"profile":"manual","whatsapp":true},"effective":{"agent":"codex","whatsapp":false},"unselected":{"agent":"codex","whatsapp":false},"claude":{"agent":"claude","whatsapp":true},"stopped":{"agent":"codex","stopped":true,"whatsapp":true}}}
JSON
printf '{"session":"effective"}' > "$state/target.json"
manual_thread=01a10365-0000-7000-8000-000000000006
mkdir -p "$TEST_ROOT/home-for-manual/sessions/2026/10/03"
touch "$TEST_ROOT/home-for-manual/sessions/2026/10/03/rollout-x-$manual_thread.jsonl"
$JQ --arg t "$manual_thread" '. + {"manual-remote": {thread: $t}}' \
  "$state/codex-threads.json" > "$state/codex-threads.new"
mv "$state/codex-threads.new" "$state/codex-threads.json"
calls="$TEST_ROOT/sync-calls"
native_calls="$TEST_ROOT/native-calls"
session_watches_events() { [ "$1" = remote ] || [ "$1" = manual-remote ]; }
codex_wake_thread() { printf '%s' 01a10362-0000-7000-8000-000000000003; }
resolve_codex_home() { printf '%s' "$TEST_ROOT/home-for-${1:-default}"; }
session_launch_id() { printf '%s' launch-for-"$1"; }
codex_live_thread() { :; }
codex_rollout_uuid() {
  case "$1" in
    launch-for-tui) _resolved=01a10363-0000-7000-8000-000000000004 ;;
    launch-for-effective) _resolved=01a10364-0000-7000-8000-000000000005 ;;
    (*) return 0 ;;
  esac
  [ "$2" = "$TEST_ROOT/home-for-default" ] || return 0
  printf '%s' "$_resolved"
}
codex_publish_thread() { printf '%s:%s\n' "$1" "$2" >> "$calls"; }
record_native_session() { printf '%s:%s\n' "$1" "$2" >> "$native_calls"; }
codex_sync_last=0
touch "$state/ready"
sync_codex_threads
grep -Fx 'tui:01a10363-0000-7000-8000-000000000004' "$calls" >/dev/null \
  || { echo "FAIL: TUI rollout was not synchronized" >&2; exit 1; }
grep -Fx 'remote:01a10362-0000-7000-8000-000000000003' "$calls" >/dev/null \
  || { echo "FAIL: Remote Control wake thread was not synchronized" >&2; exit 1; }
grep -Fx 'effective:01a10364-0000-7000-8000-000000000005' "$calls" >/dev/null \
  || { echo "FAIL: the bridge's effective target was not synchronized" >&2; exit 1; }
[ "$(wc -l < "$calls")" = 3 ] \
  || { echo "FAIL: stopped, unselected, or non-Codex session was synchronized" >&2; exit 1; }
[ "$(sort "$native_calls")" = "$(sort "$calls")" ] \
  || { echo "FAIL: synchronized Codex threads were not persisted as native IDs" >&2; exit 1; }
! grep -F 'manual-remote:' "$calls" >/dev/null \
  || { echo "FAIL: a manual Remote Control registration was overwritten" >&2; exit 1; }

echo "codex_registered_thread: ok"
