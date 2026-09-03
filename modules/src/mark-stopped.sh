# Pane epilogue: how the agent's pane ENDED, recorded on this session's
# sessions.json entry. $1 = session name, $2 = the agent's exit status
# (absent, or 0, means a clean exit — a box that predates the second
# argument passes none).
#
#   status 0 — an exit somebody ASKED for (/quit, Ctrl+D): stopped=true, so
#     the supervisor's reconcile loop leaves the session down instead of
#     respawning-and-resuming it (issue #167).
#   non-zero — a CRASH, whose pane is a post-mortem shell the supervisor
#     deliberately does not respawn over: died=<status>. Nothing about the
#     respawn changes (the live pane is what suppresses it) — what changes
#     is that every reader can now SAY so. The pane is bash, so tmux reports
#     the session as live and the settings page, the workspace tab and
#     `agent-box-session ls` all called a dead agent "Running": a codex
#     Remote Control daemon died on the deployed box and nothing anywhere
#     said it had (issue #516).
#
# kill-session / reboot / Spot stop end the pane without running any
# epilogue at all — those still respawn.
#
# The session registry — where it lives, how it is locked and how it is
# rewritten — is one file every shell writer splices in (issue #254). This is
# the writer with no environment worth trusting: it runs as the last command in
# an agent's pane and inherits that agent's PATH, so the generated wrapper
# hands it a PATH, the two binaries the protocol needs, and the registry of the
# user it was generated for.
@@include:lib/registry.sh@@
[ -n "${1:-}" ] && [ -s "$REGISTRY_FILE" ] || exit 0
# The status arrives from the pane's own shell as `$?`, so it is a small
# non-negative integer or nothing. Anything else is still an ending that was
# not a clean exit, and counts as a crash rather than being dropped: a
# malformed status must never be read as "the operator asked for this".
_status="${2:-0}"
case "$_status" in (""|*[!0-9]*) _status=1 ;; esac
if [ "$_status" -eq 0 ]; then
  _edit='if .sessions | has($s) then .sessions[$s].stopped = true else . end'
  _check='(.sessions | has($s) | not) or (.sessions[$s].stopped == true)'
else
  # Recorded as the STATUS, not a bare true: it is the only thing anyone
  # knows about the crash without attaching to the post-mortem pane, and it
  # costs the same field. Cleared by the next spawn (src/supervisor.sh), so a
  # session that comes back is never left looking dead.
  _edit='if .sessions | has($s) then .sessions[$s].died = $st else . end'
  _check='(.sessions | has($s) | not) or (.sessions[$s].died == $st)'
fi
# Verified write, retried: on an agent that exits within its first
# seconds, the supervisor's mark_started rewrite can race this one
# (both are tmp+mv, last writer wins). The sidecar lock below makes each
# pass a read-modify-write no other writer can interleave — the loop stays
# as the backstop for the one case a lock cannot cover, a holder that times
# us out. Re-read until the flag stuck.
# A session delisted meanwhile is left alone rather than re-created.
for _ in 1 2 3; do
  # Held across the edit and the re-read, so the pass verifies the document it
  # published, but never across the sleep: the supervisor's reconcile loop
  # takes the same lock, and parking it for a second would delay every session
  # on the box. registry_edit nests inside this, and nothing this script starts
  # outlives it, so no child can carry the fd (and the lock) away.
  # Both calls are silenced, because this prints into the pane the user just
  # quit: a lock timeout would otherwise put a warning there once per retry
  # pass (`flock -w` itself printed nothing before this), and an unparseable
  # registry is the supervisor's news to report, not this script's.
  registry_lock 2>/dev/null
  registry_edit --arg s "$1" --argjson st "$_status" "$_edit" 2>/dev/null
  "$REGISTRY_JQ" -e --arg s "$1" --argjson st "$_status" \
    "$_check" "$REGISTRY_FILE" >/dev/null 2>&1 && exit 0
  registry_unlock
  sleep 1
done
exit 0
