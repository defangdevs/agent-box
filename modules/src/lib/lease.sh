# A durable audit record for a hook-* session's GitHub claim, so an
# assignment this box accepted is never silently lost when its worker dies
# before saying so (issue #535).
#
# lib/registry.sh's own header already measures what SHARED, multi-writer
# state costs (issue #254) -- a lease avoids that entirely by giving each
# session its own file, written by exactly one program at a time:
# agent-box-webhook-spawn creates it, and whichever of mark-stopped.sh (the
# pane epilogue) or the supervisor's reconcile loop sees how that spawn ended
# writes the outcome. No lock: a session has one pane at a time, so only
# that pane's ending ever writes here, and the next spawn's own ending is a
# later write to the same file, never a concurrent one.
#
# Presence, not a status enum, is what a reader acts on:
#   no file                        -- never leased, or resolved (lease_clear)
#   outcome: null                  -- spawned, not yet accounted for
#   outcome: "died:N" / "vanished" -- accepted work this box cannot say
#     finished; agent-box-session ls/peers surface it for an operator or a
#     sibling session to act on.
LEASE_DIR="${LEASE_DIR:-$HOME/.local/state/agent-box/lease}"
LEASE_JQ="${LEASE_JQ:-${AGENT_BOX_JQ_BIN:-jq}}"

lease_file() {
  # lease_file NAME -- the one place this path is spelled, mirroring
  # session_state_file (src/supervisor.sh, issue #282/#284's convention for a
  # supervisor-owned per-session side file).
  printf '%s/%s.json\n' "$LEASE_DIR" "$1"
}

lease_create() {
  # lease_create NAME TOPIC OBJECT -- called once, at spawn, by
  # agent-box-webhook-spawn. TOPIC is "source:key" (e.g.
  # github:defangdevs/agent-box); OBJECT is the numbered issue/PR this
  # session claims, or empty for a CI-shaped claim with no single number.
  # Best effort throughout, like every other write in this file: a session
  # must spawn whether or not this write lands.
  mkdir -p "$LEASE_DIR" 2>/dev/null || return 0
  _lf="$(lease_file "$1")"
  _lt="$(mktemp "$_lf.XXXXXX" 2>/dev/null)" || return 0
  # gen identifies THIS lease instance, distinct from any earlier or later
  # one at the same NAME -- session names are reusable (a delist-then-add
  # can reuse one), so lease_mark_outcome checks this back before writing
  # an outcome, and a stale write from a session that no longer holds this
  # instance never lands on the lease that replaced it. A fresh random
  # value rather than a timestamp: two creates within the same wall-clock
  # second would otherwise mint the same "generation".
  _gen=""
  [ -r /proc/sys/kernel/random/uuid ] && read -r _gen < /proc/sys/kernel/random/uuid
  if "$LEASE_JQ" -n --arg topic "$2" --arg object "$3" --arg gen "$_gen" \
      --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      '{"//": "Durable claim record for a hook-* session (agent-box#535). Written once at spawn; outcome is set on a crash (mark-stopped.sh) or a silent, epilogue-skipped death (supervisor.sh start_session). Deleted on a clean exit -- absence means never leased, or resolved. gen identifies this instance across a name reuse; a writer that read a different gen must not apply its outcome here.",
        topic: $topic, object: (if $object == "" then null else $object end),
        claimedAt: $at, gen: (if $gen == "" then null else $gen end), outcome: null}' \
      > "$_lt" 2>/dev/null; then
    mv -f "$_lt" "$_lf" 2>/dev/null || rm -f "$_lt"
  else
    rm -f "$_lt"
  fi
}

lease_mark_outcome() {
  # lease_mark_outcome NAME OUTCOME -- record how an unresolved lease ended.
  # A no-op when NAME never had one open (every non-hook session, a hook
  # session whose spawn-time write failed, or one already resolved) --
  # silently: the caller (the pane epilogue, the supervisor's reconcile
  # loop) must never fail or warn over a session that carries no lease.
  #
  # Overwrites only a NULL outcome. A lease that already names one ending
  # keeps it: "vanished", recorded when a respawn found no epilogue ran,
  # must not be replaced by a later crash of that same never-resumed work,
  # and the first hard fact recorded is what an operator needs -- not the
  # most recent one.
  #
  # A resolve (lease_clear, called from rm/reap_ephemeral or a clean exit)
  # can race this call if something else deletes -- and, since a session
  # NAME is reusable, potentially re-creates -- the same NAME's lease
  # concurrently: an operator running `rm` then `add` at the exact moment
  # this pane is also crashing. Existence alone does not catch that: `mv -f`
  # only needs a path to exist, not that it still holds the SAME lease this
  # call read, so a plain existence re-check would let this write land on a
  # brand new lease that reused the name.
  #
  # gen (lease_create) is the guard: captured before the edit, re-read
  # immediately before the rename, and the write is dropped on ANY
  # mismatch. `[ -e "$_lf" ]` is checked separately from the gen compare
  # rather than folded into it, so a file that vanished entirely is always
  # rejected even for a legacy lease minted before this field existed
  # (empty gen on both sides would otherwise compare equal). What is left
  # is the width of the read-compare-rename sequence itself, which no
  # shell script makes fully atomic without a lock this
  # single-writer-per-file design deliberately avoids; the only way to
  # land in it is a NEW lease minted for the exact reused name inside that
  # handful of syscalls, and sweep_lease_state's 30s grace window is the
  # backstop if it ever does.
  _lf="$(lease_file "$1")"
  [ -s "$_lf" ] || return 0
  _gen="$("$LEASE_JQ" -r '.gen // empty' "$_lf" 2>/dev/null)" || _gen=""
  _lt="$(mktemp "$_lf.XXXXXX" 2>/dev/null)" || return 0
  if "$LEASE_JQ" --arg outcome "$2" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      'if .outcome == null then .outcome = $outcome | .endedAt = $at else . end' \
      "$_lf" > "$_lt" 2>/dev/null \
      && [ -e "$_lf" ] \
      && [ "$("$LEASE_JQ" -r '.gen // empty' "$_lf" 2>/dev/null)" = "$_gen" ]; then
    mv -f "$_lt" "$_lf" 2>/dev/null || rm -f "$_lt"
  else
    rm -f "$_lt"
  fi
}

lease_clear() {
  # lease_clear NAME -- called on a clean exit (mark-stopped.sh's status-0
  # branch) and on delist (agent-box-session rm, reap_ephemeral): the
  # session either got the chance to say it was blocked or finished and
  # chose to stop, or the entry is gone outright, so whatever an earlier
  # respawn's lease recorded (including "vanished") is resolved. Deleting
  # the file, not blanking it, is what makes every reader's check a single
  # `-s` test and keeps a resolved lease from being misread as unresolved.
  rm -f "$(lease_file "$1")" 2>/dev/null || true
}

lease_outcome() {
  # lease_outcome NAME -- the recorded outcome, or nothing when there is no
  # lease or it is not yet resolved. Read-only, so it takes no lock and
  # tolerates a lease file mid-write elsewhere: a jq failure on a half
  # written file answers empty, the same as "no lease", rather than erroring
  # a caller (ls, peers) that must never fail over this.
  _lf="$(lease_file "$1")"
  [ -s "$_lf" ] || return 0
  "$LEASE_JQ" -r '.outcome // empty' "$_lf" 2>/dev/null || true
}

# A durable, readable trail of hook-* sessions, so a spawn that a session then
# yielded is not indistinguishable from a spawn that never happened. An
# ephemeral hook session delists itself within seconds when it yields to an
# interactive one, the receiver logs nothing on a successful spawn, and the
# journal is unreadable to the agent user -- so "did that event start a
# session?" had no answer left on the box once the session was gone.
#
# One JSON object per line, append-only, trimmed to the newest lines when it
# grows. Best effort throughout, like every other write in this file: a log
# that cannot be written must never stop a spawn, an rm or a reap.
HOOKLOG="${HOOKLOG:-$HOME/.local/state/agent-box/hook-sessions.jsonl}"
HOOKLOG_KEEP=200

hooklog_append() {
  # hooklog_append JSON_OBJECT -- one line, trimmed in place past 2x HOOKLOG_KEEP.
  #
  # The append, the count and the trim are ONE critical section: an unlocked
  # trim replaces the file with a snapshot that omits a line another writer
  # appended meanwhile. The lock is a sidecar on fd 8 (never fd 9: the
  # registry lock, which rm and the reap loop hold while they call this) and
  # is bounded; if it cannot be had the line is skipped, never written
  # unlocked. With no flock on the box the line is still appended (a single
  # O_APPEND write) but the file is never trimmed.
  #
  # umask 077: this names sessions, topics and objects, so it is created
  # owner-only. An existing state directory keeps its mode - it holds other
  # state too - but the log and its lock are always 0600.
  ( umask 077; mkdir -p "$(dirname "$HOOKLOG")" ) 2>/dev/null || return 0
  _hf="${HOOKLOG_FLOCK-${REGISTRY_FLOCK:-${AGENT_BOX_FLOCK_BIN:-}}}"
  (
    umask 077
    if [ -n "$_hf" ]; then
      exec 8>>"$HOOKLOG.lock" || exit 0
      "$_hf" -w 2 8 || exit 0
    fi
    printf '%s\n' "$1" >> "$HOOKLOG" || exit 0
    chmod 600 "$HOOKLOG" 2>/dev/null
    [ -n "$_hf" ] || exit 0
    _hn="$(wc -l < "$HOOKLOG")" || exit 0
    case "$_hn" in (""|*[!0-9]*) exit 0 ;; esac
    [ "$_hn" -gt $((HOOKLOG_KEEP * 2)) ] || exit 0
    _ht="$(mktemp "$HOOKLOG.XXXXXX")" || exit 0
    if tail -n "$HOOKLOG_KEEP" "$HOOKLOG" > "$_ht"; then
      mv -f "$_ht" "$HOOKLOG" || rm -f "$_ht"
    else
      rm -f "$_ht"
    fi
  ) 2>/dev/null || true
}

hooklog_spawn() {
  # hooklog_spawn NAME TOPIC EVENT OBJECT COUNT -- called by
  # agent-box-webhook-spawn once the session is being added.
  "$LEASE_JQ" -cn --arg n "$1" --arg topic "$2" --arg event "$3" --arg object "$4" \
    --arg count "$5" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{at: $at, what: "spawn", name: $n, topic: $topic, event: $event,
      object: (if $object == "" then null else $object end),
      count: (if $count | test("^[0-9]+$") then ($count | tonumber) else null end)}' \
    2>/dev/null | { read -r _hl && hooklog_append "$_hl"; } || true
}

hooklog_end() {
  # hooklog_end NAME HOW -- a hook-* session leaving the registry. HOW says by
  # whom: "rm" (agent-box-session rm, which is what a yielding session runs),
  # "exited" (a clean agent exit, from the pane epilogue) or "died:N" (a crash,
  # likewise). The supervisor's reap logs nothing: by then the lease is gone.
  # Call BEFORE lease_clear: the lease is where the spawn time and claim live. Any other
  # name is ignored, so the log stays about dispatched work.
  case "$1" in (hook-*) ;; (*) return 0 ;; esac
  _lf="$(lease_file "$1")"
  _claimed=""
  [ -s "$_lf" ] && _claimed="$("$LEASE_JQ" -r '.claimedAt // empty' "$_lf" 2>/dev/null)"
  _out=""
  [ -s "$_lf" ] && _out="$("$LEASE_JQ" -r '.outcome // empty' "$_lf" 2>/dev/null)"
  "$LEASE_JQ" -cn --arg n "$1" --arg how "$2" --arg claimed "$_claimed" --arg outcome "$_out" \
    --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '(($claimed | fromdateiso8601?) // null) as $c
     | {at: $at, what: "end", name: $n, how: $how,
        lifeSeconds: (if $c == null then null else ((now | floor) - $c) end),
        outcome: (if $outcome == "" then null else $outcome end)}' \
    2>/dev/null | { read -r _hl && hooklog_append "$_hl"; } || true
}
