set -eu
# jq/tmux resolve from PATH (system packages + every agent unit's PATH);
# the installed-agent list and default come from the AGENT_BOX_* env the
# generated wrapper exports (issue #154, Phase 2).
JQ=jq
FILE="$HOME/.config/agent-box/sessions.json"
AGENTS="${AGENT_BOX_AGENTS:?}"
DEFAULT_AGENT="${AGENT_BOX_DEFAULT_AGENT:?}"
export TMUX_TMPDIR="${TMUX_TMPDIR:-/run/agent-box-$USER}"

t() { tmux -L agent-box "$@"; }
usage() {
  echo "usage: agent-box-session ls"
  echo "       agent-box-session add [NAME] [--agent AGENT] [--cwd DIR]"
  echo "                             [--prompt TEXT] [--resume-prompt TEXT] [-- EXTRA_ARGS...]"
  echo "       agent-box-session rm NAME"
  echo "       agent-box-session stop NAME"
  echo "       agent-box-session restart NAME | --all"
  echo "       agent-box-session env ls | set KEY VALUE | rm KEY"
  echo "agents: $AGENTS (default: $DEFAULT_AGENT)"
  echo "--prompt kicks the session off with a task (first spawn only); a later"
  echo "respawn resumes the prior transcript instead of redoing it."
  echo "Listed sessions are (re)started by the per-user supervisor within ~2s."
  echo "stop parks a session (no respawn; an agent quitting cleanly does the"
  echo "same) until 'restart NAME' revives it; rm delists it for good."
  echo "Attach: tmux -L agent-box attach -t NAME, or the browser terminal /<user>/?arg=NAME"
}
valid_name() {
  case "$1" in (*[!A-Za-z0-9_-]*|"") return 1 ;; esac
}
valid_key() {
  # env var name charset — mirrors the settings daemon's KEY_RE and the
  # env-exec wrapper: letters/digits/underscore, not starting with a digit.
  case "$1" in (*[!A-Za-z0-9_]*|""|[0-9]*) return 1 ;; esac
}
ensure_file() {
  mkdir -p "$(dirname "$FILE")"
  [ -s "$FILE" ] || printf '{"version":1,"sessions":{}}\n' > "$FILE"
}
prune_filter() {
  # Drop the delisted session's webhook filter file. webhook.py names it
  # filter.<LOCAL_WEBHOOK_SESSION>.json and the supervisor sets that to
  # "<user>-<session>" (webhookSessionEnvArgs); the spawn wrapper seeds one
  # for every dispatched hook-* session, and webhook_subscribe writes one for
  # any session that subscribes. Nothing removed them, so they accumulated
  # one per session that ever existed (31 files for 3 live sessions).
  #
  # Only ever called for a name that has just been DELISTED. A live session's
  # file must stay: session routing fails OPEN (webhook.py:682), so deleting
  # it would not mute that session, it would hand it the whole bus. For the
  # same reason removing a dead session's file is the right end state and not
  # merely tidy — the name is reusable, and a later 'add' of the same name
  # would otherwise inherit the dead session's subscriptions.
  _sd="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
  rm -f "$_sd/filter.$(id -un)-$1.json"
}
jq_edit() {
  # jq_edit JQ_ARGS... — atomically rewrite FILE through jq.
  tmp="$(mktemp "$FILE.XXXXXX")"
  if "$JQ" "$@" < "$FILE" > "$tmp"; then
    mv "$tmp" "$FILE"
  else
    rm -f "$tmp"
    exit 1
  fi
}
taken() { "$JQ" -e --arg n "$1" '.sessions | has($n)' "$FILE" >/dev/null; }
gen_name() {
  # gen_name AGENT — echo a unique session name derived from AGENT: the
  # bare name when free ("claude"), else a short random suffix
  # ("claude-a3f9"). Callers pass a validated agent, itself a valid name.
  a="$1"
  taken "$a" || { printf '%s' "$a"; return; }
  while :; do
    cand="$a-$(printf '%04x' $((RANDOM % 65536)))"
    taken "$cand" || { printf '%s' "$cand"; return; }
  done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  ls)
    live="$(t list-sessions -F '#S' 2>/dev/null || true)"
    printf '%-24s %-8s %s\n' NAME AGENT STATE
    if [ -s "$FILE" ]; then
      "$JQ" -r '.sessions | to_entries[] | [.key, (.value.agent // "?"), (if .value.stopped == true then "stopped" else "starting" end)] | @tsv' "$FILE" \
      | while IFS="$(printf '\t')" read -r n a state; do
        printf '%s\n' "$live" | grep -qxF "$n" && state=live
        printf '%-24s %-8s %s\n' "$n" "$a" "$state"
      done
    fi
    # Live tmux sessions nobody listed (started by hand): show, don't hide.
    printf '%s\n' "$live" | while IFS= read -r n; do
      [ -n "$n" ] || continue
      if [ ! -s "$FILE" ] || ! "$JQ" -e --arg n "$n" '.sessions | has($n)' "$FILE" >/dev/null; then
        printf '%-24s %-8s %s\n' "$n" '-' 'unmanaged'
      fi
    done
    ;;
  add)
    # NAME is optional and positional: a leading non-flag arg is the name,
    # otherwise the name is auto-derived from the agent below.
    name=""
    case "${1:-}" in
      ""|-*) ;;
      *) name="$1"; shift; valid_name "$name" || { usage >&2; exit 2; } ;;
    esac
    agent="$DEFAULT_AGENT"; cwd=""; prompt=""; rprompt=""; has_prompt=0; has_rprompt=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --agent) agent="${2:?--agent needs a value}"; shift 2 ;;
        --cwd) cwd="${2:?--cwd needs a value}"; shift 2 ;;
        --prompt) prompt="${2?--prompt needs a value}"; has_prompt=1; shift 2 ;;
        --resume-prompt) rprompt="${2?--resume-prompt needs a value}"; has_rprompt=1; shift 2 ;;
        --) shift; break ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    case " $AGENTS " in
      (*" $agent "*) ;;
      (*) echo "agent '$agent' is not available (available: $AGENTS)" >&2; exit 2 ;;
    esac
    ensure_file
    [ -n "$name" ] || name="$(gen_name "$agent")"
    if taken "$name"; then
      echo "session '$name' already exists — 'agent-box-session rm $name' first, or 'restart $name' to bounce it" >&2
      exit 2
    fi
    # The stable box-session id we own across respawns (Claude --session-id /
    # --resume; Codex transcript marker). Minted here so it's set before the
    # first spawn; the supervisor mints one too for legacy sessions.
    bid=""
    [ -r /proc/sys/kernel/random/uuid ] && read -r bid < /proc/sys/kernel/random/uuid || true
    # `--` after --args: jq otherwise still option-parses positional
    # args, so a dashed extra arg like --model would error out.
    jq_edit --arg n "$name" --arg a "$agent" --arg c "$cwd" \
      --arg p "$prompt" --arg pp "$has_prompt" \
      --arg rp "$rprompt" --arg rpp "$has_rprompt" --arg bid "$bid" \
      '.sessions[$n] = {agent: $a, skipPermissions: true, remoteControl: true,
                        remoteControlName: null,
                        workingDirectory: (if $c == "" then null else $c end),
                        extraArgs: $ARGS.positional,
                        initialPrompt: (if $pp == "1" then $p else null end),
                        resumePrompt: (if $rpp == "1" then $rp else null end),
                        boxSessionId: (if $bid == "" then null else $bid end),
                        hasRun: false}' \
      --args -- "$@"
    # The mascot (issue #185) marks the closest thing this CLI has to
    # "an agent just started". Small on purpose: this runs in webhook
    # spawns and scripts too, and nothing parses the lines below it.
    printf '%s\n' \
        "   .-~~-." \
        "  ( (o) )" \
        "   \`-~~-'"
    if [ "$has_prompt" = 1 ]; then
      echo "session '$name' ($agent) added with a kickoff prompt — the supervisor starts it within ~2s"
    else
      echo "session '$name' ($agent) added — the supervisor starts it within ~2s"
    fi
    ;;
  rm)
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    ensure_file
    jq_edit --arg n "$name" 'del(.sessions[$n])'
    t kill-session -t "=$name" 2>/dev/null || true
    prune_filter "$name"
    echo "session '$name' removed"
    ;;
  stop)
    # Park a listed session (issue #167): flag it stopped FIRST so the
    # supervisor's post-spawn re-check catches a spawn racing this kill,
    # then take the live session down. The entry (agent, cwd, transcript
    # id) stays listed for a later 'restart' to revive.
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    ensure_file
    if ! taken "$name"; then
      echo "no such session: '$name' (see agent-box-session ls)" >&2
      exit 2
    fi
    jq_edit --arg n "$name" '.sessions[$n].stopped = true'
    t kill-session -t "=$name" 2>/dev/null || true
    echo "session '$name' stopped — still listed, not respawned; 'agent-box-session restart $name' revives it"
    ;;
  restart)
    # Clearing the stopped flag makes restart double as the revive verb
    # for parked sessions; kill-session tolerates one with nothing live.
    if [ "${1:-}" = "--all" ]; then
      ensure_file
      jq_edit 'del(.sessions[].stopped)'
      "$JQ" -r '.sessions | keys[]' "$FILE" | while IFS= read -r n; do
        [ -n "$n" ] && t kill-session -t "=$n" 2>/dev/null || true
      done
      echo "all sessions killed — the supervisor restarts each within ~2s (re-reading env)"
    else
      name="${1:-}"
      valid_name "$name" || { usage >&2; exit 2; }
      ensure_file
      if taken "$name"; then
        jq_edit --arg n "$name" 'del(.sessions[$n].stopped)'
        # || true: a stopped session has no live tmux session to kill.
        t kill-session -t "=$name" 2>/dev/null || true
        echo "session '$name' killed — the supervisor restarts it within ~2s"
      else
        # Unlisted (hand-started) session: the kill is all there is, and
        # its own error covers the name-typo case.
        t kill-session -t "=$name"
        echo "session '$name' killed — unlisted, so nothing restarts it"
      fi
    fi
    ;;
  env)
    # Manages the same ~/.config/agent-box/env the settings page writes and
    # the env-exec wrapper reads at every session spawn. Applies on the next
    # (re)start — see 'restart'. ls shows KEYS only, never values (matching
    # the settings page, which never surfaces a stored secret).
    ENV_FILE="$HOME/.config/agent-box/env"
    env_header() {
      printf '# Managed by agent-box settings page. KEY=value, one per line.\n'
      printf '# Do not add secrets by hand here unless you know what you are doing.\n'
    }
    env_rewrite() {
      # env_rewrite DROP_KEY [APPEND_KEY APPEND_VALUE] — atomically rewrite
      # ENV_FILE dropping DROP_KEY, keeping every other valid KEY=value, then
      # optionally appending a fresh pair.
      mkdir -p "$(dirname "$ENV_FILE")"
      tmp="$(mktemp "$ENV_FILE.XXXXXX")"
      { env_header
        if [ -f "$ENV_FILE" ]; then
          while IFS= read -r line; do
            case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
            ek="${line%%=*}"
            valid_key "$ek" || continue
            [ "$ek" = "$1" ] && continue
            printf '%s\n' "$line"
          done < "$ENV_FILE"
        fi
        if [ $# -ge 3 ]; then printf '%s=%s\n' "$2" "$3"; fi
      } > "$tmp"
      chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
    }
    sub="${1:-}"; shift || true
    case "$sub" in
      ls)
        [ -f "$ENV_FILE" ] || exit 0
        while IFS= read -r line; do
          case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
          k="${line%%=*}"
          valid_key "$k" && printf '%s\n' "$k"
        done < "$ENV_FILE" | sort -u
        ;;
      set)
        k="${1:-}"; v="${2-}"
        valid_key "$k" || { echo "invalid key '$k' (use letters, digits, underscore; not starting with a digit)" >&2; exit 2; }
        case "$v" in (*"
"*) echo "value may not contain a newline" >&2; exit 2 ;; esac
        env_rewrite "$k" "$k" "$v"
        echo "env '$k' set — applies on the next session (re)start ('agent-box-session restart --all')"
        ;;
      rm)
        k="${1:-}"
        valid_key "$k" || { usage >&2; exit 2; }
        [ -f "$ENV_FILE" ] || exit 0
        env_rewrite "$k"
        echo "env '$k' removed — applies on the next session (re)start ('agent-box-session restart --all')"
        ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
