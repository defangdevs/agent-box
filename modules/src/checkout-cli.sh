# agent-box-checkout — put the box's OWN sources on the box (issue #242).
#
# A deployed box fetches exactly one file, the generated
# modules/agent-box.nix, by rev + sha256 (the single-file contract, issue
# #51). It never saw the sources it is built from, so an agent asked "why
# does my box do X" had nothing to read and answered from a model of
# agent-box instead of from this box's agent-box. PR #293 settled that the
# box ships the source with each operator; this is the step that puts it
# there.
#
# What it does, in order, each step idempotent and independently skippable:
#
#   1. clone $AGENT_BOX_CHECKOUT_URL into $AGENT_BOX_CHECKOUT_DIR,
#   2. park it on $AGENT_BOX_CHECKOUT_REV — the rev this box is RUNNING,
#      not the remote's head, because a tree that describes a different box
#      is worse than no tree (a wrong answer given confidently),
#   3. add a `fork` remote, when a token allows, so an agent can push work
#      and open a PR without write access to upstream.
#
# It is NOT a sync. Step 2 fetches only to reach a rev the box already runs,
# and only from the state this script itself leaves behind — detached HEAD,
# clean tree. Anything else (a branch checked out, a modified file, a
# rebase in progress) is somebody's work, and the tree is left exactly as
# found with a line in the journal saying so. Nothing here ever decides what
# the box RUNS: that is still the root updater fetching a hash-pinned file
# from the deploy-time selfUpdate.repo, and no agent-writable path reaches
# it (issue #127 — a local tree as the build source would make the agent
# root-equivalent).
#
# The supervisor runs this in the background at every start, through the
# env-exec wrapper so the fork step sees the user's GH_TOKEN. Re-running it
# by hand is the supported repair for "my checkout is gone".
set -u

prog=agent-box-checkout
say() { printf '%s: %s\n' "$prog" "$*" >&2; }

dir=${AGENT_BOX_CHECKOUT_DIR:-}
if [ -z "$dir" ]; then
  # Not the maintainer, checkout disabled, or a backend that does not
  # render this yet (the native one — issue #242 decision 5(a), declared in
  # scripts/check_backend_parity.py). Silent-ish and successful: the
  # supervisor calls this unconditionally.
  say "no checkout is configured for this user — nothing to do"
  exit 0
fi

case "$dir" in
  (/*) ;;
  (*) say "AGENT_BOX_CHECKOUT_DIR must be absolute, got '$dir'"; exit 1 ;;
esac

url=${AGENT_BOX_CHECKOUT_URL:-}
rev=${AGENT_BOX_CHECKOUT_REV:-}
if [ -z "$url" ] || [ -z "$rev" ]; then
  say "AGENT_BOX_CHECKOUT_URL/_REV are unset — refusing to guess where $dir comes from"
  exit 1
fi

git() { command git -C "$dir" "$@"; }
rc=0

# This runs unattended, in the background, at boot. Two ways a network fetch
# can stop being a background job and become a wedged one:
#
#   - a credential prompt. Cloning a repo the box's token cannot read makes
#     git ask for a username, and there is nobody to answer — so it must
#     fail instead of waiting. GIT_TERMINAL_PROMPT=0 is the documented lever
#     and the askpass helpers are cleared because they are the other way a
#     prompt can appear.
#   - a transfer that stalls rather than fails. A half-open connection has no
#     timeout of its own, so the low-speed guard supplies one: under 1 kB/s
#     for a minute is a dead transfer, and the next supervisor start retries
#     it for free.
export GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS
export GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60

# --- 1. the tree ------------------------------------------------------
if [ ! -e "$dir/.git" ]; then
  # Clone into a SIBLING and rename, so $dir either does not exist or is a
  # finished tree — never something in between.
  #
  # git writes $dir/.git within the first moments of a clone and cleans it
  # up only when the clone itself fails. A signal is not a failure: this
  # runs in the background, in the agent unit's cgroup, which systemd kills
  # on every stop, restart and reboot. Cloned straight into $dir, one
  # unlucky reboot would leave a .git with no HEAD — and every later run
  # would skip the clone (the test above), fail to read HEAD, and refuse to
  # touch the tree. That is a checkout no boot can repair, on the exact code
  # path a first boot takes.
  #
  # A fixed sibling name rather than mktemp: an interrupted run leaves
  # exactly one of these, and the next one reclaims it instead of leaving a
  # new partial clone behind on every reboot. Nothing but this script writes
  # that path — $dir comes from the module, not from anything at runtime,
  # and was required to be absolute above.
  incoming="$dir.incoming"
  rm -rf "$incoming"
  say "cloning $url into $dir"
  # Not `git()`: there is no repo to be -C inside of yet, and what there is
  # is not at $dir.
  if ! command git clone --quiet "$url" "$incoming"; then
    say "clone failed (offline, or the repo is private to this box's token) — re-run me later"
    rm -rf "$incoming"
    exit 1
  fi
  # Detach HERE rather than falling through to step 2: a fresh clone is on
  # the remote's default BRANCH, and step 2 refuses to move a tree that is
  # on a branch (it cannot tell one somebody adopted from one git just
  # made). Without this the very first run would leave the tree on master —
  # describing whatever upstream had merged since this box was built, which
  # is the confidently-wrong answer the whole feature exists to prevent.
  if ! command git -C "$incoming" checkout --quiet --detach "$rev"; then
    say "cloned, but $rev is not in $url — discarding the partial tree"
    rm -rf "$incoming"
    exit 1
  fi
  # The publish. A rename within one directory is atomic, which is what
  # makes the paragraph above true.
  if mv "$incoming" "$dir"; then
    say "cloned and parked on $rev"
  else
    say "could not move $incoming into place — leaving it for the next run"
    exit 1
  fi
fi

# --- 2. park it on the rev this box runs ------------------------------
head=$(git rev-parse HEAD 2>/dev/null) || head=""
if [ -z "$head" ]; then
  say "$dir has no HEAD to read — leaving it alone"
  exit 1
elif [ "$head" = "$rev" ]; then
  say "$dir is at $rev, the rev this box runs"
elif [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" != "HEAD" ]; then
  # A branch is checked out, so somebody adopted this tree as a working
  # copy. Realigning it would move their branch pointer.
  say "$dir is on a branch, not the detached rev this script leaves — not touching it" \
      "(box runs $rev)"
elif [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  say "$dir has uncommitted changes — not touching it (box runs $rev)"
else
  # Clean and detached: the state the bootstrap leaves. Moving it to the
  # rev the box ALREADY runs changes nothing about what the box runs; it
  # only stops the tree from describing a different box than this one.
  say "$dir is at $head but the box runs $rev — realigning"
  if git fetch --quiet origin && git checkout --quiet --detach "$rev"; then
    say "realigned to $rev"
  else
    say "could not realign (offline, or $rev is not on origin) — the tree still reads $head"
    rc=1
  fi
fi

# --- 3. the fork remote -----------------------------------------------
# Only when the deployment asked for one. Creating a repository in the
# operator's GitHub account is an outward-facing act, so it is an option
# (selfUpdate.checkout.fork) and not a side effect of having a checkout.
if [ -z "${AGENT_BOX_CHECKOUT_FORK:-}" ]; then
  exit "$rc"
fi
if git remote get-url fork >/dev/null 2>&1; then
  exit "$rc"
fi
if ! command -v gh >/dev/null 2>&1; then
  say "gh is not on PATH — no fork remote added"
  exit "$rc"
fi
if ! gh auth status >/dev/null 2>&1; then
  # The usual first-boot case: the box has a checkout before it has a
  # token. Nothing is lost — the next supervisor start runs this again,
  # and by then `agent-box-session env set GH_TOKEN ...` has been done.
  say "gh is not authenticated (no GH_TOKEN yet?) — no fork remote added; re-run me once there is one"
  exit "$rc"
fi
say "creating the fork remote"
# --remote-name fork, deliberately: with the default name gh renames the
# existing origin to `upstream`, and origin is what AGENT_BOX_CHECKOUT_URL
# and the rev above are about. Fork work is `git push fork ...`; origin
# stays the thing this box is built from.
if (cd "$dir" && command gh repo fork --clone=false --remote --remote-name fork >/dev/null 2>&1); then
  say "fork remote added: $(git remote get-url fork 2>/dev/null)"
else
  # Forking your own repo is an error, so is a token without repo scope,
  # so is a rate limit. None of them are worth failing a boot over.
  say "gh repo fork did not add a remote (own repo, missing scope, or rate limit) — checkout is still usable"
fi
exit "$rc"
