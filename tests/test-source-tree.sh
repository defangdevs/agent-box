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

BASH_BIN=$(command -v bash)
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
# It reads the REMOTE. "Is there an update" must not be the thing that
# creates the checkout or fetches into it — and on a box that has never
# updated there is no tree to consult in the first place, so the alternative
# was cloning the whole repo to answer a question.
at "$REV_ONE"
run check
[ "$rc" = 0 ] && [ "$(out)" = "$REV_THREE" ] \
  && ok "check reports the tip" || no "check reports the tip" "rc=$rc out=$(out)"
[ "$(head_of "$dir")" = "$REV_ONE" ] \
  && ok "check does not move the tree" || no "check does not move the tree" "$(head_of "$dir")"
before_fetch=$(git -C "$dir" rev-parse refs/remotes/origin/master)
git -C "$dir" update-ref refs/remotes/origin/master "$REV_ONE"
run check
[ "$(git -C "$dir" rev-parse refs/remotes/origin/master)" = "$REV_ONE" ] \
  && ok "check does not fetch into the tree either" \
  || no "check does not fetch into the tree either" "$(git -C "$dir" rev-parse refs/remotes/origin/master) != $REV_ONE"
git -C "$dir" update-ref refs/remotes/origin/master "$before_fetch"

rm -rf "$dir"
run check
[ "$rc" = 0 ] && [ "$(out)" = "$REV_THREE" ] && [ ! -e "$dir" ] \
  && ok "check works with no tree at all, and creates none" \
  || no "check works with no tree at all, and creates none" "rc=$rc out=$(out) dir=$([ -e "$dir" ] && echo exists)"

AGENT_BOX_SRC_REF=v-two run check
[ "$rc" = 0 ] && [ "$(out)" = "$REV_TWO" ] \
  && ok "check resolves an explicit ref" || no "check resolves an explicit ref" "rc=$rc out=$(out)"

AGENT_BOX_SRC_REF=no-such-ref run check
[ "$rc" != 0 ] && [ -z "$(out)" ] \
  && ok "check refuses a ref that does not exist" || no "check refuses a ref that does not exist" "rc=$rc out=$(out)"

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

# --- git hooks in the tree never run ------------------------------------
# Every git here runs AS ROOT, and a pull runs both commands git fires hooks
# for: `git checkout -B` on the realign (post-checkout) and `git merge` on
# the fast-forward (post-merge). The module confines the tree to /var/lib so
# nothing but root can put a hook there; this is the second lock, and it is
# the one that still holds if the first is ever bypassed.
#
# The tree starts AHEAD of the running rev so the realign actually happens —
# otherwise the pull is a merge alone and post-checkout is never reached,
# which is a test that passes without the lock in place.
at "$REV_THREE"
mkdir -p "$dir/.git/hooks"
for h in post-checkout post-merge; do
  cat > "$dir/.git/hooks/$h" <<HOOK
#!$BASH_BIN
: > "$work/HOOK-RAN-$h"
HOOK
  chmod +x "$dir/.git/hooks/$h"
done
rm -f "$work"/HOOK-RAN-*
AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" = 0 ] && [ "$(head_of "$dir")" = "$REV_THREE" ] \
  && ok "the hooked tree still pulls (realign, then fast-forward)" \
  || no "the hooked tree still pulls (realign, then fast-forward)" "rc=$rc head=$(head_of "$dir")"
[ ! -e "$work/HOOK-RAN-post-checkout" ] \
  && ok "the realign's checkout runs no post-checkout hook" \
  || no "the realign's checkout runs no post-checkout hook" "the hook ran as this script's user"
[ ! -e "$work/HOOK-RAN-post-merge" ] \
  && ok "the fast-forward runs no post-merge hook" \
  || no "the fast-forward runs no post-merge hook" "the hook ran as this script's user"

# --- a $dir that exists but is not a checkout ---------------------------
# `mv src.incoming src` moves the clone INSIDE an existing src, so the
# publish would quietly produce src/src.incoming: a nested checkout, a $dir
# that still has no .git, and a clone re-attempted on every later run. An
# empty directory (a tmpfiles rule, an interrupted run) is reclaimed; one
# with anything in it is somebody's, and is refused rather than nested.
rm -rf "$dir"; mkdir -p "$dir"
AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" = 0 ] && [ -e "$dir/.git" ] && [ ! -e "$dir/src.incoming" ] \
  && ok "an empty pre-existing directory is reclaimed, not nested into" \
  || no "an empty pre-existing directory is reclaimed, not nested into" \
       "rc=$rc $(ls -a "$dir" 2>/dev/null | tr '\n' ' ')"

rm -rf "$dir"; mkdir -p "$dir"; : > "$dir/somebody-elses-file"
AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" != 0 ] && said "is not a checkout" && [ ! -e "$dir/.git" ] \
  && ok "a non-empty pre-existing directory is refused" \
  || no "a non-empty pre-existing directory is refused" "rc=$rc"

# --- an unresolvable ref writes nothing to stdout -----------------------
# `check` returns the rev ON STDOUT, so a bare `git rev-parse` echoing the
# argument it could not resolve would hand the caller that string as a rev.
at "$REV_ONE"
AGENT_BOX_SRC_BRANCH=no-such-branch run check
[ "$rc" != 0 ] && [ -z "$(out)" ] \
  && ok "a failed check prints no rev at all" \
  || no "a failed check prints no rev at all" "rc=$rc out=$(out)"

# --- a queued candidate (agent-box-candidate) ---------------------------
#
# The other half of "try a fix on this box before the fleet takes it": the
# wrapper queues a branch, and THIS script is what consumes it. Three
# properties are load-bearing, and each one is the difference between a
# candidate and a pin:
#
#   - it is consumed, once. A candidate that survived its own update would
#     be reinstalled by every later trigger, and a box that failed to build
#     it would retry forever.
#   - coming home does not need the operator. A candidate is squash-merged,
#     so its head is never an ancestor of the tracked branch and --ff-only
#     refuses the way back — the box would be stuck off-branch, which is the
#     exact permanent divergence the feature promises not to create.
#   - an explicit --rev keeps the strict guard. The relaxation is for the
#     queue and for the way home, not a general-purpose force.
candidate_file="$work/candidate"
export AGENT_BOX_CANDIDATE_FILE="$candidate_file"

# A branch off the tracked branch's tip, which is what a rebased candidate
# looks like. REV_THREE is master's head throughout.
git -C "$upstream" branch --quiet cand "$REV_THREE" 2>/dev/null || \
  git -C "$upstream" branch -f cand "$REV_THREE"
git -C "$upstream" checkout --quiet cand
echo four > "$upstream/file"
git -C "$upstream" commit --quiet -am four
REV_CAND=$(git -C "$upstream" rev-parse HEAD)
git -C "$upstream" checkout --quiet master

at "$REV_THREE"
export AGENT_BOX_SRC_REV="$REV_THREE"
printf 'want=refs/heads/cand\n' > "$candidate_file"
run pull
[ "$rc" = 0 ] && [ "$(out)" = "$REV_CAND" ] \
  && ok "a queued candidate is what the tree moves to" \
  || no "a queued candidate is what the tree moves to" "rc=$rc out=$(out) want=$REV_CAND"
said "installing candidate refs/heads/cand" \
  && ok "and it says so, naming the branch" \
  || no "and it says so, naming the branch" "$(cat "$work/err")"
grep -q "^want=" "$candidate_file" 2>/dev/null \
  && no "the queued candidate is consumed" "want= survived the pull" \
  || ok "the queued candidate is consumed, so no later update reinstalls it"
[ "$(sed -n 's/^on=//p' "$candidate_file")" = "$REV_CAND" ] \
  && ok "and the box records WHICH candidate it now runs" \
  || no "and the box records which candidate it now runs" "$(cat "$candidate_file" 2>/dev/null)"

# A candidate that is NOT ahead of the tracked branch is refused here,
# whatever wrote the marker. This is what keeps "candidate" from being a way
# to replay an older rev, and it has to live on this side: the force path
# below is taken by any box already off the branch, so a candidate accepted
# without this check would be installed with no ancestry guard at all.
git -C "$upstream" branch --quiet stale "$REV_ONE" 2>/dev/null || \
  git -C "$upstream" branch -f stale "$REV_ONE"
at "$REV_THREE"
printf 'want=refs/heads/stale\n' > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_THREE" run pull
[ "$rc" != 0 ] && said "is not ahead of master" \
  && ok "a candidate behind the tracked branch is refused" \
  || no "a candidate behind the tracked branch is refused" "rc=$rc out=$(out)"
[ "$(head_of "$dir")" = "$REV_THREE" ] \
  && ok "and the tree does not move for it" \
  || no "and the tree does not move for it" "$(head_of "$dir")"

# Same refusal on a box that is already off the branch, which is the case
# that matters: there the guard would otherwise be forced past.
at "$REV_CAND"
printf 'want=refs/heads/stale\non=%s\n' "$REV_CAND" > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_CAND" run pull
[ "$rc" != 0 ] && said "is not ahead of master" \
  && ok "and refused on a candidate box too, where force would apply" \
  || no "and refused on a candidate box too" "rc=$rc out=$(out)"

# Now the box RUNS the candidate, and the branch has been squash-merged:
# master has a new commit that is not the candidate's parent, so the
# candidate is not an ancestor of master and --ff-only cannot get home.
echo five > "$upstream/file"
git -C "$upstream" commit --quiet -am "squash of cand"
REV_MERGED=$(git -C "$upstream" rev-parse HEAD)
at "$REV_CAND"
printf 'on=%s\n' "$REV_CAND" > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_CAND" run pull
[ "$rc" = 0 ] && [ "$(out)" = "$REV_MERGED" ] \
  && ok "a plain update brings a candidate box home to the tracked branch" \
  || no "a plain update brings a candidate box home" "rc=$rc out=$(out) want=$REV_MERGED"
said "runs candidate" \
  && ok "and names why the fast-forward check was skipped" \
  || no "and names why the fast-forward check was skipped" "$(cat "$work/err")"
[ ! -e "$candidate_file" ] \
  && ok "and clears the marker, so nothing stays forced once it is home" \
  || no "and clears the marker once home" "$(cat "$candidate_file" 2>/dev/null)"

# THE safety property. "Off the tracked branch" is not the signal — a
# rewritten upstream history looks identical from the ancestry side, and
# that is the one thing --ff-only exists to refuse. Without a marker
# claiming a candidate, an off-branch box keeps the strict guard.
at "$REV_CAND"
rm -f "$candidate_file"
AGENT_BOX_SRC_REV="$REV_CAND" AGENT_BOX_SRC_REF="$REV_ONE" run pull
[ "$rc" != 0 ] && said "refusing update" \
  && ok "an off-branch box with no candidate marker is still guarded" \
  || no "an off-branch box with no candidate marker is still guarded" "rc=$rc out=$(out)"

# And a marker whose claim does not match the rev the box runs is stale —
# a rebuild that failed and rolled the tree back leaves exactly that — so
# it is ignored rather than trusted into a force.
at "$REV_CAND"
printf 'on=%s\n' "$REV_ONE" > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_CAND" AGENT_BOX_SRC_REF="$REV_ONE" run pull
[ "$rc" != 0 ] && said "ignoring it" \
  && ok "a stale candidate marker is ignored, not trusted" \
  || no "a stale candidate marker is ignored, not trusted" "rc=$rc out=$(out)"

# The relaxation is scoped to the way home. An operator naming an OLDER rev
# explicitly is refused even on a genuine candidate box, so "I once tried a
# candidate" never becomes "this box accepts downgrades".
at "$REV_CAND"
printf 'on=%s\n' "$REV_CAND" > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_CAND" AGENT_BOX_SRC_REF="$REV_ONE" run pull
[ "$rc" != 0 ] && said "refusing update" \
  && ok "an explicit older --rev is still refused on a candidate box" \
  || no "an explicit older --rev is still refused on a candidate box" "rc=$rc out=$(out)"

# And the queue does not override an operator who named a ref.
at "$REV_TWO"
printf 'want=refs/heads/cand\n' > "$candidate_file"
AGENT_BOX_SRC_REV="$REV_TWO" AGENT_BOX_SRC_REF="$REV_THREE" run pull
[ "$rc" = 0 ] && [ "$(out)" = "$REV_THREE" ] \
  && ok "an explicit --rev wins over a queued candidate" \
  || no "an explicit --rev wins over a queued candidate" "rc=$rc out=$(out)"
grep -q "^want=" "$candidate_file" 2>/dev/null \
  && ok "and leaves the queue alone, so the next plain update still sees it" \
  || no "and leaves the queue alone" "marker was consumed by a --rev run"
rm -f "$candidate_file"

# A box with the feature unwired behaves exactly as it did before it
# existed: no marker path, no candidate, no change in the guard.
at "$REV_ONE"
AGENT_BOX_CANDIDATE_FILE= AGENT_BOX_SRC_REV="$REV_ONE" run pull
[ "$rc" = 0 ] && [ "$(out)" = "$REV_MERGED" ] \
  && ok "with no marker path wired, a pull is an ordinary fast-forward" \
  || no "with no marker path wired, a pull is an ordinary fast-forward" "rc=$rc out=$(out)"

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
