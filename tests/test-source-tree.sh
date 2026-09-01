#!/usr/bin/env bash
# Unit tests for `agent-box-source` (issue #242) — the tree the box is BUILT
# FROM, and the whole of what "update the box" now moves.
#
# The happy path is one line of git. What is worth pinning is everything
# around it, because this runs as ROOT, unattended, and the tree it moves
# decides what the next `nixos-rebuild switch` (or `nix profile install`)
# builds:
#
#   - the fast-forward REFUSAL. It is the only thing standing between the
#     box and a rewritten history or a replay of an older, possibly
#     vulnerable rev, and it replaced a GitHub API compare — so it has to be
#     at least as strict as the thing it replaced, in both backends at once.
#   - the REALIGN. The guard is only meaningful if ancestry is measured from
#     the rev the box actually runs, so a tree found anywhere else has to be
#     put back before the merge, never merged from where it was.
#   - the ERRORS. A refusal that exits 0 would let update.sh rebuild from a
#     tree it never moved and call that an update.
#
# No network and no GitHub: `origin` is a local repository built here, which
# makes the whole file runnable natively on every architecture — which is
# where the VM tests cannot go.
set -u

SCRIPT=${1:?usage: test-source-tree.sh PATH/TO/source-tree.sh}
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
echo three > "$upstream/file"
git -C "$upstream" commit --quiet -am three
REV_THREE=$(git -C "$upstream" rev-parse HEAD)

export AGENT_BOX_SRC_URL="$upstream"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

# run VERB [ARG] — stdout in $work/out, stderr in $work/err, rc in $rc
run() {
  rc=0
  bash "$SCRIPT" "$@" > "$work/out" 2> "$work/err" || rc=$?
}
out() { cat "$work/out"; }
said() { grep -F "$1" "$work/err" > /dev/null; }
head_of() { git -C "$1" rev-parse HEAD 2>/dev/null; }
branch_of() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null; }

dir="$work/src"
export AGENT_BOX_SRC_DIR="$dir"
# A tree in the state the updater leaves behind: on the tracked branch, at
# the rev the box runs.
at() {
  rm -rf "$dir"
  git clone --quiet "$upstream" "$dir"
  git -C "$dir" checkout --quiet -B master "$1"
}

# --- refusals that are configuration errors -----------------------------
# Each of these must be a NON-ZERO exit: update.sh reads this script's
# stdout as the rev to rebuild, so a silent "" with rc 0 would rebuild the
# box against nothing and report success.
AGENT_BOX_SRC_DIR= run pull; [ "$rc" != 0 ] \
  && said "AGENT_BOX_SRC_DIR is unset" \
  && ok "no SRC_DIR is an error" || no "no SRC_DIR is an error" "rc=$rc"

AGENT_BOX_SRC_DIR=relative/path run pull; [ "$rc" != 0 ] \
  && said "must be absolute" \
  && ok "a relative SRC_DIR is an error" || no "a relative SRC_DIR is an error" "rc=$rc"

AGENT_BOX_SRC_URL= run pull; [ "$rc" != 0 ] \
  && said "AGENT_BOX_SRC_URL is unset" \
  && ok "no SRC_URL is an error" || no "no SRC_URL is an error" "rc=$rc"

rm -rf "$dir"
run pull; [ "$rc" != 0 ] \
  && said "refusing to move a tree with no baseline" \
  && ok "pull with no running rev is an error" || no "pull with no running rev is an error" "rc=$rc"
[ ! -e "$dir" ] && ok "a rejected pull writes nothing" \
  || no "a rejected pull writes nothing" "$dir exists"

run bogus; [ "$rc" != 0 ] && said "usage:" \
  && ok "an unknown verb is an error" || no "an unknown verb is an error" "rc=$rc"

# --- the first run: clone, then fast-forward ---------------------------
export AGENT_BOX_SRC_REV="$REV_ONE"
rm -rf "$dir"
run pull
[ "$rc" = 0 ] && ok "first run clones and pulls" || no "first run clones and pulls" "rc=$rc $(cat "$work/err")"
[ "$(out)" = "$REV_THREE" ] && ok "it prints the rev it moved to" \
  || no "it prints the rev it moved to" "got $(out), want $REV_THREE"
[ "$(head_of "$dir")" = "$REV_THREE" ] && ok "the tree is at that rev" \
  || no "the tree is at that rev" "$(head_of "$dir")"
# A detached tree has no branch for `git merge` to advance, so every later
# run would have to re-detach it. The clone lands on the branch tip, which
# is AHEAD of the running rev — hence the rewind below, not a plain merge.
[ "$(branch_of "$dir")" = master ] && ok "the tree is left on the tracked branch" \
  || no "the tree is left on the tracked branch" "$(branch_of "$dir")"

# --- already current ----------------------------------------------------
AGENT_BOX_SRC_REV="$REV_THREE" run pull
[ "$rc" = 0 ] && [ "$(out)" = "$REV_THREE" ] \
  && ok "a box already at the tip pulls to itself" \
  || no "a box already at the tip pulls to itself" "rc=$rc out=$(out)"

# --- the realign --------------------------------------------------------
# The tree says three, the box runs one. The running rev is the truth: the
# guard has to measure ancestry from what is RUNNING, so the tree is rewound
# first. Without this, a tree left ahead would make the next non-fast-forward
# look like a fast-forward.
at "$REV_THREE"
AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" = 0 ] && said "realigning" \
  && ok "a tree ahead of the box is realigned first" \
  || no "a tree ahead of the box is realigned first" "rc=$rc"
[ "$(head_of "$dir")" = "$REV_THREE" ] \
  && ok "and still ends at the branch tip" || no "and still ends at the branch tip" "$(head_of "$dir")"

# A detached tree (somebody inspected it) gets its branch back rather than
# failing the merge.
at "$REV_ONE"; git -C "$dir" checkout --quiet --detach "$REV_ONE"
AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" = 0 ] && [ "$(branch_of "$dir")" = master ] \
  && ok "a detached tree gets its branch back" \
  || no "a detached tree gets its branch back" "rc=$rc branch=$(branch_of "$dir")"

# A running rev the tree has never heard of cannot be a baseline, and
# guessing one would silently move the guard.
at "$REV_ONE"
AGENT_BOX_SRC_REV=0000000000000000000000000000000000000000 run pull
[ "$rc" != 0 ] && said "cannot describe this box" \
  && ok "an unknown running rev is refused" || no "an unknown running rev is refused" "rc=$rc"

# --- THE guard ----------------------------------------------------------
# Upstream rewrites master onto a commit that is not a descendant of what
# this box runs: a force-push, or an older rev being replayed. This is the
# refusal the GitHub compare used to make.
rewritten="$work/rewritten"
rm -rf "$rewritten"; cp -r "$upstream" "$rewritten"
git -C "$rewritten" checkout --quiet -B master "$REV_ONE"
echo sideways > "$rewritten/file"
git -C "$rewritten" commit --quiet -am sideways
REV_SIDE=$(git -C "$rewritten" rev-parse HEAD)

at "$REV_THREE"
AGENT_BOX_SRC_URL="$rewritten" AGENT_BOX_SRC_REV="$REV_THREE" run pull
[ "$rc" != 0 ] && said "refusing update" \
  && ok "a non-fast-forward target is refused" || no "a non-fast-forward target is refused" "rc=$rc"
[ "$(head_of "$dir")" = "$REV_THREE" ] \
  && ok "and the tree does not move" || no "and the tree does not move" "$(head_of "$dir")"

# --force is the ONE documented way past it (a deliberate downgrade), and it
# says so in the journal rather than moving quietly.
AGENT_BOX_SRC_URL="$rewritten" AGENT_BOX_SRC_REV="$REV_THREE" AGENT_BOX_SRC_FORCE=1 run pull
[ "$rc" = 0 ] && [ "$(head_of "$dir")" = "$REV_SIDE" ] && said "without the fast-forward check" \
  && ok "--force moves anyway, and says so" \
  || no "--force moves anyway, and says so" "rc=$rc head=$(head_of "$dir")"

# --- check: an answer, not an action ------------------------------------
at "$REV_ONE"
run check
[ "$rc" = 0 ] && [ "$(out)" = "$REV_THREE" ] \
  && ok "check reports the tip" || no "check reports the tip" "rc=$rc out=$(out)"
[ "$(head_of "$dir")" = "$REV_ONE" ] \
  && ok "check does not move the tree" || no "check does not move the tree" "$(head_of "$dir")"

# --- an explicit ref ----------------------------------------------------
# `agentbox update --rev v-two`: fetched by name, and still through the
# guard — so a tag BEHIND the running rev is refused exactly as a rewritten
# branch is.
at "$REV_ONE"
AGENT_BOX_SRC_REF=v-two run pull
[ "$rc" = 0 ] && [ "$(head_of "$dir")" = "$REV_TWO" ] \
  && ok "an explicit ref is a valid target" || no "an explicit ref is a valid target" "rc=$rc head=$(head_of "$dir")"

at "$REV_THREE"
AGENT_BOX_SRC_REV="$REV_THREE" AGENT_BOX_SRC_REF=v-two run pull
[ "$rc" != 0 ] && said "refusing update" \
  && ok "a ref behind the running rev is refused" || no "a ref behind the running rev is refused" "rc=$rc"

AGENT_BOX_SRC_REF=no-such-ref run pull
[ "$rc" != 0 ] && said "no ref" \
  && ok "a ref that does not exist is an error" || no "a ref that does not exist is an error" "rc=$rc"

# --- an explicit branch -------------------------------------------------
git -C "$upstream" branch --quiet stable "$REV_TWO"
at "$REV_ONE"
AGENT_BOX_SRC_BRANCH=stable run pull
[ "$rc" = 0 ] && [ "$(head_of "$dir")" = "$REV_TWO" ] \
  && ok "a named branch is followed instead of the default" \
  || no "a named branch is followed instead of the default" "rc=$rc head=$(head_of "$dir")"

AGENT_BOX_SRC_BRANCH=no-such-branch run pull
[ "$rc" != 0 ] && said "no branch" \
  && ok "a branch that does not exist is an error" || no "a branch that does not exist is an error" "rc=$rc"

# --- the configured URL wins --------------------------------------------
# A host that repoints selfUpdate.repo — a fork, a mirror, a rename — must
# not leave the box fetching the repo it was first cloned from and calling
# itself up to date against a repo nobody meant it to follow.
at "$REV_ONE"
git -C "$dir" remote set-url origin "$work/somewhere-else"
run pull
[ "$rc" = 0 ] && said "repointing" && [ "$(git -C "$dir" remote get-url origin)" = "$upstream" ] \
  && ok "an origin that disagrees with the config is repointed" \
  || no "an origin that disagrees with the config is repointed" "rc=$rc $(git -C "$dir" remote get-url origin)"

# --- reset: what a failed rebuild does ----------------------------------
at "$REV_THREE"
run reset "$REV_ONE"
[ "$rc" = 0 ] && [ "$(head_of "$dir")" = "$REV_ONE" ] \
  && ok "reset moves the tree back" || no "reset moves the tree back" "rc=$rc head=$(head_of "$dir")"
[ "$(branch_of "$dir")" = master ] \
  && ok "reset leaves it on the branch" || no "reset leaves it on the branch" "$(branch_of "$dir")"
run reset
[ "$rc" != 0 ] && ok "reset with no rev is an error" || no "reset with no rev is an error" "rc=$rc"

run rev
[ "$rc" = 0 ] && [ "$(out)" = "$REV_ONE" ] \
  && ok "rev prints HEAD" || no "rev prints HEAD" "rc=$rc out=$(out)"

# --- a clone that cannot happen -----------------------------------------
# Offline, or a repo this box's token cannot read. It must fail loudly and
# leave nothing behind: a half-clone at $dir would be skipped by every later
# run (the .git test) and could never repair itself.
rm -rf "$dir"
AGENT_BOX_SRC_URL="$work/nope" run pull
[ "$rc" != 0 ] && said "clone of" \
  && ok "an impossible clone is an error" || no "an impossible clone is an error" "rc=$rc"
[ ! -e "$dir" ] && [ ! -e "$dir.incoming" ] \
  && ok "and leaves neither the tree nor a partial one" \
  || no "and leaves neither the tree nor a partial one" "$(ls -d "$dir"* 2>/dev/null)"

echo
if [ "$fails" = 0 ]; then
  echo "all source-tree assertions passed"
else
  echo "$fails failing assertion(s)"
fi
exit "$fails"
