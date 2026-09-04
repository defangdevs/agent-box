# Harness resolution and lazy installation (issue #416).
#
# Sourced by the supervisor, and unit-tested directly by
# tests/test-jit-agents.sh — which is the reason it is a lib rather than a
# run of straight-line code in supervisor.sh: everything here is decided by
# what is on disk and what the network does, and neither is reachable from a
# VM test that has to stay offline.
#
# From the environment: $HOME and $AGENT_BOX_AGENT_BINS always;
# $AGENT_BOX_NIXPKGS and $AGENT_BOX_FLOCK_BIN for installs.
# $AGENT_BOX_NIX_BIN is optional — see agent_install.

# Where the lazy-harness machinery (issue #416) keeps its bookkeeping: a
# per-harness cooldown marker so a failing install cannot become one network
# call per respawn, and a lock so two sessions starting at once do not both
# pay for the same download. Under ~/.local/state like the session side
# files, so a reboot keeps them and a fresh $HOME starts clean.
#
# Plain locals, NOT AGENT_BOX_* names: neither backend supplies these and
# neither should, but scripts/check_backend_parity.py reads any assigned
# AGENT_BOX_<NAME>= as a host contract one side was missing. Tests point
# them somewhere scratch by setting $HOME, which is the only input they
# have.
_jit_dir="$HOME/.local/state/agent-box/jit"
_jit_lock="$_jit_dir/install.lock"

agent_bin() {
  # agent_bin NAME — resolve an agent (or "shell") to its binary via
  # the unit's AGENT_BOX_AGENT_BINS ("name=/path ..." pairs; store and
  # shell paths never contain whitespace, so word-splitting is safe).
  #
  # A JIT-installed harness (issue #416) is NOT in that table and never
  # can be: the table is built at EVAL time from installAgents, while a
  # lazily installed CLI lands in the user's own profile long after the
  # system closure was fixed. ~/.nix-profile/bin is FIRST on the session
  # PATH, so resolving it here finds exactly the binary a pane would —
  # and, because that directory is already on PATH, an install performed
  # now is visible to a pane that is ALREADY running.
  #
  # The table still wins when it has an entry, so an eagerly installed
  # harness keeps its pinned store path and nothing about a conventional
  # box changes — but only if that path is really there. The native
  # backend builds this table from a fixed layout ("<profile>/bin/claude")
  # rather than from resolved store paths, so an entry can name a harness
  # the runtime profile was built without; returning it would exec into
  # nothing AND shadow the profile copy a JIT install just put down.
  for pair in ${AGENT_BOX_AGENT_BINS:?}; do
    case "$pair" in
      ("$1"=*)
        _ab_path=${pair#*=}
        # Falling through is only ever useful for something a profile
        # could supply instead. "shell" is the login shell and has no
        # second source, so it resolves from the table unconditionally,
        # exactly as it always did — a missing one must fail at exec with
        # the path in the message, not vanish into "not installed".
        if [ -x "$_ab_path" ] || ! agent_attr "$1" >/dev/null 2>&1; then
          printf '%s\n' "$_ab_path"
          return 0
        fi
        break
        ;;
    esac
  done
  # Only ever a harness name we know; never an arbitrary string off the
  # registry turned into a path.
  agent_attr "$1" >/dev/null 2>&1 || return 1
  if [ -x "$HOME/.nix-profile/bin/$1" ]; then
    printf '%s\n' "$HOME/.nix-profile/bin/$1"
    return 0
  fi
  return 1
}

agent_attr() {
  # agent_attr NAME — the nixpkgs attribute a harness installs from.
  # Not derivable from the binary name (claude's is claude-code), and
  # deliberately a closed set: this is the only place a session's
  # registry data is allowed to name something to install.
  case "$1" in
    (claude) printf 'claude-code\n' ;;
    (codex)  printf 'codex\n' ;;
    (*) return 1 ;;
  esac
}

agent_install() {
  # agent_install NAME — fetch a harness into the user's own profile
  # (issue #416).
  #
  # Lazy harnesses are what let the box ship without ~1 GB of agent CLI
  # in its closure: nothing is installed until a session actually asks
  # for one. The cost is paid once, at first use, and the result is a
  # normal profile generation — a GC root, rollback-able, and upgraded
  # with `nix profile upgrade` instead of a system rebuild.
  _ai_agent="$1"
  _ai_attr="$(agent_attr "$_ai_agent")" || return 1
  if [ -z "${AGENT_BOX_NIXPKGS:-}" ]; then
    echo "session: '$_ai_agent' is not installed and cannot be fetched" \
         "(no AGENT_BOX_NIXPKGS in this unit)" >&2
    return 1
  fi
  # The NixOS module pins a store path; a native box cannot, because
  # resolving one at apply time would bake that host's nix into a generated
  # file and break on the next upgrade. So resolve here, at use, and cover
  # both install layouts: multi-user Nix puts nix in the default profile,
  # which is NOT on the native session PATH, while single-user puts it in
  # the user profile, which is.
  _ai_nix="${AGENT_BOX_NIX_BIN:-}"
  if [ -z "$_ai_nix" ]; then
    for _ai_cand in \
        "$(command -v nix 2>/dev/null || true)" \
        /nix/var/nix/profiles/default/bin/nix \
        "$HOME/.nix-profile/bin/nix"; do
      if [ -n "$_ai_cand" ] && [ -x "$_ai_cand" ]; then _ai_nix=$_ai_cand; break; fi
    done
  fi
  if [ -z "$_ai_nix" ]; then
    echo "session: '$_ai_agent' is not installed and cannot be fetched" \
         "(no nix on this box)" >&2
    return 1
  fi

  # One attempt per retry window, for the same reason seed_claude_state
  # rate-limits itself: a session that cannot start is respawned every
  # couple of seconds, so an install that fails offline must not become a
  # network call per respawn.
  mkdir -p "$_jit_dir"
  _ai_marker="$_jit_dir/$_ai_agent.failed"
  _ai_now="$(date +%s)"
  _ai_retry="${AGENT_BOX_JIT_RETRY_S:-300}"
  if [ -s "$_ai_marker" ]; then
    read -r _ai_ts _ai_rest < "$_ai_marker" || true
    case "${_ai_ts:-x}" in (""|*[!0-9]*) _ai_ts=0 ;; esac
    if [ $((_ai_now - _ai_ts)) -lt "$_ai_retry" ]; then
      echo "session: '$_ai_agent' install failed $((_ai_now - _ai_ts))s ago," \
           "not retrying for another $((_ai_retry - _ai_now + _ai_ts))s" >&2
      return 1
    fi
  fi

  # Serialize installs across this user's sessions. `nix profile` takes
  # its own profile lock, but two sessions racing here would still both
  # pay the download; worse, the loser's error is indistinguishable from
  # a real failure and would set the cooldown marker above.
  #
  # BOUNDED, not a plain flock: the supervisor's reconcile loop calls this
  # from start_session, so waiting forever on a wedged install would stop
  # it starting or reaping every OTHER session too. Generous enough for a
  # real download over a slow link, and giving up just means this pass
  # skips the session and the next one retries.
  mkdir -p "$(dirname "$_jit_lock")"
  exec 8>>"$_jit_lock"
  if ! "${AGENT_BOX_FLOCK_BIN:?}" -w "${AGENT_BOX_JIT_LOCK_WAIT_S:-900}" 8; then
    exec 8>&-
    echo "session: another session is still fetching a harness; leaving" \
         "'$_ai_agent' for the next pass" >&2
    return 1
  fi
  # The winner of the race installed it while we waited; nothing to do.
  if [ -x "$HOME/.nix-profile/bin/$_ai_agent" ]; then
    exec 8>&-
    return 0
  fi

  echo "session: fetching '$_ai_agent' ($_ai_attr) from the box's pinned" \
       "nixpkgs — first use of this harness, so this can take a few minutes" >&2
  # --impure + NIXPKGS_ALLOW_UNFREE: claude-code is unfree, and a profile
  # install gets none of the module's allowUnfreePredicate (that governs
  # the module's OWN second nixpkgs import, not this user's profile).
  # AGENT_BOX_NIXPKGS is the SAME pinned channel the module resolves the
  # eager harnesses from, so a box that also ships one gets a byte-
  # identical store path here rather than a second copy.
  #
  # BOUNDED, same reason as the flock above: this runs inside the
  # supervisor's reconcile loop, so a wedged fetch (a stalled substituter,
  # a hung download) must not stop every OTHER session from starting.
  # Giving up sets the cooldown marker below and the next pass retries.
  if NIXPKGS_ALLOW_UNFREE=1 timeout "${AGENT_BOX_JIT_INSTALL_TIMEOUT_S:-1800}" \
       "$_ai_nix" profile add --impure \
       "$AGENT_BOX_NIXPKGS#$_ai_attr" >&2; then
    rm -f "$_ai_marker"
    exec 8>&-
    # No mirror_codex_standalone call here: start_session mirrors the
    # binary it is about to launch, which on this path is the one just
    # installed, on this same reconcile pass (issue #572).
    return 0
  fi
  # Stamp the marker at WRITE time, not with the pre-attempt $_ai_now: the
  # flock wait and the install itself can together run long past
  # AGENT_BOX_JIT_RETRY_S, and a marker backdated to before the attempt
  # would already read as expired on the very next reconcile pass —
  # defeating the cooldown it exists to enforce.
  printf '%s\n' "$(date +%s)" > "$_ai_marker"
  exec 8>&-
  echo "session: could not fetch '$_ai_agent' — is the box offline?" >&2
  return 1
}

# Codex remote-control pairing currently requires the standalone
# installer layout at ~/.codex/packages/standalone/current/codex.
# Mirror that fixed path to the provided Codex so pairing works
# without a curl-installed second copy.
#
# CALLED AT SESSION START, from start_session's codex branch, and nowhere
# else (issue #572). Seeding it at INSTALL time instead needs a hook on
# every path that can put a codex on this box, and there are more of them
# than there were when this began: the eager closure, the JIT fetch
# (issue #416), the settings page's Connections card — which grew its own
# `nix profile add` and mirrored nothing, so every session on a default
# box died with "managed standalone Codex install not found" — and a
# user's own `nix profile add nixpkgs#codex`, which the platform guide
# actively suggests and which no hook of ours can ever observe. Session
# start is the one place that sees the binary about to run whatever put it
# there, and this is two ln(1)s on an idempotent path, so re-running it
# per launch costs nothing worth measuring.
#
# $1 is the codex binary to mirror (start_session passes the resolved,
# already-JIT-installed one). A bare call resolves it like agent_bin does
# and no-ops when there is no codex to point at.
mirror_codex_standalone() {
  if [ -n "${1:-}" ]; then
    cbin=$1
  else
    cbin="$(agent_bin codex)" || return 0
  fi
  mkdir -p "$HOME"/.codex/packages/standalone/agent-box-current
  ln -sfn "$cbin" "$HOME"/.codex/packages/standalone/agent-box-current/codex
  # `ln -sfn` only replaces `current` when it is already a symlink or a
  # plain file (issue #95). If a curl-installed Codex ever leaves `current`
  # as a REAL directory — an unusual manual layout, but a possible one —
  # `-sfn` can't unlink a non-empty directory, so it silently creates
  # `current/agent-box-current` INSIDE it instead of replacing it, and
  # remote-control pairing keeps resolving the stale copy underneath.
  #
  # Clear a real directory first (the `-L` check excludes a
  # symlink-to-a-directory, which `-sfn` already replaces correctly, so
  # this only ever fires on the broken layout). `rename()` cannot swap a
  # symlink over a non-empty directory in one step, so this branch is not
  # atomic: a session reading `current` between the `rm -rf` and the `mv`
  # below sees it briefly missing. That is an acceptable one-time cost to
  # repair the broken layout, and every run after this one takes the
  # atomic path below, because `current` is a symlink from here on.
  #
  # Build the new symlink under a temp name in the same directory and
  # rename it over `current` — `rename()` is atomic, so a session reading
  # `current` mid-update here sees either the old or the new target, never
  # a missing path (except immediately following the repair above).
  _mcs_current="$HOME/.codex/packages/standalone/current"
  if [ -d "$_mcs_current" ] && [ ! -L "$_mcs_current" ]; then
    rm -rf "$_mcs_current"
  fi
  _mcs_tmp="$_mcs_current.tmp.$$"
  rm -f "$_mcs_tmp"
  ln -sfn agent-box-current "$_mcs_tmp"
  mv -T "$_mcs_tmp" "$_mcs_current"
}
