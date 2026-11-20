#!/usr/bin/env bash
# What modules/src/webhook-spawn.sh does with agent-box-watch-route's answer (#1056).
#
# The router is the one place a standing watch can decide NOT to start a session,
# so the assertions are weighted at its failure modes: every way the router can
# fail (not pinned, not executable, exits non-zero, prints nothing, prints
# garbage, prints an unknown action) must still start a session, and a batch
# delivered to somebody else must leave no claim or lease for a session that
# never existed. agent-box-session, the env store and the router are shims.
set -u

SCRIPT=${1:?usage: test-webhook-spawn-route.sh PATH/TO/webhook-spawn.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SRC=$(cd "$(dirname "$SCRIPT")" && pwd)
BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

python3 - "$SRC" "$SCRIPT" "$work/spawn.sh" <<'PY'
import pathlib, re, sys
src, script, out = (pathlib.Path(p) for p in sys.argv[1:4])
def expand(path, depth=0):
    if depth > 8:
        raise SystemExit(f"include loop at {path}")
    lines = []
    for line in path.read_text().splitlines(keepends=True):
        m = re.match(r"\s*@@include:(\S+)@@\s*$", line)
        lines.append(expand(src / m.group(1), depth + 1) if m else line)
    return "".join(lines)
out.write_text(expand(script))
PY

export HOME="$work/home"; mkdir -p "$HOME/.config/agent-box"
export LOCAL_WEBHOOK_STATE_DIR="$work/state"; mkdir -p "$LOCAL_WEBHOOK_STATE_DIR"
mkdir -p "$work/bin"
# `add` records its argv, one argument per line, so the prompt can be read back.
cat > "$work/bin/session" <<SH
#!$BASH_BIN
[ "\${1:-}" = add ] && { printf '%s\n' "\$@" > "$work/add.log"; }
exit 0
SH
cat > "$work/bin/envstore" <<SH
#!$BASH_BIN
exit 1
SH
# The router shim records the request it was given and answers with \$ROUTE_ANSWER.
# "links" is the second verb the wrapper calls for NEW_LINKS.
cat > "$work/bin/route" <<SH
#!$BASH_BIN
if [ "\$1" = links ]; then
  echo "- codex transcript, last written 5m ago, mentions #42: /home/x/rollout-1.jsonl (its session cannot receive messages; do the work or ask the person)"
  exit 0
fi
cat > "$work/route-request.json"
[ -n "\${ROUTE_FAIL:-}" ] && exit \$ROUTE_FAIL
printf '%s' "\${ROUTE_ANSWER:-}"
SH
chmod +x "$work/bin/session" "$work/bin/envstore" "$work/bin/route"
export AGENT_BOX_SESSION_BIN="$work/bin/session"
export AGENT_BOX_ENVSTORE_BIN="$work/bin/envstore"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# spawn ROUTE_BIN_VALUE -> runs one batch, returns the wrapper's status
spawn() {
  rm -f "$work/add.log" "$work/route-request.json" "$LOCAL_WEBHOOK_STATE_DIR"/filter.*.json
  rm -rf "$HOME/.local/state/agent-box/lease"
  env AGENT_BOX_WATCH_ROUTE_BIN="$1" \
      LOCAL_WEBHOOK_SPAWN_SOURCE=github \
      LOCAL_WEBHOOK_SPAWN_KEY=acme/widgets \
      LOCAL_WEBHOOK_SPAWN_TOPIC='github:acme/*' \
      LOCAL_WEBHOOK_SPAWN_EVENT=issue_comment \
      LOCAL_WEBHOOK_SPAWN_META='{"number":"42","action":"created"}' \
      "$BASH_BIN" "$work/spawn.sh" >/dev/null 2>"$work/stderr" <<< '[acme/widgets] PR #42 comment: please rebase'
}
started()  { [ -f "$work/add.log" ]; }
seeded()   { ls "$LOCAL_WEBHOOK_STATE_DIR"/filter.*.json >/dev/null 2>&1; }

# 1. CONTINUE: handled, nothing started, nothing claimed.
export ROUTE_ANSWER='{"action":"CONTINUE","session":"owner","links":[],"reason":"llm"}'
spawn "$work/bin/route"; rc=$?
{ [ "$rc" = 0 ] && ! started; } && ok "CONTINUE exits 0 and starts no session" || fail "CONTINUE exits 0 and starts no session (rc=$rc)"
seeded && fail "CONTINUE seeds no claim for a session that never exists" || ok "CONTINUE seeds no claim for a session that never exists"
[ -d "$HOME/.local/state/agent-box/lease" ] && fail "CONTINUE creates no lease" || ok "CONTINUE creates no lease"
grep -q 'routed this batch to session owner' "$work/stderr" && ok "CONTINUE says where it went" || fail "CONTINUE says where it went"

# 2. the request carries what the router needs, as JSON built with jq (no shell quoting of event text).
jq -e '.key == "acme/widgets" and .topic == "github:acme/*" and .event == "issue_comment"
       and .source == "github" and .meta.number == "42"
       and (.text | contains("please rebase")) and (.id | length > 0)' "$work/route-request.json" >/dev/null \
  && ok "the router gets key, topic, event, meta, text and a batch id" || fail "the router gets key, topic, event, meta, text and a batch id"

# 3. NEW and NEW_LINKS start a session; links reach the prompt.
export ROUTE_ANSWER='{"action":"NEW","session":null,"links":[]}'
spawn "$work/bin/route"
{ started && ! grep -q 'EARLIER WORK' "$work/add.log"; } && ok "NEW starts a session with no links paragraph" || fail "NEW starts a session with no links paragraph"
export ROUTE_ANSWER='{"action":"NEW_LINKS","session":null,"links":[{"harness":"codex"}]}'
spawn "$work/bin/route"
{ started && grep -q 'EARLIER WORK ON THESE EVENTS' "$work/add.log" && grep -q 'codex transcript' "$work/add.log" \
  && grep -q 'cannot receive messages' "$work/add.log"; } \
  && ok "NEW_LINKS puts the earlier transcripts, of any harness, in the prompt" || fail "NEW_LINKS puts the earlier transcripts, of any harness, in the prompt"

# 4. every failure of the router means a session, as before.
fail_open() {  # $1 label
  if started; then ok "fails open: $1"; else fail "fails open: $1"; fi
}
export ROUTE_ANSWER='{"action":"CONTINUE","session":"owner"}'
spawn ""                 ; fail_open "no pin"
spawn "$work/bin/missing"; fail_open "pinned binary missing"
ROUTE_FAIL=3 spawn "$work/bin/route"; fail_open "router exits non-zero"
ROUTE_ANSWER='' spawn "$work/bin/route"; fail_open "router prints nothing"
ROUTE_ANSWER='not json at all' spawn "$work/bin/route"; fail_open "router prints garbage"
ROUTE_ANSWER='{"action":"DROP"}' spawn "$work/bin/route"; fail_open "router prints an unknown action"
ROUTE_ANSWER='{"action":"CONTINUE"' spawn "$work/bin/route"; fail_open "router prints truncated JSON"

# 5. The `routed` source (webhook-cli.sh) is keyed on the session and is NOT owner-trusted:
# what it carries was written by a stranger, unlike the WhatsApp router's `inbox`.
CLI="$SRC/webhook-cli.sh"
if [ -f "$CLI" ]; then
  sed -n '/^source_template() {/,/^}/p' "$CLI" > "$work/template.sh"
  routed="$("$BASH_BIN" -c ". '$work/template.sh'; source_template routed" 2>/dev/null)"
  inbox="$("$BASH_BIN" -c ". '$work/template.sh'; source_template inbox" 2>/dev/null)"
  printf '%s' "$routed" | jq -e '.keyPath == "session" and (has("trust") | not) and .retainPayload == true' >/dev/null \
    && ok "the routed source is keyed on the session and carries no owner trust" \
    || fail "the routed source is keyed on the session and carries no owner trust: $routed"
  printf '%s' "$inbox" | jq -e '.trust == "owner"' >/dev/null \
    && ok "negative control: the inbox source IS owner-trusted" || fail "negative control: the inbox source IS owner-trusted"
else
  fail "webhook-cli.sh not found next to the spawn script"
fi

[ "$fails" = 0 ] && { echo "all webhook-spawn route checks passed"; exit 0; }
echo "$fails webhook-spawn route check(s) failed" >&2
exit 1
