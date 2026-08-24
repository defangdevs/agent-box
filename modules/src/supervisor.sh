set -u
# One user-independent script (issue #154, Phase 2): everything user-
# or host-specific arrives through the unit's environment (HOME, USER
# and the AGENT_BOX_* variables), and binaries resolve from the unit
# PATH. grep/find are deliberately NOT part of that PATH (see
# agentBaseTools), so the unit pins those two via AGENT_BOX_*_BIN.
JQ=jq
TMUX="tmux -L agent-box"
GREP="${AGENT_BOX_GREP_BIN:-grep}"
FIND="${AGENT_BOX_FIND_BIN:-find}"
# The session registry — where it lives, how it is locked and how it is
# rewritten — is one file every shell writer splices in (issue #254). It is
# spliced HERE, above the seed below, because creating the registry is part of
# the same protocol as writing it (issue #289).
REGISTRY_PROG=supervisor
@@include:lib/registry.sh@@
# The webhook channel plugin, spelled once (issue #257): three places have to
# agree on it — the settings seed (an enabledPlugins key), the plugin-cache
# sync, and claude's --channels tag. The marketplace NAME comes from the
# marketplace repo's own marketplace.json, so it is a literal here rather than
# something derivable from AGENT_BOX_WEBHOOK_REPO (which names the repo that
# marketplace is CLONED from, e.g. defangdevs/local-channels).
WEBHOOK_MARKETPLACE=local-channels
WEBHOOK_PLUGIN_REF="local-webhook@$WEBHOOK_MARKETPLACE"

# First boot only: seed the Nix-declared sessions. The file is RUNTIME
# data afterwards — a rebuild must never clobber sessions the user
# added or removed while the box was live. Under the registry lock, because
# this unit and a session being added start in parallel on a first boot, and
# "the file was empty when I looked" must not survive another writer's
# decision (issue #289).
registry_ensure "${AGENT_BOX_SESSIONS_SEED:?}"

seed_json() {
  # seed_json FILE JQ_ARGS... — jq-edit FILE in place, creating it
  # if missing. A file jq can't parse is left untouched: the dialog
  # comes back, but the agent still starts.
  file=$1; shift
  [ -s "$file" ] || printf '{}' > "$file"
  if $JQ "$@" "$file" > "$file.seed-tmp" 2>/dev/null; then
    mv "$file.seed-tmp" "$file"
  else
    rm -f "$file.seed-tmp"
  fi
}

# Pre-accept claude-code's one-time startup dialogs. A fresh home
# otherwise parks the session on interactive prompts — the folder-trust
# dialog ("Is this a project you trust?") and, when running with
# --dangerously-skip-permissions, the Bypass Permissions warning (whose
# default answer is "No, exit"). On a headless box nobody is at the
# terminal to answer them, and Remote Control can't drive a session
# that is stuck on a dialog, so the ONLY interactive step left should
# be the one-time OAuth login. claude persists both acceptances in
# per-user state files, which it round-trips (read-modify-write), so
# values seeded before first launch survive login/onboarding:
#   ~/.claude.json          projects.<workdir>.hasTrustDialogAccepted
#   ~/.claude/settings.json skipDangerousModePermissionPrompt
#
# hasCompletedOnboarding skips the whole first-run onboarding wizard,
# whose first screen is the theme picker ("Choose the text style that
# looks best with your terminal"). Verified against claude 2.1.233: the
# wizard's step list pushes the theme step UNCONDITIONALLY, so a seeded
# ~/.claude/settings.json theme does NOT skip it — the only lever is the
# hasCompletedOnboarding gate on the wizard as a whole. Skipping it also
# drops the wizard's security-notes and terminal-setup screens, and its
# embedded login picker: an unauthenticated session lands on the REPL
# with "Not logged in · Run /login" in the status line, which is the
# same sign-in the README already documents as `claude auth login`.
# Theme is seeded too, but only when unset (`//=`), so /theme sticks.
#
# Runs before every claude session start (idempotent), which also
# covers upstream's occasional failure to persist an interactive
# acceptance (anthropics/claude-code issue 36403). Codex has no such
# dialogs. $1 = working directory, $2 = skipPermissions (true/false).
seed_claude_state() {
  mkdir -p "$HOME"/.claude
  seed_json "$HOME"/.claude.json --arg wd "$1" \
    '.projects[$wd] = ((.projects[$wd] // {}) + {hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true})
     | .hasCompletedOnboarding = true'
  seed_json "$HOME"/.claude/settings.json '.theme //= "dark"'
  if [ "$2" = true ]; then
    seed_json "$HOME"/.claude/settings.json \
      '.skipDangerousModePermissionPrompt = true'
  fi
  # Webhook channel (issue #101), only when the unit says the receiver
  # is live (AGENT_BOX_WEBHOOK_REPO doubles as the enable flag):
  # register the local-channels marketplace and enable the local-webhook
  # plugin non-interactively, so the session loads it as an MCP channel
  # with no /plugin prompt. These two keys are exactly what `claude
  # plugin marketplace add` + `claude plugin install` write, verified
  # against a live claude: extraKnownMarketplaces maps a marketplace
  # NAME (from the repo's marketplace.json, not the repo path) to a
  # source, and enabledPlugins is an OBJECT keyed
  # "<plugin>@<marketplace>" — not a list of records. With only these
  # seeded, the first session clones the marketplace, populates the
  # plugin cache and connects the MCP server on its own. Idempotent:
  # plain merges, so a hand `/plugin` change survives.
  if [ -n "${AGENT_BOX_WEBHOOK_REPO:-}" ]; then
    seed_json "$HOME"/.claude/settings.json \
      --arg whrepo "$AGENT_BOX_WEBHOOK_REPO" \
      --arg mkt "$WEBHOOK_MARKETPLACE" --arg ref "$WEBHOOK_PLUGIN_REF" \
      '.extraKnownMarketplaces = ((.extraKnownMarketplaces // {})
         + {($mkt): {source: {source: "github", repo: $whrepo}}})
       | .enabledPlugins = ((.enabledPlugins // {}) + {($ref): true})'
    sync_webhook_plugin
  fi
}

# Keep the local-webhook a SESSION loads in step with the one the box PINS
# (issue #193). Two copies run here: the receiver daemon and the
# agent-box-webhook CLI run webhook.rev's store path, while a claude session
# loads claude's own plugin cache — and claude only refreshes that on an
# explicit `plugin update`. Drift there is invisible and expensive: a
# pre-0.10.0 peer names its IPC socket "<pid>.sock" instead of
# "<key>.<pid>.sock", which parses to no filter key, so the dispatch ownership
# brake sees no live session and one failing run spawns a hook session per CI
# event (#192). This box ran daemon 0.11.1 against a cache of 0.3.0 for nine
# days with no symptom.
#
# Session start is the only moment that can fix it: a session loads its
# interpreter once, so updating the cache under a RUNNING session does nothing
# (that case is what `agent-box-webhook status` reports). Deliberately narrow:
#
#   - Only when the cache is OLDER than the pin. A newer cache is normal —
#     claude tracks the marketplace's default branch, which moves ahead of
#     webhook.rev between pin bumps — and `claude plugin update` could not
#     install an older version anyway.
#   - Nothing installed yet: leave it. The seeded settings above make claude
#     clone the marketplace and install on first launch, and that lands on the
#     default branch, which is never older than the pin.
#   - Best effort. An offline box, a rate-limited marketplace or a claude that
#     changed its CLI must never keep a session from starting; the session
#     just starts with what it has, and says so.
sync_webhook_plugin() {
  script="${AGENT_BOX_WEBHOOK_PINNED_SCRIPT:-}"
  { [ -n "$script" ] && [ -r "$script" ]; } || return 0
  # Read the pinned VERSION with the shell alone: grep and sed are NOT on this
  # unit's PATH (see agentBaseTools), and pinning two more binaries through the
  # environment for one line would be worse than this loop.
  pinned=""
  while IFS= read -r line; do
    case "$line" in
      ("VERSION = '"*)
        pinned="${line#VERSION = \'}"; pinned="${pinned%%\'*}"; break ;;
    esac
  done < "$script"
  [ -n "$pinned" ] || return 0

  installed="$HOME/.claude/plugins/installed_plugins.json"
  [ -s "$installed" ] || return 0
  # The OLDEST installed copy across scopes: a project-scope install shadows
  # the user one, and the stalest is the one that can break the brake.
  have="$("$JQ" -r --arg ref "$WEBHOOK_PLUGIN_REF" \
            '[.plugins[$ref] // [] | .[] | .version // empty] | .[]' \
            "$installed" 2>/dev/null | sort -V | head -1)"
  [ -n "$have" ] || return 0
  [ "$have" = "$pinned" ] && return 0
  # sort -V puts the lower version first; if that is not $have, the cache is
  # already at or ahead of the pin and there is nothing to do.
  [ "$(printf '%s\n%s\n' "$have" "$pinned" | sort -V | head -1)" = "$have" ] || return 0

  # One attempt per pin per retry window. Without this, a box that cannot
  # reach GitHub would pay the timeout on every session start — and the
  # supervisor restarts a crashed session, so "every start" can mean a loop.
  marker="$HOME/.claude/plugins/.agent-box-plugin-sync"
  now="$(date +%s)"
  retry="${AGENT_BOX_WEBHOOK_SYNC_RETRY_S:-3600}"
  if [ -s "$marker" ]; then
    read -r mver mts _rest < "$marker" || true
    case "${mts:-x}" in (""|*[!0-9]*) mts=0 ;; esac
    if [ "${mver:-}" = "$pinned" ] && [ $((now - mts)) -lt "$retry" ]; then
      echo "local-webhook plugin: cache $have is older than the pinned $pinned;" \
           "last refresh attempt $((now - mts))s ago, not retrying yet (issue 193)" >&2
      return 0
    fi
  fi
  printf '%s %s\n' "$pinned" "$now" > "$marker"

  cbin="$(agent_bin claude)" || return 0
  echo "local-webhook plugin: cache $have is older than the pinned $pinned — refreshing" \
       "before this session starts, so its peer is visible to the dispatch brake (issue 193)" >&2
  if "$cbin" plugin marketplace update "$WEBHOOK_MARKETPLACE" >/dev/null 2>&1 \
       && "$cbin" plugin update "$WEBHOOK_PLUGIN_REF" >/dev/null 2>&1; then
    now_have="$("$JQ" -r --arg ref "$WEBHOOK_PLUGIN_REF" \
                  '[.plugins[$ref] // [] | .[] | .version // empty] | .[]' \
                  "$installed" 2>/dev/null | sort -V | head -1)"
    echo "local-webhook plugin: cache is now ${now_have:-unknown} (pinned $pinned)" >&2
  else
    echo "local-webhook plugin: could not refresh the cache (offline, or claude's plugin CLI" \
         "changed); this session starts on $have — 'agent-box-webhook status' will keep saying so" >&2
  fi
}

agent_bin() {
  # agent_bin NAME — resolve an agent (or "shell") to its binary via
  # the unit's AGENT_BOX_AGENT_BINS ("name=/path ..." pairs; store and
  # shell paths never contain whitespace, so word-splitting is safe).
  for pair in ${AGENT_BOX_AGENT_BINS:?}; do
    case "$pair" in ("$1"=*) printf '%s\n' "${pair#*=}"; return 0 ;; esac
  done
  return 1
}

# Codex remote-control pairing currently requires the standalone
# installer layout at ~/.codex/packages/standalone/current/codex.
# Mirror that fixed path to the provided Codex so pairing works
# without a curl-installed second copy.
if cbin="$(agent_bin codex)"; then
  mkdir -p "$HOME"/.codex/packages/standalone/agent-box-current
  ln -sfn "$cbin" "$HOME"/.codex/packages/standalone/agent-box-current/codex
  ln -sfn agent-box-current "$HOME"/.codex/packages/standalone/current
fi

# Deliver-once + resume bookkeeping. A session's kickoff prompt
# (initialPrompt) must fire on the FIRST spawn only; every later respawn
# (crash, clean exit, reboot, Spot stop→restart — all of which keep the
# on-disk transcript because /home is the persistent root EBS volume)
# must RESUME the prior transcript instead of redoing the task.
#
# That bookkeeping is the SUPERVISOR's own observation, and observations now
# live in a supervisor-owned side file rather than in sessions.json (issue
# #282). The registry is INTENT — what the operator asked for: name, agent,
# working directory, prompts, stopped — and five writers in three languages
# edit it, so a lost update there (issue #254) used to revert a long-running
# session to "never spawned": new id, kickoff prompt fired a second time,
# previous transcript orphaned. Intent cannot revert an observation it no
# longer carries.
#
# MIGRATION — this release writes BOTH. The side file is read first and the
# registry copy is the fallback, so a session that last spawned before this
# release keeps its id, as does the id `agent-box-session add` mints up
# front. The next release stops writing hasRun / boxSessionId / the cleared
# initialPrompt into the registry and drops the fallback read; that is what
# makes the supervisor a read-only consumer of sessions.json.
SESSION_STATE_DIR="$HOME/.local/state/agent-box/session"
session_state_file() {
  # session_state_file NAME — the one place this script spells the path
  # (issue #284). The key is the session NAME for now, and a name is a
  # label rather than an identity: it is minted from a file that can be
  # stale, and it is meant to become editable. The key this should grow
  # into is one a harness mints — tmux #{session_id} plus the
  # AGENT_BOX_SESSION_ID this spawn puts in the pane environment for the
  # claim, and claude's transcript uuid / codex's rollout id for the
  # conversation — so every reader and writer goes through here and the
  # re-key is this function. `agent-box-session rm` carries the same
  # accessor under the same name, and the settings daemon has
  # session_state_path; the three must agree.
  printf '%s/%s.json\n' "$SESSION_STATE_DIR" "$1"
}

session_launch_id() {
  # session_launch_id SESSION — the id this session was last LAUNCHED with,
  # or empty when the supervisor has never recorded one.
  #
  # EAFP: attempt the read and handle the miss. Testing for the file first
  # would ask the kernel a question the read answers anyway, and the answer
  # would already be stale by the time we acted on it.
  _v="$($JQ -r '.launchSessionId // empty' "$(session_state_file "$1")" 2>/dev/null)" \
    || _v=""
  # Shape check on the way OUT, because this value reaches --resume, a
  # find -name pattern and this file's own writer.
  case "$_v" in
    (*[!0-9a-fA-F-]*) _v="" ;;
    (????????-????-????-????-????????????) ;;
    (*) _v="" ;;
  esac
  printf '%s' "$_v"
}

record_launch() {
  # record_launch SESSION BOXID — remember the id this spawn launched with,
  # so the next one resumes the same conversation.
  #
  # mktemp + rename, not a create-if-absent: a rotation legitimately CHANGES
  # the value, so the last writer must win. mktemp is the exclusive-creation
  # primitive (O_EXCL on a name nobody can guess) and the rename is what
  # keeps a reader from ever seeing half a document. Best effort throughout
  # — a failed write costs a re-derived id on the next spawn, never a crash.
  case "$2" in (*[!0-9a-fA-F-]*|"") return 0 ;; esac
  mkdir -p "$SESSION_STATE_DIR" 2>/dev/null || return 0
  _f="$(session_state_file "$1")"
  _t="$(mktemp "$_f.XXXXXX" 2>/dev/null)" || return 0
  if printf '{"launchSessionId":"%s"}\n' "$2" > "$_t" 2>/dev/null; then
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t"
  else
    rm -f "$_t"
  fi
}

mark_started() {
  # mark_started SESSION BOXID — the MIGRATION copy of what record_launch
  # just wrote (issue #282): hasRun and boxSessionId in the registry, plus
  # the consumed kickoff prompt. Kept for one release so a box that rolls
  # back to the previous module still finds its ids where that module looks;
  # the next release deletes this function and leaves sessions.json to
  # intent alone.
  #
  # Best-effort; a failed rewrite just means the prompt re-fires next spawn,
  # never a crash. Read and rename happen under the registry lock (issue
  # #254); the caller normally holds it already, and registry_lock nests.
  #
  # `has` first, because this must only ever UPDATE: jq's `|=` on a missing
  # key CREATES it (verified), so a session deleted while this spawn was
  # preparing came back as a stub {hasRun, boxSessionId, initialPrompt} that
  # the reconcile loop then started forever — a session nothing kills,
  # because no delete path knows it exists. That is the resurrect half of
  # #254; the lock is the lost-update half.
  #
  # jq's stderr is dropped rather than journalled: this runs on every spawn of
  # every session, so a registry the box cannot parse would repeat the same
  # line every two seconds, and the reconcile loop already carries on without
  # it.
  registry_edit --arg s "$1" --arg b "$2" \
    'if .sessions | has($s)
     then (.sessions[$s]) |= (.hasRun = true | .boxSessionId = $b | .initialPrompt = null)
     else . end' 2>/dev/null || true
}

claude_has_transcript() {
  # True when Claude has a saved conversation for this session id, so we
  # can safely --resume it; else the caller starts fresh with --session-id
  # (same id) instead of erroring on an unknown --resume target.
  [ -n "$1" ] || return 1
  $FIND "$HOME"/.claude/projects -maxdepth 3 -name "$1.jsonl" 2>/dev/null \
    | $GREP -q .
}

codex_rollout_uuid() {
  # Echo the Codex session UUID whose rollout transcript carries our
  # "[agent-box session <boxid>]" marker (newest wins if a prior resume
  # forked the rollout). Empty when none match — the caller then starts a
  # FRESH codex session rather than risk `resume --last` grabbing a
  # sibling session's transcript in a shared working directory.
  [ -n "$1" ] || return 0
  d="$HOME"/.codex/sessions
  [ -d "$d" ] || return 0
  f="$($GREP -rlF "agent-box session $1" "$d" 2>/dev/null \
        | while IFS= read -r p; do printf '%s\t%s\n' "$(stat -c %Y "$p" 2>/dev/null)" "$p"; done \
        | sort -rn | head -n1 | cut -f2-)"
  [ -n "$f" ] || return 0
  b="${f##*/}"; b="${b%.jsonl}"
  # rollout-<date>T<time>-<uuid>.jsonl: the UUID is the fixed trailing 36
  # chars (8-4-4-4-12), robust against the dashes inside the timestamp.
  printf '%s' "${b: -36}"
}

start_session() {
  sname=$1
  sjson="$($JQ -c --arg s "$sname" '.sessions[$s] // empty' "$REGISTRY_FILE")" || return 0
  [ -n "$sjson" ] || return 0
  agent="$($JQ -r '.agent // empty' <<<"$sjson")"
  if ! bin="$(agent_bin "$agent")"; then
    echo "session '$sname': agent '$agent' is not installed (see installAgents) — skipping" >&2
    return 0
  fi
  wd="$($JQ -r '.workingDirectory // empty' <<<"$sjson")"
  [ -n "$wd" ] || wd="$HOME"
  skip="$($JQ -r 'if .skipPermissions == false then "false" else "true" end' <<<"$sjson")"
  rc="$($JQ -r 'if .remoteControl == false then "false" else "true" end' <<<"$sjson")"
  rcname="$($JQ -r '.remoteControlName // empty' <<<"$sjson")"
  # The agent profile this session was created with (issue #321), or empty.
  # Its harness and arguments were already resolved into `agent`/`extraArgs`
  # by agent-box-session, so nothing here re-reads them: what the name is for
  # is the profile's ENVIRONMENT, which the env-exec wrapper applies at every
  # spawn — a rotated token in a profile therefore reaches the session on its
  # next restart, while the arguments it started with stay put.
  sprofile="$($JQ -r '.profile // ""' <<<"$sjson")"
  case "$sprofile" in (*[!A-Za-z0-9_-]*) sprofile="" ;; esac
  ip="$($JQ -r '.initialPrompt // ""' <<<"$sjson")"
  rp="$($JQ -r '.resumePrompt // ""' <<<"$sjson")"
  # The id this session was last LAUNCHED with (issue #282). Note what that
  # is NOT: it is not an id that survives the conversation, because a
  # segment rotation — clear, compact or resume — mints a new one and the
  # block below adopts it. It names the SEGMENT this spawn hands the agent,
  # which is why a consumer wanting "the conversation" has to follow the
  # rotation record rather than trust this value (issues #274, #277).
  #
  # Supervisor side file first, registry copy as the migration fallback (see
  # mark_started). `launched` — has this session ever been spawned at all —
  # comes from whichever answered: the side file exists only because a spawn
  # wrote it, and the registry's hasRun said the same thing before it moved.
  launched=false
  recorded="$(session_launch_id "$sname")"
  bid="$recorded"
  if [ -n "$bid" ]; then
    launched=true
  else
    bid="$($JQ -r '.boxSessionId // ""' <<<"$sjson")"
    if [ "$($JQ -r 'if .hasRun == true then "true" else "false" end' <<<"$sjson")" = true ]; then
      launched=true
    fi
  fi
  # Mint the launch id on the first spawn if nothing carries one yet
  # (legacy/seed sessions, hand-edited files). /proc/.../uuid is the
  # kernel's UUID source — a bash `read`, so nothing on PATH is needed
  # and the value is a valid UUID for Claude's --session-id.
  if [ -z "$bid" ] && [ -r /proc/sys/kernel/random/uuid ]; then
    read -r bid < /proc/sys/kernel/random/uuid || bid=
  fi
  # hasRun is DERIVED, never stored (issue #282). "Is there a conversation to
  # resume" is a question the disk already answers for claude, and
  # claude_has_transcript is the same check the resume arm below has always
  # made — a stored copy could only ever disagree with it, and did: a
  # reverted registry promised a resume of a transcript that was there, and a
  # deleted transcript promised one that was not. The other harnesses have
  # nothing equivalent to read (a codex rollout is found by a marker that
  # only a kickoff prompt carries; a shell session has no transcript at all),
  # so they keep answering from the launch record itself.
  if [ "$agent" = claude ]; then
    hasrun=false
    claude_has_transcript "$bid" && hasrun=true
  else
    hasrun=$launched
  fi
  # Segment rotation (issue #223): claude's live session id can rotate away
  # from the id this session was last launched with. Three triggers are
  # observed on the live box — clear, compact and resume — and all three do
  # the same thing: the process keeps running (and keeps its Remote Control
  # registration) while a NEW transcript opens under a new uuid. The
  # SessionStart hook (injected via AGENT_BOX_CLAUDE_SETTINGS below) fires on
  # each of them and records the live id, keyed by the launch id it finds in
  # the pane environment. Follow the record here: without it, `--resume
  # "$bid"` resurrects the segment the user rotated away from and orphans the
  # one that replaced it — and the Claude apps, which thread Remote Control
  # by NAME, then interleave both conversations in one app thread. The charset
  # check doubles as glob-safety for claude_has_transcript's -name lookup; the
  # adopted id is recorded below, so each record chains one hop at most.
  #
  # Gated on `launched`, not on a transcript: what makes a live-id record
  # meaningful is that this session has spawned before (nothing else writes
  # under our launch id), and an adoption is its own proof of a resumable
  # transcript. EAFP on the record — read it and handle the miss; a
  # readability test first would only add a syscall and a race.
  rotated=false
  if [ "$agent" = claude ] && [ "$launched" = true ] && [ -n "$bid" ]; then
    live=""
    read -r live < "$HOME/.local/state/agent-box/live-session-id/$bid" 2>/dev/null \
      || live=""
    case "$live" in
      (*[!0-9a-fA-F-]*) live="" ;;
      (????????-????-????-????-????????????) ;;
      (*) live="" ;;
    esac
    if [ -n "$live" ] && [ "$live" != "$bid" ] && claude_has_transcript "$live"; then
      bid="$live"
      rotated=true
      hasrun=true
    fi
  fi
  # Resume on every respawn (hasRun already set); the kickoff prompt fires
  # only on the very first spawn. The default resume steer both continues
  # unfinished work AND lets an already-finished task exit instead of
  # looping — resumePrompt overrides it per session.
  resuming=false; prompt="$ip"
  if [ "$hasrun" = true ]; then
    resuming=true
    if [ -n "$rp" ]; then
      prompt="$rp"
    else
      prompt="You were interrupted and automatically restarted (agent-box session $bid). Your previous transcript for this session has been resumed — review what you had already done, verify the current state, and continue from where you left off. If that work was already complete, say so briefly and stop rather than redoing it."
    fi
  fi

  # Build the command with printf %q so runtime-provided fields
  # (extraArgs, remoteControlName, cwd, prompts, ids) can't inject into
  # the tmux command line — the runtime equivalent of lib.escapeShellArg.
  cmd="$(printf '%q' "$bin")"
  # extraArgs are appended by every branch below.
  append_extra() {
    while IFS= read -r xarg; do
      cmd="$cmd $(printf '%q' "$xarg")"
    done < <($JQ -r '.extraArgs // [] | .[]' <<<"$sjson")
  }
  # Autonomy for the codex TUI arms. skipPermissions = true takes the
  # documented flag. skipPermissions = false has to UNDO the box-wide default
  # when services.agent-box.codexFullAccess wrote /etc/codex/config.toml
  # (issue 234) — otherwise the session silently inherits full access from
  # that system layer and the per-session opt-out means nothing. The unit
  # exports AGENT_BOX_CODEX_FULL_ACCESS only when that file exists, because
  # without it codex's own defaults (which depend on project trust) are the
  # right answer and pinning would change them.
  codex_autonomy() {
    if [ "$skip" = true ]; then
      cmd="$cmd --dangerously-bypass-approvals-and-sandbox"
    elif [ -n "${AGENT_BOX_CODEX_FULL_ACCESS:-}" ]; then
      cmd="$cmd -c approval_policy=on-request -c sandbox_mode=workspace-write"
    fi
  }
  case "$agent" in
    claude)
      [ "$skip" = true ] && cmd="$cmd --dangerously-skip-permissions"
      if [ "$rc" = true ]; then
        if [ -z "$rcname" ]; then
          # Host suffix for the derived "<user>-<session>@<host>" name.
          # AGENT_BOX_HOST_LABEL comes from the unit: remoteControlHost
          # if set (the CloudFormation stack name on the AWS image), else
          # the public web.domain — the address the box is actually
          # reachable at. Fall back to the live kernel hostname only if
          # both are empty; that kernel name is the INTERNAL fqdn on a
          # cloud box (ip-10-x-x-x.<region>.compute.internal), which is
          # why the public web.domain is preferred above it. Drop
          # "@<host>" entirely when even the kernel name is empty rather
          # than emitting a dangling "@". read is a bash builtin, so this
          # needs nothing on PATH.
          host="${AGENT_BOX_HOST_LABEL:-}"
          if [ -z "$host" ] && [ -r /proc/sys/kernel/hostname ]; then
            read -r host < /proc/sys/kernel/hostname || host=
          fi
          rcname=$USER-$sname
          [ -z "$host" ] || rcname="$rcname@$host"
        fi
        cmd="$cmd --remote-control $(printf '%q' "$rcname")"
      fi
      # The SessionStart hook that records the live session id (see the
      # segment-rotation block above). ADDITIONAL settings only: claude
      # merges the file on top of the user/project settings.json, which
      # stay untouched, so only supervisor-spawned sessions carry the hook.
      [ -n "${AGENT_BOX_CLAUDE_SETTINGS:-}" ] \
        && cmd="$cmd --settings $(printf '%q' "$AGENT_BOX_CLAUDE_SETTINGS")"
      # Turn the webhook channel ON for this session (issue #257). Seeding
      # enabledPlugins (above) and allowedChannelPlugins (remote-settings.json)
      # only makes the plugin loadable and NAMABLE; --channels is what makes
      # claude accept its notifications/claude/channel messages. Without it a
      # session connects the MCP server, answers webhook_subscribe happily, and
      # then DROPS every event the receiver forwards to it — logging "Channel
      # notifications skipped: server plugin:local-webhook:local-webhook not in
      # --channels list for this session" and nothing else. That looked exactly
      # like a quiet repo, so it went unnoticed for weeks: no delivery ever
      # reached a live session, and only the dispatch path (--deliver-to
      # subagent, which hands the event to a NEW session as its prompt) worked.
      # The value is a TAGGED ref, plugin:<plugin>@<marketplace> — not the
      # plugin:<plugin>:<server> id the skip message prints. Placed before
      # append_extra so a session that names its own channels appends to (or
      # overrides) ours rather than the other way round.
      [ -n "${AGENT_BOX_WEBHOOK_REPO:-}" ] \
        && cmd="$cmd --channels plugin:$WEBHOOK_PLUGIN_REF"
      # Our own id: --resume it on respawn (exact, so concurrent sessions
      # never cross), but only when a transcript actually exists — else
      # reuse it as a fresh --session-id rather than erroring on resume.
      if [ -n "$bid" ]; then
        if [ "$resuming" = true ] && claude_has_transcript "$bid"; then
          cmd="$cmd --resume $(printf '%q' "$bid")"
        else
          cmd="$cmd --session-id $(printf '%q' "$bid")"
        fi
      fi
      append_extra
      # End-of-options guard before the positional prompt. Several claude
      # flags are VARIADIC (--channels, --add-dir, --allowedTools,
      # --betas, ...), so a session whose extraArgs end in one swallow the
      # prompt as one more value of that flag. The launch then dies in
      # argument parsing before the TUI ever starts, and the error quotes
      # our own prompt back as the offending value:
      #   --channels entries must be tagged: You were interrupted and
      #   automatically restarted (agent-box session <uuid>) ...
      # "--" ends option parsing, so the prompt stays positional whatever
      # extraArgs contain. Harmless when extraArgs are empty.
      [ -n "$prompt" ] && cmd="$cmd -- $(printf '%q' "$prompt")"
      ;;
    codex)
      if [ "$rc" = true ]; then
        # Codex remote control uses a dedicated app-server daemon, not a
        # TUI flag (issue 103), whereas claude takes a
        # `--remote-control <name>` flag on its normal TUI. So a
        # remote-controlled codex session runs a DIFFERENT program: the
        # foreground supervisor wrapper (the daemon itself detaches —
        # see codexRemoteControl), which takes the host label and then
        # the codex binary as its first two args and forwards the rest to
        # `app-server daemon start`. That subcommand rejects
        # --dangerously-bypass-approvals-and-sandbox, so honour
        # skipPermissions via the two -c overrides that flag sets
        # (codex's documented config-override path). A bare value that
        # isn't valid TOML is taken as a string literal, so no quoting is
        # needed.
        #
        # WARNING: those overrides do NOT reach the app-server today (issue
        # 234). `daemon start` parses them, then spawns the app-server with a
        # fixed argv, so the process that serves every remote thread runs on
        # the box's config alone — measured on a live box, and the reason a
        # paired phone still asked for approvals. What actually applies is
        # /etc/codex/config.toml (services.agent-box.codexFullAccess). The
        # flags stay: they cost nothing and become correct the day upstream
        # forwards them.
        #
        # remoteControlName is claude-only: the codex daemon takes
        # its machine name from gethostname(2) with no override, so the
        # wrapper gives it the host label through a private UTS namespace
        # instead (see codexRemoteControl). Pairing the
        # Codex apps to a running daemon uses `codex remote-control
        # pair`; the standalone-path shim seeded above is what lets the
        # Nix codex serve as the app-server. The daemon takes no
        # positional prompt and has no TUI transcript to resume, so the
        # kickoff/resume wiring below does not apply to it.
        cmd="$(printf '%q' "${AGENT_BOX_CODEX_RC:?}") $(printf '%q' "${AGENT_BOX_HOST_LABEL:-}") $cmd"
        if [ "$skip" = true ]; then
          cmd="$cmd -c approval_policy=never -c sandbox_mode=danger-full-access"
        fi
        append_extra
      elif [ "$resuming" = true ]; then
        # Find THIS session's transcript by our injected marker. A concrete
        # match → resume it; no match → start fresh (never `resume --last`,
        # which could grab a sibling session's transcript in a shared cwd).
        target="$(codex_rollout_uuid "$bid")"
        # "--" before the positionals for the same reason as claude's:
        # codex's -i/--image takes MULTIPLE files, so extraArgs ending in
        # it would otherwise eat the resume target and the prompt. Here
        # "--" also protects the target, which `resume` reads as its first
        # positional (SESSION_ID) and the prompt as its second.
        if [ -n "$target" ]; then
          cmd="$cmd resume"
          codex_autonomy
          append_extra
          cmd="$cmd -- $(printf '%q' "$target")"
          [ -n "$prompt" ] && cmd="$cmd $(printf '%q' "$prompt")"
        else
          codex_autonomy
          append_extra
          [ -n "$prompt" ] && cmd="$cmd -- $(printf '%q' "[agent-box session $bid] $prompt")"
        fi
      else
        codex_autonomy
        append_extra
        # Stamp the box id into the kickoff prompt so the transcript is
        # findable on resume; skip the stamp when there's no prompt (an
        # interactive session with no task shouldn't burn an opening turn).
        # "--" as above: a trailing -i/--image in extraArgs would swallow
        # the prompt and codex would open with no task at all.
        if [ -n "$prompt" ]; then
          cmd="$cmd -- $(printf '%q' "[agent-box session $bid] $prompt")"
        fi
      fi
      ;;
    shell)
      append_extra
      ;;
    *)
      # Unknown/future harness: pass the prompt positionally (the common
      # convention) but skip id/resume wiring we can't verify.
      append_extra
      [ -n "$prompt" ] && cmd="$cmd $(printf '%q' "$prompt")"
      ;;
  esac
  if [ "$agent" = claude ]; then
    # Upstream claude-code bug: the client persists only
    # channelsEnabled to ~/.claude/remote-settings.json, losing the
    # org's channel-plugin allowlist; the next launch trusts the stale
    # cache and silently drops every channel notification. Clearing
    # the cache before each claude launch forces a full policy fetch.
    rm -f "$HOME"/.claude/remote-settings.json
    seed_claude_state "$wd" "$skip"
  fi
  # Seed the minimal editable AGENTS.md (points at the read-only canonical
  # guide) IFF absent, so the agent's own edits or a repo checkout there
  # never get clobbered. Not for shell sessions: no agent reads it there,
  # and scratch dirs shouldn't get littered. Unset (the user opted out with
  # agentsMd = null) seeds nothing.
  if [ -n "${AGENT_BOX_AGENTS_POINTER:-}" ] \
       && [ "$agent" != shell ] && [ ! -e "$wd/AGENTS.md" ]; then
    mkdir -p "$wd"
    install -m 0644 "$AGENT_BOX_AGENTS_POINTER" "$wd/AGENTS.md"
  fi
  # claude reads CLAUDE.md and NEVER AGENTS.md (issue #305), so the file
  # seeded above reaches a claude session only through a symlink. Two are
  # needed, one per memory scope: the project-scope one points at the
  # sibling AGENTS.md (relative target — a symlink, unlike a claude
  # `@import`, has no trouble reaching outside the project tree, but
  # AGENTS.md happens to sit right beside it), and the user-scope one
  # points straight at the canonical /etc guide, so it stays live across
  # box updates without ever being reseeded. Both IFF absent, so a repo's
  # own CLAUDE.md and any hand edits survive; the notes symlink only when
  # there IS an AGENTS.md beside it to point at (the line above just made
  # sure of that, unless the user opted out or something else owns the
  # file).
  if [ "$agent" = claude ]; then
    if [ -n "${AGENT_BOX_AGENTS_POINTER:-}" ] \
         && [ -e "$wd/AGENTS.md" ] && [ ! -e "$wd/CLAUDE.md" ]; then
      ln -s AGENTS.md "$wd/CLAUDE.md"
    fi
    if [ -n "${AGENT_BOX_CLAUDE_GUIDE_TARGET:-}" ] \
         && [ ! -e "$HOME/.claude/CLAUDE.md" ]; then
      mkdir -p "$HOME"/.claude
      ln -s "$AGENT_BOX_CLAUDE_GUIDE_TARGET" "$HOME"/.claude/CLAUDE.md
    fi
  fi
  # The env-exec wrapper loads ~/.config/agent-box/env NOW — at spawn
  # time, not unit start — then execs the agent (issue 89), so
  # settings-page secrets land on the next session (re)start.
  # The epilogue then sorts the agent's exit (the wrapper execs the
  # agent, so the exit status is the agent's) into the three cases:
  #   exit 0 — somebody ASKED it to quit (/quit, Ctrl+D): record
  #     stopped=true so the reconcile loop leaves the session down
  #     instead of respawning-and-resuming it (issue #167);
  #     `agent-box-session restart` clears the flag and revives it.
  #   non-zero — a POST-MORTEM shell: the dead session stays
  #     attachable for inspection and is NOT respawned over.
  #   killed (restart, reboot, Spot stop) — the pane ends without
  #     reaching either branch, so the reconcile loop respawns it.
  # Two session kinds opt out of the parking branch:
  #   codex + remoteControl — the pane runs the RC daemon wrapper,
  #     whose health loop falls off its end with status 0 when the
  #     daemon dies. That is a crash to respawn over (self-heal, as
  #     before #167), not a quit somebody asked for; nothing
  #     interactive in that pane can even ask to quit.
  #   shell — the command IS a shell, exiting it should hand you a
  #     fresh one (via the reconcile loop), not a nested inspection
  #     bash or a parked session; leave one closed with detach or
  #     `agent-box-session stop`.
  epilogue=" && ${AGENT_BOX_MARK_STOPPED:?} $(printf '%q' "$sname") || exec bash"
  [ "$agent" = codex ] && [ "$rc" = true ] && epilogue=" || exec bash"
  [ "$agent" = shell ] && epilogue=""
  # A delete (settings page / agent-box-session rm: delist THEN kill) or
  # a stop (agent-box-session stop: flag THEN kill) can land while this
  # function is preparing the spawn — their kill hits the OLD session
  # and this spawn would resurrect it as a live session the reconcile
  # loop never kills (it tolerates unmanaged sessions on purpose, and
  # skips stopped ones): a permanent leak. Re-check the CURRENT file at
  # the last moment, and verify again AFTER the spawn — a delist/stop
  # landing before the post-check is honored here by killing what we
  # just started; one landing after it sees the session live and kills
  # it itself. (Seen as a CI flake in the sessions VM test, run
  # 30740226645.)
  listed() {
    $JQ -e --arg s "$sname" \
      '.sessions | has($s) and (.[$s].stopped != true)' \
      "$REGISTRY_FILE" >/dev/null 2>&1
  }
  # Pre-check, spawn, post-check and mark_started are ONE critical section
  # (issue #254), so the post-check is exact: without the lock a delete could
  # land between the post-check and mark_started, whose read-modify-write then
  # republished the deleted entry. Everything slow is deliberately already
  # done — seed_claude_state above can spend minutes inside `claude plugin
  # marketplace update` over the network, and no lock may be held across
  # that, so the section covers only reads and writes of this file.
  registry_lock
  listed || { registry_unlock; return 0; }
  # Per-session webhook identity, passed via `tmux new-session -e` so it
  # lands in the SESSION environment — inherited by the agent AND by
  # anything the agent runs in that pane, which is what makes
  # `agent-box-webhook` work with no arguments. The daemon fans every
  # verified delivery out to all of this user's sessions; each keeps its
  # own subscription filter keyed on LOCAL_WEBHOOK_SESSION, so
  # subscriptions never leak between sessions. LOCAL_WEBHOOK_PORT=0
  # makes any webhook.py started here a pure IPC peer: the daemon owns
  # the ingress and a session must never steal it. $sname was validated
  # [A-Za-z0-9_-] above and $HOME has no spaces, so the unquoted $whargs
  # expansion stays one argument per token.
  whargs=""
  if [ -n "${AGENT_BOX_WEBHOOK_REPO:-}" ]; then
    whargs="-e LOCAL_WEBHOOK_SESSION=$USER-$sname -e LOCAL_WEBHOOK_STATE_DIR=$HOME/.local/state/local-webhook -e LOCAL_WEBHOOK_PORT=0"
  fi
  # AGENT_BOX_SESSION_ID rides the same session environment: it is the
  # launch id the SessionStart hook keys its live-id record by (the
  # segment-rotation block above). Quoted on its own — unlike $whargs it
  # is runtime data from sessions.json, not supervisor-built tokens.
  # 9>&- closes the registry lock fd for this child: `new-session` STARTS the
  # tmux server when none is running, and that server daemonizes and outlives
  # us — inheriting the fd would hand it the lock forever and wedge every
  # other writer (the lock lives on the open file description, not the pid).
  # AGENT_BOX_PROFILE rides the same session environment, for the env-exec
  # wrapper's profile overlay (and so anything running in the pane can say
  # which profile it is). Charset-checked above, like $sname.
  pargs=""
  [ -n "$sprofile" ] && pargs="-e AGENT_BOX_PROFILE=$sprofile"
  if $TMUX new-session -d -s "$sname" -c "$wd" $whargs $pargs \
       -e AGENT_BOX_SESSION_ID="$bid" \
       "${AGENT_BOX_ENV_EXEC:?} $cmd$epilogue" 9>&-; then
    if ! listed; then
      registry_unlock
      $TMUX kill-session -t "=$sname" 2>/dev/null || true
      return 0
    fi
    # Record the id this spawn launched with, whenever that is not already
    # what the record says: a first spawn, an adopted rotation, and the one
    # respawn that migrates a session predating the side file (issue #282).
    # Writing only on a CHANGE matters because a session that cannot start —
    # a claude with no credentials, say — is respawned every couple of
    # seconds, and neither file should be rewritten on that loop.
    #
    # The registry mirror is narrower still: it is a read-modify-write of the
    # file everything else edits, held under the registry lock, so it runs
    # only on a first-ever spawn or an adopted rotation — exactly when it has
    # something new to say. (Both ordered after the delist post-check:
    # mark_started's jq assignment would otherwise re-create a just-deleted
    # session as a stub entry, and the side file of a session deleted in this
    # window is left to the sweep.)
    if [ "$bid" != "$recorded" ] || [ "$rotated" = true ]; then
      record_launch "$sname" "$bid"
    fi
    if [ "$launched" != true ] || [ "$rotated" = true ]; then
      mark_started "$sname" "$bid"
    fi
  fi
  registry_unlock
}

# Sweep webhook filter files left by sessions that are no longer listed.
# 'agent-box-session rm' and the settings page now prune their own, so this
# is the backstop for the two paths that cannot: sessions delisted while the
# unit was down, and every orphan that accumulated before the prune existed.
# Startup only — the reconcile loop below runs every 2s and this reads a
# directory; the set only changes on a delist, which both delete paths
# already handle.
#
# Deliberately keyed on the sessions FILE, not on tmux liveness: a listed
# session that is merely down (stopped, or between respawns) keeps its
# subscriptions. Only "$USER-<name>" files are candidates, which leaves the
# shared filter.dispatch.json (standing watches, not owned by any session)
# and the bare filter.json untouched.
sweep_orphan_filters() {
  _sd="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
  [ -d "$_sd" ] || return 0
  for _f in "$_sd"/filter."$USER"-*.json; do
    [ -e "$_f" ] || continue
    _n=${_f##*/filter."$USER"-}; _n=${_n%.json}
    case "$_n" in (*[!A-Za-z0-9_-]*|"") continue ;; esac
    # A file jq cannot answer for is left alone: a transient read error must
    # not be read as "this session is gone" and unsubscribe a live session.
    if $JQ -e --arg s "$_n" '.sessions | has($s)' "$REGISTRY_FILE" >/dev/null 2>&1; then
      continue
    elif $JQ -e . "$REGISTRY_FILE" >/dev/null 2>&1; then
      rm -f "$_f"
    fi
  done
}
sweep_orphan_filters

# Reclaim the supervisor's own per-session state for names the registry no
# longer lists (issue #282).
#
# Keyed on the registry, and run from the reconcile loop rather than hung off
# a delete path, because deletion is never guaranteed: nothing makes a hook
# agent call `agent-box-session rm`, a session can be delisted while this
# unit is down, and a box that upgrades into this file has no delete path to
# have taken. Both delete paths do prune their own as an OPTIMISATION — it
# closes the window in which a re-used name inherits the previous holder's
# launch id — but neither is what makes the state reclaimable.
#
# Stale state here is not merely litter: a session name is re-usable, so a
# file left by a deleted session would hand its launch id, and with it its
# transcript, to the next session that takes the name.
#
# One jq for the whole sweep, not one per file: this runs on every tick. A
# registry jq cannot answer for leaves everything alone — a transient read
# error, or a file being replaced by rename right now, must never be read as
# "no session exists". An EMPTY registry is a different answer from an
# unreadable one, and this sweeps on it: the last session deleted is exactly
# when the last file has to go.
NL="
"
sweep_session_state() {
  _listed="$($JQ -r '.sessions | keys[]' "$REGISTRY_FILE" 2>/dev/null)" || return 0
  for _f in "$SESSION_STATE_DIR"/*.json; do
    _n=${_f##*/}; _n=${_n%.json}
    # Also how an unmatched glob leaves this loop: the literal pattern is not
    # a legal session name, so nothing has to be stat'ed to skip it.
    case "$_n" in (*[!A-Za-z0-9_-]*|"") continue ;; esac
    case "$NL$_listed$NL" in (*"$NL$_n$NL"*) continue ;; esac
    rm -f "$_f"
  done
}

# Reconcile forever; systemd stop tears the whole tree down (ExecStop
# kill-server + cgroup kill), Restart=always revives a crashed loop.
# Sessions flagged stopped (a clean agent exit, or agent-box-session
# stop) stay listed but are left down until a restart clears the flag.
while true; do
  sweep_session_state
  while IFS= read -r sname; do
    case "$sname" in
      (*[!A-Za-z0-9_-]*|"") continue ;;
    esac
    $TMUX has-session -t "=$sname" 2>/dev/null || start_session "$sname"
  done < <($JQ -r '.sessions | to_entries[] | select(.value.stopped != true) | .key' "$REGISTRY_FILE" 2>/dev/null)
  sleep 2
done
