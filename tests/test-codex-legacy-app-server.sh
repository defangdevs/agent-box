#!/usr/bin/env bash
# A current codex CLI cannot stop an app-server that an older CLI recorded in
# the legacy app-server.pid, so the remote-control wrapper ends it itself
# (issue #904). Only a live pid whose start time and argv both match.
set -euo pipefail

WRAPPER=${1:?usage: test-codex-legacy-app-server.sh PATH/TO/codex-remote-control.sh}
TEST_ROOT="$(mktemp -d)"
pids=()
cleanup() {
  for p in "${pids[@]}"; do kill -KILL "$p" 2>/dev/null || true; done
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

export CODEX_HOME="$TEST_ROOT/codex-home"
state="$CODEX_HOME/app-server-daemon"
mkdir -p "$state"

sed -n '/^stop_legacy_app_server() {$/,/^}$/p' "$WRAPPER" > "$TEST_ROOT/function.sh"
[ -s "$TEST_ROOT/function.sh" ] || { echo "stop_legacy_app_server not found" >&2; exit 1; }
# shellcheck source=/dev/null
. "$TEST_ROOT/function.sh"

# A stand-in whose argv reads like the real one: `.../codex app-server ...`.
fake="$TEST_ROOT/bin/codex"
mkdir -p "$(dirname "$fake")"
printf '#!%s\nwhile :; do sleep 1; done\n' "$(command -v bash)" > "$fake"
chmod +x "$fake"

spawn() { "$@" & spawned=$!; pids+=("$spawned"); sleep 0.2; }
record() {
  printf '{"pid":%s,"processStartTime":"%s"}' "$1" "$2" > "$state/app-server.pid"
}
started() { ps -o lstart= -p "$1"; }
alive() { kill -0 "$1" 2>/dev/null; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# No pid file, or a garbled one: nothing to do.
stop_legacy_app_server
printf 'garbage' > "$state/app-server.pid"
stop_legacy_app_server
echo "ok: missing or garbled pid file is a no-op"

# The recorded app-server, still running: ended.
spawn "$fake" app-server --remote-control --listen unix://
target=$spawned
record "$target" "$(started "$target")"
stop_legacy_app_server
sleep 0.2
alive "$target" && fail "legacy app-server $target still running"
echo "ok: recorded legacy app-server is stopped"

# Same pid, different start time (pid reused): left alone.
spawn "$fake" app-server --remote-control --listen unix://
reused=$spawned
record "$reused" "Thu Jan  1 00:00:00 1970"
stop_legacy_app_server
alive "$reused" || fail "pid $reused with a stale start time was killed"
echo "ok: start-time mismatch is left alone"

# Start time matches but the process is not a codex app-server: left alone.
spawn sleep 300
other=$spawned
record "$other" "$(started "$other")"
stop_legacy_app_server
alive "$other" || fail "non-app-server pid $other was killed"
echo "ok: unrelated process is left alone"

# A dead pid: no-op.
record 999999999 "Tue Oct 20 15:15:59 2026"
stop_legacy_app_server
echo "ok: dead pid is a no-op"
