# The per-user secrets file the settings page and `agent-box-session env`
# manage. Runs inside the user's session, so $HOME names it directly.
FILE="$HOME/.config/agent-box/env"
if [ -r "$FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
    key=${line%%=*}
    case "$key" in (*[!A-Za-z0-9_]*|""|[0-9]*) continue ;; esac
    val=${line#*=}
    case "$val" in
      \"*\") val=${val#\"}; val=${val%\"} ;;
      \'*\') val=${val#\'}; val=${val%\'} ;;
    esac
    export "$key=$val"
  done < "$FILE"
fi

# The agent profile's own environment (issue #321), on top of the file above:
# every key in ~/.config/agent-box/profiles/<name>.env that is not one of the
# reserved LAUNCH keys agent-box-profile turns into harness arguments. The
# supervisor passes the name through the tmux session environment, so this
# applies at every spawn — an edited profile reaches the session on its next
# restart, exactly like the file above.
#
# Convenience, NOT isolation: this process's environment is readable through
# /proc/<pid>/environ by every other session of this user (issue #135, wiki
# Users-vs-Sessions), so a secret in a profile is a secret all of them have.
# What a profile buys is that sessions started with OTHER profiles do not get
# it handed to them, not that they cannot reach it.
if [ -n "${AGENT_BOX_PROFILE:-}" ]; then
  case "$AGENT_BOX_PROFILE" in
    (*[!A-Za-z0-9_-]*|"") ;;
    (*)
      PFILE="$HOME/.config/agent-box/profiles/$AGENT_BOX_PROFILE.env"
      if [ -r "$PFILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
          case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
          key=${line%%=*}
          case "$key" in (*[!A-Za-z0-9_]*|""|[0-9]*) continue ;; esac
          # The reserved keys are launch config, not environment: HARNESS,
          # MODEL, EFFORT and SYSTEM_PROMPT are already in the session's
          # agent/extraArgs, and exporting them would put a system prompt in
          # the environment of everything the agent runs.
          case "$key" in (HARNESS|MODEL|EFFORT|SYSTEM_PROMPT) continue ;; esac
          val=${line#*=}
          case "$val" in
            \"*\") val=${val#\"}; val=${val%\"} ;;
            \'*\') val=${val#\'}; val=${val%\'} ;;
          esac
          export "$key=$val"
        done < "$PFILE"
      fi
      ;;
  esac
fi

# The GitHub login this box acts as, for local-webhook's "@self" sender mute
# (issue #261). Resolved HERE because this is the one process that holds the
# token: the loop above just exported it, and the identity is a property of
# that token, not of the deployment. A value the env store set wins (the
# resolver echoes it straight back); otherwise the resolver answers from its
# cache, and only calls GitHub when the token changed. --throttled because
# this runs at EVERY session start: one failed lookup per token per hour is
# enough, and a session must not wait on a network timeout to begin. Best
# effort — no token, no network or no resolver leaves it unset, which is what
# a box that writes nothing to GitHub wants anyway.
if command -v agent-box-webhook-self >/dev/null 2>&1; then
  _self=$(agent-box-webhook-self --throttled 2>/dev/null) || _self=""
  [ -n "$_self" ] && export LOCAL_WEBHOOK_SELF="$_self"
  unset _self
fi

exec "$@"
