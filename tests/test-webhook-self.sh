#!/usr/bin/env bash
# What `agent-box-webhook-self` resolves and caches, for local-webhook's
# "@self" sender mute (issue #261). Every assertion here is a pure function of
# a stubbed `gh` and a state directory, so it runs natively in about a second
# instead of costing a VM boot - the move `webhook-spawn-claim` and
# `webhook-defer` already made out of tests/webhook.nix, which had no
# testScript budget left (issue #610).
#
# tests/webhook.nix keeps only what a VM can show: a session's own webhook
# peer resolving the same answer through env-exec, and the receiver unit
# loading the cache as an EnvironmentFile.
set -u

SCRIPT=${1:?usage: test-webhook-self.sh PATH/TO/webhook-self.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

STATE_DIR="$work/state"
FILE="$STATE_DIR/self.env"
STAMP="$STATE_DIR/.self-attempt"
mkdir -p "$STATE_DIR"

# A stub the agent cannot execute would fail INSIDE the resolver, where gh's
# stderr is deliberately silenced, and look like "no token" - so this is a
# real, executable stub rather than a PATH override alone.
mkdir -p "$work/bin"
cat > "$work/bin/gh" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "$work/calls"
printf '%s\n' "\${FAKE_LOGIN:-box-bot}"
EOF
chmod 755 "$work/bin/gh"

# self [VAR=val ...] [-- SCRIPT-ARG ...] — run the resolver with gh on PATH.
self() {
  local envs=() args=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  args=("$@")
  env HOME="$work/home" LOCAL_WEBHOOK_STATE_DIR="$STATE_DIR" \
      PATH="$work/bin:$PATH" "${envs[@]}" "$BASH_BIN" "$SCRIPT" "${args[@]}"
}
calls() { wc -l < "$work/calls" 2>/dev/null || echo 0; }

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# --- first resolution: asks GitHub once, caches, world-readable -------------
out=$(self)
if [ "$out" = "box-bot" ]; then
  ok "first resolution asks GitHub and answers with the login"
else
  fail "first resolution asks GitHub and answers with the login — got '$out'"
fi
if grep -qx 'LOCAL_WEBHOOK_SELF=box-bot' "$FILE" 2>/dev/null; then
  ok "the login is cached in the env-file"
else
  fail "the login is cached in the env-file"
fi
if grep -q '^# fp=[0-9a-f]*$' "$FILE" 2>/dev/null; then
  ok "the cache is keyed by a token fingerprint, not the token itself"
else
  fail "the cache is keyed by a token fingerprint, not the token itself"
fi
if [ "$(stat -c '%a' "$FILE" 2>/dev/null)" = 644 ]; then
  ok "a login is not a secret: the cache file is world-readable"
else
  fail "a login is not a secret: the cache file is world-readable"
fi
[ "$(calls)" = 1 ] || fail "expected exactly one gh call so far, got $(calls)"

# --- same token: the cache stands, without gh on PATH at all ----------------
out=$(env HOME="$work/home" LOCAL_WEBHOOK_STATE_DIR="$STATE_DIR" \
      "$BASH_BIN" "$SCRIPT")
if [ "$out" = "box-bot" ] && [ "$(calls)" = 1 ]; then
  ok "an unchanged token reuses the cache and never touches gh"
else
  fail "an unchanged token reuses the cache and never touches gh — got '$out', $(calls) call(s)"
fi

# --- a different token is a different account until proven otherwise --------
out=$(self GH_TOKEN=second FAKE_LOGIN=other-bot)
if [ "$out" = "other-bot" ] && [ "$(calls)" = 2 ]; then
  ok "a changed token re-resolves rather than trusting the stale cache"
else
  fail "a changed token re-resolves rather than trusting the stale cache — got '$out', $(calls) call(s)"
fi
grep -qx 'LOCAL_WEBHOOK_SELF=other-bot' "$FILE" 2>/dev/null \
  || fail "the cache moves to the newly resolved login"

# --- an explicit LOCAL_WEBHOOK_SELF wins outright, and is never cached ------
out=$(self LOCAL_WEBHOOK_SELF=someone-else)
if [ "$out" = "someone-else" ] && [ "$(calls)" = 2 ]; then
  ok "an explicit LOCAL_WEBHOOK_SELF is echoed back with no lookup"
else
  fail "an explicit LOCAL_WEBHOOK_SELF is echoed back with no lookup — got '$out', $(calls) call(s)"
fi
grep -qx 'LOCAL_WEBHOOK_SELF=other-bot' "$FILE" 2>/dev/null \
  || fail "an explicit override leaves the cache describing the token, untouched"

# --- unreachable GitHub: the last known identity beats no identity ----------
out=$(env HOME="$work/home" LOCAL_WEBHOOK_STATE_DIR="$STATE_DIR" GH_TOKEN=third \
      "$BASH_BIN" "$SCRIPT" 2>/dev/null)
if [ "$out" = "other-bot" ]; then
  ok "no gh on PATH: the box did not stop being its last known account"
else
  fail "no gh on PATH: the box did not stop being its last known account — got '$out'"
fi
[ -s "$STAMP" ] || fail "a failed attempt is stamped"

# --- --throttled honors that stamp; a direct ask never does -----------------
before=$(calls)
out=$(self GH_TOKEN=third -- --throttled)
if [ "$out" = "other-bot" ] && [ "$(calls)" = "$before" ]; then
  ok "--throttled skips the lookup a recent failure for this token already made"
else
  fail "--throttled skips the lookup a recent failure for this token already made — got '$out', $(calls) call(s)"
fi
out=$(self GH_TOKEN=third FAKE_LOGIN=third-bot)
if [ "$out" = "third-bot" ] && [ "$(calls)" != "$before" ]; then
  ok "a direct ask always makes a real attempt, throttle or not"
else
  fail "a direct ask always makes a real attempt, throttle or not — got '$out'"
fi

if [ "$fails" -eq 0 ]; then
  printf '\nall webhook-self assertions passed\n'
else
  printf '\n%s webhook-self assertion(s) failed\n' "$fails"
  exit 1
fi
