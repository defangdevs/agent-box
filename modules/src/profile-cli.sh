set -eu
# jq resolves from PATH (system packages + every agent unit's PATH); the
# installed-harness list comes from the AGENT_BOX_* env the generated wrapper
# exports, exactly as src/session-cli.sh does (issue #154, Phase 2).
JQ=jq
# An agent PROFILE (issue #321): the concrete worker, as opposed to the
# HARNESS (claude/codex/shell) that `agent-box-session --agent` selects. A
# profile names a harness plus the knobs that make two sessions on the same
# harness different workers — model, effort, an appended system prompt, and
# environment for the session.
#
# Runtime data, not a NixOS option: this is per-user preference a user must be
# able to change from chat with no root and no rebuild (issue #290/#291's
# precedent), so it lives beside ~/.config/agent-box/env in the same KEY=VALUE
# format the settings page and `agent-box-session env` already write.
DIR="$HOME/.config/agent-box/profiles"
HARNESSES="${AGENT_BOX_AGENTS:?}"

# Reserved keys are the LAUNCH config: this script turns them into harness
# arguments. Every other key in the file is environment for a session started
# with the profile (see the warning in usage() — env is not a boundary).
RESERVED="HARNESS MODEL EFFORT SYSTEM_PROMPT"

usage() {
  echo "usage: agent-box-profile ls"
  echo "       agent-box-profile show NAME"
  echo "       agent-box-profile set NAME KEY=VALUE..."
  echo "       agent-box-profile rm NAME [KEY...]"
  echo "       agent-box-profile launch NAME [HARNESS]   (JSON, for agent-box-session)"
  echo "A profile is a worker: a harness plus the knobs that tell two sessions"
  echo "on that harness apart. Start one with:"
  echo "       agent-box-session add [NAME] --profile PROFILE"
  echo "Reserved keys (turned into harness arguments):"
  echo "  HARNESS        $HARNESSES"
  echo "  MODEL          claude: --model VALUE; codex: -m VALUE"
  echo "  EFFORT         claude: --effort VALUE; codex: -c model_reasoning_effort=VALUE"
  echo "  SYSTEM_PROMPT  claude: --append-system-prompt VALUE"
  echo "Any OTHER key is environment for sessions started with this profile,"
  echo "applied at spawn on top of 'agent-box-session env'. It is convenience,"
  echo "NOT isolation: a sibling session on this account reads it out of"
  echo "/proc/<pid>/environ (issue #135, wiki: Users-vs-Sessions), so a secret"
  echo "in a profile is a secret every session of this user has."
  echo "NAME: letters, digits, '_' and '-', at most $NAME_MAX characters."
  echo "Changes apply to sessions started AFTERWARDS: a running session keeps"
  echo "the arguments and environment it started with."
}

NAME_MAX=64
valid_name() {
  case "$1" in (*[!A-Za-z0-9_-]*|"") return 1 ;; esac
  [ "${#1}" -le "$NAME_MAX" ]
}
valid_key() {
  # Same charset as the env store and the settings daemon's KEY_RE: letters,
  # digits, underscore, not starting with a digit.
  case "$1" in (*[!A-Za-z0-9_]*|""|[0-9]*) return 1 ;; esac
}
reserved_key() {
  case " $RESERVED " in (*" $1 "*) return 0 ;; esac
  return 1
}
file_for() { printf '%s/%s.env\n' "$DIR" "$1"; }

# read_profile NAME — set the res_<KEY> variables for the reserved keys and
# collect the remaining key NAMES in env_keys. Same defensive parse as the
# env-exec wrapper (issue #212): a line that is not KEY=value is skipped
# rather than interpreted, so a hand-edited file cannot run anything.
read_profile() {
  res_HARNESS=""; res_MODEL=""; res_EFFORT=""; res_SYSTEM_PROMPT=""
  env_keys=""
  pf="$(file_for "$1")"
  [ -r "$pf" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
    k=${line%%=*}
    valid_key "$k" || continue
    v=${line#*=}
    case "$v" in
      \"*\") v=${v#\"}; v=${v%\"} ;;
      \'*\') v=${v#\'}; v=${v%\'} ;;
    esac
    if reserved_key "$k"; then
      eval "res_$k=\$v"
    else
      env_keys="$env_keys $k"
    fi
  done < "$pf"
}

# launch_json NAME — {harness, args, envKeys, warnings} for the resolved
# profile. ONE resolver, because two would drift: `agent-box-session add
# --profile` reads it to pick the harness and the arguments it stores, and
# `show` prints the same answer so what an operator reads is what a session
# gets. The harness-specific mapping lives here rather than in the supervisor
# on purpose — the arguments are resolved once, when the session is created,
# and a later profile edit does not silently re-arm a running session (the
# same rule webhook.hookSessionArgs already states).
launch_json() {
  read_profile "$1" || { echo "no such profile: '$1' (see agent-box-profile ls)" >&2; return 2; }
  # $2 = a harness that overrides the profile's own (agent-box-session add
  # --profile P --agent H). It has to be resolved HERE and not corrected
  # afterwards: the arguments are harness-specific, so a profile resolved for
  # codex and then started on another harness would hand it codex's flags.
  h="${2:-}"
  [ -n "$h" ] || h="$res_HARNESS"
  [ -n "$h" ] || h="${AGENT_BOX_DEFAULT_AGENT:-}"
  warn=""
  case " $HARNESSES " in
    (*" $h "*) ;;
    (*) echo "profile '$1': harness '$h' is not available (available: $HARNESSES)" >&2; return 2 ;;
  esac
  set --
  case "$h" in
    claude)
      [ -n "$res_MODEL" ] && set -- "$@" --model "$res_MODEL"
      [ -n "$res_EFFORT" ] && set -- "$@" --effort "$res_EFFORT"
      [ -n "$res_SYSTEM_PROMPT" ] && set -- "$@" --append-system-prompt "$res_SYSTEM_PROMPT"
      ;;
    codex)
      [ -n "$res_MODEL" ] && set -- "$@" -m "$res_MODEL"
      # codex takes reasoning effort as a config override, not a flag.
      [ -n "$res_EFFORT" ] && set -- "$@" -c "model_reasoning_effort=$res_EFFORT"
      # codex has no --append-system-prompt equivalent; its instructions come
      # from AGENTS.md in the working directory. Say so instead of dropping
      # the key silently — a profile whose prompt never arrives is worse than
      # one that refuses to pretend.
      [ -n "$res_SYSTEM_PROMPT" ] && warn="$warn|SYSTEM_PROMPT is ignored for the codex harness (no --append-system-prompt equivalent); put the instructions in the working directory's AGENTS.md"
      ;;
    *)
      # shell, and any harness added later: no argument mapping exists, so
      # nothing is invented for it.
      for k in MODEL EFFORT SYSTEM_PROMPT; do
        eval "kv=\$res_$k"
        [ -n "$kv" ] && warn="$warn|$k is ignored for the '$h' harness"
      done
      ;;
  esac
  "$JQ" -n --arg h "$h" --arg ek "$env_keys" --arg w "$warn" \
    '{harness: $h,
      args: $ARGS.positional,
      envKeys: ($ek | split(" ") | map(select(length > 0))),
      warnings: ($w | split("|") | map(select(length > 0)))}' \
    --args -- "$@"
}

# rewrite NAME [KEY VALUE]... — atomically rewrite the profile file, dropping
# every key named in the (whitespace-separated) DROP list and appending the
# pairs given. Same shape as session-cli's env_rewrite, and 600 for the same
# reason: a profile may carry environment.
DROP=""
rewrite() {
  pname="$1"; shift
  f="$(file_for "$pname")"
  mkdir -p "$DIR"
  chmod 700 "$DIR" 2>/dev/null || true
  tmp="$(mktemp "$f.XXXXXX")"
  { printf '# agent-box agent profile "%s" — managed by agent-box-profile.\n' "$pname"
    printf '# KEY=value, one per line. HARNESS/MODEL/EFFORT/SYSTEM_PROMPT become\n'
    printf '# harness arguments; any other key becomes session environment, which\n'
    printf '# every session of this user can read (issue #135).\n'
    if [ -f "$f" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
        ek="${line%%=*}"
        valid_key "$ek" || continue
        skip=0
        for d in $DROP; do [ "$ek" = "$d" ] && skip=1; done
        [ "$skip" = 1 ] && continue
        printf '%s\n' "$line"
      done < "$f"
    fi
    while [ $# -ge 2 ]; do
      printf '%s=%s\n' "$1" "$2"
      shift 2
    done
  } > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$f"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  ls)
    [ -d "$DIR" ] || exit 0
    printf '%-20s %-8s %-18s %s\n' NAME HARNESS MODEL EFFORT
    for f in "$DIR"/*.env; do
      [ -f "$f" ] || continue
      n="${f##*/}"; n="${n%.env}"
      valid_name "$n" || continue
      read_profile "$n" || continue
      printf '%-20s %-8s %-18s %s\n' "$n" "${res_HARNESS:-${AGENT_BOX_DEFAULT_AGENT:-?}}" \
        "${res_MODEL:--}" "${res_EFFORT:--}"
    done
    ;;
  show)
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    read_profile "$name" || { echo "no such profile: '$name' (see agent-box-profile ls)" >&2; exit 2; }
    j="$(launch_json "$name")" || exit 2
    printf 'profile %s (%s)\n' "$name" "$(file_for "$name")"
    printf '  HARNESS        %s\n' "$("$JQ" -r '.harness' <<<"$j")"
    printf '  MODEL          %s\n' "${res_MODEL:--}"
    printf '  EFFORT         %s\n' "${res_EFFORT:--}"
    printf '  SYSTEM_PROMPT  %s\n' "${res_SYSTEM_PROMPT:--}"
    # Environment KEYS only, never their values — the settings page and
    # `agent-box-session env ls` hold the same line, and a profile is where a
    # token is most likely to sit.
    if [ -n "$env_keys" ]; then
      printf '  env            %s\n' "$(printf '%s' "${env_keys# }")"
    else
      printf '  env            (none)\n'
    fi
    printf 'Launch: %s %s\n' "$("$JQ" -r '.harness' <<<"$j")" \
      "$("$JQ" -r '.args | map(@sh) | join(" ")' <<<"$j")"
    "$JQ" -r '.warnings[] | "warning: " + .' <<<"$j"
    printf 'Start it: agent-box-session add --profile %s\n' "$name"
    ;;
  set)
    name="${1:-}"; shift || true
    valid_name "$name" || { echo "invalid profile name '$name' (letters, digits, '_' and '-', at most $NAME_MAX characters)" >&2; exit 2; }
    [ $# -gt 0 ] || { usage >&2; exit 2; }
    # Validate every assignment BEFORE writing any of them: a `set` that
    # stored the first two of three pairs and then failed would leave a
    # profile nobody asked for.
    pairs=()
    DROP=""
    for a in "$@"; do
      case "$a" in (*=*) ;; (*) echo "not a KEY=VALUE assignment: '$a'" >&2; exit 2 ;; esac
      k="${a%%=*}"; v="${a#*=}"
      valid_key "$k" || { echo "invalid key '$k' (use letters, digits, underscore; not starting with a digit)" >&2; exit 2; }
      case "$v" in (*"
"*) echo "value for '$k' may not contain a newline" >&2; exit 2 ;; esac
      if [ "$k" = HARNESS ]; then
        case " $HARNESSES " in
          (*" $v "*) ;;
          (*) echo "harness '$v' is not available (available: $HARNESSES)" >&2; exit 2 ;;
        esac
      fi
      DROP="$DROP $k"
      pairs+=("$k" "$v")
    done
    rewrite "$name" "${pairs[@]}"
    echo "profile '$name' updated — 'agent-box-session add --profile $name' starts a session with it"
    # Report what the profile now launches, so a key the harness cannot use
    # shows up here rather than silently at the next spawn.
    j="$(launch_json "$name")" || exit 0
    "$JQ" -r '.warnings[] | "warning: " + .' <<<"$j"
    ;;
  rm)
    name="${1:-}"; shift || true
    valid_name "$name" || { usage >&2; exit 2; }
    f="$(file_for "$name")"
    [ -f "$f" ] || { echo "no such profile: '$name' (see agent-box-profile ls)" >&2; exit 2; }
    if [ $# -eq 0 ]; then
      rm -f "$f"
      echo "profile '$name' removed — sessions already running with it are unaffected"
    else
      DROP=""
      for k in "$@"; do
        valid_key "$k" || { usage >&2; exit 2; }
        DROP="$DROP $k"
      done
      rewrite "$name"
      echo "profile '$name': removed$DROP"
    fi
    ;;
  launch)
    # The machine-readable half of `show`, for `agent-box-session add
    # --profile`. Warnings go to stderr there too, so the operator sees them
    # once, on the CLI they ran. The optional second argument is the harness
    # the caller settled on (`add --profile P --agent H`), which decides the
    # argument mapping.
    name="${1:-}"; shift || true
    valid_name "$name" || { usage >&2; exit 2; }
    launch_json "$name" "${1:-}" || exit 2
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
