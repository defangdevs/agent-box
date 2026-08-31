set -u
# tmux, jq and head resolve from the web-terminal unit's PATH; the socket
# name is the module-wide constant. The sessions registry arrives as unit
# environment (the same file the settings daemon and the supervisor read),
# so a dead end can tell a name this box knows from one it does not.
T="tmux -T hyperlinks -L agent-box"
SESSIONS="${AGENT_BOX_SESSIONS_FILE:-}"
# The session CLI, pinned by the generated wrapper: this pane STARTS a
# session it found stopped, and the web-terminal unit's PATH is tmux, jq,
# coreutils and this script. Clearing the stopped flag is the registry's
# write protocol (issue #254), so it goes through the one program that
# implements it rather than through a second jq edit of its own.
SESSION_CLI="${AGENT_BOX_SESSION_BIN:-agent-box-session}"
# The mascot (issue #185), for the dead ends below: each is a full-screen
# "nothing to attach to" moment, so the art costs nothing and softens a
# failure. printf per line, not a heredoc: the assembler re-indents this
# script into the module's Nix indented string, where a heredoc terminator
# would ride on that indent.
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
reg() {   # reg FILTER — ask jq about "$want", false on any error
  [ -n "$SESSIONS" ] || return 1
  jq -e --arg s "$want" "$1" "$SESSIONS" >/dev/null 2>&1
}
stopped_now() { reg '.sessions[$s].stopped == true'; }
# Is there a person at the other end of this pane? ttyd runs the wrapper on a
# pty, which is what makes the offer below pressable at all.
#
# NOT [ -t 0 ]: that is true of things with no reader too, and one of them is
# CI. A NixOS VM test's backdoor shell runs with `exec < /dev/hvc0` — a virtio
# console, a tty with nobody behind it — and `su -c` passes it down, so the
# offer's wait-for-a-keypress loop hung the sessions-web test until the job
# timed out (PR #452, run 33421803747). A pty is /dev/pts/N and a console is
# not, which separates ttyd (and a human's ssh) from every automated caller.
interactive() {
  case "$(readlink /proc/self/fd/0 2>/dev/null)" in
    (/dev/pts/*) return 0 ;;
  esac
  return 1
}
live_list() {
  echo ""
  echo "Live sessions:"
  $T list-sessions -F '  #S' 2>/dev/null || echo "  (none)"
}
# Attach the moment the session exists — $1 half-second tries, so the same
# helper answers "is it up right now" and "wait for the supervisor to bring
# it up". Never returns on success: the pane BECOMES the terminal.
attach_when_live() {
  n=0
  while :; do
    if $T has-session -t "=$want" 2>/dev/null; then
      exec $T attach -t "=$want"
    fi
    n=$((n + 1))
    [ "$n" -lt "$1" ] || return 1
    sleep 0.5
  done
}
# Start a stopped session from this pane, then attach to it.
# The operator is already looking at the pane that has to change, and the
# settings page was the only place that could make it change — a detour that
# also ends with "now reload the terminal", because pressing Start there
# leaves this pane on a stale message.
start_it() {
  echo ""
  echo "starting '$want'…"
  if ! "$SESSION_CLI" restart "$want" >/dev/null 2>&1; then
    echo "could not start '$want' from here. Press Start on the settings"
    echo "page, or run:"
    echo "  agent-box-session restart $want"
    return 0
  fi
  # ~2s for a listed session; a FIRST spawn fetches the agent CLI, which
  # takes as long as the download does, so falling out of this wait is not
  # a failure — the offer loop keeps watching.
  attach_when_live 120 || true
  if stopped_now; then
    echo "'$want' came up and parked itself again: an agent that exits"
    echo "cleanly stops its own session, which is not a Start that did"
    echo "nothing. Its transcript is on the settings page (download icon)."
  else
    echo "'$want' has not come up yet — a first spawn fetches its agent"
    echo "CLI, which can take minutes. Still waiting."
  fi
}
# Wait for a keypress and for the session at the same time: a Start pressed
# on the settings page, a restart from another pane or the agent CLI arriving
# all bring the session up while this pane sits here, and the pane the
# operator is watching should attach to it rather than hold a stale message.
# read's exit status tells the three cases apart: 0 = a line was typed,
# >128 = the timeout expired, anything else = EOF, which is the browser tab
# closing and the only reason to stop.
offer() {
  # Bounded at 30 minutes, not endless: a pane nobody has touched for that
  # long costs a process per open connection, and a bound is also what keeps
  # a wrong answer from interactive() above to a slow caller rather than a
  # hung one. ttyd reconnects when this exits, so the offer comes straight
  # back for a pane somebody IS looking at.
  waited=0
  while [ "$waited" -lt 600 ]; do
    attach_when_live 1 || true
    rc=0
    read -r -t 3 _key || rc=$?
    if [ "$rc" = 0 ]; then
      start_it
    elif [ "$rc" -le 128 ]; then
      exit 0
    fi
    waited=$((waited + 1))
  done
  exit 1
}
want="${1:-}"
case "$want" in (*[!A-Za-z0-9_-]*) want="" ;; esac
if [ -n "$want" ]; then
  attach_when_live 1 || true
  potato
  # What to say about a requested name that has no tmux session. The
  # registry is what tells the three cases apart, and every one of them used
  # to get "create it with: agent-box-session add" — advice that FAILS on a
  # name the registry already carries, which is what the settings page's
  # session links hand you for any session that is not live (issue #241).
  if ! reg '.sessions | has($s)'; then
    echo "no session named '$want' — nothing on this box is listed under that"
    echo "name. Add one from the settings page, or run:"
    echo "  agent-box-session add $want"
    live_list
    sleep 5
    exit 1
  fi
  if stopped_now; then
    echo "session '$want' is stopped: it is still listed, but nothing starts"
    echo "it on its own — an agent that exits cleanly parks its session"
    echo "this way."
    echo ""
    # A pane with no terminal on the other end (a script, a VM test, the
    # `su -c` shape a test driver uses) cannot press anything, so it keeps
    # the printed advice and the 5s retry it has always had — see
    # interactive() for why that is not [ -t 0 ].
    if interactive; then
      echo "  Press Enter to start it here — this pane then attaches to it."
      echo ""
      echo "  Start on the settings page does the same, as does:"
      echo "    agent-box-session restart $want"
      live_list
      offer
    else
      echo "Press Start on the settings page, or run:"
      echo "  agent-box-session restart $want"
      echo "then reload this page — Start does not reconnect this pane on its own."
      live_list
      sleep 5
      exit 1
    fi
  fi
  echo "session '$want' is starting — the supervisor spawns it within a few"
  echo "seconds, and this pane attaches to it by itself."
  live_list
  # A minute of waiting for a real terminal, five seconds for anything else:
  # a caller with no pty is a script reading this output, not somebody
  # watching a pane, and it has always had the 5s-and-exit retry.
  if interactive; then
    attach_when_live 120 || true
  else
    attach_when_live 10 || true
  fi
  # A minute with nothing to attach to: report what the registry says NOW,
  # because a session that parked itself meanwhile is a different message.
  if stopped_now; then
    echo ""
    echo "'$want' parked itself instead of coming up (a clean agent exit)."
    if interactive; then
      echo "Press Enter to start it again."
      offer
    fi
  else
    echo ""
    echo "'$want' is still not up — the supervisor may be fetching its agent"
    echo "CLI. Reload this page to keep waiting."
  fi
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
