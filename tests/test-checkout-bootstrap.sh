#!/usr/bin/env bash
# Unit tests for `agent-box-checkout` (issue #242) — the script that puts
# this box's own sources on the box.
#
# What is worth pinning here is not the happy path. It is the REFUSALS: this
# script runs unattended, in the background, at every supervisor start, on a
# tree that sibling sessions are working in. A realign that moved somebody's
# branch pointer, or a second clone over a working tree, would destroy work
# that exists nowhere else — and it would do it silently, at boot, on a box
# nobody is watching. So every "leave it alone" branch gets an assertion.
#
# No network and no GitHub: `origin` is a local repository built here, and
# `gh` is a shim that records its argv. That makes the whole file runnable
# natively on every architecture, which is where the VM tests cannot go.
set -u

SCRIPT=${1:?usage: test-checkout-bootstrap.sh PATH/TO/checkout-cli.sh}
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

export AGENT_BOX_CHECKOUT_URL="$upstream"

# --- gh shim: records argv, answers `auth status` per GH_SHIM_AUTH ------
mkdir -p "$work/bin"
cat > "$work/bin/gh" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "$work/gh.log"
if [ "\$1 \$2" = "auth status" ]; then
  [ -n "\${GH_SHIM_AUTH:-}" ] && exit 0
  exit 1
fi
if [ "\$1 \$2" = "repo fork" ]; then
  [ -n "\${GH_SHIM_FORK_FAILS:-}" ] && exit 1
  # What the real thing does with --remote --remote-name fork: add the
  # remote, leave origin alone.
  git remote add fork "$work/forked.git" 2>/dev/null || true
  exit 0
fi
exit 0
EOF
chmod +x "$work/bin/gh"
PATH="$work/bin:$PATH"; export PATH

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

run() { bash "$SCRIPT" > "$work/out" 2>&1; }
head_of() { git -C "$1" rev-parse HEAD 2>/dev/null; }
said() { grep -F "$1" "$work/out" > /dev/null; }

fresh() { # fresh DIR — a checkout in the state the bootstrap leaves it
  rm -rf "$1"
  git clone --quiet "$upstream" "$1"
  git -C "$1" checkout --quiet --detach "$2"
}

# --- no checkout configured: a no-op, and a SUCCESSFUL one --------------
# The supervisor calls this unconditionally, and every user who is not the
# maintainer lands here. A non-zero exit would be a failed unit start.
unset AGENT_BOX_CHECKOUT_DIR AGENT_BOX_CHECKOUT_REV
if run; then ok "no AGENT_BOX_CHECKOUT_DIR exits 0"
else no "no AGENT_BOX_CHECKOUT_DIR exits 0" "$(cat "$work/out")"; fi

# --- a relative dir: refuse. The clone publishes by renaming a SIBLING of
# this path, so "absolute" is a precondition of the step below, not taste.
export AGENT_BOX_CHECKOUT_DIR=box
export AGENT_BOX_CHECKOUT_REV=$REV_ONE
if run; then no "a relative dir is an error" "exited 0"
else ok "a relative dir is an error"; fi
unset AGENT_BOX_CHECKOUT_REV

# --- a dir but no rev: refuse rather than guess -------------------------
export AGENT_BOX_CHECKOUT_DIR="$work/box"
if run; then no "no rev is an error" "exited 0"
else ok "no rev is an error"; fi
if [ -e "$AGENT_BOX_CHECKOUT_DIR" ]; then
  no "no rev writes nothing" "created $AGENT_BOX_CHECKOUT_DIR"
else ok "no rev writes nothing"; fi

# --- first boot: clone, parked on the rev the box RUNS ------------------
# Not the remote's head. REV_ONE is deliberately the older commit: a tree
# parked on master would describe a box nobody is running.
export AGENT_BOX_CHECKOUT_REV=$REV_ONE
if run; then ok "first run clones"; else no "first run clones" "$(cat "$work/out")"; fi
if [ "$(head_of "$AGENT_BOX_CHECKOUT_DIR")" = "$REV_ONE" ]; then
  ok "clone is parked on the running rev, not the remote head"
else
  no "clone is parked on the running rev, not the remote head" \
     "HEAD is $(head_of "$AGENT_BOX_CHECKOUT_DIR"), wanted $REV_ONE"
fi
if [ "$(git -C "$AGENT_BOX_CHECKOUT_DIR" rev-parse --abbrev-ref HEAD)" = "HEAD" ]; then
  ok "clone is left detached"
else no "clone is left detached" "on a branch"; fi

# --- the clone is published by rename, so it is never half a tree -------
# git writes .git early and cleans up only after ITS OWN failure; a SIGKILL
# mid-clone is not a failure. Cloned straight into place, one unlucky reboot
# would leave a .git with no HEAD — which every later run would skip (the
# "no .git" test), fail to read, and refuse to touch. A checkout no boot can
# repair, on the code path a first boot takes. The rename is what makes that
# state unreachable, so both halves are pinned: no leftover after success...
if [ ! -e "$AGENT_BOX_CHECKOUT_DIR.incoming" ]; then
  ok "the staging tree is gone once the clone is published"
else no "the staging tree is gone once the clone is published" "still there"; fi

# ...and a leftover from a killed run is reclaimed rather than inherited.
rm -rf "$AGENT_BOX_CHECKOUT_DIR"
mkdir -p "$AGENT_BOX_CHECKOUT_DIR.incoming/.git"   # what a SIGKILL leaves
run
if [ "$(head_of "$AGENT_BOX_CHECKOUT_DIR")" = "$AGENT_BOX_CHECKOUT_REV" ] \
   && [ ! -e "$AGENT_BOX_CHECKOUT_DIR.incoming" ]; then
  ok "an interrupted clone is reclaimed by the next run"
else
  no "an interrupted clone is reclaimed by the next run" "$(cat "$work/out")"
fi

# --- REFUSAL: $dir already exists ---------------------------------------
# Plain `mv` into an existing directory moves the source INSIDE it, so the
# publish would produce $dir/<basename>.incoming: a nested checkout, a $dir
# with no .git, and a clone repeated on every later run. An operator or an
# agent running `mkdir -p` on the configured path is all it takes. `mv -T`
# is what refuses that, in rename(2) rather than in a check that could be
# raced, so both destination states are pinned here.
rm -rf "$AGENT_BOX_CHECKOUT_DIR" "$AGENT_BOX_CHECKOUT_DIR.incoming"
mkdir -p "$AGENT_BOX_CHECKOUT_DIR"                 # empty: reclaim it
AGENT_BOX_CHECKOUT_REV=$REV_ONE run
if [ "$(head_of "$AGENT_BOX_CHECKOUT_DIR")" = "$REV_ONE" ] \
   && [ ! -e "$AGENT_BOX_CHECKOUT_DIR/$(basename "$AGENT_BOX_CHECKOUT_DIR").incoming" ]; then
  ok "an empty dir is reclaimed, not cloned into"
else
  no "an empty dir is reclaimed, not cloned into" "$(cat "$work/out")"
fi

# Non-empty is somebody's data: refuse, say so, and leave it alone. The
# clone must not be published anywhere — a nested tree is worse than none,
# because the next run cannot tell it from a fresh start.
rm -rf "$AGENT_BOX_CHECKOUT_DIR" "$AGENT_BOX_CHECKOUT_DIR.incoming"
mkdir -p "$AGENT_BOX_CHECKOUT_DIR"
echo mine > "$AGENT_BOX_CHECKOUT_DIR/not-a-checkout"
AGENT_BOX_CHECKOUT_REV=$REV_ONE run
if said "could not publish" \
   && [ "$(cat "$AGENT_BOX_CHECKOUT_DIR/not-a-checkout")" = mine ] \
   && [ ! -e "$AGENT_BOX_CHECKOUT_DIR/.git" ]; then
  ok "a non-empty dir is refused, and its contents survive"
else
  no "a non-empty dir is refused, and its contents survive" "$(cat "$work/out")"
fi
rm -rf "$AGENT_BOX_CHECKOUT_DIR" "$AGENT_BOX_CHECKOUT_DIR.incoming"

# A rev the remote does not have leaves NOTHING behind — publishing a tree
# parked on the wrong commit is the failure this whole script exists to
# avoid, so a half-right tree is worse than none.
rm -rf "$AGENT_BOX_CHECKOUT_DIR"
AGENT_BOX_CHECKOUT_REV=0000000000000000000000000000000000000000 run
if [ ! -e "$AGENT_BOX_CHECKOUT_DIR" ] && [ ! -e "$AGENT_BOX_CHECKOUT_DIR.incoming" ]; then
  ok "an unreachable rev publishes nothing"
else no "an unreachable rev publishes nothing" "$(cat "$work/out")"; fi
fresh "$AGENT_BOX_CHECKOUT_DIR" "$REV_ONE"

# --- second run over the same tree: idempotent, no re-clone ------------
marker="$AGENT_BOX_CHECKOUT_DIR/.mine"
: > "$marker"
if run && [ -e "$marker" ]; then ok "a second run leaves the tree alone"
else no "a second run leaves the tree alone" "$(cat "$work/out")"; fi

# --- the box updated past the tree: realign a clean detached tree ------
# This is the only case where the script moves anything, and it moves it to
# a rev the box is ALREADY running — so it changes what the tree SAYS, never
# what the box does.
rm -f "$marker"
export AGENT_BOX_CHECKOUT_REV=$REV_TWO
run
if [ "$(head_of "$AGENT_BOX_CHECKOUT_DIR")" = "$REV_TWO" ]; then
  ok "a clean detached tree realigns to the new running rev"
else
  no "a clean detached tree realigns to the new running rev" \
     "HEAD is $(head_of "$AGENT_BOX_CHECKOUT_DIR")"
fi

# --- REFUSAL: a tree with uncommitted changes --------------------------
fresh "$AGENT_BOX_CHECKOUT_DIR" "$REV_ONE"
echo mine > "$AGENT_BOX_CHECKOUT_DIR/file"
run
if [ "$(head_of "$AGENT_BOX_CHECKOUT_DIR")" = "$REV_ONE" ] \
   && [ "$(cat "$AGENT_BOX_CHECKOUT_DIR/file")" = "mine" ]; then
  ok "a dirty tree is not realigned"
else no "a dirty tree is not realigned" "$(cat "$work/out")"; fi
if said "uncommitted changes"; then ok "a dirty tree says why in the journal"
else no "a dirty tree says why in the journal" "$(cat "$work/out")"; fi

# --- REFUSAL: a tree somebody put on a branch --------------------------
# Realigning here would move their branch pointer, which is the one thing
# `git checkout --detach` on a named branch does that cannot be undone from
# the reflog by somebody who does not know it happened.
fresh "$AGENT_BOX_CHECKOUT_DIR" "$REV_ONE"
git -C "$AGENT_BOX_CHECKOUT_DIR" checkout --quiet -b work
run
if [ "$(git -C "$AGENT_BOX_CHECKOUT_DIR" rev-parse --abbrev-ref HEAD)" = "work" ]; then
  ok "a tree on a branch is not realigned"
else no "a tree on a branch is not realigned" "$(cat "$work/out")"; fi
if said "on a branch"; then ok "a branched tree says why in the journal"
else no "a branched tree says why in the journal" "$(cat "$work/out")"; fi

# --- the fork remote ---------------------------------------------------
fork_url() { git -C "$AGENT_BOX_CHECKOUT_DIR" remote get-url fork 2>/dev/null; }

# Off by default: creating a repository in the operator's GitHub account is
# not a side effect of wanting a tree to read.
fresh "$AGENT_BOX_CHECKOUT_DIR" "$REV_TWO"
unset AGENT_BOX_CHECKOUT_FORK
: > "$work/gh.log"
run
if [ -z "$(fork_url)" ] && [ ! -s "$work/gh.log" ]; then
  ok "no fork remote, and gh is not called, without AGENT_BOX_CHECKOUT_FORK"
else no "no fork remote without AGENT_BOX_CHECKOUT_FORK" "$(cat "$work/gh.log")"; fi

# Asked for, but no token yet — the ordinary first boot. The tree must
# still be there, and the run must not fail over it.
export AGENT_BOX_CHECKOUT_FORK=1
unset GH_SHIM_AUTH
: > "$work/gh.log"
if run && [ -z "$(fork_url)" ]; then
  ok "an unauthenticated gh adds no remote and is not fatal"
else no "an unauthenticated gh adds no remote and is not fatal" "$(cat "$work/out")"; fi

# With a token: the fork remote appears, and origin is untouched — origin is
# what the box is built FROM, and what the rev above is a rev of.
export GH_SHIM_AUTH=1
: > "$work/gh.log"
run
if [ -n "$(fork_url)" ]; then ok "a token adds the fork remote"
else no "a token adds the fork remote" "$(cat "$work/out")"; fi
if [ "$(git -C "$AGENT_BOX_CHECKOUT_DIR" remote get-url origin)" = "$upstream" ]; then
  ok "origin is not renamed"
else no "origin is not renamed" "$(git -C "$AGENT_BOX_CHECKOUT_DIR" remote -v)"; fi
if grep -F -- "--remote-name fork" "$work/gh.log" > /dev/null; then
  ok "gh repo fork is asked for the name that leaves origin alone"
else no "gh repo fork is asked for the name that leaves origin alone" "$(cat "$work/gh.log")"; fi

# Already there: no second call to gh.
: > "$work/gh.log"
run
if [ ! -s "$work/gh.log" ]; then ok "an existing fork remote calls gh again for nothing"
else no "an existing fork remote calls gh again for nothing" "$(cat "$work/gh.log")"; fi

# A fork that fails (own repo, missing scope, rate limit) keeps the tree.
fresh "$AGENT_BOX_CHECKOUT_DIR" "$REV_TWO"
export GH_SHIM_FORK_FAILS=1
if run && [ -e "$AGENT_BOX_CHECKOUT_DIR/file" ]; then
  ok "a failed fork leaves a usable checkout"
else no "a failed fork leaves a usable checkout" "$(cat "$work/out")"; fi

printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo "all checks passed" || echo "$fails failed")"
[ "$fails" -eq 0 ]
