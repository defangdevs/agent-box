# The session registry's write protocol, spelled once (issue #254).
#
# ~/.config/agent-box/sessions.json is INTENT: what the operator asked this box
# to run — name, agent, working directory, prompts, stopped. FIVE programs
# write it (the session CLI, the supervisor's reconcile loop, the mark-stopped
# pane epilogue, the webhook spawn wrapper, the settings daemon's three
# routes), and every one of them replaces the file by rename. That buys exactly
# ONE guarantee: a reader never sees half a document. It says nothing about the
# interval between a writer's read and its rename, so two writers that start
# from the same base each publish a document that never contained the other's
# edit, and a one-field update silently reverts every field the other writer
# changed. Measured on the live box: two writers of the registry_edit shape
# below lost 96 of 300 updates (32%).
#
# So the four SHELL writers splice this file in (the assembler resolves nested
# includes) rather than carrying a copy each. What they have to agree on is
# small — the sidecar path, the primitive, the bound, who may skip the lock —
# and a copy per program is how those four facts drift apart. A lock only some
# writers take is not a lock.
#
# The fifth writer is python: the settings daemon takes the same flock(2) on
# the same sidecar through fcntl, and its sessions_lock() cites this file for
# the protocol rather than restating it. tests/test-registry.py holds the two
# implementations to it from both sides, because that agreement is the whole
# guarantee and nothing else checks it.
#
# The lock is a SIDECAR file, never the registry itself: every writer REPLACES
# that inode, so a lock taken on the inode a writer read is not the lock the
# next writer takes.
#
# READERS take no lock and need none — agent-box-webhook, the spot notifier and
# the reads in this file's own callers all get a whole document from the
# rename. The lock exists for the interval a read-modify-write spans.
#
# What a caller may set before the include, all optional:
#   REGISTRY_FILE       the registry path, when the caller already knows it
#   REGISTRY_PROG       the name the one warning below prints
#   REGISTRY_JQ         jq, when it is not on this program's PATH
#   REGISTRY_FLOCK      flock, likewise; EMPTY means "no lock, carry on"
#   REGISTRY_LOCK_WAIT  seconds to wait for a holder (the test shortens it)
#
# AGENT_BOX_SESSIONS_FILE is what the settings daemon is told; the mark-stopped
# epilogue is generated per user and bakes the path rather than trusting an
# inherited $HOME, and every other writer runs as the user whose registry it
# is.
: "${REGISTRY_FILE:=${AGENT_BOX_SESSIONS_FILE:-$HOME/.config/agent-box/sessions.json}}"
: "${REGISTRY_PROG:=agent-box}"
: "${REGISTRY_JQ:=jq}"
# flock ships in util-linux ONLY, which is not on every PATH a writer here runs
# from: a plain `su -c 'agent-box-session ...'` gets the system PATH, the
# webhook receiver unit's PATH is jq + coreutils + the session CLI, and the
# pane epilogue has none worth the name. So each generated wrapper pins the
# binary — the AGENT_BOX_*_BIN convention. Unset means no lock and no error: a
# session must still be addable, startable and stoppable on a box whose module
# predates this.
# Assigned only when UNSET, never when empty: "" is a caller saying it has no
# flock, and must not be answered with one from the environment.
: "${REGISTRY_FLOCK=${AGENT_BOX_FLOCK_BIN:-}}"
: "${REGISTRY_LOCK_WAIT:=10}"
# 1 while the lock is genuinely held — taken here, inherited, or nested inside
# a section that holds it. Only the webhook spawn wrapper reads it, because it
# may advertise an inherited fd only if it really got the lock.
REGISTRY_HELD=0
_registry_depth=0

registry_close_fd() {
  # Close fd 9 and NOTHING ELSE. The braces are the whole point: `exec` with no
  # command applies its redirections to the CURRENT SHELL and keeps them, so
  # the obvious `exec 9>&- 2>/dev/null` closes the lock fd and then sends this
  # program's stderr to /dev/null for the rest of its life. That is how the
  # supervisor lost every diagnostic it prints after its first unlock —
  # including the line a VM test waits for — and it is why the same shape in
  # registry_lock wraps the OPEN in braces too: a redirection on a group is
  # scoped to the group, while `exec`'s own fd change survives it.
  { exec 9>&-; } 2>/dev/null || true
}

registry_lock() {
  # Nesting-safe on purpose: flock(2) conflicts between two open file
  # DESCRIPTIONS, including two of the same process, so a second fd on the
  # sidecar blocks a writer against ITSELF (verified). Both the supervisor
  # (start_session holds the lock across the mark_started it calls) and the
  # session CLI (a check-then-write around registry_edit) do exactly that.
  _registry_depth=$((_registry_depth + 1))
  [ "$_registry_depth" = 1 ] || return 0
  # An INHERITED lock: agent-box-webhook-spawn holds this lock across its exec
  # into `agent-box-session add`, so its hook-session cap check and the add are
  # one step. It hands the fd over and says so through the environment;
  # re-opening fd 9 here would first CLOSE that description and drop the lock
  # it took.
  if [ "${AGENT_BOX_REGISTRY_LOCK_FD:-}" = 9 ]; then
    REGISTRY_HELD=1
    return 0
  fi
  [ -n "$REGISTRY_FLOCK" ] || return 0
  # A missing directory is a first boot, which is the one moment when even
  # CREATION races (issue #289) — so make it and take the lock, rather than
  # writing the file that decides which sessions exist with no lock at all.
  { exec 9>>"$REGISTRY_FILE.lock"; } 2>/dev/null \
    || { mkdir -p "${REGISTRY_FILE%/*}" 2>/dev/null
         { exec 9>>"$REGISTRY_FILE.lock"; } 2>/dev/null; } \
    || return 0
  # Bounded, never an unbounded wait: nothing may park the supervisor's
  # reconcile loop (every session on the box waits behind it) or a CLI a user
  # is waiting on. A holder that times us out degrades THIS write to the
  # pre-#254 lost-update behaviour, which is a bad write rather than a hung
  # box, and says so on stderr.
  if "$REGISTRY_FLOCK" -w "$REGISTRY_LOCK_WAIT" 9; then
    REGISTRY_HELD=1
  else
    echo "$REGISTRY_PROG: sessions.json lock timed out; continuing unlocked (issue #254)" >&2
    registry_close_fd
  fi
  return 0
}

registry_unlock() {
  [ "$_registry_depth" -gt 0 ] || return 0
  _registry_depth=$((_registry_depth - 1))
  [ "$_registry_depth" = 0 ] || return 0
  # An inherited fd belongs to the process that opened it: closing it here
  # would drop a lock this program never took.
  [ "${AGENT_BOX_REGISTRY_LOCK_FD:-}" != 9 ] || return 0
  REGISTRY_HELD=0
  registry_close_fd
}

registry_edit() {
  # registry_edit JQ_ARGS... — rewrite the registry through jq as ONE
  # read-modify-write under the lock. The document arrives on jq's stdin, not
  # as an argument, so a filter may end in `--args -- "$@"` without the path
  # being read as one of those arguments.
  #
  # Returns 1 with the registry untouched when jq fails. jq's own stderr is
  # left alone: a caller that must stay quiet — the pane epilogue prints into
  # the user's terminal — redirects it at the call site, which keeps that
  # policy where the reason for it is.
  #
  # The lock nests, so a caller already holding it across a check-then-write
  # keeps holding it here and does not deadlock against itself.
  registry_lock
  _registry_tmp="$(mktemp "$REGISTRY_FILE.XXXXXX")" || { registry_unlock; return 1; }
  # The RENAME is checked too, not just jq: a read-only $HOME or a full disk
  # must not be reported as a write to the two writers that do not run under
  # `set -e` (the supervisor and the pane epilogue would carry on as if the
  # flag had stuck), and must not leave a sessions.json.XXXXXX behind for the
  # two that do.
  if "$REGISTRY_JQ" "$@" < "$REGISTRY_FILE" > "$_registry_tmp" \
     && mv "$_registry_tmp" "$REGISTRY_FILE"; then
    registry_unlock
    return 0
  fi
  rm -f "$_registry_tmp"
  registry_unlock
  return 1
}

registry_ensure() {
  # registry_ensure [SEED] — make sure the registry EXISTS, inside the same
  # critical section as everything that writes it (issue #289).
  #
  # Creation was the one step outside the protocol, and two paths create the
  # file: `agent-box-session add` (an empty registry) and the supervisor's
  # first-boot seed (the Nix-declared one). Both asked "is it empty?" with no
  # lock held, and the units that run them start in parallel, so on a first
  # boot a `hook-*` session added by the webhook spawner could be replaced
  # wholesale by the seed landing on top of it — and the batch that spawned
  # that session is never redelivered.
  #
  # An existing file is never touched, seed or no seed: sessions are RUNTIME
  # data (issue #59), so a rebuild must not clobber what the operator changed
  # while the box was live.
  #
  # Which means the lock makes the first-boot race DETERMINISTIC rather than
  # merging its two outcomes, and the losing outcome is worth stating: if an
  # `add` gets there first — a webhook spawn on a box that has just come up —
  # it publishes an empty registry, this seed then finds a non-empty file, and
  # the sessions declared in the NixOS config are not seeded on that boot or
  # any later one. That is the same rule as above (a registry that exists is
  # the operator's, not the config's) and it is preferable to the reverse,
  # where the seed silently deletes a session that was already added and the
  # webhook batch behind it is never redelivered. An operator who wants the
  # declared set back deletes sessions.json and restarts the unit.
  mkdir -p "${REGISTRY_FILE%/*}"
  registry_lock
  if [ ! -s "$REGISTRY_FILE" ]; then
    # A seed is trusted only after it PASSES this shape check (issue #356):
    # two independent producers (this module's Nix-declared seed and the
    # native backend's) write the file this reads, and a shape they disagree
    # on — .sessions as a list instead of an object, seen live on the native
    # side — used to be installed as-is. The reconcile loop then read a
    # session name as an array index and jq errored "Cannot index array with
    # string" every couple of seconds forever, with nothing pointing at the
    # seed as the cause. Reject it once, loudly, instead.
    if [ -n "${1:-}" ] && "$REGISTRY_JQ" -e \
         '(.version == 1) and (.sessions | type == "object")' \
         "$1" >/dev/null 2>&1; then
      install -m 0600 "$1" "$REGISTRY_FILE"
    else
      if [ -n "${1:-}" ]; then
        echo "$REGISTRY_PROG: seed $1 is not a valid sessions.json" \
             '(want {"version":1,"sessions":{...}}); starting empty instead' >&2
      fi
      # 0600 like every other writer's output (the daemon's write_sessions, the
      # seed above, and mktemp's own mode in registry_edit): the registry
      # carries kickoff prompts and working directories, and only this user and
      # root have any business reading them.
      printf '{"version":1,"sessions":{}}\n' > "$REGISTRY_FILE"
      chmod 600 "$REGISTRY_FILE"
    fi
  fi
  registry_unlock
}
