set -u
# tmux and head resolve from the web-terminal unit's PATH; the socket name
# is the module-wide constant.
T="tmux -T hyperlinks -L agent-box"
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
want="${1:-}"
case "$want" in (*[!A-Za-z0-9_-]*) want="" ;; esac
if [ -n "$want" ]; then
  if $T has-session -t "=$want" 2>/dev/null; then
    exec $T attach -t "=$want"
  fi
  potato
  echo "no session named '$want'. Live sessions:"
  $T list-sessions -F '  #S' 2>/dev/null || echo "  (none)"
  echo "create it with: agent-box-session add $want"
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
