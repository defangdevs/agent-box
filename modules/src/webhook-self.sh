# Resolve the GitHub login this box ACTS AS and cache it, for local-webhook's
# "@self" sender mute (issue #261). Prints the login on stdout; prints nothing
# and exits 1 when it cannot be resolved.
#
# The identity is NOT a deploy-time fact. It is a property of whatever token
# this user's environment carries, and that token arrives at runtime — through
# the settings page or `agent-box-session env`, and it can be swapped for a
# different account at any moment. So it is derived from the token itself and
# re-derived whenever the token changes: the cache is keyed by a fingerprint of
# the token rather than by an age, so a swapped token invalidates it at once
# while an unchanged one never costs a network call.
#
# Two consumers read the cache, and both need the same answer or a session
# mutes an identity the receiver does not:
#   - env-exec.sh exports it into every session, so the session's own webhook
#     peer — the process that filters session deliveries — resolves "@self";
#   - the receiver unit loads it as an EnvironmentFile, for standing watches
#     and for the ownership probe over other sessions' filters.
# The file is an env-file for exactly that reason: systemd and shell agree on
# the format, so there is no second parser.
#
# LOCAL_WEBHOOK_SELF already set in the environment wins outright and is echoed
# back untouched: a user who names the login in the env store has answered the
# question, and no lookup should second-guess it.
set -u

usage() {
  cat <<'EOF'
usage: agent-box-webhook-self [--refresh] [--throttled]

Print the GitHub login this box acts as — the account whose token it comments,
pushes and edits issues with — and cache it for the webhook receiver and for
new sessions. Resolved with `gh api user`, from the token in THIS environment.

--refresh    re-resolve even when the cached answer still matches the token.
--throttled  skip the lookup when a recent one for this same token already
             failed. For the session-start path, which must not pay a network
             timeout on every respawn; a person asking directly always gets a
             real attempt.

Exits 1 with no output when there is no token, no network, or no gh.
EOF
}

REFRESH=0
THROTTLED=0
for a in "$@"; do
  case "$a" in
    --refresh) REFRESH=1 ;;
    --throttled) THROTTLED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

STATE_DIR="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
FILE="$STATE_DIR/self.env"
# Failed lookups are stamped so an offline box does not pay a timeout at every
# session start. Same shape as the plugin-cache sync: at most one attempt per
# token per hour, and a token change retries immediately. The stamp is only
# READ under --throttled: it exists for the automatic caller, and a person (or
# a test) who runs this directly has just done something about the failure —
# fixed the token, plugged in the network — so refusing to look for another
# hour would answer the wrong question.
STAMP="$STATE_DIR/.self-attempt"
RETRY_S=3600

valid() {
  # What webhook.py sanitizes a sender to, so anything else is not a login we
  # could ever match against an event.
  case "$1" in
    "" | *[!A-Za-z0-9._-]*) return 1 ;;
    *) return 0 ;;
  esac
}

# An explicit value beats every other source, and is not cached: the env store
# it comes from is already the durable copy.
if [ -n "${LOCAL_WEBHOOK_SELF:-}" ] && valid "${LOCAL_WEBHOOK_SELF}"; then
  printf '%s\n' "$LOCAL_WEBHOOK_SELF"
  exit 0
fi

# Fingerprint, never the token: this file is world-readable by design (a login
# is not a secret) and the receiver reads it as an EnvironmentFile.
fp=$(printf '%s\n%s' "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" \
  | sha256sum 2>/dev/null | cut -c1-16)

cached=""
cached_fp=""
if [ -r "$FILE" ]; then
  cached=$(sed -n 's/^LOCAL_WEBHOOK_SELF=//p' "$FILE" | head -1)
  cached_fp=$(sed -n 's/^# fp=//p' "$FILE" | head -1)
  valid "$cached" || cached=""
fi

if [ "$REFRESH" = 0 ] && [ -n "$cached" ] && [ "$cached_fp" = "$fp" ]; then
  printf '%s\n' "$cached"
  exit 0
fi

# Nothing usable is cached for THIS token. Ask GitHub, unless this is the
# throttled path and a recent attempt for the same token already failed.
tried=""
tried_fp=""
why="a recent lookup for this token already failed"
if [ -r "$STAMP" ]; then
  read -r tried tried_fp < "$STAMP" || true
fi
# Digits only before it reaches arithmetic: the stamp is a plain file this user
# can hand-edit, and shell arithmetic re-evaluates whatever a variable holds.
case "$tried" in (*[!0-9]*|"") tried=0 ;; esac
now=$(date +%s 2>/dev/null || echo 0)
if [ "$THROTTLED" = 1 ] && [ "$REFRESH" = 0 ] && [ "$tried_fp" = "$fp" ] \
   && [ "$tried" -gt 0 ] && [ "$((now - tried))" -lt "$RETRY_S" ]; then
  login=""
elif command -v gh >/dev/null 2>&1; then
  # gh, not curl: it resolves the token the same way every other tool on this
  # box does — $GH_TOKEN, then $GITHUB_TOKEN, then its own stored credentials —
  # so the answer describes the identity the agent actually pushes with.
  login=$(timeout 8 gh api user --jq .login 2>/dev/null | tr -d '\r' | head -1)
  [ -n "$login" ] || why="gh api user answered nothing (no token, or GitHub unreachable)"
else
  login=""
  why="no gh on PATH"
fi

if valid "${login:-}"; then
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  tmp="$FILE.$$"
  {
    echo "# Written by agent-box-webhook-self. The GitHub login this box acts"
    echo "# as, derived from the token in its environment (issue #261) — read"
    echo "# by env-exec.sh and by the webhook receiver's EnvironmentFile."
    echo "# fp=$fp"
    echo "LOCAL_WEBHOOK_SELF=$login"
  } > "$tmp" 2>/dev/null &&
    chmod 0644 "$tmp" 2>/dev/null &&
    mv "$tmp" "$FILE" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  rm -f "$STAMP" 2>/dev/null || true
  printf '%s\n' "$login"
  exit 0
fi

# Could not resolve. A previously cached login is still the best answer there
# is — the box did not stop being that account because GitHub was unreachable —
# so it is used, but the cache is left keyed to the OLD token so the next
# session retries rather than adopting a stale identity for good.
mkdir -p "$STATE_DIR" 2>/dev/null || true
printf '%s %s\n' "$now" "$fp" > "$STAMP" 2>/dev/null || true
if [ -n "$cached" ]; then
  echo "agent-box-webhook-self: $why; keeping the last known login" >&2
  printf '%s\n' "$cached"
  exit 0
fi
echo "agent-box-webhook-self: $why; \"@self\" will match nobody" >&2
exit 1
