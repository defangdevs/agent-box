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

SCRIPT=${1:?usage: test-webhook-claim.sh PATH/TO/webhook-cli.sh [PATH/TO/webhook.py]}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
# Optional, and the second half of the promise (issue #706): shape is what the
# wrapper wrote, but what an agent actually receives is the pinned webhook.py
# matching that rule against a payload. Given one, the delivery-policy
# assertions below are made against the real matcher rather than against jq's
# reading of the JSON. Skipped, out loud, when it is not given.
WEBHOOK_PY=${2:-}
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

# --- a Codex session subscription leaves a durable restart target -------
wake_id=52345678-9abc-4def-8123-456789abcdef
export CODEX_THREAD_ID="$wake_id"
export LOCAL_WEBHOOK_SESSION=agent-main
wake_file="$HOME/.local/state/agent-box/codex-wake/agent-main"

run defangdevs/agent-box >/dev/null
[ "$(cat "$wake_file" 2>/dev/null)" = "$wake_id" ] \
  && ok "a Codex session subscription records its task for restart" \
  || no "a Codex session subscription records its task for restart"

rm -f "$wake_file"
run defangdevs/agent-box --deliver-to subagent --when '{"path":"action","in":["opened"]}' \
  >/dev/null
[ ! -e "$wake_file" ] \
  && ok "a standing watch does not become a Codex restart target" \
  || no "a standing watch does not become a Codex restart target"

run defangdevs/agent-box >/dev/null
bash "$SCRIPT" unsubscribe defangdevs/agent-box >/dev/null 2>"$work/err"
[ ! -e "$wake_file" ] \
  && ok "the last unsubscribe removes the Codex restart target" \
  || no "the last unsubscribe removes the Codex restart target"
unset CODEX_THREAD_ID LOCAL_WEBHOOK_SESSION
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
       "pull_request.number" defangdevs/agent-box --all-events --claim 42
# GitHub reports a PR comment as issue_comment with issue.number, so an
# agent that claimed "42" must be claimed for both or it is claimed for
# neither in practice.
covers "a bare number claims issue.number too" \
       "issue.number" defangdevs/agent-box --all-events --claim 42

# --- the branch claim covers every shape CI reports a branch under ------
# This is the regression that matters: the old documented example named
# workflow_run and nothing else, so a red check_run on your own branch
# spawned a sibling.
for p in workflow_run.head_branch workflow_job.head_branch \
         check_run.check_suite.head_branch check_suite.head_branch \
         deployment.ref pull_request.head.ref ref; do
  covers "branch: claims $p" "$p" defangdevs/agent-box --all-events --claim branch:fix/42
done

# `ref` is the push spelling and needs the refs/heads/ prefix, not the bare
# name — a claim that got this wrong would look right and match nothing.
if include_of defangdevs/agent-box --all-events --claim branch:fix/42 \
   | jq -e '[.any[] | select(.path=="ref") | .in[]] | index("refs/heads/fix/42")' \
   >/dev/null 2>&1
then ok "the push ref claim is fully qualified"
else no "the push ref claim is fully qualified" \
        "$(include_of defangdevs/agent-box --all-events --claim branch:fix/42)"; fi

# --- the shape the guide tells you to use --------------------------------
# A PR is claimed with the BARE number, not pr:N. GitHub reports a comment
# on a PR as issue_comment with issue.number, so pr:N alone leaves reviews
# and comments on your own PR unclaimed — which is the #417 collision.
# Assert the documented example covers both spellings.
for p in pull_request.number issue.number; do
  covers "the documented PR claim covers $p" "$p" \
         defangdevs/agent-box --all-events --claim 42 --claim branch:fix/42
done

# --- narrow forms -------------------------------------------------------
if include_of defangdevs/agent-box --all-events --claim pr:42 \
   | jq -e '[.any[].path] == ["pull_request.number"]' >/dev/null 2>&1
then ok "pr: claims only the pull request"
else no "pr: claims only the pull request"; fi
if include_of defangdevs/agent-box --all-events --claim issue:42 \
   | jq -e '[.any[].path] == ["issue.number"]' >/dev/null 2>&1
then ok "issue: claims only the issue"
else no "issue: claims only the issue"; fi

# --- claims OR together; --claim=X spelling works -----------------------
n=$(include_of defangdevs/agent-box --all-events --claim pr:42 --claim branch:fix/42 \
    | jq '.any | length')
[ "$n" = 8 ] && ok "repeated claims OR together (1 + 7 clauses)" \
  || no "repeated claims OR together" "got $n clauses"

# The one shape a claim CANNOT cover, asserted so nobody "fixes" it with a
# guess: a bare commit status carries only a `branches` ARRAY, and
# webhook.py's get_path indexes lists by number alone (no wildcard). A
# branches.0.name rule would look claimed and match nothing.
if include_of defangdevs/agent-box --all-events --claim branch:fix/42 \
   | jq -e '[.any[].path] | any(startswith("branches"))' >/dev/null 2>&1
then no "commit status is left unclaimed, not guessed at" \
        "a branches[] rule crept in"
else ok "commit status is left unclaimed, not guessed at"; fi
n=$(include_of defangdevs/agent-box --all-events --claim=pr:42 | jq '.any | length')
[ "$n" = 1 ] && ok "--claim=SPEC is accepted too" \
  || no "--claim=SPEC is accepted too" "got $n"

# --- the flag never reaches webhook.py ----------------------------------
if run defangdevs/agent-box --all-events --claim pr:42 --note hi | grep -qx -- --claim
then no "--claim is consumed, not forwarded" "webhook.py saw --claim"
else ok "--claim is consumed, not forwarded"; fi
# ...and everything else still is, in order.
out=$(run defangdevs/agent-box --all-events --claim pr:42 --note "why I care" --ttl 3)
for want in defangdevs/agent-box --note --ttl 3; do
  printf '%s\n' "$out" | grep -qxF -- "$want" \
    || no "other arguments survive (looking for [$want])" "argv was: $out"
done
ok "other arguments survive --claim filtering"
# The note itself now carries the merge boundary appended to it (issue #706),
# so it survives as a PREFIX rather than byte for byte.
printf '%s\n' "$out" | grep -q '^why I care' \
  && ok "the caller's own note text survives" \
  || no "the caller's own note text survives" "argv was: $out"

# --- refusals -----------------------------------------------------------
run defangdevs/agent-box --all-events --claim pr:42 \
    --include '{"path":"action","in":["closed"]}' >/dev/null
[ $? -ne 0 ] && grep -q "contradict" "$work/err" \
  && ok "--all-events and --include together are refused" \
  || no "--all-events and --include together are refused" "$(cat "$work/err")"

run defangdevs/agent-box --all-events --claim fix/42 >/dev/null
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
    "--claim=") run defangdevs/agent-box --all-events --claim= >"$work/out" ;;
    *)          run defangdevs/agent-box --all-events --claim "" >"$work/out" ;;
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

run defangdevs/agent-box --all-events --claim >/dev/null
[ $? -ne 0 ] && ok "a --claim with no value is refused" \
  || no "a --claim with no value is refused"

run defangdevs/agent-box --all-events --claim branch: >/dev/null
[ $? -ne 0 ] && ok "an empty branch: value is refused" \
  || no "an empty branch: value is refused"

# --- issue #562: --include/--exclude count as "rules given" too ---------
# The subagent-default guard used to look only for --when/--drop, so a
# caller writing --include/--exclude (the current names since local-webhook
# 0.19.0 renamed when -> include, drop -> exclude) still got the wrapper's
# own default --when appended on top, plus a false "no --when/--drop given"
# warning — even though the caller's own rule reached webhook.py first and
# won there. Pin that the wrapper now recognizes both current names.
custom_include='{"any":[{"path":"sender.login","in":["someone"]}]}'
out=$(run defangdevs/agent-box --deliver-to subagent --include "$custom_include")
if printf '%s\n' "$out" | grep -qxF "$custom_include"; then
  ok "--include is forwarded verbatim (#562)"
else
  no "--include is forwarded verbatim (#562)" "argv: $out"
fi
if printf '%s\n' "$out" | grep -qx -- '--when'; then
  no "--include suppresses the wrapper's default --when (#562)" "argv: $out"
else
  ok "--include suppresses the wrapper's default --when (#562)"
fi
if grep -q "no --when/--drop given" "$work/err"; then
  no "--include gets no false 'no --when/--drop given' warning (#562)" \
     "$(cat "$work/err")"
else
  ok "--include gets no false 'no --when/--drop given' warning (#562)"
fi

custom_exclude='{"path":"workflow_run.event","in":["dynamic"]}'
out=$(run defangdevs/agent-box --deliver-to subagent --exclude "$custom_exclude")
if printf '%s\n' "$out" | grep -qxF "$custom_exclude"; then
  ok "--exclude is forwarded verbatim (#562)"
else
  no "--exclude is forwarded verbatim (#562)" "argv: $out"
fi
if printf '%s\n' "$out" | grep -qx -- '--when'; then
  no "--exclude suppresses the wrapper's default --when (#562)" "argv: $out"
else
  ok "--exclude suppresses the wrapper's default --when (#562)"
fi
if grep -q "no --when/--drop given" "$work/err"; then
  no "--exclude gets no false 'no --when/--drop given' warning (#562)" \
     "$(cat "$work/err")"
else
  ok "--exclude gets no false 'no --when/--drop given' warning (#562)"
fi

# The parser accepts --exclude=VALUE too (like --include=VALUE); pin that
# form separately rather than assuming it behaves like the two-argument one.
out=$(run defangdevs/agent-box --deliver-to subagent --exclude="$custom_exclude")
if printf '%s\n' "$out" | grep -qxF -- "--exclude=$custom_exclude"; then
  ok "--exclude=VALUE is forwarded verbatim (#562)"
else
  no "--exclude=VALUE is forwarded verbatim (#562)" "argv: $out"
fi
if printf '%s\n' "$out" | grep -qx -- '--when'; then
  no "--exclude=VALUE suppresses the wrapper's default --when (#562)" "argv: $out"
else
  ok "--exclude=VALUE suppresses the wrapper's default --when (#562)"
fi

# Sanity: with truly no rules at all, the default --when is still appended —
# guards against a fix that goes too far and disables the default outright.
out=$(run defangdevs/agent-box --deliver-to subagent)
if printf '%s\n' "$out" | grep -qx -- '--when'; then
  ok "no rules at all still gets the default --when (#562 regression guard)"
else
  no "no rules at all still gets the default --when (#562 regression guard)" \
     "argv: $out"
fi

# =======================================================================
# issue #706: a claim is object scope, and scope is not an event policy
# =======================================================================
#
# `--claim branch:master --ttl 4` queued 37 submissions into one Codex
# session in under two minutes — 21 check_run, 15 workflow, 1 push, almost
# all of them for unrelated work that merely landed on the branch. Two
# separate mistakes made that possible and both are pinned here: claiming a
# moving shared ref at all, and treating object scope as interest in every
# lifecycle event of that object.

# --- a session claim must say WHICH events it needs ---------------------
run defangdevs/agent-box --claim 42 >"$work/out"
st=$?
if [ "$st" -eq 0 ]; then
  no "a scope-only session claim is refused" \
     "exited 0, argv: $(tr '\n' ' ' < "$work/out")"
elif grep -q -- "--events actionable" "$work/err" \
     && grep -q -- "--events terminal-ci" "$work/err" \
     && grep -q -- "--include" "$work/err" \
     && grep -q -- "--all-events" "$work/err"; then
  ok "a scope-only session claim is refused, naming every way out"
else
  no "a scope-only session claim is refused, naming every way out" \
     "$(cat "$work/err")"
fi

# A standing watch is the exception and must stay one: its own --when is
# already its entire spawn policy, so a claim there is extra scoping rather
# than a delivery filter, and requiring a second one would break every
# governed watch on the box.
run defangdevs/agent-box --deliver-to subagent --claim 42 \
    --when '{"path":"action","in":["opened"]}' >/dev/null
[ $? -eq 0 ] \
  && ok "a standing watch's claim still needs no --events" \
  || no "a standing watch's claim still needs no --events" "$(cat "$work/err")"

# --- scope AND relevance, with a floor ----------------------------------
pol=$(include_of defangdevs/agent-box --claim 42 --events terminal-ci)
if printf '%s' "$pol" | jq -e '(.all | length) == 2' >/dev/null 2>&1; then
  ok "--claim + --events is an AND of two predicates"
else
  no "--claim + --events is an AND of two predicates" "$pol"
fi
if printf '%s' "$pol" \
   | jq -e '[.all[0].any[].path] | index("pull_request.number")' >/dev/null 2>&1
then ok "the first half of the AND is the claim scope"
else no "the first half of the AND is the claim scope" "$pol"; fi
# The floor is the whole reason this can be narrowed at all. webhook.py reads
# one `include` for two questions — what to deliver, and (filter_claims)
# whether a standing watch should spawn a second session onto this work — so
# a relevance rule that dropped a review would also stop claiming it.
if printf '%s' "$pol" \
   | jq -e '[.all[1].any[] | .. | .path? // empty] | index("review.state")' \
   >/dev/null 2>&1
then ok "the relevance half keeps the watch-spawn vocabulary as a floor"
else no "the relevance half keeps the watch-spawn vocabulary as a floor" "$pol"; fi

# --all-events is the explicit opt-in to what a scope-only claim used to
# mean, and it must stay exactly that: the bare claim, no AND, no floor.
if include_of defangdevs/agent-box --all-events --claim 42 \
   | jq -e 'has("all") | not' >/dev/null 2>&1
then ok "--all-events keeps the claim unwrapped"
else no "--all-events keeps the claim unwrapped"; fi

# A caller's own predicate is now the third spelling of the same dimension,
# ANDed rather than refused — the refusal was the reason the incident command
# looked correct: it already "had a filter", because --claim generates one.
ui=$(include_of defangdevs/agent-box --claim 42 \
       --include '{"path":"action","in":["closed"]}')
if printf '%s' "$ui" | jq -e '(.all | length) == 2' >/dev/null 2>&1; then
  ok "--claim and --include AND together instead of being refused"
else
  no "--claim and --include AND together instead of being refused" "$ui"; fi
# ...and --when, its old name, takes the same path rather than reaching
# webhook.py as a second, competing rule alongside the generated --include.
if run defangdevs/agent-box --claim 42 --when '{"path":"action","in":["closed"]}' \
   | grep -qx -- '--when'
then no "--when is folded into the claim, not forwarded alongside it"
else ok "--when is folded into the claim, not forwarded alongside it"; fi

run defangdevs/agent-box --claim 42 --events terminal-ci --all-events >/dev/null
[ $? -ne 0 ] && grep -q "contradict" "$work/err" \
  && ok "--events and --all-events are refused together" \
  || no "--events and --all-events are refused together" "$(cat "$work/err")"
run defangdevs/agent-box --claim 42 --events nonsense >/dev/null
[ $? -ne 0 ] && grep -q "is not a policy" "$work/err" \
  && ok "an unknown --events policy is refused by name" \
  || no "an unknown --events policy is refused by name" "$(cat "$work/err")"
# With no claim, --events is still a legitimate OBSERVATION subscription —
# the sanctioned way to watch a shared branch instead of claiming it.
if include_of defangdevs/agent-box --events terminal-ci \
   | jq -e 'has("any")' >/dev/null 2>&1
then ok "--events alone is a filtered observation subscription"
else no "--events alone is a filtered observation subscription"; fi
run defangdevs/agent-box --all-events >/dev/null
[ $? -ne 0 ] \
  && ok "--all-events without a claim is refused rather than ignored" \
  || no "--all-events without a claim is refused rather than ignored"

# --- sha: the immutable claim -------------------------------------------
SHA=1234567890abcdef1234567890abcdef12345678
for p in workflow_run.head_sha workflow_job.head_sha check_run.head_sha \
         check_suite.head_sha deployment.sha sha; do
  covers "sha: claims $p" "$p" defangdevs/agent-box --all-events --claim "sha:$SHA"
done
# All SIX shapes, where branch: can only reach five: a bare commit status
# carries the sha as a scalar where its only branch field is an unindexable
# `branches` array. Pin that difference, since it is the reason to prefer a
# sha claim for CI at all.
n=$(include_of defangdevs/agent-box --all-events --claim "sha:$SHA" | jq '.any | length')
[ "$n" = 6 ] && ok "sha: claims all six CI/deployment/status shapes" \
  || no "sha: claims all six CI/deployment/status shapes" "got $n"
# commit: is the same claim under the word a person reaches for.
if [ "$(include_of defangdevs/agent-box --all-events --claim "commit:$SHA")" \
   = "$(include_of defangdevs/agent-box --all-events --claim "sha:$SHA")" ]
then ok "commit: is a synonym for sha:"
else no "commit: is a synonym for sha:"; fi
# GitHub sends lowercase hex, so an uppercase claim would look right and
# match nothing.
if include_of defangdevs/agent-box --all-events \
     --claim "sha:$(printf '%s' "$SHA" | tr 'a-f' 'A-F')" \
   | jq -e --arg s "$SHA" '[.any[].in[]] | unique == [$s]' >/dev/null 2>&1
then ok "an uppercase sha is lowercased to match the payload"
else no "an uppercase sha is lowercased to match the payload"; fi
# An ABBREVIATED sha is the dangerous one: the predicate language compares
# scalars exactly, so a 7-character claim is a filter that matches nothing
# while reading as a claim.
run defangdevs/agent-box --all-events --claim sha:1234567 >/dev/null
[ $? -ne 0 ] && grep -q "40" "$work/err" \
  && ok "an abbreviated sha is refused, not silently unmatchable" \
  || no "an abbreviated sha is refused" "$(cat "$work/err")"
run defangdevs/agent-box --all-events --claim sha:nothexatall >/dev/null
[ $? -ne 0 ] && ok "a non-hex sha is refused" || no "a non-hex sha is refused"
run defangdevs/agent-box --all-events --claim sha: >/dev/null
[ $? -ne 0 ] && ok "an empty sha: value is refused" \
  || no "an empty sha: value is refused"

# --- the default branch is not a claimable object -----------------------
# A real gh may well be on this PATH, and then these assertions would be
# about whatever defangdevs/agent-box currently calls its default branch
# rather than about the code. Both answers are shimmed.
db_shim() { # db_shim BRANCH | db_shim fail
  if [ "$1" = fail ]; then
    printf '#!%s\nexit 1\n' "$BASH_BIN" > "$work/bin/gh"
  else
    printf '#!%s\n[ "$1" = api ] || exit 1\necho %s\n' "$BASH_BIN" "$1" \
      > "$work/bin/gh"
  fi
  chmod +x "$work/bin/gh"
  rm -f "$LOCAL_WEBHOOK_STATE_DIR/default-branch.json"
}

# A box that cannot ask — offline, no token, no gh — falls back to the two
# common names, because refusing permissively is the direction that costs
# something.
db_shim fail
run defangdevs/agent-box --all-events --claim branch:master >/dev/null
[ $? -ne 0 ] && grep -q "not a bounded unit of work" "$work/err" \
  && ok "branch:master is refused with no way to resolve the default" \
  || no "branch:master is refused with no way to resolve the default" \
        "$(cat "$work/err")"
run defangdevs/agent-box --all-events --claim branch:main >/dev/null
[ $? -ne 0 ] && ok "branch:main is refused too" || no "branch:main is refused too"
# The guidance has to be actionable, or an agent just picks another wrong
# claim: name the three shapes that ARE bounded, and the no-claim way to
# watch the branch it was told it cannot own.
for want in "--claim sha:" "--events terminal-ci" "branch:<topic branch>"; do
  grep -qF -- "$want" "$work/err" \
    || no "the refusal offers [$want]" "$(cat "$work/err")"
done
ok "the refusal names the bounded alternatives"
run defangdevs/agent-box --all-events --claim branch:fix/706 >/dev/null
[ $? -eq 0 ] && ok "a topic branch is still claimable" \
  || no "a topic branch is still claimable" "$(cat "$work/err")"

# A repository's default branch is whatever it SAYS it is, so refusing only
# main/master would wave `branch:trunk` through on a repo whose default is
# trunk — and would refuse `branch:main` on one that has moved off it.
db_shim trunk
run defangdevs/agent-box --all-events --claim branch:trunk >/dev/null
[ $? -ne 0 ] && grep -q "the default branch of defangdevs/agent-box" "$work/err" \
  && ok "the resolved default branch is refused, not just main/master" \
  || no "the resolved default branch is refused, not just main/master" \
        "$(cat "$work/err")"
run defangdevs/agent-box --all-events --claim branch:master >/dev/null
[ $? -eq 0 ] \
  && ok "a branch that only LOOKS like a default is allowed once resolved" \
  || no "a branch that only LOOKS like a default is allowed once resolved" \
        "$(cat "$work/err")"
if [ -s "$LOCAL_WEBHOOK_STATE_DIR/default-branch.json" ]; then
  ok "the resolved default branch is cached"
else
  no "the resolved default branch is cached"
fi
# ...and a branch that only looks like a default name is fine once the
# repository has answered.
db_shim trunk
run defangdevs/agent-box --all-events --claim branch:main >/dev/null
[ $? -eq 0 ] && ok "branch:main is claimable on a repo whose default is trunk" \
  || no "branch:main is claimable on a repo whose default is trunk" \
        "$(cat "$work/err")"
rm -f "$work/bin/gh" "$LOCAL_WEBHOOK_STATE_DIR/default-branch.json"

# A wildcard topic names no repository to ask about, and a non-GitHub source
# has no GitHub default branch; neither may become a hard refusal of an
# ordinary branch name.
run 'defangdevs/*' --all-events --claim branch:release-2 >/dev/null
[ $? -eq 0 ] && ok "a wildcard topic does not break branch claims" \
  || no "a wildcard topic does not break branch claims" "$(cat "$work/err")"
run gitlab:group/project --all-events --claim branch:release-2 >/dev/null
[ $? -eq 0 ] && ok "a non-GitHub topic does not break branch claims" \
  || no "a non-GitHub topic does not break branch claims" "$(cat "$work/err")"

# --- the merge boundary is handed over, not left to be rediscovered -----
run defangdevs/agent-box --claim 42 --events actionable \
    --note "PR 42: CI and review" >"$work/out"
if grep -q "POST-MERGE CI IS NOT COVERED" "$work/err" \
   && grep -q -- "--claim sha:<merge commit>" "$work/err"; then
  ok "a numbered claim explains where it stops"
else
  no "a numbered claim explains where it stops" "$(cat "$work/err")"
fi
# ...and in the NOTE, which is echoed under every delivery: a resumed session
# reads the note, not the command line it no longer has.
stored=$(awk '/^--note$/ { getline; print; exit }' "$work/out")
case "$stored" in
  ("PR 42: CI and review ["*"post-merge CI is NOT covered"*)
    ok "the merge boundary is stored in the note" ;;
  (*) no "the merge boundary is stored in the note" "note was: $stored" ;;
esac
# Re-subscribing must not stack a second copy onto the note it already has.
run defangdevs/agent-box --claim 42 --events actionable --note "$stored" >"$work/out"
again=$(awk '/^--note$/ { getline; print; exit }' "$work/out")
[ "$again" = "$stored" ] \
  && ok "re-subscribing does not stack the boundary onto the note again" \
  || no "re-subscribing does not stack the boundary onto the note again" "$again"
# A branch or sha claim has no merge to explain, and the sentence would be
# noise under every delivery.
run defangdevs/agent-box --claim "sha:$SHA" --events terminal-ci >/dev/null
if grep -q "POST-MERGE" "$work/err"; then
  no "a sha claim gets no merge-boundary sentence" "$(cat "$work/err")"
else
  ok "a sha claim gets no merge-boundary sentence"
fi

# --- the matcher's half of the promise ----------------------------------
#
# Shape is what the wrapper wrote; what an agent RECEIVES is the pinned
# webhook.py matching that rule against a payload. The incident was 21
# check_run and 15 workflow events, so the assertion that matters is which
# of those the policy lets through.
if [ -n "$WEBHOOK_PY" ]; then
  include_of defangdevs/agent-box --claim 42 --claim "branch:fix/706" \
    --events terminal-ci > "$work/policy.json"
  include_of defangdevs/agent-box --all-events --claim "sha:$SHA" \
    > "$work/sha.json"
  if out=$(python3 - "$WEBHOOK_PY" "$work/policy.json" "$work/sha.json" "$SHA" 2>&1 <<'MATCH'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('wh', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
policy = json.load(open(sys.argv[2]))
sha_claim = json.load(open(sys.argv[3]))
SHA, OTHER = sys.argv[4], '0123456789abcdef0123456789abcdef01234567'
for rule in (policy, sha_claim):
    assert m.predicate_error(rule) is None, m.predicate_error(rule)

# The noise the incident drowned in: lifecycle events on the claimed object
# that nobody is waiting for.
noise = [
    {'action': 'created', 'check_run': {'check_suite': {'head_branch': 'fix/706'},
                                        'status': 'queued', 'conclusion': None}},
    {'action': 'in_progress', 'workflow_run': {'head_branch': 'fix/706',
                                               'status': 'in_progress',
                                               'conclusion': None}},
    {'action': 'queued', 'workflow_job': {'head_branch': 'fix/706',
                                          'conclusion': None}},
]
for p in noise:
    assert not m.match_predicate(policy, p), p

# ...and what a session holding that PR still has to receive.
wanted = [
    {'action': 'completed', 'workflow_run': {'head_branch': 'fix/706',
                                             'conclusion': 'success'}},
    {'action': 'completed', 'check_run': {'check_suite': {'head_branch': 'fix/706'},
                                          'conclusion': 'failure'}},
    # The floor: a review is not a terminal CI outcome, but dropping it would
    # also drop the CLAIM on it, and a standing watch would spawn a second
    # session onto this PR.
    {'action': 'submitted', 'review': {'state': 'changes_requested'},
     'pull_request': {'number': 42}},
    {'action': 'created', 'issue': {'number': 42},
     'comment': {'html_url': 'https://github.com/o/r/pull/42#issuecomment-1'}},
]
for p in wanted:
    assert m.match_predicate(policy, p), p

# An unrelated PR's green run is the whole point of claim scope.
assert not m.match_predicate(
    policy, {'action': 'completed',
             'workflow_run': {'head_branch': 'other', 'conclusion': 'success'}})

# The sha claim: every shape of one run, and no other commit on that branch.
same = [{'workflow_run': {'head_sha': SHA, 'head_branch': 'master'}},
        {'workflow_job': {'head_sha': SHA}},
        {'check_run': {'head_sha': SHA}},
        {'check_suite': {'head_sha': SHA}},
        {'deployment': {'sha': SHA}, 'deployment_status': {'state': 'failure'}},
        {'sha': SHA, 'state': 'failure'}]
for p in same:
    assert m.match_predicate(sha_claim, p), p
for p in ({'workflow_run': {'head_sha': OTHER, 'head_branch': 'master'}},
          {'check_run': {'head_sha': OTHER}},
          {'sha': OTHER, 'state': 'failure'}):
    assert not m.match_predicate(sha_claim, p), p
print('ok')
MATCH
  ); then
    ok "webhook.py drops the lifecycle noise and keeps what the session needs"
  else
    no "webhook.py drops the lifecycle noise and keeps what the session needs" \
       "$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  fi
else
  echo "skip webhook.py match (no pin given)"
fi

echo
if [ "$fails" -eq 0 ]; then echo "all assertions passed"; else
  echo "$fails assertion(s) failed"; exit 1
fi
