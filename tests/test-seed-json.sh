#!/usr/bin/env bash
# Unit tests for seed_json in modules/src/supervisor.sh, the in-place jq edit
# that pre-accepts claude's startup dialogs (issue #749).
#
# The functions are cut out of the supervisor by name rather than sourcing the
# whole script, which would start the reconcile loop. The supervisor is not
# the file's only writer - every running claude rewrites ~/.claude.json - so
# what is under test is how a seed survives the others: it takes claude's own
# lock (a "<file>.lock" directory), it retries a file that does not parse
# before believing it, and seed_json_settled puts back an edit that an
# exiting claude, which writes without that lock, renamed away.
set -u

SCRIPT=${1:?usage: test-seed-json.sh PATH/TO/supervisor.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fn="$work/seed_json.sh"
{ echo 'JQ=jq'; sed -n '/^seed_json() {$/,/^}$/p' "$SCRIPT"
  sed -n '/^seed_json_settled() {$/,/^}$/p' "$SCRIPT"; } > "$fn"
grep -q 'seed_json()' "$fn" || { echo "seed_json not found in $SCRIPT" >&2; exit 2; }
grep -q 'seed_json_settled()' "$fn" \
  || { echo "seed_json_settled not found in $SCRIPT" >&2; exit 2; }

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

seed() { bash -c '. "$1"; shift; seed_json "$@"' _ "$fn" "$@"; }
settled() { bash -c '. "$1"; shift; seed_json_settled "$@"' _ "$fn" "$@"; }
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

# --- the edit waits for claude's lock ------------------------------------
# A claude holding "<file>.lock" has read the file and is about to rename its
# copy over it. A seed that went ahead would be renamed away. Here the holder
# publishes a new version and only then lets go, so a seed that waited has
# both keys and one that did not has lost "late".
f="$work/locked.json"
printf '{"keep":1}' > "$f"
mkdir "$f.lock"
( sleep 0.5; printf '{"keep":1,"late":1}' > "$f.w" && mv "$f.w" "$f"
  rmdir "$f.lock" ) &
seed "$f" "$filter" 2> "$work/locked.err"
wait
if jq -e '.late == 1 and .projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null; then
  ok "a held lock is waited for, not written through"
else fail "a held lock is waited for, not written through: $(cat "$f")"; fi
if [ ! -e "$f.lock" ] && [ ! -s "$work/locked.err" ]; then
  ok "the lock is released and the wait says nothing"
else fail "the lock is released and the wait says nothing: $(cat "$work/locked.err")"; fi

# --- a stale lock is broken, the way proper-lockfile breaks it -----------
f="$work/stale.json"
printf '{"keep":1}' > "$f"
mkdir "$f.lock"
touch -d '-20 seconds' "$f.lock"
start=$(date +%s)
seed "$f" "$filter" 2> "$work/stale.err"
if jq -e '.projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null \
    && [ $(( $(date +%s) - start )) -lt 3 ] && [ ! -e "$f.lock" ]; then
  ok "a lock nobody refreshed in 10s is broken at once"
else fail "a lock nobody refreshed in 10s is broken at once: $(cat "$work/stale.err")"; fi
if ! ls -d "$f".lock.stale.* >/dev/null 2>&1; then
  ok "breaking a stale lock leaves nothing renamed aside"
else fail "breaking a stale lock leaves nothing renamed aside"; fi

# --- a lock that never frees delays the seed, never blocks it ------------
f="$work/held.json"
printf '{"keep":1}' > "$f"
mkdir "$f.lock"
seed "$f" "$filter" 2> "$work/held.err"
if jq -e '.projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null \
    && grep -q "stayed held" "$work/held.err"; then
  ok "a lock held past the wait is reported and the seed goes ahead"
else fail "a lock held past the wait is reported: $(cat "$work/held.err")"; fi
if [ -d "$f.lock" ]; then
  ok "somebody else's live lock is not removed"
else fail "somebody else's live lock is not removed"; fi
rmdir "$f.lock"

# --- an edit renamed away by an unlocked writer is put back --------------
# The exiting claude of issue #749: it read the file before the seed's mv
# and renames its copy, without the key, over it just after.
f="$work/clobbered.json"
printf '{"keep":1}' > "$f"
( sleep 0.2; printf '{"keep":1,"late":1}' > "$f.w" && mv "$f.w" "$f" ) &
settled "$f" --arg wd /w '.projects[$wd] = {hasTrustDialogAccepted: true}' \
  2> "$work/clobbered.err"
wait
if jq -e '.late == 1 and .projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null; then
  ok "a seed overwritten after its mv is applied again"
else fail "a seed overwritten after its mv is applied again: $(cat "$f")"; fi
if grep -q "seeding it again" "$work/clobbered.err"; then
  ok "the repair is reported"
else fail "the repair is reported: $(cat "$work/clobbered.err")"; fi

# --- the quiet case costs one check and says nothing ---------------------
f="$work/quiet.json"
printf '{"keep":1}' > "$f"
settled "$f" --arg wd /w '.projects[$wd] = {hasTrustDialogAccepted: true}' \
  2> "$work/quiet.err"
if jq -e '.projects["/w"].hasTrustDialogAccepted' "$f" >/dev/null \
    && [ ! -s "$work/quiet.err" ]; then
  ok "a seed nobody overwrites settles quietly"
else fail "a seed nobody overwrites settles quietly: $(cat "$work/quiet.err")"; fi

# --- a file that does not parse is seed_json's to report, once -----------
f="$work/settle-broken.json"
printf 'not json' > "$f"
settled "$f" "$filter" 2> "$work/settle-broken.err"
if [ "$(cat "$f")" = "not json" ] \
    && [ "$(grep -c . "$work/settle-broken.err")" = 1 ]; then
  ok "an unparseable file is not re-seeded in a loop"
else fail "an unparseable file is not re-seeded in a loop: $(cat "$work/settle-broken.err")"; fi

[ "$fails" = 0 ] || { echo "$fails failure(s)"; exit 1; }
echo "all seed_json tests passed"
