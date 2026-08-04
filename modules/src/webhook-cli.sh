    set -eu
    JQ=${pkgs.jq}/bin/jq
    PY=${webhookPython}
    SCRIPT=${localWebhookScript}
    # The supervisor puts these in every session's tmux environment; the
    # fallbacks keep the CLI usable from a stray login shell or a cron job.
    STATE_DIR="''${LOCAL_WEBHOOK_STATE_DIR:-$HOME/.local/state/local-webhook}"
    SOURCES="$STATE_DIR/sources.json"
    export LOCAL_WEBHOOK_STATE_DIR="$STATE_DIR"
    # Never let a CLI invocation bind the ingress the daemon owns.
    export LOCAL_WEBHOOK_PORT=0

    usage() {
      cat <<'USAGE'
    usage: agent-box-webhook subscribe TOPIC [--note TEXT] [--ttl HOURS]
                                             [--deliver-to session|subagent]
                                             [--renew-on-event] [--ignore-sender LOGIN]...
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

    --ignore-sender LOGIN mutes echoes of that sender's own comments and pushes
    ("@self" is $LOCAL_WEBHOOK_SELF); CI-outcome events are delivered anyway.

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
      if [ -n "''${AGENT_BOX_WEBHOOK_URL:-}" ]; then
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

    cmd="''${1:-}"; shift || true
    case "$cmd" in
      subscribe|unsubscribe|ls|subscriptions|status)
        # webhook.py owns topic parsing, TTL/renew semantics and the filter
        # file — including the per-session LOCAL_WEBHOOK_SESSION scope, which
        # the supervisor already put in this session's environment.
        ensure_state
        exec "$PY" "$SCRIPT" "$cmd" "$@"
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
        src="''${1:-github}"
        valid_source "$src" || { echo "invalid source name '$src' (letters, digits, _ and - only)" >&2; exit 2; }
        url="$(endpoint)"
        ensure_state
        existing="$(secret_of "$src")"
        if [ -n "$existing" ]; then
          secret="$existing"
          note="already configured — reusing the existing secret"
        else
          # 32 hex chars from the kernel CSPRNG; GitHub accepts any string.
          secret="$(${pkgs.coreutils}/bin/od -An -tx1 -N16 /dev/urandom | ${pkgs.coreutils}/bin/tr -d ' \n')"
          umask 077
          printf '%s' "$secret" > "$STATE_DIR/$src.secret"
          note="secret written to $STATE_DIR/$src.secret (0600)"
          # Merge, never clobber: another source may already be configured, and
          # defaultSource must keep pointing at whatever was set up first.
          [ -f "$SOURCES" ] || printf '{"sources":{}}\n' > "$SOURCES"
          tmp="$(${pkgs.coreutils}/bin/mktemp "$SOURCES.XXXXXX")"
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
