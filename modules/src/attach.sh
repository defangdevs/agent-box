set -u
# tmux and head resolve from the web-terminal unit's PATH; the socket name
# is the module-wide constant.
T="tmux -T hyperlinks -L agent-box"
want="${1:-}"
case "$want" in (*[!A-Za-z0-9_-]*) want="" ;; esac
if [ -n "$want" ]; then
  if $T has-session -t "=$want" 2>/dev/null; then
    exec $T attach -t "=$want"
  fi
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
echo "no live sessions yet — the supervisor may still be starting them."
sleep 5
exit 1
