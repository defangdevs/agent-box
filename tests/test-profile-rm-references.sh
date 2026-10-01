#!/usr/bin/env bash
# Unit tests for what `agent-box-profile rm NAME` says about the places that
# still store NAME after it is gone.
#
# Deleting a profile fails nothing loudly, by design: a standing watch that
# names it falls back to AGENT_BOX_HOOK_PROFILE or the box default agent (a
# delivery must never be dropped over a profile), and a listed session keeps
# its launch arguments but loses the profile's environment at its next
# restart. So the only signal anybody gets is the one rm prints, and the
# settings page's delete confirm had it (#582) while the CLI - what an agent
# deleting a profile from chat runs - printed nothing. What is pinned here is
# that each reference is named, that an unrelated profile's rm says nothing,
# and that no reference (or an unreadable file) ever turns rm into a failure.
#
# The env store is the real one - library plus CLI, concatenated the way the
# module splices them - so AGENT_BOX_HOOK_PROFILE is read by the parser the
# spawn wrapper reads it with.
set -u

SCRIPT=${1:?usage: test-profile-rm-references.sh profile-cli.sh lib/envstore.py envstore-cli.py}
LIB=${2:?}
CLI=${3:?}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

PY=$(command -v python3)
cat "$LIB" "$CLI" > "$work/envstore.py"
printf '#!%s\nexec %s %s "$@"\n' "$(command -v bash)" "$PY" "$work/envstore.py" > "$work/envstore"
chmod +x "$work/envstore"

fail=0
check() {
  # check DESCRIPTION CONDITION...
  local what=$1; shift
  if "$@"; then echo "ok - $what"; else echo "FAIL - $what"; fail=1; fi
}

H="$work/home"
P="$H/.config/agent-box/profiles"
D="$H/.local/state/local-webhook"
mkdir -p "$P" "$D"
mkprof() { printf 'HARNESS=claude\n' > "$P/$1.env"; }

run_rm() {
  env -u LOCAL_WEBHOOK_STATE_DIR -u REGISTRY_FILE HOME="$H" \
    AGENT_BOX_AGENTS="claude codex" AGENT_BOX_ENVSTORE_BIN="$work/envstore" \
    bash "$SCRIPT" rm "$1" > "$work/out" 2> "$work/err"
  echo $? > "$work/rc"
}
rc_is() { [ "$(cat "$work/rc")" = "$1" ]; }
err_has() { grep -F -- "$1" "$work/err" >/dev/null; }
err_empty() { [ ! -s "$work/err" ]; }

mkprof triage
mkprof other
"$work/envstore" --file "$H/.config/agent-box/env" set AGENT_BOX_HOOK_PROFILE=triage
cat > "$D/filter.dispatch.json" <<'EOF'
{"topics": [
  {"topic": "github:o/r", "spawnConfig": {"profile": "triage"}},
  {"topic": "github:o/r", "name": "ci", "spawnConfig": {"profile": "triage"}},
  {"topic": "github:o/x", "spawnConfig": {"profile": "other"}},
  {"topic": "github:o/y"},
  {"topic": "github:o/z", "spawnConfig": {"profile": 5}},
  5
]}
EOF
cat > "$H/.config/agent-box/sessions.json" <<'EOF'
{"sessions": {"a": {"profile": "triage"}, "b": {"profile": null}, "c": {"profile": "triage"}, "d": 3}}
EOF

run_rm triage
check "rm succeeds with references left behind" rc_is 0
check "the profile file is gone" test ! -e "$P/triage.env"
check "AGENT_BOX_HOOK_PROFILE is named" err_has "AGENT_BOX_HOOK_PROFILE still names 'triage'"
check "the unnamed watch is named" err_has "standing watch 'github:o/r' still names 'triage'"
check "the named watch is named with its name" err_has "standing watch 'github:o/r / ci' still names 'triage'"
check "a watch on another profile is not named" sh -c "! grep -F 'github:o/x' '$work/err'"
check "one fix line for all the watches" test "$(grep -c 'recreate it' "$work/err")" = 1
check "the listed sessions are named" err_has "listed session(s) a c were started with 'triage'"

# Nothing refers to 'other' but one watch - and only the watch is reported.
run_rm other
check "rm other succeeds" rc_is 0
check "rm other names its watch" err_has "standing watch 'github:o/x' still names 'other'"
check "rm other does not claim AGENT_BOX_HOOK_PROFILE" sh -c "! grep -F 'AGENT_BOX_HOOK_PROFILE still names' '$work/err'"
check "rm other names no session" sh -c "! grep -F 'listed session' '$work/err'"

# No references at all: rm is exactly as quiet as it was.
mkprof lonely
run_rm lonely
check "an unreferenced rm succeeds" rc_is 0
check "an unreferenced rm warns about nothing" err_empty

# Garbage in every file is skipped, never a failed rm.
mkprof junk
printf 'not json' > "$D/filter.dispatch.json"
printf '{' > "$H/.config/agent-box/sessions.json"
run_rm junk
check "unreadable reference files do not fail rm" rc_is 0
check "unreadable reference files produce no warning" err_empty

# LOCAL_WEBHOOK_STATE_DIR, when set, is where the watches are read from -
# the same override the webhook CLI honours.
mkprof moved
mkdir -p "$work/state"
printf '{"topics":[{"topic":"github:o/m","spawnConfig":{"profile":"moved"}}]}' > "$work/state/filter.dispatch.json"
HOME="$H" LOCAL_WEBHOOK_STATE_DIR="$work/state" AGENT_BOX_AGENTS="claude codex" \
  AGENT_BOX_ENVSTORE_BIN="$work/envstore" bash "$SCRIPT" rm moved > "$work/out" 2> "$work/err"
check "LOCAL_WEBHOOK_STATE_DIR is honoured" err_has "standing watch 'github:o/m' still names 'moved'"

# rm NAME KEY only removes a key; the profile still exists, so nothing is left
# dangling and nothing is reported.
mkprof keyed
HOME="$H" "$work/envstore" --profile keyed set TOKEN=x
printf '{"topics":[{"topic":"github:o/k","spawnConfig":{"profile":"keyed"}}]}' > "$D/filter.dispatch.json"
env -u LOCAL_WEBHOOK_STATE_DIR HOME="$H" AGENT_BOX_AGENTS="claude codex" \
  AGENT_BOX_ENVSTORE_BIN="$work/envstore" bash "$SCRIPT" rm keyed TOKEN > "$work/out" 2> "$work/err"
echo $? > "$work/rc"
check "rm NAME KEY succeeds" rc_is 0
check "rm NAME KEY reports no references" err_empty

[ "$fail" = 0 ] && echo "all profile rm reference checks passed"
exit "$fail"
