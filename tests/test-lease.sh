#!/usr/bin/env bash
# Unit tests for modules/src/lib/lease.sh, the durable audit record for a
# hook-* session's GitHub claim (issue #535).
#
# Pure functions of a JSON file on disk: nothing here needs tmux, a
# registry, or a real session, so these run as a native `runCommand` check
# rather than costing a VM boot. The WIRING that calls these functions
# (mark-stopped.sh on a crash or a clean exit, supervisor.sh's vanish
# detection) is exercised with the fake-agent harness in tests/sessions.nix
# instead — what is under test here is the outcome-precedence and
# resolution logic in isolation.
set -u

SCRIPT=${1:?usage: test-lease.sh PATH/TO/lib/lease.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export HOME="$work/home"
mkdir -p "$HOME"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

run() {
  # run NAME -- source lease.sh fresh (LEASE_DIR/LEASE_JQ pick up the
  # per-test HOME) and run the remaining args as a function call.
  "$BASH_BIN" -c ". \"\$1\"; \"\${@:2}\"" _ "$SCRIPT" "$@"
}

field() {
  # field NAME JQ_EXPR -- read one field back out of a lease file, "" if
  # the file does not exist (jq's own "no such file" goes to stderr).
  jq -r "$2" "$HOME/.local/state/agent-box/lease/$1.json" 2>/dev/null
}

# --- create writes exactly the shape callers rely on ------------------
run lease_create alpha "github:defangdevs/agent-box" "535"
[ "$(field alpha .topic)" = "github:defangdevs/agent-box" ] \
  && ok "lease_create records the topic" \
  || fail "lease_create records the topic"
[ "$(field alpha .object)" = "535" ] \
  && ok "lease_create records a numbered object" \
  || fail "lease_create records a numbered object"
[ "$(field alpha .outcome)" = "null" ] \
  && ok "a fresh lease has no outcome" \
  || fail "a fresh lease has no outcome"
[ -n "$(field alpha .claimedAt)" ] \
  && ok "lease_create stamps claimedAt" \
  || fail "lease_create stamps claimedAt"

# An empty object (a CI-shaped claim with no single number) is recorded as
# JSON null, never the literal string "" -- a reader must be able to tell
# "no number" from "the object is an empty string".
run lease_create beta "github:defangdevs/*" ""
[ "$(field beta .object)" = "null" ] \
  && ok "an empty object is stored as JSON null, not the string \"\"" \
  || fail "an empty object is stored as JSON null, not the string \"\""

# --- mark_outcome: first ending wins, not the most recent -------------
run lease_create gamma "github:defangdevs/agent-box" "1"
run lease_mark_outcome gamma vanished
[ "$(field gamma .outcome)" = "vanished" ] \
  && ok "mark_outcome records the outcome" \
  || fail "mark_outcome records the outcome"
[ -n "$(field gamma .endedAt)" ] \
  && ok "mark_outcome stamps endedAt" \
  || fail "mark_outcome stamps endedAt"
run lease_mark_outcome gamma "died:1"
[ "$(field gamma .outcome)" = "vanished" ] \
  && ok "a second outcome never overwrites the first" \
  || fail "a second outcome never overwrites the first (got $(field gamma .outcome))"

# --- mark_outcome on a name with no open lease is a silent no-op ------
run lease_mark_outcome nonexistent "died:1"
if [ "$?" -eq 0 ] && [ ! -e "$HOME/.local/state/agent-box/lease/nonexistent.json" ]; then
  ok "mark_outcome on an unleased name is a silent no-op"
else
  fail "mark_outcome on an unleased name is a silent no-op"
fi

# --- clear deletes outright, resolving whatever an earlier outcome said ---
run lease_clear gamma
if [ ! -e "$HOME/.local/state/agent-box/lease/gamma.json" ]; then
  ok "lease_clear deletes a resolved (or unresolved) lease"
else
  fail "lease_clear deletes a resolved (or unresolved) lease"
fi
# Idempotent: clearing an already-cleared (or never-leased) name is not an
# error -- rm/reap_ephemeral call this unconditionally on every delist.
run lease_clear gamma
[ "$?" -eq 0 ] && ok "lease_clear on an already-clear name succeeds" \
  || fail "lease_clear on an already-clear name succeeds"

# --- outcome: the read-only accessor used by ls/peers ------------------
out=$(run lease_outcome alpha)
[ "$out" = "" ] && ok "lease_outcome is empty for an unresolved lease" \
  || fail "lease_outcome is empty for an unresolved lease (got '$out')"
run lease_mark_outcome alpha "died:2"
out=$(run lease_outcome alpha)
[ "$out" = "died:2" ] && ok "lease_outcome reports a resolved outcome" \
  || fail "lease_outcome reports a resolved outcome (got '$out')"
out=$(run lease_outcome never-leased)
[ "$out" = "" ] && ok "lease_outcome is empty (and does not error) for no lease at all" \
  || fail "lease_outcome is empty for no lease at all (got '$out')"

[ "$fails" -eq 0 ] || { echo "$fails assertion(s) failed" >&2; exit 1; }
echo "all lease assertions passed"
