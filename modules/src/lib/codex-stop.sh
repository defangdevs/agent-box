# stop_codex_pid PID — end a codex process: TERM, wait up to 5 s, then KILL.
#
# Shared by the remote-control wrapper (an app-server only the legacy pid
# file records, issue #904) and the session-start mirror (a managed daemon
# running a stale codex build, issue #949). The caller proves the process
# is the right one first: this helper never checks identity, so never hand
# it a pid that has not been matched against something stronger than the
# pid alone.
stop_codex_pid() {
  kill -TERM "$1" 2>/dev/null || return 0
  for _scp in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$1" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -KILL "$1" 2>/dev/null || true
}
