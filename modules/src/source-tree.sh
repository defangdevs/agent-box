# agent-box-source — the tree the box is BUILT FROM (issue #242).
#
# Until this existed, "update the box" meant asking GitHub's API for a rev
# and fetching a COPY of one generated file by rev + sha256 (the single-file
# contract, issue #51). The box never held the sources it runs, so the round
# trip an agent needs to fix its own box — read, edit, rebuild — had no
# local end. Issue #242 asks for the other shape: "update box should be
# `git pull` with the box using the checked out files (no copies)".
#
# This is that tree. It is ROOT-owned and lives outside every agent's home,
# because whoever can write the tree decides what root builds: a rebuild
# from an agent-writable path is root-equivalence for that user (issue
# #127's users-are-the-trust-boundary doctrine). The maintainer's own
# ~/agent-box checkout (agent-box-checkout, same issue) is a different tree
# for a different job — that one is the agent's working copy, and a fix
# reaches this tree the way any other change does: through the repo.
#
# Three verbs, all idempotent, all env-configured:
#
#   check   fetch and print the rev the target ref is at. Touches nothing.
#   pull    fast-forward the tree to that rev and print the new HEAD.
#   reset   move the tree back to a named rev (what a failed rebuild does).
#   rev     print the tree's HEAD.
#
# The fast-forward guard is `git merge --ff-only` itself, which is stricter
# than the API compare it replaces and needs no second opinion from a
# server: a target that is not a descendant of the running rev means
# upstream history was rewritten or an older, possibly vulnerable rev is
# being replayed, and both are refused by construction.
set -u

prog=agent-box-source
say() { printf '%s: %s\n' "$prog" "$*" >&2; }
die() { say "$@"; exit 1; }

dir=${AGENT_BOX_SRC_DIR:-}
url=${AGENT_BOX_SRC_URL:-}
rev=${AGENT_BOX_SRC_REV:-}
branch=${AGENT_BOX_SRC_BRANCH:-}
# An explicit target ref (a tag, a branch, a commit) instead of the tracked
# branch's head — `agentbox update --rev`. It is fetched by name and still
# goes through the fast-forward guard, so "update to this tag" refuses a tag
# that is behind the running rev exactly as following the branch would.
ref=${AGENT_BOX_SRC_REF:-}
# The one documented way past that guard: a deliberate downgrade
# (`agentbox update --force`). Non-empty means the target replaces HEAD even
# when it is not a descendant.
force=${AGENT_BOX_SRC_FORCE:-}
# Where agent-box-candidate leaves the branch it wants tried on this box
# before it is merged. Read by `pull` and CONSUMED there — it is intent, not
# state: what this box runs is the tree's own HEAD, and a marker that
# outlived its update would be a second answer able to disagree with it.
# Empty (no such wiring) is a box with the feature off, which behaves
# exactly as it did before it existed.
candidate_file=${AGENT_BOX_CANDIDATE_FILE:-}

[ -n "$dir" ] || die "AGENT_BOX_SRC_DIR is unset — there is no tree to act on"
case "$dir" in
  (/*) ;;
  (*) die "AGENT_BOX_SRC_DIR must be absolute, got '$dir'" ;;
esac
[ -n "$url" ] || die "AGENT_BOX_SRC_URL is unset — refusing to guess where $dir comes from"

# Root-owned and world-readable on purpose: `nix` evaluates the module out
# of this tree, and the settings page and an agent both want to be able to
# read what the box is running. Nothing but root writes it.
umask 022

# This runs unattended from a root oneshot. A credential prompt has nobody
# to answer it and a stalled transfer has no timeout of its own, so both are
# turned into failures the next trigger can retry — the same guards
# agent-box-checkout takes, for the same reason.
export GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS
export GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60

# Every git below runs AS ROOT in a tree fetched from the network, and
# `checkout`/`merge` are exactly the commands git runs hooks for. The module
# already confines the tree to /var/lib so nothing but root can put a hook
# there; this is the second lock, and it costs one environment variable:
# core.hooksPath at a path that holds no hooks means no hook is ever found.
# GIT_CONFIG_* rather than `git -c`, so it covers `git clone` too — which
# runs before the tree this is protecting even exists.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null

git() { command git -C "$dir" "$@"; }

# --- the tree exists --------------------------------------------------
clone_if_missing() {
  [ -e "$dir/.git" ] && return 0
  # Clone into a SIBLING and rename, so $dir is either absent or a finished
  # tree and never something in between: git writes .git within the first
  # moments of a clone and cleans it up only when the clone FAILS. A signal
  # is not a failure, and this runs where a reboot can arrive — cloned
  # straight into $dir, one unlucky reboot leaves a .git with no HEAD that
  # every later run would find, skip the clone for, and refuse to touch.
  # A fixed sibling name rather than mktemp: an interrupted run leaves
  # exactly one of these and the next one reclaims it.
  incoming="$dir.incoming"
  rm -rf "$incoming"
  say "cloning $url into $dir"
  mkdir -p "$(dirname "$dir")" || die "cannot create $(dirname "$dir")"
  # $dir exists but holds no .git — a tmpfiles rule that created the
  # directory, or an interrupted run that left something behind. `mv` into an
  # EXISTING directory moves the source INSIDE it, so the publish below would
  # silently produce $dir/<basename>.incoming instead of $dir: a nested
  # checkout, a $dir that still has no .git, and a clone attempted again on
  # every later run. Reclaim it when it is empty, and refuse loudly when it
  # is not, rather than nesting.
  if [ -e "$dir" ]; then
    rmdir "$dir" 2>/dev/null || true
    [ -e "$dir" ] && die "$dir exists and is not a checkout — move it aside"
  fi
  command git clone --quiet "$url" "$incoming" \
    || { rm -rf "$incoming"; die "clone of $url failed — offline, or the repo is not public"; }
  mv "$incoming" "$dir" || { rm -rf "$incoming"; die "could not move $incoming into place"; }
  say "cloned"
}

# The branch to track. Named explicitly by the host, or read from the
# remote's own default — never guessed as "master", because a repo that
# renamed its default branch would then be silently un-updatable.
resolve_branch() {
  [ -n "$branch" ] && return 0
  branch=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null) || branch=""
  branch=${branch#origin/}
  if [ -z "$branch" ]; then
    git remote set-head origin --auto >/dev/null 2>&1 || true
    branch=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null) || branch=""
    branch=${branch#origin/}
  fi
  [ -n "$branch" ] || die "cannot tell which branch origin defaults to — set AGENT_BOX_SRC_BRANCH"
}

fetch() {
  # The configured URL wins over the one the tree was cloned from. A host
  # that repoints selfUpdate.repo (a fork, a mirror, a rename) would
  # otherwise keep fetching the old origin forever, and the box would look
  # up to date against a repo nobody meant it to follow.
  have=$(git remote get-url origin 2>/dev/null) || have=""
  if [ "$have" != "$url" ]; then
    say "origin is $have, but this box is configured for $url — repointing"
    git remote set-url origin "$url" || die "could not repoint origin at $url"
  fi
  git fetch --quiet --prune origin || die "fetch from $url failed — offline?"
}

# What rev does the remote have for a ref, asked WITHOUT a tree — no clone,
# no fetch, no .git touched. This is what makes `check` a question: "is there
# an update" must not be the thing that creates the checkout, and on a box
# that has never updated the tree does not exist yet, so the alternative was
# cloning the whole repo to answer.
#
# ls-remote prints "<sha>\t<ref>" per matching ref. An annotated tag matches
# twice — the tag object, and the commit it peels to as "<ref>^{}" — and the
# peeled line is the one a caller means by "the rev of v1.2.3".
remote_rev() {
  out=$(command git ls-remote "$url" "$@" 2>/dev/null) \
    || die "cannot reach $url — offline?"
  [ -n "$out" ] || return 1
  peeled=$(printf '%s\n' "$out" | grep '\^{}$' | head -n 1 | cut -f1)
  if [ -n "$peeled" ]; then
    printf '%s\n' "$peeled"
  else
    printf '%s\n' "$out" | head -n 1 | cut -f1
  fi
}

# What are we aiming at? Either a named ref, fetched by name, or the head of
# the tracked branch. Both end as a bare rev, so everything downstream is
# ref-shaped in exactly one place.
target_rev() {
  if [ -n "$ref" ]; then
    fetch
    git fetch --quiet origin "$ref" || die "origin has no ref '$ref'"
    git rev-parse --verify --quiet 'FETCH_HEAD^{commit}' \
      || die "'$ref' does not name a commit"
  else
    fetch
    resolve_branch
    git rev-parse --verify --quiet "refs/remotes/origin/$branch^{commit}" \
      || die "origin has no branch '$branch'"
  fi
}

case "${1:-}" in
  (check)
    # Deliberately NOT clone_if_missing/target_rev: see remote_rev above.
    # With no branch configured the remote's own HEAD is the tracked branch,
    # which is the same answer resolve_branch reaches through origin/HEAD.
    if [ -n "$ref" ]; then
      remote_rev "$ref" || die "origin has no ref '$ref'"
    elif [ -n "$branch" ]; then
      remote_rev "refs/heads/$branch" || die "origin has no branch '$branch'"
    else
      remote_rev HEAD || die "$url has no HEAD"
    fi
    ;;

  (pull)
    [ -n "$rev" ] || die "AGENT_BOX_SRC_REV is unset — refusing to move a tree with no baseline"
    # A queued candidate (agent-box-candidate BRANCH) is the target for
    # exactly one update. Consumed BEFORE anything can fail, so a candidate
    # that does not build is not retried on every later trigger — the next
    # one goes back to the tracked branch, which is the whole of the
    # "nothing here pins this box" promise. An explicit --rev wins: an
    # operator naming a ref is not asking about the queue.
    # Whether the TARGET was named by the caller, which decides below
    # whether the fast-forward guard may relax itself. Recorded before the
    # queue can overwrite $ref.
    explicit_ref=$ref
    # The candidate marker carries two fields, and the difference between
    # them is the difference between a request and a fact:
    #
    #   want=REF  agent-box-candidate asks for REF on the next update.
    #   on=REV    this box IS running the candidate at REV.
    #
    # `on` is checked against the rev the box actually runs before it is
    # believed, and that check is what makes it safe to relax the
    # fast-forward guard. "The running rev is not an ancestor of the tracked
    # branch" is NOT a usable signal on its own: a squash-merged candidate
    # and a rewritten upstream history look identical from here, and the
    # second is the exact attack --ff-only exists to refuse. So a stale `on`
    # (a rebuild that failed and rolled the tree back, an operator moving
    # the tree by hand) is ignored rather than trusted, and the strict guard
    # comes straight back.
    candidate=""
    on_rev=""
    if [ -n "$candidate_file" ] && [ -e "$candidate_file" ]; then
      on_rev=$(sed -n 's/^on=//p' "$candidate_file" 2>/dev/null | head -n 1)
      if [ -z "$ref" ]; then
        candidate=$(sed -n 's/^want=//p' "$candidate_file" 2>/dev/null | head -n 1)
      fi
      if [ -n "$candidate" ]; then
        # Consumed BEFORE anything can fail, so a candidate that does not
        # build is not reinstalled by every later trigger. What survives is
        # only the `on` fact, which the next run re-verifies.
        if [ -n "$on_rev" ]; then
          printf 'on=%s\n' "$on_rev" > "$candidate_file.tmp" \
            && mv "$candidate_file.tmp" "$candidate_file"
        else
          rm -f "$candidate_file"
        fi
        say "installing candidate $candidate (queued on this box, not merged)"
        ref=$candidate
      fi
    fi
    if [ -n "$on_rev" ] && [ "$on_rev" != "$rev" ]; then
      say "candidate marker names $on_rev but the box runs $rev — ignoring it"
      on_rev=""
    fi
    clone_if_missing
    target=$(target_rev) || exit 1
    resolve_branch
    head=$(git rev-parse HEAD 2>/dev/null) || die "$dir has no HEAD to read"
    # Realign to the rev the box RUNS before fast-forwarding. Normally these
    # are already equal — nothing but this script writes the tree. When they
    # are not (a fresh clone landed on the branch tip, an operator reset it,
    # a rebuild rolled the system back but not the tree), the running rev is
    # the truth and the tree is the copy: moving the tree changes what the
    # box will BUILD, never what it currently runs, and starting the
    # fast-forward anywhere else would let the guard below measure ancestry
    # from a rev this box never ran.
    if [ "$head" != "$rev" ] || [ "$(git rev-parse --abbrev-ref HEAD)" = "HEAD" ]; then
      git cat-file -e "$rev^{commit}" 2>/dev/null \
        || die "the running rev $rev is not in $url — this tree cannot describe this box"
      [ "$head" = "$rev" ] || say "$dir is at $head but the box runs $rev — realigning before the fast-forward"
      # -B, not a detached checkout: `git merge` advances a BRANCH, and a
      # tree left detached (a fresh clone, an operator's inspection) has
      # nothing for it to advance.
      git checkout --quiet -B "$branch" "$rev" \
        || die "could not put $dir on $branch at $rev"
    fi
    # A candidate must be strictly AHEAD of the tracked branch, and this is
    # where that is settled: the fetch above already brought both objects
    # in, so it costs one ancestry query, and it holds however the marker
    # came to be written. It is what keeps "candidate" from becoming a way
    # to replay an older, possibly vulnerable rev — the exact thing
    # --ff-only refuses and the exact thing the force below would otherwise
    # reopen, since a box already off the branch takes that force path.
    if [ -n "$candidate" ]; then
      if ! git merge-base --is-ancestor "refs/remotes/origin/$branch" \
           "$target" 2>/dev/null; then
        die "refusing candidate $candidate: $target is not ahead of $branch — rebase it, so what is tested here is what will land"
      fi
    fi
    # Coming BACK from a candidate needs the guard relaxed, and only that.
    # A candidate is squash-merged, so its head is never an ancestor of the
    # tracked branch: once this box has taken one, --ff-only refuses the way
    # home and the box would be stuck off-branch — the exact permanent
    # divergence the feature promises not to create.
    #
    # Narrow on purpose. It needs the marker to say this box is running a
    # candidate AND that claim to match the rev it actually runs (checked
    # above), so a rewritten upstream history — which looks the same from
    # the ancestry side — is still refused. The target is then either the
    # tracked branch's own head or a branch agent-box-candidate already
    # proved is ahead of it, so neither can be a downgrade. An explicit
    # --rev keeps the strict guard and still has --force of its own.
    if [ -z "$force" ] && [ -z "$explicit_ref" ] && [ -n "$on_rev" ]; then
      say "the box runs candidate $rev — moving to $target without the fast-forward check"
      force=1
    fi
    if [ "$target" = "$rev" ]; then
      : # nothing to move to; fall through and print the rev we are on
    elif [ -z "$force" ] && git merge-base --is-ancestor "$target" "$rev"; then
      # `git merge --ff-only <ancestor>` reports "Already up to date" and
      # exits 0, so without this an explicit downgrade target (`--rev` at an
      # older tag) would silently do nothing and be reported as a successful
      # update. The old GitHub compare refused this as status "behind"; so
      # does this.
      die "refusing update: $target is behind the running rev $rev"
    fi
    if [ -n "$force" ]; then
      # The deliberate downgrade. Named, logged, and never the default.
      say "moving to $target without the fast-forward check (force)"
      git checkout --quiet -B "$branch" "$target" || die "could not move $dir to $target"
    elif ! git merge --ff-only --quiet "$target" 2>/dev/null; then
      # THE guard. --ff-only refuses anything that is not a descendant of
      # the running rev: a force-push, a rewritten history, a replay of an
      # older rev. It is also the whole of the merge — the tree carries no
      # local commits to reconcile, and a tree that somehow does is a tree
      # nothing here should be resolving conflicts in.
      die "refusing update: $target is not a fast-forward of the running rev $rev"
    fi
    # Record what the box is now running, so the NEXT plain update knows it
    # has a way home to relax the guard for — and stop recording it once it
    # is home, so nothing is left pinned or forced afterwards.
    if [ -n "$candidate_file" ]; then
      if [ -n "$candidate" ]; then
        printf 'on=%s\n' "$(git rev-parse HEAD)" > "$candidate_file.tmp" \
          && mv "$candidate_file.tmp" "$candidate_file"
      elif [ -n "$on_rev" ]; then
        rm -f "$candidate_file"
      fi
    fi
    git rev-parse HEAD
    ;;

  (reset)
    to=${2:-}
    [ -n "$to" ] || die "reset needs a rev"
    [ -e "$dir/.git" ] || die "$dir is not a checkout — nothing to reset"
    resolve_branch
    git checkout --quiet -B "$branch" "$to" \
      || die "could not move $dir back to $to"
    say "$dir is back at $to"
    ;;

  (rev)
    git rev-parse --verify --quiet HEAD || die "$dir has no HEAD to read"
    ;;

  (*)
    die "usage: $prog check|pull|reset REV|rev"
    ;;
esac
