    # Pane epilogue for a CLEAN agent exit (status 0 — /quit, Ctrl+D: an
    # exit somebody asked for). Records stopped=true on this session's
    # sessions.json entry so the supervisor's reconcile loop leaves the
    # session down instead of respawning-and-resuming it (issue #167).
    # Crashes never reach this (non-zero exit takes the post-mortem bash
    # branch), and kill-session / reboot / Spot stop end the pane without
    # running any epilogue — those still respawn. $1 = session name.
    FILE=${lib.escapeShellArg (userSessionsFile name)}
    JQ=${pkgs.jq}/bin/jq
    CU=${pkgs.coreutils}/bin
    [ -n "''${1:-}" ] && [ -s "$FILE" ] || exit 0
    # Verified write, retried: on an agent that exits within its first
    # seconds, the supervisor's mark_started rewrite can race this one
    # (both are tmp+mv, last writer wins). Re-read until the flag stuck.
    # A session delisted meanwhile is left alone rather than re-created.
    for _ in 1 2 3; do
      tmp="$("$CU"/mktemp "$FILE.XXXXXX")" || exit 0
      if "$JQ" --arg s "$1" \
           'if .sessions | has($s) then .sessions[$s].stopped = true else . end' \
           "$FILE" > "$tmp" 2>/dev/null; then
        "$CU"/mv "$tmp" "$FILE"
      else
        "$CU"/rm -f "$tmp"
      fi
      "$JQ" -e --arg s "$1" \
        '(.sessions | has($s) | not) or (.sessions[$s].stopped == true)' \
        "$FILE" >/dev/null 2>&1 && exit 0
      "$CU"/sleep 1
    done
    exit 0
