set -eu
# jq/python3/coreutils resolve from PATH (system packages); the pinned
# webhook.py store path comes from the env the generated wrapper exports
# (issue #154, Phase 2).
JQ=jq
PY=python3
SCRIPT="${AGENT_BOX_WEBHOOK_SCRIPT:?}"
# The supervisor puts these in every session's tmux environment; the
# fallbacks keep the CLI usable from a stray login shell or a cron job.
STATE_DIR="${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
SOURCES="$STATE_DIR/sources.json"
export LOCAL_WEBHOOK_STATE_DIR="$STATE_DIR"
# Never let a CLI invocation bind the ingress the daemon owns.
export LOCAL_WEBHOOK_PORT=0
# The box's own per-user state, not local-webhook's: the session registry the
# hook-* ceiling is counted from, and where the spawn wrapper writes down a
# batch it refused (issue #170). Both are read-only here.
SESSIONS="$HOME/.config/agent-box/sessions.json"
HOOK_REFUSED="$HOME/.local/state/agent-box/webhook-spawn-refused.json"

usage() {
  cat <<'USAGE'
usage: agent-box-webhook subscribe TOPIC [--note TEXT] [--ttl HOURS]
                                         [--deliver-to session|subagent]
                                         [--renew-on-event] [--ignore-sender LOGIN]...
                                         [--when JSON] [--drop JSON]
       agent-box-webhook unsubscribe TOPIC [--deliver-to session|subagent]
       agent-box-webhook ls
       agent-box-webhook status
       agent-box-webhook url
       agent-box-webhook setup [SOURCE]
       agent-box-webhook rotate [SOURCE]

Route webhook events to an agent instead of polling for them. TOPIC is
"source:key" — a bare "owner/repo" means github:owner/repo, and "owner/*"
also works. There is no wildcard for a whole source or for everything
(local-webhook 0.13.0), so name a repo or a prefix. A session receives only
what it subscribed to: no filter file now means no deliveries, where before
0.13.0 it meant all of them. --note says why you subscribed and is echoed
under every delivery, so a later session still has the context.

Two delivery shapes:
  --deliver-to session   (default) events arrive as messages in THIS
                         session. Per session, expires after 1h — --ttl
                         HOURS for a longer wait, --renew-on-event to reset
                         the clock on every delivery. Avoid --ttl 0 here: a
                         pinned topic interrupts whatever session is active,
                         indefinitely.
  --deliver-to subagent  standing watch, for events no session owns (new
                         issues/PRs, CI on a repo nobody is working on).
                         Each matching event batch spawns a FRESH hook-*
                         session primed with the event text; bursts coalesce
                         into one. SHARED across sessions and pinned (--ttl
                         0) by default; `ls` shows these under "dispatch".
                         local-webhook >= 0.23.0 has no built-in policy: a
                         subagent watch MUST carry --when/--drop rules or it
                         is refused outright — there is no failures-only CI
                         brake standing in for a rule-less one any more. A
                         GitHub topic (github:owner/repo, or the bare
                         owner/repo shorthand) with neither flag gets a
                         DEFAULT --when filled in here — opened/reopened
                         issues and PRs, an assignment or @mention naming
                         this box, a review verdict on a PR it wrote, and
                         terminal CI failure — the same vocabulary
                         services.agent-box.webhook.watchPolicy's own default
                         uses, scoped to this box's GitHub login when known.
                         Pass --when/--drop yourself to replace it, or to
                         subscribe a non-GitHub source (which gets no
                         default). A watch's own rules are the only thing
                         deciding whether it spawns; a live session
                         subscribed to the same topic still holds first
                         claim over any watch, GitHub or not.
                         A spawned session is subscribed to the event's own
                         repo for it, so its own CI spawns no sibling.
                         THERE IS A CEILING: at most 4 hook-* sessions may
                         RUN at once (AGENT_BOX_HOOK_SESSION_MAX in the
                         receiver daemon's environment). Hook sessions are
                         removed by the agent they start, so four of them
                         still running wedge every watch on the box — a
                         refused batch is DROPPED, never queued. Stopping one
                         frees its slot; `agent-box-session rm NAME` also
                         delists it. `status` reports the count, the ceiling
                         and the last refusal, and `ls` says so too once the
                         box is at the ceiling.

--ignore-sender LOGIN mutes echoes of that sender's own comments and pushes
("@self" is $LOCAL_WEBHOOK_SELF — the login this box acts as, resolved from
this environment's GitHub token by `agent-box-webhook-self`; with no token and
no cached answer it matches nobody). Since local-webhook 0.23.0 this is a PURE
sender mute — it also drops that sender's CI-outcome events, where earlier
versions delivered those anyway. Put the sender check INSIDE --when/--drop
instead ({"path": "sender.login", "notIn": [...]}) when a CI result from that
sender should still get through.

--when / --drop attach payload rules to the subscription: deliver (or
spawn) ONLY events matching --when, never those matching --drop. Rules are
JSON — {"any"/"all": [...]} over {"path": "a.b.c", "in"/"notIn": [values]}
leaves. A subagent subscription's rules are its ENTIRE policy (0.23.0 removed
the built-in failures-only CI fallback), and sender muting belongs INSIDE the
rules ({"path": "sender.login", "notIn": [...]}) rather than --ignore-sender.
NOTE: rules on the box's own standing watches may be managed declaratively
(services.agent-box.webhook.watchPolicy) and re-applied whenever the
receiver daemon starts — such entries say so in their note; change the
NixOS config, not the entry.

`status` prints JSON, and warns on stderr when the local-webhook a SESSION
loads is OLDER than the version this box pins, or when a live session
predates scoped instance sockets. Either one blinds the standing-watch
ownership brake, and neither is visible anywhere else (issue #193). A cache
NEWER than the pin is normal — claude tracks the marketplace's default
branch — and is reported in the JSON (`plugin.skew`) without a warning.

Its `dispatch` object is where to look when standing watches seem dead:
`hookSessions` is the running hook-* count against the ceiling, `lastRefusal`
is the batch the ceiling most recently dropped (with a running total), and
`warning` — the same field `ls` sets when the receiver has no spawn command
— is present exactly when a match right now would spawn nothing.

One-time per box, to make deliveries possible at all:
  agent-box-webhook setup      # mints the HMAC secret, prints URL + secret
then register that URL + secret in the sender (GitHub: repo Settings ->
Webhooks -> Add webhook, content type application/json, pick the events).
USAGE
}

ensure_state() { mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"; }

valid_source() {
  case "$1" in (*[!A-Za-z0-9_-]*|"") return 1 ;; esac
}

endpoint() {
  # Exported by the agent unit (alongside AGENT_BOX_URL) only when this user
  # actually has a browser terminal — which is exactly when Caddy serves the
  # endpoint, so an unset value means there is nothing to register.
  if [ -n "${AGENT_BOX_WEBHOOK_URL:-}" ]; then
    printf '%s' "$AGENT_BOX_WEBHOOK_URL"
  else
    echo "agent-box-webhook: no endpoint — this user has no browser terminal (web.passwordHashFile unset)" >&2
    return 1
  fi
}

secret_of() {
  # secret_of SOURCE — echo the configured secret, or nothing.
  [ -f "$SOURCES" ] || return 0
  f="$("$JQ" -r --arg s "$1" '.sources[$s].secretFile // empty' "$SOURCES" 2>/dev/null || true)"
  if [ -n "$f" ]; then
    case "$f" in (/*) ;; (*) f="$STATE_DIR/$f" ;; esac
    [ -f "$f" ] && cat "$f"
    return 0
  fi
  "$JQ" -r --arg s "$1" '.sources[$s].secret // empty' "$SOURCES" 2>/dev/null || true
}

# Which webhook.py the SESSIONS run, versus the one the box pins (issue #193).
# Two copies exist by design: the receiver daemon and this CLI run the pinned
# store path, while a claude session loads the plugin from claude's own cache,
# which claude clones and updates on its own schedule. Skew there is not
# cosmetic — a pre-0.10.0 peer names its IPC socket "<pid>.sock" instead of
# "<key>.<pid>.sock", which parses to no filter key, so the dispatch ownership
# brake sees no live session at all and one failing run spawns a session per CI
# event again (#192). The plugin fails that way deliberately (a mixed-version
# state dir loses the suppression rather than misapplying it), which is right,
# and which is exactly why the box has to say so: a brake that quietly does
# nothing reads like a brake that works (#170).
PLUGINS="$HOME/.claude/plugins/installed_plugins.json"

plugin_versions() {
  # Versions of the local-webhook plugin claude has installed, newest-listed
  # first, one per line. All scopes: a project-scope install shadows the user
  # one, so reporting only "user" could name a copy no session loads.
  [ -f "$PLUGINS" ] || return 0
  "$JQ" -r '[.plugins["local-webhook@local-channels"] // [] | .[] | .version // empty]
            | unique | .[]' "$PLUGINS" 2>/dev/null || true
}

peer_kinds() {
  # One word per LIVE session peer, from its instance socket name — which is
  # the only thing the dispatch brake has to go on:
  #   keyed   "<key>.<pid>.sock"  claims filter.<key>.json — its own subscriptions
  #   shared  ".<pid>.sock"       empty key, so it claims the shared filter.json
  #   legacy  "<pid>.sock"        pre-0.10.0: parses to no key, claims NOTHING
  # Liveness is checked per pid, like peer_scopes_live(): broadcast() only
  # unlinks a socket after a failed connect, so a crashed peer's socket
  # outlives it and would otherwise count as a live session. /proc rather than
  # `kill -0`, which cannot tell "no such process" from "not yours to signal"
  # — the plugin counts the second as alive, and so must this.
  for sock in "$STATE_DIR"/instances/*.sock "$STATE_DIR"/instances/.*.sock; do
    [ -S "$sock" ] || continue
    base="${sock##*/}"; base="${base%.sock}"
    pid="${base##*.}"
    case "$pid" in (""|*[!0-9]*) continue ;; esac
    [ -d "/proc/$pid" ] || continue
    if [ "$base" = "$pid" ]; then printf 'legacy\n'
    elif [ "${base%.*}" = "" ]; then printf 'shared\n'
    else printf 'keyed\n'; fi
  done
}

# ----------------------------------------------------- standing-watch cap ---
# Standing watches are the one delivery shape with no session behind it, so
# when the spawn wrapper refuses a batch (too many hook-* sessions running)
# webhook.py drops it and NOBODY got those events. That refusal used to
# reach only the receiver daemon journal while every listing here still said
# "subscribed" — four hook sessions whose agents forgot `agent-box-session rm`
# made the whole box inert and it read like a quiet week (issue #170). These
# three read the state the wrapper decides on, so `status` and `ls` can say it.

hook_sessions() {
  # The capacity in use, counted the way the wrapper counts it
  # (src/webhook-spawn.sh): a hook-* entry that is not `stopped` — running, or
  # queued for the supervisor's reconcile loop to start within ~2s. A `stopped`
  # entry is FREE capacity, so counting it here would report a healthy box as
  # wedged (issue #280) — the same over-count that used to wedge it for real.
  #
  # The wrapper additionally counts live hook-* tmux panes that no entry
  # claims, which needs the tmux binary its unit pins; this process has no such
  # pin, and the divergence can only under-count. The wrapper stays the
  # authority either way: when the two disagree, lastRefusal is the decision
  # that was enforced.
  if [ -s "$SESSIONS" ]; then
    "$JQ" -r '[.sessions | to_entries[]
               | select((.key | startswith("hook-")) and .value.stopped != true)]
              | length' "$SESSIONS" 2>/dev/null || printf '0'
  else
    printf '0'
  fi
}

hook_max() {
  # The ceiling itself. It is an env knob on the RECEIVER daemon unit, whose
  # environment this process does not share, so a box that raised it there and
  # nowhere else would read the built-in here. That is why a recorded refusal
  # carries the cap the wrapper actually applied: when the two disagree,
  # lastRefusal.max is the one that was enforced. Unset on both sides — the
  # normal case — makes them the same number.
  m="${AGENT_BOX_HOOK_SESSION_MAX:-4}"
  case "$m" in (""|*[!0-9]*) m=4 ;; esac
  printf '%s' "$m"
}

dispatch_topics() {
  # How many standing watches exist. The shared dispatch filter is webhook.py's
  # file and its format; this only counts entries, so that nothing warns about
  # a ceiling on a box that dispatches nothing.
  f="$STATE_DIR/filter.dispatch.json"
  if [ -s "$f" ]; then
    "$JQ" -r '(.topics // []) | length' "$f" 2>/dev/null || printf '0'
  else
    printf '0'
  fi
}

hook_capacity_warning() {
  # hook_capacity_warning LIVE MAX — the one sentence `status` and `ls` both
  # print when the ceiling has made the watches inert, and nothing when it has
  # not. One wording in one place: two copies would drift, and this is the
  # sentence the reader acts on.
  [ "$1" -ge "$2" ] || return 0
  printf '%s' "$1 of at most $2 hook-* sessions are running, so every \
standing watch is inert: a matching event batch is refused and DROPPED, never \
queued. Free a slot (agent-box-session ls, then agent-box-session stop NAME, \
or agent-box-session rm NAME to delist it for good), or raise \
AGENT_BOX_HOOK_SESSION_MAX on the receiver daemon unit."
}

# local-webhook >= 0.23.0 refuses to create a --deliver-to subagent entry
# that carries neither --when nor --drop (issue #380: the built-in
# failures-only CI brake that used to stand in for a rule-less one is gone).
# That is exactly the shape of the one-liner this box's own guide documents
# (`agent-box-webhook subscribe OWNER/REPO --deliver-to subagent --note ...`),
# so fill in a default --when here for a GitHub topic left rule-less — the
# same vocabulary services.agent-box.webhook.watchPolicy's own default uses
# (opened/reopened, assignment, @mention, a review verdict on our own PR,
# terminal CI failure), scoped to this box's login when it is known. A
# non-GitHub topic (a source this vocabulary cannot describe) gets nothing
# here and falls through to webhook.py's own refusal and its own message.
ci_failure_json='["failure","timed_out","action_required","startup_failure","stale","error"]'
default_subagent_when() {
  self="${LOCAL_WEBHOOK_SELF:-}"
  if [ -n "$self" ]; then
    "$JQ" -nc --arg self "$self" --argjson ci "$ci_failure_json" '
      {any: [
        {all: [{path:"action", "in":["opened","reopened"]}, {path:"sender.login", notIn:[$self]}]},
        {all: [{path:"action", "in":["assigned"]}, {path:"assignee.login", "in":[$self]},
               {path:"sender.login", notIn:[$self]}]},
        {all: [{path:"action", "in":["created","edited"]}, {path:"sender.login", notIn:[$self]},
               {path:"comment.body", contains:["@" + $self]}]},
        {all: [{path:"action", "in":["submitted"]},
               {path:"review.state", "in":["approved","changes_requested","commented"]},
               {path:"pull_request.user.login", "in":[$self]}, {path:"sender.login", notIn:[$self]}]},
        {path:"workflow_run.conclusion", "in":$ci}, {path:"workflow_job.conclusion", "in":$ci},
        {path:"check_run.conclusion", "in":$ci}, {path:"check_suite.conclusion", "in":$ci},
        {path:"deployment_status.state", "in":["error","failure"]}, {path:"state", "in":["error","failure"]}
      ]}'
  else
    # No known login: the sender-scoped clauses (assignment, mention, our own
    # review) cannot be written, so only the sender-agnostic ones apply —
    # narrower coverage than the box's own watchPolicy default, said out loud
    # below rather than silently.
    "$JQ" -nc --argjson ci "$ci_failure_json" '
      {any: [
        {path:"action", "in":["opened","reopened"]},
        {path:"workflow_run.conclusion", "in":$ci}, {path:"workflow_job.conclusion", "in":$ci},
        {path:"check_run.conclusion", "in":$ci}, {path:"check_suite.conclusion", "in":$ci},
        {path:"deployment_status.state", "in":["error","failure"]}, {path:"state", "in":["error","failure"]}
      ]}'
  fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  subscribe)
    ensure_state
    if [ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ]; then
      deliver_to=session; have_when=0; have_drop=0; topic=""; want=""
      for a in "$@"; do
        if [ -n "$want" ]; then
          case "$want" in (deliver-to) deliver_to="$a" ;; esac
          want=""; continue
        fi
        case "$a" in
          --deliver-to) want=deliver-to ;;
          --deliver-to=*) deliver_to="${a#--deliver-to=}" ;;
          --when|--when=*) have_when=1 ;;
          --drop|--drop=*) have_drop=1 ;;
          --*) ;;
          *) [ -n "$topic" ] || topic="$a" ;;
        esac
      done
      if [ "$deliver_to" = subagent ] && [ "$have_when" = 0 ] && [ "$have_drop" = 0 ]; then
        # A bare "owner/repo" (no "source:" prefix at all) is the github
        # shorthand; anything with its OWN "source:" prefix — including a
        # non-github one whose key happens to contain a "/", e.g.
        # "gitlab:group/project" — must fall through to webhook.py's refusal
        # instead of getting GitHub-only vocabulary tacked onto it.
        is_github=0
        case "$topic" in
          (github:*) is_github=1 ;;
          (*:*) is_github=0 ;;
          (*/*) is_github=1 ;;
          (*) is_github=0 ;;
        esac
        if [ "$is_github" = 1 ]; then
          w="$(default_subagent_when)"
          set -- "$@" --when "$w"
          if [ -n "${LOCAL_WEBHOOK_SELF:-}" ]; then
            echo "agent-box-webhook: no --when/--drop given for a subagent watch on" \
                 "$topic — defaulting to the box's own GitHub triage rules, scoped to" \
                 "$LOCAL_WEBHOOK_SELF; pass --when/--drop yourself to replace them" >&2
          else
            echo "agent-box-webhook: no --when/--drop given for a subagent watch on" \
                 "$topic, and this box's GitHub login is unknown — defaulting to" \
                 "opened/reopened plus CI failures only (no assignment/mention/review" \
                 "clauses); pass --when/--drop yourself for the full default" >&2
          fi
        fi
      fi
    fi
    exec "$PY" "$SCRIPT" "$cmd" "$@"
    ;;
  unsubscribe)
    # webhook.py owns topic parsing, TTL/renew semantics and the filter
    # file — including the per-session LOCAL_WEBHOOK_SESSION scope, which
    # the supervisor already put in this session's environment.
    ensure_state
    exec "$PY" "$SCRIPT" "$cmd" "$@"
    ;;
  ls|subscriptions)
    # Same delegation, deliberately not exec'd: a listing of standing watches
    # is exactly where a wedged box has to say it is wedged (issue #170).
    # stdout stays byte-for-byte webhook.py's, so a caller parsing the listing
    # is unaffected; the line goes to stderr like every other warning here.
    ensure_state
    "$PY" "$SCRIPT" "$cmd" "$@"
    if [ "$(dispatch_topics)" -gt 0 ]; then
      w="$(hook_capacity_warning "$(hook_sessions)" "$(hook_max)")"
      [ -z "$w" ] || echo "agent-box-webhook: $w Run" \
        "'agent-box-webhook status' for the last refused batch." >&2
    fi
    ;;
  status)
    # Same status webhook.py prints (its own version is the pin, since this
    # wrapper runs the pinned script), plus the two facts only the box can
    # see: what a NEW session would load, and what the live ones DID load.
    ensure_state
    out="$("$PY" "$SCRIPT" status "$@")" || exit $?
    pinned="$(printf '%s' "$out" | "$JQ" -r '.version // ""')"
    installed="$(plugin_versions | tr '\n' ' ')"; installed="${installed% }"
    keyed=0; shared=0; legacy=0
    # Word-splitting is the point here — peer_kinds emits one bare word per
    # live peer and nothing else.
    for kind in $(peer_kinds); do
      case "$kind" in
        (keyed) keyed=$((keyed + 1)) ;;
        (shared) shared=$((shared + 1)) ;;
        (legacy) legacy=$((legacy + 1)) ;;
      esac
    done
    # Which side of the pin the cache sits on. Only "older" is a fault: claude
    # tracks the marketplace's default branch, so a cache AHEAD of the pin is
    # the normal state between pin bumps, and calling that an error every time
    # would train everyone to ignore the line that matters.
    # The OLDEST installed copy decides: a project-scope install shadows the
    # user one, and the stalest is the one that can break the brake.
    skew=none
    if [ -z "$installed" ]; then
      skew=unknown
    elif [ -n "$pinned" ]; then
      oldest="$(printf '%s\n' $installed | sort -V | head -1)"
      if [ "$oldest" = "$pinned" ]; then
        skew=none
      elif [ "$(printf '%s\n%s\n' "$oldest" "$pinned" | sort -V | head -1)" = "$oldest" ]; then
        skew=older
      else
        skew=newer
      fi
    fi
    # ...and the third: whether a standing watch could spawn anything at all
    # (issue #170). dispatchTopicCount says how many are subscribed; it does
    # not say that the box is at its hook-* ceiling, which drops every match.
    hlive="$(hook_sessions)"
    hmax="$(hook_max)"
    dtopics="$(printf '%s' "$out" | "$JQ" -r '.dispatchTopicCount // 0')"
    refusal=null
    if [ -s "$HOOK_REFUSED" ]; then
      refusal="$("$JQ" -c . "$HOOK_REFUSED" 2>/dev/null)" || refusal=null
      [ -n "$refusal" ] || refusal=null
    fi
    # ONE field answers one question — "would a match spawn a session right
    # now?" — so there is a single place to look. webhook.py already sets
    # dispatch.warning in the `ls` listing for the no-spawn-command case, so
    # this reuses that name and that meaning instead of opening a second
    # channel; the two causes are mutually exclusive (no spawner at all beats
    # a full one). Nothing is said when a match would spawn.
    dwarn=""
    if [ "$dtopics" -gt 0 ]; then
      # "unknown" is a receiver that has advertised nothing (no receiver.json,
      # so probably not running). Never claim its spawn command is missing on
      # that evidence — webhook.py does not either — but the ceiling below is
      # true whether or not the daemon is up, so it is still reported.
      spawn="$(printf '%s' "$out" \
        | "$JQ" -r 'if .receiver == null then "unknown"
                    elif .receiver.spawn then "yes" else "no" end')"
      if [ "$spawn" = no ]; then
        dwarn="receiver daemon has no LOCAL_WEBHOOK_SPAWN_CMD configured; dispatch topics are inert"
      else
        dwarn="$(hook_capacity_warning "$hlive" "$hmax")"
      fi
    fi
    printf '%s' "$out" | "$JQ" \
      --arg installed "$installed" --arg pf "$PLUGINS" --arg skew "$skew" \
      --argjson keyed "$keyed" --argjson shared "$shared" --argjson legacy "$legacy" \
      --argjson hlive "$hlive" --argjson hmax "$hmax" --argjson refusal "$refusal" \
      --arg dwarn "$dwarn" '
        .plugin = {sessionVersions: ($installed | if . == "" then [] else split(" ") end),
                   pinnedVersion: .version, skew: $skew, installedFrom: $pf}
        | .peers = {live: ($keyed + $shared + $legacy),
                    keyed: $keyed, shared: $shared, legacy: $legacy}
        | .dispatch = ({topicCount: .dispatchTopicCount,
                        spawnCommand: (if .receiver == null then null
                                       else .receiver.spawn == true end),
                        hookSessions: {live: $hlive, max: $hmax,
                                       atCapacity: ($hlive >= $hmax)},
                        lastRefusal: $refusal}
                       + (if $dwarn == "" then {} else {warning: $dwarn} end))'
    # Warnings on stderr only, and only when something is wrong: status stays
    # valid JSON on stdout for a caller that parses it, and a healthy box says
    # nothing at all.
    if [ "$skew" = unknown ]; then
      echo "agent-box-webhook: cannot tell which local-webhook a session loads" \
           "($PLUGINS is missing) — if sessions run one, its version is unverified (issue #193)" >&2
    elif [ "$skew" = older ]; then
      echo "agent-box-webhook: version skew — sessions load local-webhook $installed," \
           "OLDER than the pinned $pinned. webhook.syncSessionPlugin normally cures this at the" \
           "next session start; by hand: claude plugin marketplace update local-channels &&" \
           "claude plugin update local-webhook@local-channels (issue #193)" >&2
    fi
    if [ -n "$dwarn" ]; then
      echo "agent-box-webhook: $dwarn" >&2
    fi
    if [ "$legacy" -gt 0 ]; then
      echo "agent-box-webhook: $legacy live session peer(s) name their socket the pre-0.10.0" \
           "way, so they claim no subscriptions at all. The dispatch ownership brake cannot see" \
           "those sessions, and a failing run can spawn one hook session per CI event (issue" \
           "#192). Updating the plugin does not reach them — a session loads its interpreter" \
           "once — so restart them: agent-box-session restart NAME" >&2
    fi
    ;;
  url)
    url="$(endpoint)"
    printf 'endpoint: %s\n' "$url"
    if [ -f "$SOURCES" ]; then
      "$JQ" -r '"sources:  " + ((.sources | keys | join(", ")) // "(none)")' "$SOURCES"
      printf 'per-source path: %s/<source>  (bare %s -> %s)\n' \
        "$url" "$url" "$("$JQ" -r '.defaultSource // "github"' "$SOURCES")"
    else
      echo 'sources:  (none yet — run: agent-box-webhook setup)'
    fi
    ;;
  setup)
    src="${1:-github}"
    valid_source "$src" || { echo "invalid source name '$src' (letters, digits, _ and - only)" >&2; exit 2; }
    url="$(endpoint)"
    ensure_state
    existing="$(secret_of "$src")"
    if [ -n "$existing" ]; then
      secret="$existing"
      note="already configured — reusing the existing secret"
    else
      # 32 hex chars from the kernel CSPRNG; GitHub accepts any string.
      secret="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
      umask 077
      printf '%s' "$secret" > "$STATE_DIR/$src.secret"
      note="secret written to $STATE_DIR/$src.secret (0600)"
      # Merge, never clobber: another source may already be configured, and
      # defaultSource must keep pointing at whatever was set up first.
      [ -f "$SOURCES" ] || printf '{"sources":{}}\n' > "$SOURCES"
      tmp="$(mktemp "$SOURCES.XXXXXX")"
      if "$JQ" --arg s "$src" \
           '.sources = ((.sources // {}) + {($s): (((.sources // {})[$s]) // {} | .secretFile = ($s + ".secret"))})
            | .defaultSource = (.defaultSource // $s)' "$SOURCES" > "$tmp"; then
        chmod 600 "$tmp"; mv "$tmp" "$SOURCES"
      else
        rm -f "$tmp"; exit 1
      fi
    fi
    cat <<EOF
$note

Register this in the sender ($src):
  Payload URL   $url/$src
  Secret        $secret
  Content type  application/json
  Signature     HMAC-SHA256 of the raw body, hex, in x-hub-signature-256
EOF
    # The one-liner is only correct for GitHub; another sender has its own
    # registration flow (and possibly its own signature header, which
    # sources.json's signatureHeader covers).
    if [ "$src" = github ]; then
      cat <<EOF

In the repo: Settings -> Webhooks -> Add webhook. With admin rights:
  gh api -X POST repos/OWNER/REPO/hooks -f name=web -F active=true \\
    -f 'config[url]=$url/$src' -f 'config[secret]=$secret' \\
    -f 'config[content_type]=json' \\
    -f 'events[]=push' -f 'events[]=pull_request' \\
    -f 'events[]=pull_request_review' -f 'events[]=issue_comment' \\
    -f 'events[]=workflow_run' -f 'events[]=check_run'
EOF
    fi
    cat <<EOF

Then route the events you care about into this session:
  agent-box-webhook subscribe OWNER/REPO --note "why you care"

Or, for events no session owns, a standing watch that spawns a fresh
session per event batch instead of interrupting this one:
  agent-box-webhook subscribe OWNER/REPO --deliver-to subagent --note "triage"
EOF
    ;;
  rotate)
    # Mint a NEW secret for a source that already exists, so a leaked one can
    # be replaced. `setup` deliberately reuses an existing secret (it is the
    # idempotent "make this work" command), which left no way to change one.
    #
    # Deliberately a HARD cutover: the receiver verifies against exactly one
    # secret per source (webhook.py's source_secret), so from the moment this
    # returns, a delivery still signed with the old secret is answered 401 —
    # and GitHub does not retry, so events in the window between here and
    # updating the sender are lost, not delayed. Say so, loudly, rather than
    # implying an overlap that does not exist (an overlap needs the receiver
    # to accept a previous secret: defangdevs/local-channels#49).
    src="${1:-}"
    if [ -z "$src" ]; then
      src="$("$JQ" -r '.defaultSource // "github"' "$SOURCES" 2>/dev/null || echo github)"
    fi
    valid_source "$src" || { echo "invalid source name '$src' (letters, digits, _ and - only)" >&2; exit 2; }
    url="$(endpoint)"
    ensure_state
    entry="$("$JQ" -c --arg s "$src" '.sources[$s] // empty' "$SOURCES" 2>/dev/null || true)"
    if [ -z "$entry" ]; then
      echo "agent-box-webhook: no source '$src' to rotate — run: agent-box-webhook setup $src" >&2
      exit 1
    fi
    # An inline `secret` in sources.json WINS over secretFile in the receiver,
    # so rotating the file while an inline value sits there would change
    # nothing and say it had. Move the source onto its file and drop the
    # inline key in the same edit.
    inline="$("$JQ" -r --arg s "$src" '.sources[$s].secret // ""' "$SOURCES" 2>/dev/null || true)"
    file="$("$JQ" -r --arg s "$src" '.sources[$s].secretFile // ""' "$SOURCES" 2>/dev/null || true)"
    # No secretFile yet (a hand-written inline-only source): give it the same
    # name setup would have, and record it below.
    declared="$file"
    [ -n "$file" ] || file="$src.secret"
    case "$file" in (/*) path="$file" ;; (*) path="$STATE_DIR/$file" ;; esac
    secret="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
    umask 077
    tmp="$(mktemp "$path.XXXXXX")"
    printf '%s' "$secret" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$path"
    if [ -n "$inline" ] || [ "$declared" != "$file" ]; then
      tmp="$(mktemp "$SOURCES.XXXXXX")"
      if "$JQ" --arg s "$src" --arg f "$file" \
           '.sources[$s] = ((.sources[$s] // {}) | del(.secret) | .secretFile = $f)' \
           "$SOURCES" > "$tmp"; then
        chmod 600 "$tmp"; mv "$tmp" "$SOURCES"
      else
        rm -f "$tmp"; exit 1
      fi
    fi
    cat <<EOF
rotated $src — new secret written to $path (0600)

UPDATE THE SENDER NOW. Deliveries signed with the old secret are rejected
(401) from this moment, and GitHub does not retry them.

  Payload URL   $url/$src
  Secret        $secret
EOF
    if [ "$src" = github ]; then
      cat <<EOF

In the repo: Settings -> Webhooks -> the hook -> Secret. With admin rights,
find the hook id and patch it in place:
  gh api repos/OWNER/REPO/hooks --jq '.[] | "\(.id) \(.config.url)"'
  gh api -X PATCH repos/OWNER/REPO/hooks/HOOK_ID -f 'config[secret]=$secret'
EOF
    fi
    ;;
  ""|-h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
