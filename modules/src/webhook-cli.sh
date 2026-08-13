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

Route webhook events to an agent instead of polling for them. TOPIC is
"source:key" — a bare "owner/repo" means github:owner/repo, "owner/*" and "*"
also work. --note says why you subscribed and is echoed under every delivery,
so a later session still has the context.

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
                         A CI event spawns only if it reports a FAILURE, and
                         never while a live session is subscribed to that
                         topic; new issues and others' PRs always spawn.
                         A spawned session is subscribed to the event's own
                         repo for it, so its own CI spawns no sibling.

--ignore-sender LOGIN mutes echoes of that sender's own comments and pushes
("@self" is $LOCAL_WEBHOOK_SELF); CI-outcome events are delivered anyway.

--when / --drop attach payload rules to the subscription: deliver (or
spawn) ONLY events matching --when, never those matching --drop. Rules are
JSON — {"any"/"all": [...]} over {"path": "a.b.c", "in"/"notIn": [values]}
leaves. A subscription with rules sets its own policy: the failure-only CI
brake steps aside for it, and sender muting belongs INSIDE the rules
({"path": "sender.login", "notIn": [...]}) rather than --ignore-sender.
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

cmd="${1:-}"; shift || true
case "$cmd" in
  subscribe|unsubscribe|ls|subscriptions)
    # webhook.py owns topic parsing, TTL/renew semantics and the filter
    # file — including the per-session LOCAL_WEBHOOK_SESSION scope, which
    # the supervisor already put in this session's environment.
    ensure_state
    exec "$PY" "$SCRIPT" "$cmd" "$@"
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
    printf '%s' "$out" | "$JQ" \
      --arg installed "$installed" --arg pf "$PLUGINS" --arg skew "$skew" \
      --argjson keyed "$keyed" --argjson shared "$shared" --argjson legacy "$legacy" '
        .plugin = {sessionVersions: ($installed | if . == "" then [] else split(" ") end),
                   pinnedVersion: .version, skew: $skew, installedFrom: $pf}
        | .peers = {live: ($keyed + $shared + $legacy),
                    keyed: $keyed, shared: $shared, legacy: $legacy}'
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
  ""|-h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
