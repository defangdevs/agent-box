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
  if "$LEASE_JQ" -n --arg topic "$2" --arg object "$3" \
      --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      '{"//": "Durable claim record for a hook-* session (agent-box#535). Written once at spawn; outcome is set on a crash (mark-stopped.sh) or a silent, epilogue-skipped death (supervisor.sh start_session). Deleted on a clean exit -- absence means never leased, or resolved.",
        topic: $topic, object: (if $object == "" then null else $object end),
        claimedAt: $at, outcome: null}' \
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
  _lf="$(lease_file "$1")"
  [ -s "$_lf" ] || return 0
  _lt="$(mktemp "$_lf.XXXXXX" 2>/dev/null)" || return 0
  if "$LEASE_JQ" --arg outcome "$2" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      'if .outcome == null then .outcome = $outcome | .endedAt = $at else . end' \
      "$_lf" > "$_lt" 2>/dev/null; then
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
