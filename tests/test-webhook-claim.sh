#!/usr/bin/env bash
# Unit tests for `agent-box-webhook subscribe --claim` (issues #419, #420).
#
# A claim is what stops a standing watch spawning a second session on top of
# one already doing the work. Its failure mode is SILENT: an incomplete claim
# warns about nothing, a sibling just turns up — which is how PR #417 ended
# up with two sessions editing one git worktree. So the assertions that
# matter are about which payload paths a claim actually covers, and they are
# worth pinning byte for byte.
#
# webhook.py is shimmed to print its argv, so nothing here needs the daemon,
# the network or a real subscription.
set -u

SCRIPT=${1:?usage: test-webhook-claim.sh PATH/TO/webhook-cli.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export HOME="$work/home"; mkdir -p "$HOME"
export LOCAL_WEBHOOK_STATE_DIR="$work/state"
export AGENT_BOX_WEBHOOK_SCRIPT="$work/webhook.py"
: > "$AGENT_BOX_WEBHOOK_SCRIPT"

# The shim stands in for `python3 webhook.py`: print argv, one per line, so a
# test can look at exactly what the wrapper decided to pass on.
mkdir -p "$work/bin"
cat > "$work/bin/python3" <<EOF
#!$BASH_BIN
shift            # the webhook.py path
printf '%s\n' "\$@"
EOF
chmod +x "$work/bin/python3"
PATH="$work/bin:$PATH"; export PATH

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

run() { bash "$SCRIPT" subscribe "$@" 2>"$work/err"; }
# The --include value the wrapper built, or empty.
include_of() {
  run "$@" | awk '/^--include$/ { getline; print; exit }'
}
# Does the built include cover this payload path?
covers() { # covers LABEL PATH ARGS...
  _label=$1; _path=$2; shift 2
  if include_of "$@" | jq -e --arg p "$_path" '[.any[].path] | index($p)' >/dev/null 2>&1
  then ok "$_label"; else no "$_label" "path $_path not in $(include_of "$@")"; fi
}

# --- a bare number claims both spellings of the same object -------------
covers "a bare number claims pull_request.number" \
       "pull_request.number" defangdevs/agent-box --claim 42
# GitHub reports a PR comment as issue_comment with issue.number, so an
# agent that claimed "42" must be claimed for both or it is claimed for
# neither in practice.
covers "a bare number claims issue.number too" \
       "issue.number" defangdevs/agent-box --claim 42

# --- the branch claim covers every shape CI reports a branch under ------
# This is the regression that matters: the old documented example named
# workflow_run and nothing else, so a red check_run on your own branch
# spawned a sibling.
for p in workflow_run.head_branch workflow_job.head_branch \
         check_run.check_suite.head_branch check_suite.head_branch \
         deployment.ref pull_request.head.ref ref; do
  covers "branch: claims $p" "$p" defangdevs/agent-box --claim branch:fix/42
done

# `ref` is the push spelling and needs the refs/heads/ prefix, not the bare
# name — a claim that got this wrong would look right and match nothing.
if include_of defangdevs/agent-box --claim branch:fix/42 \
   | jq -e '[.any[] | select(.path=="ref") | .in[]] | index("refs/heads/fix/42")' \
   >/dev/null 2>&1
then ok "the push ref claim is fully qualified"
else no "the push ref claim is fully qualified" \
        "$(include_of defangdevs/agent-box --claim branch:fix/42)"; fi

# --- narrow forms -------------------------------------------------------
if include_of defangdevs/agent-box --claim pr:42 \
   | jq -e '[.any[].path] == ["pull_request.number"]' >/dev/null 2>&1
then ok "pr: claims only the pull request"
else no "pr: claims only the pull request"; fi
if include_of defangdevs/agent-box --claim issue:42 \
   | jq -e '[.any[].path] == ["issue.number"]' >/dev/null 2>&1
then ok "issue: claims only the issue"
else no "issue: claims only the issue"; fi

# --- claims OR together; --claim=X spelling works -----------------------
n=$(include_of defangdevs/agent-box --claim pr:42 --claim branch:fix/42 \
    | jq '.any | length')
[ "$n" = 8 ] && ok "repeated claims OR together (1 + 7 clauses)" \
  || no "repeated claims OR together" "got $n clauses"

# The one shape a claim CANNOT cover, asserted so nobody "fixes" it with a
# guess: a bare commit status carries only a `branches` ARRAY, and
# webhook.py's get_path indexes lists by number alone (no wildcard). A
# branches.0.name rule would look claimed and match nothing.
if include_of defangdevs/agent-box --claim branch:fix/42 \
   | jq -e '[.any[].path] | any(startswith("branches"))' >/dev/null 2>&1
then no "commit status is left unclaimed, not guessed at" \
        "a branches[] rule crept in"
else ok "commit status is left unclaimed, not guessed at"; fi
n=$(include_of defangdevs/agent-box --claim=pr:42 | jq '.any | length')
[ "$n" = 1 ] && ok "--claim=SPEC is accepted too" \
  || no "--claim=SPEC is accepted too" "got $n"

# --- the flag never reaches webhook.py ----------------------------------
if run defangdevs/agent-box --claim pr:42 --note hi | grep -qx -- --claim
then no "--claim is consumed, not forwarded" "webhook.py saw --claim"
else ok "--claim is consumed, not forwarded"; fi
# ...and everything else still is, in order.
out=$(run defangdevs/agent-box --claim pr:42 --note "why I care" --ttl 3)
for want in defangdevs/agent-box --note "why I care" --ttl 3; do
  printf '%s\n' "$out" | grep -qxF -- "$want" \
    || no "other arguments survive (looking for [$want])" "argv was: $out"
done
ok "other arguments survive --claim filtering"

# --- refusals -----------------------------------------------------------
run defangdevs/agent-box --claim pr:42 --include '{"any":[]}' >/dev/null
[ $? -ne 0 ] && grep -q "not both" "$work/err" \
  && ok "--claim and --include together are refused" \
  || no "--claim and --include together are refused" "$(cat "$work/err")"

run defangdevs/agent-box --claim fix/42 >/dev/null
[ $? -ne 0 ] && grep -q "branch:NAME" "$work/err" \
  && ok "a bare branch name is refused, and says to use branch:" \
  || no "a bare branch name is refused" "$(cat "$work/err")"

# An EMPTY value is the dangerous one: it used to vanish in the word-split
# that builds the rule list, so claim_clauses' own guard never ran and the
# CLI emitted {"any":[]} — a filter matching NOTHING — and exited 0. You
# would believe you were claimed and subscribed, and no event would ever
# arrive. Both spellings, and never a silent success.
for empty_form in "--claim=" "--claim "; do
  case "$empty_form" in
    "--claim=") run defangdevs/agent-box --claim= >"$work/out" ;;
    *)          run defangdevs/agent-box --claim "" >"$work/out" ;;
  esac
  st=$?
  if [ "$st" -eq 0 ]; then
    no "an empty [$empty_form] value is refused" \
       "exited 0, argv: $(tr '\n' ' ' < "$work/out")"
  elif grep -q '{"any":\[\]}' "$work/out"; then
    no "an empty [$empty_form] value is refused" "emitted an empty filter"
  else
    ok "an empty [$empty_form] value is refused"
  fi
done

run defangdevs/agent-box --claim >/dev/null
[ $? -ne 0 ] && ok "a --claim with no value is refused" \
  || no "a --claim with no value is refused"

run defangdevs/agent-box --claim branch: >/dev/null
[ $? -ne 0 ] && ok "an empty branch: value is refused" \
  || no "an empty branch: value is refused"

echo
if [ "$fails" -eq 0 ]; then echo "all assertions passed"; else
  echo "$fails assertion(s) failed"; exit 1
fi
