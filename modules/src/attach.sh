set -u
# tmux, jq and head resolve from the web-terminal unit's PATH; the socket
# name is the module-wide constant. The sessions registry arrives as unit
# environment (the same file the settings daemon and the supervisor read),
# so a dead end can tell a name this box knows from one it does not.
T="tmux -T hyperlinks -L agent-box"
SESSIONS="${AGENT_BOX_SESSIONS_FILE:-}"
# The mascot (issue #185), for the two dead ends below: both are
# full-screen "nothing to attach to" moments, so the art costs
# nothing and softens a failure. printf per line, not a heredoc: the
# assembler re-indents this script into the module's Nix indented
# string, where a heredoc terminator would ride on that indent.
potato() {
  printf '%s\n' \
    "             .-~~~~~~~~~~~~-." \
    "  >=--.___.-~                ~-." \
    "        ,~   .         .-~~~-.  ~." \
    "       |    .   .      | (o) |    |" \
    "        \`~.       .    \`-~~~-' .~'" \
    "  >=--.___\`-._              _.-'" \
    "             \`~-..........-~'" \
    ""
}
# What to say about a requested name that has no tmux session. The
# registry is what tells the three cases apart, and every one of them used
# to get "create it with: agent-box-session add" — advice that FAILS on a
# name the registry already carries, which is what the settings page's
# session links hand you for any session that is not live (issue #241).
reg() {   # reg FILTER — ask jq about "$want", false on any error
  [ -n "$SESSIONS" ] || return 1
  jq -e --arg s "$want" "$1" "$SESSIONS" >/dev/null 2>&1
}
advice() {
  if ! reg '.sessions | has($s)'; then
    echo "no session named '$want' — nothing on this box is listed under that"
    echo "name. Add one from the settings page, or run:"
    echo "  agent-box-session add $want"
  elif reg '.sessions[$s].stopped == true'; then
    echo "session '$want' is stopped: it is still listed, but nothing will"
    echo "bring it back on its own. Press Start on the settings page, or run:"
    echo "  agent-box-session restart $want"
    echo "then reload this page — Start does not reconnect this pane on its own."
  else
    echo "session '$want' is starting — the supervisor spawns it within a few"
    echo "seconds. Reload this page."
  fi
}
want="${1:-}"
case "$want" in (*[!A-Za-z0-9_-]*) want="" ;; esac
if [ -n "$want" ]; then
  if $T has-session -t "=$want" 2>/dev/null; then
    exec $T attach -t "=$want"
  fi
  potato
  advice
  echo ""
  echo "Live sessions:"
  $T list-sessions -F '  #S' 2>/dev/null || echo "  (none)"
  sleep 5
  exit 1
fi
# No session requested: prefer "main", else the first live session.
if $T has-session -t "=main" 2>/dev/null; then
  exec $T attach -t "=main"
fi
first="$($T list-sessions -F '#S' 2>/dev/null | head -n 1)"
if [ -n "$first" ]; then
  exec $T attach -t "=$first"
fi
potato
echo "no live sessions yet — the supervisor may still be starting them."
sleep 5
exit 1
