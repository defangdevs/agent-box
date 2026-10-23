#!/usr/bin/env bash
# Unit tests for the claim a DISPATCHED session is seeded with
# (`claim_include` / `claim_note` in modules/src/webhook-spawn.sh).
#
# A hook session declares which object it owns in its own filter file, written
# before the session exists, and that declaration is the only thing that stops
# a standing watch spawning a second agent on top of it. Its failure mode is
# silent — an over-narrow claim warns about nothing, a sibling just turns up
# and starts editing the same worktree (#417/#420) — so the payload paths a
# claim covers are worth pinning byte for byte.
#
# These assertions used to live in tests/webhook.nix, where they cost a VM
# boot to check a pure function of LOCAL_WEBHOOK_SPAWN_META. They also pushed
# that test's script past a hard limit: nixpkgs passes testScript to the
# driver build as an ENVIRONMENT VARIABLE, and Linux caps one variable at
# MAX_ARG_STRLEN (128 KiB), so the driver failed to build with "Argument list
# too long" before any VM booted. What the VM still owns is the wiring — that
# a delivery spawns a session at all, and that it lands with a filter file.
#
# agent-box-session and the env store are shimmed, so nothing here needs tmux,
# a registry or a real session. webhook.py is the REAL pinned one: shape is
# only half the promise, and the other half is the matcher agreeing.
set -u

SCRIPT=${1:?usage: test-webhook-spawn-claim.sh PATH/TO/webhook-spawn.sh [WEBHOOK.PY]}
WEBHOOK_PY=${2:-}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SRC=$(cd "$(dirname "$SCRIPT")" && pwd)

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The source form carries @@include markers the assembler resolves; expand
# them the same way (nested includes and all) so the test drives the same
# text the box runs.
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
# `add` is the exec target and `peers` the read the preamble embeds; neither
# is under test here, so both just succeed quietly.
cat > "$work/bin/session" <<EOF
#!$BASH_BIN
exit 0
EOF
# No hook profile and no extra args: the env store answers "unset" (rc 1).
cat > "$work/bin/envstore" <<EOF
#!$BASH_BIN
exit 1
EOF
chmod +x "$work/bin/session" "$work/bin/envstore"
export AGENT_BOX_SESSION_BIN="$work/bin/session"
export AGENT_BOX_ENVSTORE_BIN="$work/bin/envstore"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# Drive one spawn and echo the filter file it seeded.
#   $1 the repo key, $2 the LOCAL_WEBHOOK_SPAWN_META json
spawn() {
  rm -f "$LOCAL_WEBHOOK_STATE_DIR"/filter.*.json
  env LOCAL_WEBHOOK_SPAWN_SOURCE=${SPAWN_SRC:-github} \
      LOCAL_WEBHOOK_SPAWN_KEY="$1" \
      LOCAL_WEBHOOK_SPAWN_TOPIC='github:defangdevs/*' \
      LOCAL_WEBHOOK_SPAWN_EVENT=workflow_run \
      LOCAL_WEBHOOK_SPAWN_META="$2" \
      ${SPAWN_METAS+LOCAL_WEBHOOK_SPAWN_METAS="$SPAWN_METAS"} \
      "$BASH_BIN" "$work/spawn.sh" >/dev/null 2>&1 <<< 'hi' || return 1
  set -- "$LOCAL_WEBHOOK_STATE_DIR"/filter.*.json
  [ -f "$1" ] || return 1
  printf '%s' "$1"
}

# $1 name, $2 filter file, $3 jq predicate
check() {
  if [ -n "$2" ] && jq -e "$3" "$2" >/dev/null 2>&1; then
    ok "$1"
  else
    fail "$1"
    [ -n "$2" ] && jq -c '.topics[0]' "$2" 2>/dev/null | sed 's/^/     /'
  fi
}

# The lease file (issue #535), keyed by session name — resolved by NAME
# rather than a fixed glob, since the wrapper mints a random suffix. Exact
# prefix strip (not a wildcard) so a hyphenated build-sandbox username (or
# hyphenated session name, which every spawn() call here has) never confuses
# the two. $1 the lease's expected topic, $2 its expected object (jq value,
# e.g. '"4242"' or 'null'), $3 the filter file `spawn` returned (its own name
# carries the session name spawn() minted).
SPAWN_USER=$(id -un)
check_lease() {
  ses=$(basename "$3" .json); ses=${ses#filter."$SPAWN_USER"-}
  lf="$HOME/.local/state/agent-box/lease/$ses.json"
  check "$4" "$lf" \
    "(.topic == $1) and (.object == $2) and (.outcome == null) and (.claimedAt != null)"
}

# --- a non-GitHub source (agent-box#875) ---
#
# A Slack event has no number and no sha, so it used to fall to the GitHub CI
# claim: a predicate nothing in a Slack payload matches, and a note about
# "topic branches". Every follow-up in the thread then started a session.
SPAWN_SRC=slack
f=$(spawn T1/C1 '{"event":"thread.settled","key":"T1/C1","identifier":"T1/C1/1700000000.000100"}')
check "a slack spawn claims exactly its thread identifier" "$f" \
  '.topics[0].topic == "slack:T1/C1"
   and .topics[0].include == {path: "identifier", in: ["T1/C1/1700000000.000100"]}'
check "a slack note names the object, not GitHub CI" "$f" \
  '.topics[0].note | contains("T1/C1/1700000000.000100") and (contains("TOPIC BRANCHES") | not)'
f=$(spawn T1/C1 '{"event":"thread.settled","key":"T1/C1"}')
check "a slack spawn with no identifier claims nothing, honestly" "$f" \
  '(.topics[0] | has("include") | not) and (.topics[0].note | contains("claims nothing"))'
f=$(spawn T1/C1 '{"event":"thread.settled","identifier":"x\"} or {\"a"}')
check "a malformed identifier is not turned into a predicate" "$f" \
  '.topics[0] | has("include") | not'
# spawn() clears earlier filters, so keep a copy for the matcher check below.
slack_filter="$work/slack-filter.json"
cp "$(spawn T1/C1 '{"event":"thread.settled","identifier":"T1/C1/1.5"}')" "$slack_filter"
unset SPAWN_SRC

# --- a numbered object: exactly that issue or PR, and never the repo's CI ---
#
# Not workflow_run.pull_requests.0.number either, which reads well and never
# matches: the predicate walker steps through dicts only, so a list index ends
# the walk (local-channels#46).
f=$(spawn defangdevs/numbered '{"event":"issues","action":"assigned","number":"4242"}')
check "a numbered spawn claims both number paths, and no CI path" "$f" \
  '(.topics[0].include.any | map(.path)) == ["issue.number", "pull_request.number"]
   and (.topics[0].include.any | all(.in == [4242]))'
# The note is echoed under every delivery, so a fresh-context session reads it
# to learn why the event matters. It used to be one fixed sentence, identical
# across every hook session on the box (#251).
check "the note names the object it claims" "$f" \
  '.topics[0].note | contains("defangdevs/numbered#4242") and contains("issues.assigned")'
# The durable lease agrees with the claim on the SAME object -- both are
# derived from the same $meta.number, so a numbered claim never sits beside
# an object-less lease or vice versa.
check_lease '"github:defangdevs/numbered"' '"4242"' "$f" \
  "a numbered spawn's lease names the same object as its claim"

# --- a `number` that is not a number ---
#
# The claim and the NOTE used to test the value differently, so {"number":"n/a"}
# produced a note saying the session owned KEY#n/a while the claim covered CI
# outcomes. A note describing a claim nobody holds is worse than no note.
f=$(spawn defangdevs/unnumbered '{"event":"issues","action":"opened","number":"n/a"}')
check "a non-numeric number falls back to the branch claim" "$f" \
  '([.topics[0].include.all[].any[]?.path] | index("workflow_run.head_branch")) != null
   and ([.topics[0].include.all[].any[]?.path] | index("workflow_run")) != null'
check "and the note falls back with it" "$f" \
  '.topics[0].note | contains("TOPIC BRANCHES") and (contains("#") | not)'
# A non-numeric number is exactly the case object extraction must also
# reject: a lease naming "n/a" would be worse than one naming nothing.
check_lease '"github:defangdevs/unnumbered"' 'null' "$f" \
  "a non-numeric number leaves the lease's object null, not \"n/a\""

# --- a CI outcome that names its commit (#510, #511) ---
#
# One failing run reaches the watch as up to six event shapes; the sha is the
# only field all six carry, so it is what recognises them as one run. Before
# this the widest claim available was "this repo's CI", which swallowed a red
# master (#384), and the topic-branch narrowing that fixed THAT left a shared
# ref claiming nothing — so one red master spawned two sessions 63s apart and
# they filed the same diagnosis twice.
SHA=da73cb12762a543f83f69996d4c43179df13c5e7
f=$(spawn defangdevs/shaclaim \
  "{\"event\":\"workflow_run\",\"action\":\"completed\",\"conclusion\":\"failure\",\"sha\":\"$SHA\",\"branch\":\"master\"}")
# Kept aside: the next spawn clears the state dir, and the matcher below
# needs the claim exactly as the wrapper wrote it.
sha_filter=$work/sha-claim.json
cp "$f" "$sha_filter"
# `sha` bare is the commit-status shape, whose only branch field is a LIST the
# predicate walker cannot index — the one shape a branch claim can never
# cover. And no branch qualifier: a sha is already run-scoped, so the
# shared-ref carve-out the branch claim needs is not needed here.
check "a sha spawn claims all six commit paths and nothing else" "$f" \
  '(.topics[0].include.any | map(.path)) ==
     ["workflow_run.head_sha", "workflow_job.head_sha", "check_run.head_sha",
      "check_suite.head_sha", "deployment.sha", "sha"]
   and (.topics[0].include.any | all(.in == ["'"$SHA"'"]))
   and (.topics[0].include | has("all") | not)'
# The note names the ref a reader recognises AND the commit the claim is
# actually keyed on, so "master" cannot be read as "every run on master".
check "the note names both the ref and the commit" "$f" \
  '.topics[0].note | contains("defangdevs/shaclaim@da73cb12")
   and contains("on master") and contains("THAT COMMIT")'

# --- a `sha` that is not a sha ---
#
# The meta is payload-derived, and {"path": "sha", "in": [""]} would match
# every payload that has no sha at all.
f=$(spawn defangdevs/badsha '{"event":"workflow_run","sha":"HEAD"}')
check "a malformed sha falls back to the branch claim" "$f" \
  '([.topics[0].include.all[].any[]?.path] | index("workflow_run.head_branch")) != null
   and (.topics[0].note | contains("TOPIC BRANCHES"))'

# --- the matcher's half of the promise ---
#
# Shape is what the wrapper wrote; what decides a spawn is the pinned
# webhook.py matching that claim against a real payload. Both directions
# matter: the second shape of the same run must be swallowed, and a later run
# on the same branch must NOT be — the #384 guarantee this claim keeps without
# naming a ref at all.
if [ -n "$WEBHOOK_PY" ]; then
  if out=$(python3 - "$WEBHOOK_PY" "$sha_filter" "$SHA" 2>&1 <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('wh', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
claim = json.load(open(sys.argv[2]))['topics'][0]['include']
assert m.predicate_error(claim) is None, m.predicate_error(claim)
SHA = sys.argv[3]
OTHER = '0123456789abcdef0123456789abcdef01234567'
same = [{'workflow_run': {'head_sha': SHA, 'head_branch': 'master'}},
        {'check_run': {'head_sha': SHA}},
        {'check_suite': {'head_sha': SHA}},
        {'workflow_job': {'head_sha': SHA}},
        {'deployment': {'sha': SHA}, 'deployment_status': {'state': 'failure'}},
        {'sha': SHA, 'state': 'failure'}]
for p in same:
    assert m.match_predicate(claim, p), p
later = [{'workflow_run': {'head_sha': OTHER, 'head_branch': 'master'}},
         {'check_run': {'head_sha': OTHER}},
         {'sha': OTHER, 'state': 'failure'}]
for p in later:
    assert not m.match_predicate(claim, p), p
print('ok')
PY
  ); then
    ok "webhook.py matches every shape of that run, and no later run"
  else
    fail "webhook.py matches every shape of that run, and no later run"
    printf '%s\n' "$out" | sed 's/^/     /'
  fi
  if out=$(python3 - "$WEBHOOK_PY" "$slack_filter" 2>&1 <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('wh', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
claim = json.load(open(sys.argv[2]))['topics'][0]['include']
assert m.predicate_error(claim) is None, m.predicate_error(claim)
assert m.match_predicate(claim, {'identifier': 'T1/C1/1.5', 'action': 'message.posted'})
assert m.match_predicate(claim, {'identifier': 'T1/C1/1.5', 'action': 'thread.settled'})
assert not m.match_predicate(claim, {'identifier': 'T1/C1/2.5'})
assert not m.match_predicate(claim, {'identifier': 'T1/C2/1.5'})
print('ok')
PY
  ); then
    ok "webhook.py matches later events of that thread, and no other thread"
  else
    fail "webhook.py matches later events of that thread, and no other thread"
    printf '%s\n' "$out" | sed 's/^/     /'
  fi
else
  echo "skip webhook.py match (no interpreter/pin given)"
fi


# --- a coalesced batch with several objects (agent-box#887) ---
#
# The dispatcher labels a batch with its NEWEST event only, so an earlier
# object was unclaimed and unleased until the model repaired it. A receiver
# that exports LOCAL_WEBHOOK_SPAWN_METAS (one meta per surviving event) gets
# every object claimed, leased and logged.
SPAWN_METAS='[{"event":"issues","action":"assigned","number":"92"},{"event":"issues","action":"assigned","number":"94"},{"event":"issues","action":"assigned","number":"96"},{"event":"issue_comment","number":"96"}]'
f=$(spawn defangdevs/batch '{"event":"issues","action":"assigned","number":"96"}')
check "a batch claims every numbered object once" "$f" \
  '[.topics[0].include.any[] | .any[0].in[0]] == [92, 94, 96]'
check "the note names every object" "$f" \
  '.topics[0].note | contains("92, 94, 96")'
ses=$(basename "$f" .json); ses=${ses#filter."$SPAWN_USER"-}
check "the lease lists every object, newest number kept as object" \
  "$HOME/.local/state/agent-box/lease/$ses.json" \
  '.object == "96" and .objects == ["92","94","96"]'
check "the spawn log lists every object" "$HOME/.local/state/agent-box/hook-sessions.jsonl" \
  'select(.what == "spawn" and .objects == ["92","94","96"])'
SPAWN_METAS='[{"number":"7"},{"event":"workflow_run","sha":"da73cb12762a543f83f69996d4c43179df13c5e7"}]'
f=$(spawn defangdevs/mixed '{"event":"workflow_run","sha":"da73cb12762a543f83f69996d4c43179df13c5e7"}')
check "a number and a commit in one batch are both claimed" "$f" \
  '(.topics[0].include.any | map(.path // "any") | index("any")) != null
   and ([.. | objects | select(.path? == "sha") | .in[0]] == ["da73cb12762a543f83f69996d4c43179df13c5e7"])
   and ([.. | objects | select(.path? == "issue.number") | .in[0]] == [7])'
# A receiver that sends garbage costs the extra objects, never the spawn.
for bad in 'not json' '{"number":"5"}' '[]' '[1,2]'; do
  SPAWN_METAS="$bad"
  f=$(spawn defangdevs/badmetas '{"event":"issues","action":"assigned","number":"4242"}')
  check "unusable METAS ($bad) falls back to the single meta" "$f" \
    '(.topics[0].include.any | all(.in == [4242]))'
done
unset SPAWN_METAS

[ "$fails" -eq 0 ] || { echo "$fails assertion(s) failed" >&2; exit 1; }
echo "all webhook-spawn claim assertions passed"
