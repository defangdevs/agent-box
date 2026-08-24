# Pane epilogue for a CLEAN agent exit (status 0 — /quit, Ctrl+D: an
# exit somebody asked for). Records stopped=true on this session's
# sessions.json entry so the supervisor's reconcile loop leaves the
# session down instead of respawning-and-resuming it (issue #167).
# Crashes never reach this (non-zero exit takes the post-mortem bash
# branch), and kill-session / reboot / Spot stop end the pane without
# running any epilogue — those still respawn. $1 = session name.
#
# The session registry — where it lives, how it is locked and how it is
# rewritten — is one file every shell writer splices in (issue #254). This is
# the writer with no environment worth trusting: it runs as the last command in
# an agent's pane and inherits that agent's PATH, so the generated wrapper
# hands it a PATH, the two binaries the protocol needs, and the registry of the
# user it was generated for.
@@include:lib/registry.sh@@
[ -n "${1:-}" ] && [ -s "$REGISTRY_FILE" ] || exit 0
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
  registry_edit --arg s "$1" \
    'if .sessions | has($s) then .sessions[$s].stopped = true else . end' \
    2>/dev/null
  "$REGISTRY_JQ" -e --arg s "$1" \
    '(.sessions | has($s) | not) or (.sessions[$s].stopped == true)' \
    "$REGISTRY_FILE" >/dev/null 2>&1 && exit 0
  registry_unlock
  sleep 1
done
exit 0
