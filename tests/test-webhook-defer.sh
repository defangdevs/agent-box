#!/usr/bin/env bash
# What the spawn wrapper ANSWERS at the hook-session ceiling, and what the
# pinned local-webhook does with that answer (issues #170, #301).
#
# The cap itself is right: webhook.py rate-limits and coalesces spawns but
# bounds nothing over time, so agents that forget `agent-box-session rm` would
# otherwise fill the box. The answer was wrong. A refusal used to print a
# message and `exit 1`, which the dispatcher cannot tell apart from "command
# not found", so it dropped the batch — and a standing watch is for events NO
# session owns, so nothing else was holding them. Since local-channels#28 the
# exit code is a three-way answer, and 75 (EX_TEMPFAIL) is "declined for now":
# the batch goes back at the head of its key's pending list and is re-offered
# until a slot frees.
#
# Both halves are asserted here, against the REAL wrapper and the REAL pinned
# webhook.py, because the whole bug was the two disagreeing about what a
# non-zero exit meant. The VM test owns the wiring an interpreter cannot show
# (a signed delivery reaching a watch, a session landing in the registry); this
# owns the contract between the two programs, which costs a VM boot there and
# a few seconds here.
set -u

SCRIPT=${1:?usage: test-webhook-defer.sh PATH/TO/webhook-spawn.sh [WEBHOOK.PY]}
WEBHOOK_PY=${2:-}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SRC=$(cd "$(dirname "$SCRIPT")" && pwd)

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The source form carries @@include markers the assembler resolves; expand
# them the same way (nested includes and all) so the test drives the same text
# the box runs.
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
REGISTRY="$HOME/.config/agent-box/sessions.json"
REFUSED="$HOME/.local/state/agent-box/webhook-spawn-refused.json"

mkdir -p "$work/bin"
# `add` is the wrapper's exec target and `peers` a read the preamble embeds.
# Neither is under test, so the shim records the call and succeeds — the
# recording is what says a spawn was ACCEPTED, since no session registry
# entry is written when `add` is a stub.
cat > "$work/bin/session" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "$work/session.log"
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
# No tmux here, and deliberately so: the liveness probe must not read a failure
# as "nothing is running", so it falls back to counting registry keys and says
# it did. That makes the capacity this test drives a pure function of the file
# below — the same conservative path a box whose tmux is down takes.
export AGENT_BOX_TMUX_BIN="$work/nonexistent-tmux"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# hook_registry N — a registry holding N running hook-* sessions.
hook_registry() {
  python3 - "$REGISTRY" "$1" <<'PY'
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
sessions = {f"hook-filler-{i}": {"agent": "claude"} for i in range(n)}
json.dump({"version": 1, "sessions": sessions}, open(path, "w"))
PY
}

# spawn KEY [MAX] — drive one batch through the wrapper; echo its exit status.
spawn() {
  env LOCAL_WEBHOOK_SPAWN_SOURCE=github \
      LOCAL_WEBHOOK_SPAWN_KEY="$1" \
      LOCAL_WEBHOOK_SPAWN_TOPIC='github:defangdevs/*' \
      LOCAL_WEBHOOK_SPAWN_EVENT=issues \
      AGENT_BOX_HOOK_SESSION_MAX="${2-}" \
      "$BASH_BIN" "$work/spawn.sh" > "$work/spawn.out" 2>&1 <<< 'hi'
  printf '%s' "$?"
}

# --- at the ceiling: declined, not failed -----------------------------------
#
# 75 and 1 differ in nothing an operator sees and everything the receiver does,
# so the code is the assertion. Anything else in this script keeps its own
# status: a broken spawner must still be dropped rather than retried forever.
hook_registry 2
rc=$(spawn defangdevs/blocked 2)
if [ "$rc" = 75 ]; then
  ok "the hook-session cap exits 75 (EX_TEMPFAIL), not 1"
else
  fail "the hook-session cap exits 75 (EX_TEMPFAIL), not 1 — got $rc"
  sed 's/^/     /' "$work/spawn.out"
fi
if grep -q 'declining this batch for now' "$work/spawn.out"; then
  ok "it says the batch is kept, not dropped"
else
  fail "it says the batch is kept, not dropped"
  sed 's/^/     /' "$work/spawn.out"
fi
if grep -q 'cannot ask tmux' "$work/spawn.out"; then
  ok "a probe that cannot run falls back to the key count and says so"
else
  fail "a probe that cannot run falls back to the key count and says so"
fi
if [ ! -s "$work/session.log" ]; then
  ok "no session is started at the ceiling"
else
  fail "no session is started at the ceiling"
fi

# The record #170 leaves now carries WHICH answer was given, so `status` cannot
# report as lost a batch the receiver is still holding.
if jq -e '.count == 1 and .deferred == true and .live == 2 and .max == 2
          and .key == "defangdevs/blocked"' "$REFUSED" >/dev/null 2>&1; then
  ok "the refusal is recorded as deferred, with the capacity it was refused on"
else
  fail "the refusal is recorded as deferred, with the capacity it was refused on"
  jq -c . "$REFUSED" 2>/dev/null | sed 's/^/     /'
fi

# --- below the ceiling, and a ceiling nobody can read ------------------------
hook_registry 1
rc=$(spawn defangdevs/free 2)
if [ "$rc" = 0 ] && grep -q 'add hook-defangdevs-free-' "$work/session.log"; then
  ok "a slot below the cap spawns"
else
  fail "a slot below the cap spawns — exit $rc"
  sed 's/^/     /' "$work/spawn.out"
fi
# A knob that --help documents is a knob someone will typo, and an unusable
# value must not refuse every batch for a reason nobody can see: `[ n -ge foo ]`
# is fatal under set -e, which would decline every batch on every box that
# typed it. It says so and falls back to the built-in 4.
: > "$work/session.log"
hook_registry 2
rc=$(spawn defangdevs/typo lots)
if [ "$rc" = 0 ] && grep -q 'is not a number' "$work/spawn.out" \
   && grep -q 'add hook-defangdevs-typo-' "$work/session.log"; then
  ok "an unusable AGENT_BOX_HOOK_SESSION_MAX says so and falls back to 4"
else
  fail "an unusable AGENT_BOX_HOOK_SESSION_MAX says so and falls back to 4 — exit $rc"
  sed 's/^/     /' "$work/spawn.out"
fi

# --- and the dispatcher KEEPS what the wrapper declined ----------------------
#
# The half the exit code exists for, driven through the pinned webhook.py's own
# Dispatcher with the REAL wrapper as its spawn command: at the cap the batch
# waits, and it starts by itself once a slot frees, with no second delivery to
# prompt it. Skipped rather than silently passed when no webhook.py was given.
if [ -z "$WEBHOOK_PY" ]; then
  printf 'skip the dispatcher half (no webhook.py given)\n'
elif [ ! -f "$WEBHOOK_PY" ]; then
  fail "webhook.py not found: $WEBHOOK_PY"
else
  : > "$work/session.log"
  hook_registry 1
  if AGENT_BOX_HOOK_SESSION_MAX=1 SPAWN="$BASH_BIN $work/spawn.sh" \
     REGISTRY="$REGISTRY" LOG="$work/session.log" \
     python3 - "$WEBHOOK_PY" > "$work/dispatch.out" 2>&1 <<'PY'
import importlib.util, json, os, sys, time

spec = importlib.util.spec_from_file_location("webhook_pinned", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

log, registry = os.environ["LOG"], os.environ["REGISTRY"]
# window 1s so a re-offer costs a second rather than the 60 a box waits;
# owner_of is stubbed to "nobody owns it", the answer a box with no live peer
# sockets gives anyway, so the deferral is what decides the outcome here.
d = mod.Dispatcher(os.environ["SPAWN"], 1, 1, 30,
                   owner_of=lambda env: None, defer_max_s=60, pending_max=200)
meta = {"source": "github", "key": "defangdevs/deferred", "event": "issues",
        "topic": "github:defangdevs/*", "note": "standing watch"}
d.add("defangdevs/deferred", "issue #30 opened on defangdevs/deferred", meta, {})

# At the cap: declined every time the window reopens, and never dropped.
deadline = time.monotonic() + 6
while time.monotonic() < deadline:
    time.sleep(0.25)
if os.path.getsize(log):
    raise SystemExit("a session started while the cap was full: "
                     + open(log).read())
st = d.keys[d._bucket("defangdevs/deferred", meta)]
if st["defer_n"] < 2:
    raise SystemExit(f"the batch was offered {st['defer_n']} time(s), not re-offered")
# Pending OR in flight: a re-offer that is mid-decline has already taken the
# batch off the list, and which side of that the sample lands on is a race.
if len(st["pending"]) + (1 if st["running"] else 0) != 1:
    raise SystemExit(f"the batch is not waiting: {st['pending']!r}")

# A slot frees. Nothing new is delivered — the batch that was declined is the
# one that has to start.
json.dump({"version": 1, "sessions": {}}, open(registry, "w"))
deadline = time.monotonic() + 30
while time.monotonic() < deadline and not os.path.getsize(log):
    time.sleep(0.25)
started = open(log).read()
if "add hook-defangdevs-deferred-" not in started:
    raise SystemExit(f"the deferred batch never started: {started!r}")
print("started:", started.strip())
PY
  then
    ok "a batch declined at the cap starts by itself when a slot frees"
  else
    fail "a batch declined at the cap starts by itself when a slot frees"
    sed 's/^/     /' "$work/dispatch.out"
  fi
fi

if [ "$fails" -eq 0 ]; then
  printf '\nall webhook-defer assertions passed\n'
else
  printf '\n%s webhook-defer assertion(s) failed\n' "$fails"
  exit 1
fi
