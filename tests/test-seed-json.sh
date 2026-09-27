#!/usr/bin/env bash
# Unit tests for seed_json in modules/src/supervisor.sh, the in-place jq edit
# that pre-accepts claude's startup dialogs (issue #749).
#
# The function is cut out of the supervisor by name rather than sourcing the
# whole script, which would start the reconcile loop. What is under test is
# the one decision it makes: a file that does not parse is retried before it
# is believed, because the supervisor is not its only writer - a running
# claude rewrites ~/.claude.json in place - and a seed that gave up on a
# half-written file used to lose the folder-trust key with no word said.
set -u

SCRIPT=${1:?usage: test-seed-json.sh PATH/TO/supervisor.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fn="$work/seed_json.sh"
{ echo 'JQ=jq'; sed -n '/^seed_json() {$/,/^}$/p' "$SCRIPT"; } > "$fn"
grep -q 'seed_json()' "$fn" || { echo "seed_json not found in $SCRIPT" >&2; exit 2; }

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

seed() { bash -c '. "$1"; shift; seed_json "$@"' _ "$fn" "$@"; }
filter='.projects["/w"] = {hasTrustDialogAccepted: true}'

# --- the ordinary case ---------------------------------------------------
f="$work/plain.json"
printf '{"keep":1}' > "$f"
seed "$f" "$filter"
if jq -e '.keep == 1 and .projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null; then
  ok "a parseable file is seeded and keeps its other keys"
else fail "a parseable file is seeded and keeps its other keys: $(cat "$f")"; fi

# --- a missing file is created -------------------------------------------
f="$work/missing.json"
seed "$f" "$filter"
if jq -e '.projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null; then
  ok "a missing file is created and seeded"
else fail "a missing file is created and seeded"; fi

# --- a file caught mid-write is retried, not skipped ---------------------
# Truncated JSON, completed by a second writer while seed_json is retrying:
# the shape of a claude rewriting the file at the moment the seed reads it.
f="$work/midwrite.json"
printf '{"keep":1,"proj' > "$f"
( sleep 0.3; printf '{"keep":1}' > "$f.w" && mv "$f.w" "$f" ) &
seed "$f" "$filter" 2> "$work/midwrite.err"
wait
if jq -e '.keep == 1 and .projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null; then
  ok "a file that parses on a retry is seeded"
else fail "a file that parses on a retry is seeded: $(cat "$f")"; fi
if [ ! -s "$work/midwrite.err" ]; then
  ok "a retry that succeeds says nothing"
else fail "a retry that succeeds says nothing: $(cat "$work/midwrite.err")"; fi

# --- an all-whitespace file is never replaced with an empty one ----------
# jq reads it as zero inputs: exit 0, no output. Taking that as success
# published an empty file over ~/.claude.json (CodeRabbit on PR #755).
f="$work/blank.json"
printf '\n' > "$f"
seed "$f" "$filter" 2> "$work/blank.err"
if [ "$(cat "$f")" = "" ] && [ "$(wc -c < "$f")" = 1 ]; then
  ok "a blank file is left as it was, not emptied"
else fail "a blank file is left as it was, not emptied: $(wc -c < "$f") bytes"; fi
if grep -q "could not parse $f" "$work/blank.err"; then
  ok "a blank file is reported like any other unparseable one"
else fail "a blank file is reported: $(cat "$work/blank.err")"; fi

# --- a file that never parses is left alone, and SAYS so -----------------
f="$work/broken.json"
printf 'not json' > "$f"
seed "$f" "$filter" 2> "$work/broken.err"
rc=$?
if [ "$rc" = 0 ] && [ "$(cat "$f")" = "not json" ]; then
  ok "an unparseable file is left untouched and the agent still starts"
else fail "an unparseable file is left untouched (rc=$rc): $(cat "$f")"; fi
if grep -q "could not parse $f" "$work/broken.err"; then
  ok "giving up is reported, not silent"
else fail "giving up is reported, not silent: $(cat "$work/broken.err")"; fi
if [ ! -e "$f.seed-tmp" ]; then
  ok "no temp file is left behind"
else fail "no temp file is left behind"; fi

[ "$fails" = 0 ] || { echo "$fails failure(s)"; exit 1; }
echo "all seed_json tests passed"
