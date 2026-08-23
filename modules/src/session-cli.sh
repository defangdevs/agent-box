set -eu
# jq/tmux resolve from PATH (system packages + every agent unit's PATH);
# the installed-agent list and default come from the AGENT_BOX_* env the
# generated wrapper exports (issue #154, Phase 2).
JQ=jq
FILE="$HOME/.config/agent-box/sessions.json"
AGENTS="${AGENT_BOX_AGENTS:?}"
DEFAULT_AGENT="${AGENT_BOX_DEFAULT_AGENT:?}"
# NOT ${TMUX_TMPDIR:-...}: the socket dir is the agent unit's
# RuntimeDirectory, never the caller's to choose (issue #268). Deferring to
# an inherited value let any system-wide setting repoint the whole CLI at an
# empty directory, where every verb finds no sessions and reports success --
# `programs.tmux` with secureSocket on exports TMUX_TMPDIR through
# /etc/profile, so an SSH login shell did exactly that.
export TMUX_TMPDIR="/run/agent-box-${USER:-$(id -un)}"

t() { tmux -L agent-box "$@"; }

# A kill that did not happen must never be reported as a removal (issue
# #268): the entry leaves sessions.json while the session keeps running, and
# nothing collects it afterwards -- the supervisor's reconcile loop is
# ensure-only and never kills. But "already gone" is success for every caller
# here, and it arrives two ways: no such session, or no server at all (killing
# the LAST session takes the server down with it, so `restart main` on a
# single-session box legitimately finds nothing). Ask tmux what is true now
# instead of matching its error text, which differs by version.
kill_session() {
  kerr="$(t kill-session -t "=$1" 2>&1)" && return 0
  t has-session -t "=$1" 2>/dev/null || return 0
  echo "agent-box-session: could not kill tmux session '$1': $kerr" >&2
  return 1
}

usage() {
  echo "usage: agent-box-session ls"
  echo "       agent-box-session add [NAME] [--agent AGENT] [--profile PROFILE] [--cwd DIR]"
  echo "                             [--prompt TEXT] [--resume-prompt TEXT] [-- EXTRA_ARGS...]"
  echo "       agent-box-session rm NAME"
  echo "       agent-box-session stop NAME"
  echo "       agent-box-session restart NAME | --all"
  echo "       agent-box-session env ls | set KEY VALUE | rm KEY"
  echo "NAME: letters, digits, '_' and '-', at most $NAME_MAX characters (a"
  echo "longer name would be invisible in the web UI)."
  echo "agents: $AGENTS (default: $DEFAULT_AGENT) — the HARNESS to run."
  echo "--profile names an agent profile (agent-box-profile ls): a harness plus"
  echo "a model, an effort level, an appended system prompt and session env."
  echo "--agent and a '-- EXTRA_ARGS' tail override what the profile resolved."
  echo "--prompt kicks the session off with a task (first spawn only); a later"
  echo "respawn resumes the prior transcript instead of redoing it."
  echo "Listed sessions are (re)started by the per-user supervisor within ~2s."
  echo "stop parks a session (no respawn; an agent quitting cleanly does the"
  echo "same) until 'restart NAME' revives it; rm delists it for good."
  echo "Attach: tmux -L agent-box attach -t NAME, or the browser terminal /<user>/?arg=NAME"
}
# The settings daemon's SESSION_RE bounds a name at NAME_MAX and DROPS what
# does not match, so a longer session runs, holds subscriptions and receives
# events while having no row in the Sessions list and none in the webhook
# panel — it cannot be attached, restarted or deleted from the web UI, and
# its claimed topic silences a standing watch for a session nobody can see
# (issue #236). Keep this number equal to the daemon's: it is the length
# every name-minting path is allowed to reach (hook-<owner/repo>-<4 hex> for
# GitHub's maxima), never a length names get shortened to fit.
NAME_MAX=150
valid_name() {
  case "$1" in (*[!A-Za-z0-9_-]*|"") return 1 ;; esac
}
valid_new_name() {
  # The length rule belongs on CREATION only — this is the one gate every
  # creation path passes through (add, and the webhook spawn wrapper through
  # it), so refusing here is what keeps such a name from existing. rm, stop
  # and restart deliberately keep to the charset: a name minted before this
  # bound existed, or written into sessions.json by hand, is invisible in the
  # UI and the CLI is the only way left to get rid of it.
  valid_name "$1" && [ "${#1}" -le "$NAME_MAX" ]
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
  # file must stay: it IS that session's subscriptions, so deleting it
  # silently unsubscribes a session that is still running. (Before
  # local-webhook 0.13.0 it was worse than that — routing failed open, so the
  # deletion handed that session the whole bus instead.) Removing a dead
  # session's file is the right end state and not merely tidy: the name is
  # reusable, and a later 'add' of the same name would otherwise inherit the
  # dead session's subscriptions.
  _sd="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
  rm -f "$_sd/filter.$(id -un)-$1.json"
}
session_state_file() {
  # session_state_file NAME — the supervisor's per-session observations
  # (issue #282), spelled in one place per program: this accessor, the
  # supervisor's function of the same name, and the settings daemon's
  # session_state_path. Keying on the session NAME is a placeholder for a
  # harness-minted id (issue #284), and going through an accessor is what
  # makes that re-key a change to three functions rather than a migration.
  printf '%s/%s.json\n' "$HOME/.local/state/agent-box/session" "$1"
}
prune_session_state() {
  # Only ever called for a name that has just been DELISTED, and only as an
  # OPTIMISATION: the supervisor sweeps this directory against the registry
  # on every reconcile tick, because nothing guarantees anybody runs `rm` at
  # all. What the prune buys is the window in between — `rm foo` followed
  # straight by `add foo` would otherwise hand the new session the dead
  # one's launch id, and with it the dead one's transcript.
  rm -f "$(session_state_file "$1")"
}
# Serialize the read-modify-write of the session registry (issue #254). Every
# writer of this file — this CLI, the supervisor, the mark-stopped epilogue,
# the settings daemon, the webhook spawn wrapper — replaces it by rename, so a
# reader always gets a whole document but two writers that read the same base
# each publish one that never contained the other's edit. Two writers of the
# jq_edit shape below lost 96 of 300 updates when measured on the live box.
# Hence a SIDECAR lock file: locking sessions.json itself would lock the inode
# the next writer is about to replace, which is not the lock it takes.
#
# flock ships in util-linux ONLY, which is not on every PATH this CLI runs
# from (a plain `su -c 'agent-box-session ...'` gets the system PATH, the
# webhook receiver unit's PATH has jq + coreutils + this CLI and nothing
# else), so the generated wrapper pins the binary — the AGENT_BOX_*_BIN
# convention. Unset means no lock and no error: a session must still be
# addable on a box whose module predates this.
LOCK_FILE="$FILE.lock"
FLOCK="${AGENT_BOX_FLOCK_BIN:-}"
_lock_depth=0
registry_lock() {
  # Two cases skip the open. Nesting: flock(2) conflicts between two open file
  # descriptions of the SAME process, so re-locking inside a held section
  # would deadlock (a caller wraps check-then-write around jq_edit, which
  # locks too). Inherited: agent-box-webhook-spawn holds this lock across the
  # `exec` into `add` so its hook-session cap check and the add are one step —
  # it hands the fd over and says so through the environment, and re-opening
  # would first CLOSE that fd and drop the lock it took.
  _lock_depth=$((_lock_depth + 1))
  [ "$_lock_depth" = 1 ] || return 0
  [ "${AGENT_BOX_REGISTRY_LOCK_FD:-}" != 9 ] || return 0
  [ -n "$FLOCK" ] || return 0
  { exec 9>>"$LOCK_FILE"; } 2>/dev/null || return 0
  # -w so a wedged holder degrades to the old lost-update behaviour instead of
  # hanging a CLI the user is waiting on.
  "$FLOCK" -w 10 9 \
    || echo "agent-box-session: sessions.json lock timed out; continuing unlocked (issue #254)" >&2
}
registry_unlock() {
  [ "$_lock_depth" -gt 0 ] || return 0
  _lock_depth=$((_lock_depth - 1))
  [ "$_lock_depth" = 0 ] || return 0
  [ "${AGENT_BOX_REGISTRY_LOCK_FD:-}" != 9 ] || return 0
  exec 9>&- 2>/dev/null || true
}
jq_edit() {
  # jq_edit JQ_ARGS... — atomically rewrite FILE through jq, under the
  # registry lock so the read and the rename are one step.
  registry_lock
  tmp="$(mktemp "$FILE.XXXXXX")"
  if "$JQ" "$@" < "$FILE" > "$tmp"; then
    mv "$tmp" "$FILE"
  else
    rm -f "$tmp"
    exit 1
  fi
  registry_unlock
}
taken() { "$JQ" -e --arg n "$1" '.sessions | has($n)' "$FILE" >/dev/null; }
gen_name() {
  # gen_name AGENT [CWD] — echo a unique session name derived from AGENT: the
  # bare name when free ("claude"), else the working directory's own name
  # ("portal", then "portal-2"). Callers pass a validated agent, itself a
  # valid name; CWD is whatever the caller will store, so it is untrusted
  # text and gets folded into the name charset here.
  #
  # A random "claude-a3f9" named nothing an operator could recognise, and the
  # web UI's row shows only name, agent, cwd and state — so two auto-named
  # claude sessions under one project tree were indistinguishable and the
  # wrong transcript got downloaded (issue #277). The directory name is the
  # one fact that says WHERE this session works.
  a="$1"
  taken "$a" || { printf '%s' "$a"; return; }
  # HOME is deliberately not used: its basename is the user's own name, which
  # says nothing (every default session sits there), so those keep the hex.
  base="${2:-}"
  [ "$base" != "$HOME" ] || base=""
  base="${base%/}"
  base="${base##*/}"
  base="${base//[!A-Za-z0-9_-]/-}"     # bash-only, like the $RANDOM below
  base="${base#-}"
  base="${base%-}"
  if [ -n "$base" ] && [ "${#base}" -le "$((NAME_MAX - 2))" ]; then
    taken "$base" || { printf '%s' "$base"; return; }
    n=2
    while [ "$n" -le 9 ]; do
      cand="$base-$n"
      taken "$cand" || { printf '%s' "$cand"; return; }
      n=$((n + 1))
    done
  fi
  # Last resort: no usable directory name, or nine sessions already work in
  # that directory. Random cannot collide the way a tenth "-N" guess would.
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
      *) name="$1"; shift; valid_new_name "$name" || { usage >&2; exit 2; } ;;
    esac
    agent="$DEFAULT_AGENT"; cwd=""; prompt=""; rprompt=""; has_prompt=0; has_rprompt=0
    profile=""; has_agent=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --agent) agent="${2:?--agent needs a value}"; has_agent=1; shift 2 ;;
        --profile) profile="${2:?--profile needs a value}"; shift 2 ;;
        --cwd) cwd="${2:?--cwd needs a value}"; shift 2 ;;
        --prompt) prompt="${2?--prompt needs a value}"; has_prompt=1; shift 2 ;;
        --resume-prompt) rprompt="${2?--resume-prompt needs a value}"; has_rprompt=1; shift 2 ;;
        --) shift; break ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    # An agent PROFILE (issue #321) resolves to a harness plus the arguments
    # that make this session a particular worker — model, effort, appended
    # system prompt. Resolved by agent-box-profile, the one place that mapping
    # lives, and resolved NOW rather than at every spawn: what a session was
    # started with must not change under it when the profile is later edited
    # (the same rule webhook.hookSessionArgs states). The profile's remaining
    # keys are session ENVIRONMENT, which the env-exec wrapper applies at each
    # spawn from the profile name recorded below — so a rotated token in a
    # profile reaches the session on its next restart.
    #
    # The binary is pinned by the generated wrapper (AGENT_BOX_PROFILE_BIN):
    # this CLI also runs from the webhook receiver unit's PATH, which carries
    # jq, coreutils and this script and nothing else.
    profile_args=""
    if [ -n "$profile" ]; then
      # --agent on the command line wins over the profile's harness, the same
      # override order the env file has over the NixOS option elsewhere here —
      # and it is passed INTO the resolver, because the arguments a profile
      # resolves to are harness-specific (`--model` for claude, `-m` for
      # codex, none at all for a shell session).
      povr=""
      [ "$has_agent" = 1 ] && povr="$agent"
      pjson="$("${AGENT_BOX_PROFILE_BIN:-agent-box-profile}" launch "$profile" "$povr")" || exit 2
      agent="$("$JQ" -r '.harness' <<<"$pjson")"
      "$JQ" -r --arg p "$profile" \
        '.warnings[] | "agent-box-session: profile \($p): " + .' <<<"$pjson" >&2
      # Profile args go FIRST so an explicit `-- EXTRA_ARGS` tail still has the
      # last word (both harness CLIs take the last occurrence of a flag).
      profile_args="$("$JQ" -r '.args[]' <<<"$pjson")"
    fi
    case " $AGENTS " in
      (*" $agent "*) ;;
      (*) echo "agent '$agent' is not available (available: $AGENTS)" >&2; exit 2 ;;
    esac
    ensure_file
    # Name choice and write are one critical section (issue #254): gen_name
    # and taken() both decide from a READ of the file, so two concurrent adds
    # could pick the same free name and the second rename would drop the first
    # session outright.
    registry_lock
    [ -n "$name" ] || name="$(gen_name "$agent" "$cwd")"
    if taken "$name"; then
      echo "session '$name' already exists — 'agent-box-session rm $name' first, or 'restart $name' to bounce it" >&2
      exit 2
    fi
    # The id this session's FIRST spawn is launched with (Claude
    # --session-id / --resume; Codex transcript marker). Not a stable handle
    # on the conversation: a clear, a compact or a resume rotates the agent
    # onto a NEW segment, and the supervisor adopts that id in its own side
    # file (issue #282) — so this is where the session starts, not what it
    # is. Minted here so it is set before the first spawn; the supervisor
    # mints one too for legacy sessions.
    bid=""
    [ -r /proc/sys/kernel/random/uuid ] && read -r bid < /proc/sys/kernel/random/uuid || true
    # `--` after --args: jq otherwise still option-parses positional
    # args, so a dashed extra arg like --model would error out.
    # One argument vector: the profile's args first, the caller's own `--`
    # tail after them, so an explicit flag still has the last word.
    sargs=()
    if [ -n "$profile_args" ]; then
      while IFS= read -r parg; do
        sargs+=("$parg")
      done <<<"$profile_args"
    fi
    sargs+=("$@")
    jq_edit --arg n "$name" --arg a "$agent" --arg c "$cwd" \
      --arg p "$prompt" --arg pp "$has_prompt" \
      --arg rp "$rprompt" --arg rpp "$has_rprompt" --arg bid "$bid" \
      --arg prof "$profile" \
      '.sessions[$n] = {agent: $a, skipPermissions: true, remoteControl: true,
                        remoteControlName: null,
                        workingDirectory: (if $c == "" then null else $c end),
                        extraArgs: $ARGS.positional,
                        profile: (if $prof == "" then null else $prof end),
                        initialPrompt: (if $pp == "1" then $p else null end),
                        resumePrompt: (if $rpp == "1" then $rp else null end),
                        boxSessionId: (if $bid == "" then null else $bid end),
                        hasRun: false}' \
      --args -- "${sargs[@]}"
    registry_unlock
    # The mascot (issue #185) marks the closest thing this CLI has to
    # "an agent just started". Small on purpose: this runs in webhook
    # spawns and scripts too, and nothing parses the lines below it.
    printf '%s\n' \
        "   .-~~-." \
        "  ( (o) )" \
        "   \`-~~-'"
    what="$agent"
    [ -n "$profile" ] && what="$agent, profile $profile"
    if [ "$has_prompt" = 1 ]; then
      echo "session '$name' ($what) added with a kickoff prompt — the supervisor starts it within ~2s"
    else
      echo "session '$name' ($what) added — the supervisor starts it within ~2s"
    fi
    ;;
  rm)
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    ensure_file
    jq_edit --arg n "$name" 'del(.sessions[$n])'
    kill_session "$name" || exit 1
    prune_filter "$name"
    prune_session_state "$name"
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
    # Existence check and flag write together (issue #254): jq's assignment
    # CREATES a missing key, so a session deleted between the two came back as
    # a stub {stopped: true} — listed forever, startable by nobody, and the
    # name is then taken for a later add.
    registry_lock
    if ! taken "$name"; then
      echo "no such session: '$name' (see agent-box-session ls)" >&2
      exit 2
    fi
    jq_edit --arg n "$name" '.sessions[$n].stopped = true'
    registry_unlock
    kill_session "$name" || exit 1
    echo "session '$name' stopped — still listed, not respawned; 'agent-box-session restart $name' revives it"
    ;;
  restart)
    # Clearing the stopped flag makes restart double as the revive verb
    # for parked sessions; kill-session tolerates one with nothing live.
    if [ "${1:-}" = "--all" ]; then
      ensure_file
      jq_edit 'del(.sessions[].stopped)'
      "$JQ" -r '.sessions | keys[]' "$FILE" | while IFS= read -r n; do
        [ -n "$n" ] && kill_session "$n" || true
      done
      echo "all sessions killed — the supervisor restarts each within ~2s (re-reading env)"
    else
      name="${1:-}"
      valid_name "$name" || { usage >&2; exit 2; }
      ensure_file
      # Listed-or-not decides which branch runs, so read it under the lock
      # (issue #254) — the flag write must apply to the file the check saw.
      registry_lock
      if taken "$name"; then
        jq_edit --arg n "$name" 'del(.sessions[$n].stopped)'
        registry_unlock
        # A stopped session has no live tmux session to kill; kill_session
        # treats that "can't find session" as success.
        kill_session "$name" || exit 1
        echo "session '$name' killed — the supervisor restarts it within ~2s"
      else
        registry_unlock
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
