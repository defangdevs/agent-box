#!/usr/bin/env bash
# Unit tests for `agent-box-candidate` — installing a fix on THIS box before
# it is merged and every other box takes it.
#
# Weighted almost entirely at the REFUSALS, for the same reason
# test-source-tree.sh is: this runs as root, it decides what the next
# rebuild builds, and it is reached through a sudo grant whose argument is
# whatever an agent typed. sudoers deliberately does not validate that
# argument (`* ` allows one argument of anything) — this script is the
# validator, so each thing it must refuse gets an assertion:
#
#   - anything that is not a BRANCH on this box's own origin. A tag or a
#     sha would be a downgrade primitive; refs/pull/N/head would be a
#     stranger's unreviewed pull request built as root, which on a public
#     repo is anyone at all. The `--heads` scoping is what stops it, and
#     the assertion below has a real refs/pull/1/head to prove it.
#   - a candidate that is the tracked branch's own head, so there is
#     nothing to try. The rest of "strictly ahead" is agent-box-source's:
#     ANCESTRY needs the objects, and a question — or a mistyped branch
#     name — must not fetch into the tree that decides what root builds to
#     answer itself. tests/test-source-tree.sh asserts that half.
#   - a name that is not a branch name at all: traversal, empty
#     components, characters that have no business reaching git or a path.
#
# And two things it must PRESERVE, which are less obvious than the
# refusals: the `on` fact survives both a re-queue and a --reset, because
# it is what tells agent-box-source the box has a way home. Deleting it
# would strand the box exactly where a reset is meant to rescue it from.
#
# No network and no systemd: `origin` is a local repository built here and
# the update trigger is a shim that records its own invocation, so the whole
# file runs natively on every architecture.
set -u

SCRIPT=${1:?usage: test-candidate.sh PATH/TO/candidate.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export HOME="$work/home"; mkdir -p "$HOME"
export GIT_CONFIG_GLOBAL="$work/gitconfig"
export GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid

# --- the "upstream" this box is built from ------------------------------
upstream="$work/upstream"
git init --quiet --initial-branch=master "$upstream"
echo one > "$upstream/file"
git -C "$upstream" add file
git -C "$upstream" commit --quiet -m one
REV_ONE=$(git -C "$upstream" rev-parse HEAD)
echo two > "$upstream/file"
git -C "$upstream" commit --quiet -am two
REV_TWO=$(git -C "$upstream" rev-parse HEAD)
git -C "$upstream" tag v-two

# A candidate branch, off master's tip: what a rebased branch looks like.
git -C "$upstream" checkout --quiet -b ahead
echo three > "$upstream/file"
git -C "$upstream" commit --quiet -am three
REV_AHEAD=$(git -C "$upstream" rev-parse HEAD)
# One that is NOT ahead: it branches from the first commit.
git -C "$upstream" checkout --quiet -b behind "$REV_ONE"
git -C "$upstream" checkout --quiet master
# And one that is master's head exactly, so there is nothing to try.
git -C "$upstream" branch --quiet same "$REV_TWO"
# A pull-request ref, exactly as GitHub serves one. Fetchable by name and
# NOT a branch — the refusal this repo cares about most, since a public
# repo takes pull requests from anyone.
git -C "$upstream" update-ref refs/pull/1/head "$REV_AHEAD"

src="$work/src"
git clone --quiet "$upstream" "$src"
git -C "$src" checkout --quiet -B master "$REV_TWO"

marker="$work/candidate"
trigger_log="$work/triggered"
trigger="$work/bin/trigger"
mkdir -p "$work/bin"
cat > "$trigger" <<'SHIM'
#!/bin/sh
# Stands in for `systemctl start --no-block agent-box-update.service`.
echo "triggered" >> "$TRIGGER_LOG"
SHIM
chmod +x "$trigger"
export TRIGGER_LOG="$trigger_log"

# The renderer prepends the constants; do exactly that, so what is under
# test is the shipped body and not a paraphrase of it.
runner="$work/agent-box-candidate"
{
  printf 'CANDIDATE_FILE=%s\n' "$marker"
  printf 'SRC_DIR=%s\n' "$src"
  printf 'SRC_URL=%s\n' "$upstream"
  printf "SRC_BRANCH=master\n"
  printf 'SYSTEMCTL=/bin/true\n'
  printf 'UPDATE_TRIGGER=%s\n' "$trigger"
  cat "$SCRIPT"
} > "$runner"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

# run ARG... — stdout in $work/out, stderr in $work/err, rc in $rc
run() {
  rc=0
  : > "$trigger_log"
  sh "$runner" "$@" > "$work/out" 2> "$work/err" || rc=$?
}
said() { grep -F "$1" "$work/err" > /dev/null; }
printed() { grep -F "$1" "$work/out" > /dev/null; }
triggered() { [ -s "$trigger_log" ]; }
queued() { sed -n 's/^want=//p' "$marker" 2>/dev/null | head -n 1; }
fact() { sed -n 's/^on=//p' "$marker" 2>/dev/null | head -n 1; }

# --- refusals: not a branch on our origin -------------------------------
# Every one of these must exit non-zero AND leave the queue untouched: a
# refusal that still queued something would be installed by the next
# update, which is the whole failure this validator exists to prevent.
refuses() {
  what=$1; shift
  rm -f "$marker"
  run "$@"
  if [ "$rc" != 0 ] && ! triggered && [ ! -e "$marker" ]; then
    ok "refuses $what"
  else
    no "refuses $what" "rc=$rc triggered=$(triggered && echo yes || echo no) marker=$(cat "$marker" 2>/dev/null)"
  fi
}

refuses "a pull-request ref"            refs/pull/1/head
refuses "a bare sha"                    "$REV_AHEAD"
refuses "a tag"                         v-two
refuses "a branch that does not exist"  no-such-branch
refuses "a path traversal"              ../../etc/passwd
refuses "an embedded .."                "fix/..\\/etc"
refuses "a leading slash"               /master
refuses "a trailing slash"              master/
refuses "an empty component"            fix//thing
refuses "a shell metacharacter"         'fix/x;reboot'
refuses "a space"                       'fix/ x'
refuses "a branch at the tracked branch's own head" same
refuses "two arguments" ahead extra

# Usage goes to stderr, like every other diagnostic here.
run ""; [ "$rc" != 0 ] && said "usage:" \
  && ok "refuses an empty argument, with usage" \
  || no "refuses an empty argument, with usage" "rc=$rc"

# A pull-request ref must be refused for the right REASON — because it is
# not a branch, not because the name looks odd. This is the negative
# control on the --heads scoping: the ref exists and is fetchable.
rm -f "$marker"
run refs/pull/1/head
said "is a ref path, not a branch" \
  && ok "and says so, without inventing a branch to suggest" \
  || no "and says so, without inventing a branch to suggest" "$(cat "$work/err")"
# A refs/heads/ path DOES have a branch to suggest, and says which.
run refs/heads/ahead
said "name the branch itself: 'ahead'" \
  && ok "and a refs/heads/ path is told which branch it meant" \
  || no "and a refs/heads/ path is told which branch it meant" "$(cat "$work/err")"

# --- the accept path ----------------------------------------------------
rm -f "$marker"
run ahead
[ "$rc" = 0 ] && ok "accepts a branch that is ahead" \
  || no "accepts a branch that is ahead" "rc=$rc $(cat "$work/err")"
[ "$(queued)" = "refs/heads/ahead" ] \
  && ok "and queues it as a refs/heads/ path" \
  || no "and queues it as a refs/heads/ path" "$(cat "$marker" 2>/dev/null)"
triggered && ok "and triggers the update" \
  || no "and triggers the update" "the trigger was not run"
said "the next update returns to it" \
  && ok "and says the candidate is temporary" \
  || no "and says the candidate is temporary" "$(cat "$work/err")"

# --- the `on` fact survives what must not strand the box ----------------
printf 'on=%s\n' "$REV_AHEAD" > "$marker"
run ahead
[ "$rc" = 0 ] && [ "$(fact)" = "$REV_AHEAD" ] && [ "$(queued)" = "refs/heads/ahead" ] \
  && ok "re-queueing keeps the on= fact, so a sideways move can still come home" \
  || no "re-queueing keeps the on= fact" "$(cat "$marker" 2>/dev/null)"

printf 'want=refs/heads/ahead\non=%s\n' "$REV_AHEAD" > "$marker"
run --reset
[ "$rc" = 0 ] && [ -z "$(queued)" ] && [ "$(fact)" = "$REV_AHEAD" ] \
  && ok "--reset drops the queue and keeps the on= fact" \
  || no "--reset drops the queue and keeps the on= fact" "$(cat "$marker" 2>/dev/null)"
triggered && ok "and triggers the update that brings the box home" \
  || no "and triggers the update that brings the box home" "the trigger was not run"

printf 'want=refs/heads/ahead\n' > "$marker"
run --reset
[ "$rc" = 0 ] && [ ! -e "$marker" ] \
  && ok "--reset with no on= fact removes the marker entirely" \
  || no "--reset with no on= fact removes the marker" "$(cat "$marker" 2>/dev/null)"

# --- --status reports the tree, not the marker's wishes -----------------
rm -f "$marker"
run --status
[ "$rc" = 0 ] && printed "on the tracked branch" \
  && ok "--status: a box at the tracked branch's head says so" \
  || no "--status: a box at the tracked branch's head says so" "$(cat "$work/out")"

git -C "$src" checkout --quiet -B master "$REV_ONE"
run --status
printed "an update is available" \
  && ok "--status: a box behind the branch says an update is available" \
  || no "--status: behind the branch" "$(cat "$work/out")"

# Off the branch and CLAIMING a candidate, with the claim matching the rev
# the tree is on: a candidate.
git -C "$src" checkout --quiet -B master "$REV_AHEAD"
printf 'on=%s\n' "$REV_AHEAD" > "$marker"
run --status
printed "CANDIDATE" \
  && ok "--status: a verified candidate is reported as one" \
  || no "--status: a verified candidate is reported as one" "$(cat "$work/out")"

# Off the branch with a claim that does NOT match: stale, so not a
# candidate — the same judgement agent-box-source makes before it would
# relax the fast-forward guard, and it must not read as "all fine here".
printf 'on=%s\n' "$REV_ONE" > "$marker"
run --status
printed "the fast-forward guard applies" \
  && ok "--status: a stale claim is not reported as a candidate" \
  || no "--status: a stale claim is not reported as a candidate" "$(cat "$work/out")"

# --- a question is never a change ---------------------------------------
# --status and every refusal above run `git ls-remote` and touch the tree
# not at all — no fetch, no ref, no object. An agent asking what the box
# runs, or fat-fingering a branch name, must leave nothing behind in the
# tree that decides what root builds. (This is why the ancestry half of
# "strictly ahead" lives in agent-box-source: doing it here would have
# meant fetching, on a question.)
# Every ref, not just HEAD, and FETCH_HEAD too: a fetch would show up as a
# new object and a FETCH_HEAD even when the branch pointer never moved, and
# "does not move the tree" has to mean the whole tree.
before=$(git -C "$src" for-each-ref; git -C "$src" rev-parse HEAD)
before_objs=$(find "$src/.git/objects" -type f | sort | wc -l)
rm -f "$src/.git/FETCH_HEAD"
rm -f "$marker"
run --status
run no-such-branch
run refs/pull/1/head
run behind
after=$(git -C "$src" for-each-ref; git -C "$src" rev-parse HEAD)
after_objs=$(find "$src/.git/objects" -type f | sort | wc -l)
[ "$before" = "$after" ] \
  && ok "neither a question nor a refusal moves any ref in the tree" \
  || no "neither a question nor a refusal moves any ref" "refs changed"
[ "$before_objs" = "$after_objs" ] && [ ! -e "$src/.git/FETCH_HEAD" ] \
  && ok "and fetches nothing into it" \
  || no "and fetches nothing into it" "objects $before_objs -> $after_objs, FETCH_HEAD $([ -e "$src/.git/FETCH_HEAD" ] && echo present || echo absent)"

echo
if [ "$fails" = 0 ]; then
  echo "all candidate assertions passed"
else
  echo "$fails failing assertion(s)"
fi
exit "$fails"
