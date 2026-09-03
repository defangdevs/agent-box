set -eu
# jq/tmux resolve from PATH (system packages + every agent unit's PATH);
# the installed-agent list and default come from the AGENT_BOX_* env the
# generated wrapper exports (issue #154, Phase 2).
JQ=jq
# The session registry — where it lives, how it is locked and how it is
# rewritten — is one file every shell writer splices in (issue #254). This CLI
# is one of five writers, and the lock it takes has to be the same lock the
# supervisor, the pane epilogue, the webhook spawner and the settings daemon
# take, or it is not a lock.
REGISTRY_PROG=agent-box-session
@@include:lib/registry.sh@@
AGENTS="${AGENT_BOX_AGENTS:?}"
DEFAULT_AGENT="${AGENT_BOX_DEFAULT_AGENT:?}"
# NOT ${TMUX_TMPDIR:-...}: the socket dir is the agent unit's
# RuntimeDirectory, never the caller's to choose (issue #268). Deferring to
# an inherited value let any system-wide setting repoint the whole CLI at an
# empty directory, where every verb finds no sessions and reports success --
# `programs.tmux` with secureSocket on exports TMUX_TMPDIR through
# /etc/profile, so an SSH login shell did exactly that.
export TMUX_TMPDIR="/run/agent-box-${USER:-$(id -un)}"

# tmux, which is NOT on every PATH this CLI runs from: the webhook receiver
# unit's PATH is jq, coreutils and this script, deliberately (src/webhook-spawn.sh
# says so where it pins the same binary). A `tmux` that cannot be run makes
# every verb here answer as if no session were live — `ls` reports a running
# session as merely `starting` (issue #287), and `peers` would report a busy
# box as empty, which is the reading a yield rule must never get. So take the
# pinned path when the caller has one, exactly as the settings daemon and the
# spawn wrapper do; unset means the PATH tmux, which is what a pane has.
TMUX_BIN="${AGENT_BOX_TMUX_BIN:-tmux}"
t() { "$TMUX_BIN" -L agent-box "$@"; }

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
  echo "       agent-box-session peers"
  echo "       agent-box-session add [NAME] [--harness HARNESS] [--profile PROFILE]"
  echo "                             [--cwd DIR]"
  echo "                             [--prompt TEXT] [--resume-prompt TEXT] [--ephemeral]"
  echo "                             [-- EXTRA_ARGS...]"
  echo "       agent-box-session rm NAME"
  echo "       agent-box-session stop NAME"
  echo "       agent-box-session restart NAME | --all"
  echo "       agent-box-session env ls | set KEY VALUE | set KEY --stdin | rm KEY"
  echo "         (--stdin reads the value from stdin: for a multi-line secret"
  echo "          such as a PEM, and to keep any secret out of the command line)"
  echo "NAME: letters, digits, '_' and '-', at most $NAME_MAX characters, and"
  echo "not one of: $RESERVED_NAMES (each is already a path under /<user>/). (a"
  echo "longer name would be invisible in the web UI)."
  echo "harnesses: $AGENTS (default: $DEFAULT_AGENT) — the CLI PROGRAM to run."
  echo "--profile names an agent profile (agent-box-profile ls): a harness plus"
  echo "a model, an effort level, an appended system prompt and session env."
  echo "--harness and a '-- EXTRA_ARGS' tail override what the profile resolved."
  echo "(--agent is the old name for --harness and still works. It is"
  echo "deprecated: claude and opencode both spell --agent for the PROFILE,"
  echo "which is this box's --profile, so the two meanings collided.)"
  echo '--cwd is where the session starts (default $HOME, shared by every'
  echo "session). To work a repo another session is already in, give this one"
  echo "a checkout of its own: git worktree add ~/worktrees/NAME -b BRANCH."
  echo "--prompt kicks the session off with a task (first spawn only); a later"
  echo "respawn resumes the prior transcript instead of redoing it."
  echo "--ephemeral marks a ONE-SHOT session: parking it (a clean agent exit, or"
  echo "'stop') delists it outright, because nobody is going to resume it. A"
  echo "CRASH still parks nothing and leaves the post-mortem shell attachable."
  echo "The transcript is kept either way; only the registry entry goes."
  echo "Listed sessions are (re)started by the per-user supervisor within ~2s."
  echo "peers: the OTHER live sessions, where each one works and what it claims"
  echo "(its webhook subscriptions) — ask before you touch a worktree, a branch"
  echo "or an issue somebody else may already have."
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
# Names the vhost already spends on something else. A session's terminal
# lives at /<user>/<session>/, so a session called "settings" would collide
# with the settings page — the more specific route wins and the session
# becomes unreachable, with nothing on the page to say why. Mirrored by the
# daemon's RESERVED_NAMES and the module's session-name assertion.
RESERVED_NAMES="settings downloads webhook sessions token ws"
reserved_name() {
  for r in $RESERVED_NAMES; do
    [ "$1" = "$r" ] && return 0
  done
  return 1
}
valid_new_name() {
  # The length and reserved-name rules belong on CREATION only — this is the
  # one gate every creation path passes through (add, and the webhook spawn
  # wrapper through it), so refusing here is what keeps such a name from
  # existing. rm, stop and restart deliberately keep to the charset: a name
  # minted before these bounds existed, or written into sessions.json by
  # hand, is invisible in the UI and the CLI is the only way left to get rid
  # of it.
  valid_name "$1" && [ "${#1}" -le "$NAME_MAX" ] && ! reserved_name "$1"
}
valid_key() {
  # env var name charset — mirrors the settings daemon's KEY_RE and the
  # env-exec wrapper: letters/digits/underscore, not starting with a digit.
  case "$1" in (*[!A-Za-z0-9_]*|""|[0-9]*) return 1 ;; esac
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
taken() { "$JQ" -e --arg n "$1" '.sessions | has($n)' "$REGISTRY_FILE" >/dev/null; }
# Free to MINT, which is not the same question as `taken`: stop and start ask
# whether a session exists, and answering "yes" for a reserved name would have
# them write a stub entry under it. Only auto-naming asks this one — and it
# must, because the name can come from a WORKING DIRECTORY's own basename, so
# a session in ~/ws or ~/settings would otherwise be minted with exactly the
# name the vhost cannot route to a terminal.
mintable() {
  ! reserved_name "$1" && ! taken "$1"
}
gen_name() {
  # gen_name HARNESS [CWD] — echo a unique session name derived from HARNESS:
  # the bare name when free ("claude"), else the working directory's own name
  # ("portal", then "portal-2"). Callers pass a validated harness, itself a
  # valid name; CWD is whatever the caller will store, so it is untrusted
  # text and gets folded into the name charset here.
  #
  # A random "claude-a3f9" named nothing an operator could recognise, and the
  # web UI's row shows only name, agent, cwd and state — so two auto-named
  # claude sessions under one project tree were indistinguishable and the
  # wrong transcript got downloaded (issue #277). The directory name is the
  # one fact that says WHERE this session works.
  a="$1"
  mintable "$a" && { printf '%s' "$a"; return; }
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
    mintable "$base" && { printf '%s' "$base"; return; }
    n=2
    while [ "$n" -le 9 ]; do
      cand="$base-$n"
      mintable "$cand" && { printf '%s' "$cand"; return; }
      n=$((n + 1))
    done
  fi
  # Last resort: no usable directory name, or nine sessions already work in
  # that directory. Random cannot collide the way a tenth "-N" guess would.
  while :; do
    cand="$a-$(printf '%04x' $((RANDOM % 65536)))"
    mintable "$cand" && { printf '%s' "$cand"; return; }
  done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  ls)
    live="$(t list-sessions -F '#S' 2>/dev/null || true)"
    printf '%-24s %-8s %s\n' NAME HARNESS STATE
    if [ -s "$REGISTRY_FILE" ]; then
      # A fourth column the state needs but nobody prints: the exit status a
      # crashed pane recorded (issue #516). tmux says a post-mortem bash pane
      # is alive, so liveness alone cannot tell a dead agent from a running
      # one, and this used to report the crash as `live`.
      "$JQ" -r '.sessions | to_entries[] | [.key, (.value.agent // "?"), (if .value.stopped == true then "stopped" else "starting" end), (.value.died // "" | tostring)] | @tsv' "$REGISTRY_FILE" \
      | while IFS="$(printf '\t')" read -r n a state dead; do
        case $'\n'"$live"$'\n' in
          *$'\n'"$n"$'\n'*)
            if [ -n "$dead" ]; then state="died($dead)"; else state=live; fi ;;
        esac
        printf '%-24s %-8s %s\n' "$n" "$a" "$state"
      done
    fi
    # Live tmux sessions nobody listed (started by hand): show, don't hide.
    printf '%s\n' "$live" | while IFS= read -r n; do
      [ -n "$n" ] || continue
      if [ ! -s "$REGISTRY_FILE" ] || ! "$JQ" -e --arg n "$n" '.sessions | has($n)' "$REGISTRY_FILE" >/dev/null; then
        printf '%-24s %-8s %s\n' "$n" '-' 'unmanaged'
      fi
    done
    ;;
  peers)
    # Who ELSE is live, where they work, and what they claim.
    #
    # `ls` says which sessions EXIST. It does not say what any of them is
    # doing, and that is the question that decides whether a second session
    # may touch an object. Three facts settle it between cooperating agents,
    # and each one lives somewhere else: liveness in tmux, the working
    # directory in the registry, and the webhook claim in that session's own
    # filter file — which nothing else could read, because agent-box-webhook
    # only ever reads its OWN (defangdevs/local-channels#27). So a session
    # asking "is anybody already on this?" had no way to find out: PR #417's
    # dispatched session walked into a live session's git worktree and
    # committed over it, and said afterwards that nothing in
    # `agent-box-session ls` distinguished an owned worktree from an
    # abandoned one (issue #420).
    #
    # The caller that needs this most is a dispatched hook-* session: it was
    # started by an EVENT, not by a person, so it knows nothing about the
    # session that may already own the object. Its spawn preamble therefore
    # carries this output verbatim (src/webhook-spawn.sh) and tells it to
    # yield to any interactive session that has the work — the snapshot goes
    # stale as it runs, so the command exists for it to ask again.
    #
    # A READER, so it takes no lock (lib/registry.sh), and it must stay one:
    # the webhook spawn wrapper runs this while HOLDING the registry lock
    # across its exec into `add`, so a lock taken here would deadlock every
    # dispatch on the box.
    #
    # Coordination, never containment: a session is not a security boundary
    # (the wiki's Users-vs-Sessions page), and this tells a cooperating agent
    # what its neighbours are doing. It cannot stop one that ignores it.
    if ! "$TMUX_BIN" -V >/dev/null 2>&1; then
      # An empty answer is the honest one for a box whose tmux server is
      # down, but "I could not ask" must never read as "nobody is live" —
      # that is the reading that makes a yield rule fail open.
      echo "agent-box-session: cannot ask tmux which sessions are live (tmux did not run)" >&2
      exit 1
    fi
    live="$(t list-sessions -F '#S' 2>/dev/null || true)"
    # The caller's own session, so it is not reported as its own neighbour.
    #
    # The $TMUX guard is load-bearing, not a shortcut. `display-message` with
    # no target does not FAIL outside a pane: with no client to ask, tmux
    # falls back to the most recently active session and prints that name
    # (verified — two sessions on a fresh server, no $TMUX, and it answers
    # `beta`). Unguarded, a caller with no pane of its own — the webhook spawn
    # wrapper, a cron job, a `su -c` — would therefore drop the most recently
    # active session from the list, which is exactly the session most likely
    # to be the busy owner this command exists to reveal. So ask tmux only
    # when there IS a client; otherwise take the supervisor's own
    # LOCAL_WEBHOOK_SESSION (<user>-<session>), which every pane carries; and
    # failing both, exclude nothing, because then every live session really is
    # a peer.
    user="$(id -un)"
    me=""
    if [ -n "${TMUX:-}" ]; then
      me="$(t display-message -p '#S' 2>/dev/null || true)"
    fi
    if [ -z "$me" ] && [ -n "${LOCAL_WEBHOOK_SESSION:-}" ]; then
      me="${LOCAL_WEBHOOK_SESSION#"$user-"}"
    fi
    # The state dir local-webhook keeps its per-session filter files in,
    # spelled the same way prune_filter above spells it.
    sd="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
    peers=0
    out=""
    while IFS= read -r n; do
      [ -n "$n" ] || continue
      [ "$n" != "$me" ] || continue
      peers=$((peers + 1))
      harness="-"; cwd="-"; unmanaged=""; dead=""
      if [ -s "$REGISTRY_FILE" ]; then
        # One read per session, and a session tmux knows but the registry
        # does not is reported as `unmanaged` rather than skipped: it is
        # running, so it can be in your files (issue #284).
        meta="$("$JQ" -r --arg n "$n" '
          if (.sessions | has($n)) then
            [(.sessions[$n].agent // "-"),
             (.sessions[$n].workingDirectory // "-"),
             "",
             (.sessions[$n].died // "" | tostring)] | @tsv
          else "-\t-\tunmanaged\t" end' "$REGISTRY_FILE" 2>/dev/null)" || meta=""
        IFS="$(printf '\t')" read -r harness cwd unmanaged dead <<<"$meta"
        [ -n "$harness" ] || harness="-"
        [ -n "$cwd" ] || cwd="-"
        # No recorded working directory means the supervisor starts it in
        # $HOME — say that, rather than a dash a reader has to interpret. It
        # is where the session STARTED either way: an agent can cd anywhere,
        # so this narrows where to look and never proves where it is.
        [ "$cwd" != "-" ] || cwd="$HOME (its default)"
      fi
      # hook-* is the name every dispatched session gets (webhook-spawn.sh),
      # and the rank the yield rule turns on: a hook session was started by
      # an event, an interactive one by a person or by the box's own config.
      # A session tmux knows and the registry does not keeps BOTH facts: it
      # is running whoever started it, so it can be in your files.
      [ "$harness" != "-" ] || harness="harness unknown"
      kind="interactive"
      case "$n" in (hook-*) kind="dispatched (hook session)" ;; esac
      [ -z "$unmanaged" ] || kind="$kind, not in the registry"
      # A session whose agent CRASHED is still a live tmux session, because
      # the pane it left behind is a post-mortem shell (issue #516). Nobody
      # is working in it, so it is not a peer to yield to — and yielding to
      # one is exactly what this command exists to prevent.
      [ -z "$dead" ] \
        || kind="$kind, DIED (exit $dead) — its pane is a post-mortem shell, so nobody is working in it"
      out="$out$(printf '%s — %s, %s, cwd %s' "$n" "$harness" "$kind" "$cwd")"$'\n'
      ff="$sd/filter.$user-$n.json"
      claims=""
      if [ -s "$ff" ]; then
        # An entry with NO `include` predicate is deliberately not a claim —
        # the dispatcher spawns for such an event anyway (local-channels#16),
        # because one session must not silence a watch for every unrelated
        # object in a repo. Say which kind each topic is: "subscribed" and
        # "claimed" are different answers to "is this session on my object?".
        claims="$("$JQ" -r '
          if (.enabled // true) == false then
            "    claims nothing — its subscriptions are switched off"
          elif ((.topics // []) | length) == 0 then
            "    claims nothing — subscribed to no topic"
          else
            .topics[]
            | "    " + (if .include then "CLAIMS " else "listens to (no predicate, so not a claim) " end)
              + (.topic // "?")
              + (if (.note // "") == "" then ""
                 else " — note: \"" + ((.note | gsub("[\r\n\t]"; " "))[:200]) + "\"" end)
          end' "$ff" 2>/dev/null)" || claims=""
        [ -n "$claims" ] || claims="    claims nothing — its filter file is unreadable"
      else
        claims="    claims nothing — no subscription file, so an event on its work spawns a session beside it"
      fi
      out="$out$claims"$'\n'
    done <<<"$live"
    if [ "$peers" = 0 ]; then
      echo "No other session is live on this box."
    else
      printf '%s live session(s) beside you, and what each one says it is working on:\n' "$peers"
      printf '%s' "$out"
      # The reading that matters, printed with the facts rather than left in
      # a guide: this is what the rule in a hook session's spawn preamble
      # (src/webhook-spawn.sh) and in the shipped guide turn on.
      echo "A note is what that session chose to say; a CLAIM is what suppresses"
      echo "a standing watch. Neither proves the session is idle — read its pane"
      echo "(tmux -L agent-box capture-pane -pt NAME | tail -40) or ask it."
    fi
    ;;
  add)
    # NAME is optional and positional: a leading non-flag arg is the name,
    # otherwise the name is auto-derived from the agent below.
    name=""
    case "${1:-}" in
      ""|-*) ;;
      *) name="$1"; shift; valid_new_name "$name" || { usage >&2; exit 2; } ;;
    esac
    harness="$DEFAULT_AGENT"; cwd=""; prompt=""; rprompt=""; has_prompt=0; has_rprompt=0
    profile=""; has_harness=0; ephemeral=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --harness) harness="${2:?--harness needs a value}"; has_harness=1; shift 2 ;;
        # `--agent` is what this flag was called until the harnesses took the
        # word for something else: `claude --agent` and `opencode --agent` both
        # name a WORKER — a prompt, a model, an effort level — which is this
        # box's `--profile`. Kept working, because it is written down in every
        # note, transcript and README a box has; deprecated, because a reader
        # who knows one of those CLIs reads it as the other thing.
        --agent)
          harness="${2:?--agent needs a value}"; has_harness=1; shift 2
          echo "agent-box-session: --agent is deprecated, use --harness (it picks the CLI program; --profile picks the worker)" >&2
          ;;
        --profile) profile="${2:?--profile needs a value}"; shift 2 ;;
        --cwd) cwd="${2:?--cwd needs a value}"; shift 2 ;;
        --prompt) prompt="${2?--prompt needs a value}"; has_prompt=1; shift 2 ;;
        --resume-prompt) rprompt="${2?--resume-prompt needs a value}"; has_rprompt=1; shift 2 ;;
        --ephemeral) ephemeral=1; shift ;;
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
    pargs=()
    if [ -n "$profile" ]; then
      # --harness on the command line wins over the profile's harness, the same
      # override order the env file has over the NixOS option elsewhere here —
      # and it is passed INTO the resolver, because the arguments a profile
      # resolves to are harness-specific (`--model` for claude, `-m` for
      # codex, none at all for a shell session).
      povr=""
      [ "$has_harness" = 1 ] && povr="$harness"
      pjson="$("${AGENT_BOX_PROFILE_BIN:-agent-box-profile}" launch "$profile" "$povr")" || exit 2
      harness="$("$JQ" -r '.harness' <<<"$pjson")"
      "$JQ" -r --arg p "$profile" \
        '.warnings[] | "agent-box-session: profile \($p): " + .' <<<"$pjson" >&2
      # Profile args go FIRST so an explicit `-- EXTRA_ARGS` tail still has the
      # last word (both harness CLIs take the last occurrence of a flag).
      #
      # NUL-delimited, not one-per-line: an argument may CONTAIN newlines now
      # that a SYSTEM_PROMPT can (issue #212), and a newline-separated list
      # cannot tell ONE multi-line argument from SEVERAL arguments — a
      # two-paragraph prompt silently reached the agent CLI as three separate
      # flags. NUL is the one byte a value cannot hold: the env store refuses
      # to write one and skips an entry that holds one (src/lib/envstore.py),
      # so a profile file cannot put the frame byte inside a frame.
      while IFS= read -r -d '' parg; do
        pargs+=("$parg")
      done < <("$JQ" -j '.args[] + "\u0000"' <<<"$pjson")
    fi
    case " $AGENTS " in
      (*" $harness "*) ;;
      (*) echo "harness '$harness' is not available (available: $AGENTS)" >&2; exit 2 ;;
    esac
    registry_ensure
    # Name choice and write are one critical section (issue #254): gen_name
    # and taken() both decide from a READ of the file, so two concurrent adds
    # could pick the same free name and the second rename would drop the first
    # session outright.
    registry_lock
    [ -n "$name" ] || name="$(gen_name "$harness" "$cwd")"
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
    sargs=("${pargs[@]}" "$@")
    registry_edit --arg n "$name" --arg a "$harness" --arg c "$cwd" \
      --arg p "$prompt" --arg pp "$has_prompt" \
      --arg rp "$rprompt" --arg rpp "$has_rprompt" --arg bid "$bid" \
      --arg prof "$profile" --arg eph "$ephemeral" \
      '.sessions[$n] = ({agent: $a, skipPermissions: true, remoteControl: true,
                        remoteControlName: null,
                        workingDirectory: (if $c == "" then null else $c end),
                        extraArgs: $ARGS.positional,
                        profile: (if $prof == "" then null else $prof end),
                        initialPrompt: (if $pp == "1" then $p else null end),
                        resumePrompt: (if $rpp == "1" then $rp else null end),
                        boxSessionId: (if $bid == "" then null else $bid end),
                        hasRun: false}
                       # Added only when set, so every session that is NOT
                       # one-shot keeps the entry shape it has always had.
                       + (if $eph == "1" then {ephemeral: true} else {} end))' \
      --args -- "${sargs[@]}"
    registry_unlock
    # The mascot (issue #185) marks the closest thing this CLI has to
    # "an agent just started". Small on purpose: this runs in webhook
    # spawns and scripts too, and nothing parses the lines below it.
    printf '%s\n' \
        "   .-~~-." \
        "  ( (o) )" \
        "   \`-~~-'"
    what="$harness"
    [ -n "$profile" ] && what="$harness, profile $profile"
    if [ "$has_prompt" = 1 ]; then
      echo "session '$name' ($what) added with a kickoff prompt — the supervisor starts it within ~2s"
    else
      echo "session '$name' ($what) added — the supervisor starts it within ~2s"
    fi
    ;;
  rm)
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    registry_ensure
    registry_edit --arg n "$name" 'del(.sessions[$n])'
    kill_session "$name" || exit 1
    prune_filter "$name"
    prune_session_state "$name"
    echo "session '$name' removed"
    ;;
  stop)
    # Park a listed session (issue #167): flag it stopped FIRST so the
    # supervisor's post-spawn re-check catches a spawn racing this kill,
    # then take the live session down. The entry (agent, cwd, transcript
    # id) stays listed for a later 'restart' to revive — unless the session
    # is one-shot (--ephemeral), for which parked means finished and the
    # supervisor reaps the entry on its next tick.
    name="${1:-}"
    valid_name "$name" || { usage >&2; exit 2; }
    registry_ensure
    # Existence check and flag write together (issue #254): jq's assignment
    # CREATES a missing key, so a session deleted between the two came back as
    # a stub {stopped: true} — listed forever, startable by nobody, and the
    # name is then taken for a later add.
    registry_lock
    if ! taken "$name"; then
      echo "no such session: '$name' (see agent-box-session ls)" >&2
      exit 2
    fi
    # Read before the write, under the same lock the write takes: what is
    # printed below must describe the entry this command actually parked.
    eph=0
    "$JQ" -e --arg n "$name" '.sessions[$n].ephemeral == true' \
      "$REGISTRY_FILE" >/dev/null 2>&1 && eph=1
    registry_edit --arg n "$name" '.sessions[$n].stopped = true'
    registry_unlock
    kill_session "$name" || exit 1
    if [ "$eph" = 1 ]; then
      echo "session '$name' stopped — one-shot (--ephemeral), so the supervisor delists it within ~2s; its transcript is kept"
    else
      echo "session '$name' stopped — still listed, not respawned; 'agent-box-session restart $name' revives it"
    fi
    ;;
  restart)
    # Clearing the stopped flag makes restart double as the revive verb
    # for parked sessions; kill-session tolerates one with nothing live.
    if [ "${1:-}" = "--all" ]; then
      registry_ensure
      registry_edit 'del(.sessions[].stopped)'
      "$JQ" -r '.sessions | keys[]' "$REGISTRY_FILE" | while IFS= read -r n; do
        [ -n "$n" ] && kill_session "$n" || true
      done
      echo "all sessions killed — the supervisor restarts each within ~2s (re-reading env)"
    else
      name="${1:-}"
      valid_name "$name" || { usage >&2; exit 2; }
      registry_ensure
      # Listed-or-not decides which branch runs, so read it under the lock
      # (issue #254) — the flag write must apply to the file the check saw.
      registry_lock
      if taken "$name"; then
        registry_edit --arg n "$name" 'del(.sessions[$n].stopped)'
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
    #
    # The format has ONE owner (issue #212) and this verb delegates to it
    # rather than carrying a second KEY=value parser: a value that spans lines
    # — a PEM, an SSH key — has to mean the same thing here, on the settings
    # page and at session spawn, or setting it in one place corrupts it in
    # another.
    ENVSTORE="${AGENT_BOX_ENVSTORE_BIN:?the env-store CLI is pinned by the generated wrapper; run this through the installed command}"
    sub="${1:-}"; shift || true
    case "$sub" in
      ls)
        "$ENVSTORE" keys
        ;;
      set)
        k="${1:-}"
        valid_key "$k" || { echo "invalid key '$k' (use letters, digits, underscore; not starting with a digit)" >&2; exit 2; }
        if [ "${2-}" = "--stdin" ]; then
          # The way to set a multi-line secret, and the safer way to set any
          # secret: the value never reaches a command line, so it is not in
          # the shell history and not in anyone's `ps` output.
          "$ENVSTORE" set --stdin "$k"
        else
          "$ENVSTORE" set "$k=${2-}"
        fi
        echo "env '$k' set — applies on the next session (re)start ('agent-box-session restart --all')"
        ;;
      rm)
        k="${1:-}"
        valid_key "$k" || { usage >&2; exit 2; }
        # "no store, nothing to remove" is the store's own answer: this CLI
        # does not know where the file is, which is what keeps it right when
        # AGENT_BOX_CONFIG_DIR moves it.
        "$ENVSTORE" unset "$k"
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
