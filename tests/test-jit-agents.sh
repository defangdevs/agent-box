#!/usr/bin/env bash
# Unit tests for modules/src/lib/agents.sh — harness resolution and the
# lazy (JIT) install (issue #416).
#
# This is where the lazy-harness model is actually pinned. The VM tests
# cannot cover it: a real install reaches the network, and a check that
# depends on channels.nixos.org being up is a check that goes red for
# someone else's outage. So `nix` and `flock` are shimmed and every branch
# is driven from the filesystem.
#
# What must not regress:
#   - the AGENT_BOX_AGENT_BINS table still WINS, so an eagerly installed
#     harness keeps its pinned store path and a conventional box is
#     unchanged;
#   - a name off the session registry can never become an arbitrary path;
#   - a failing install is rate-limited, because the supervisor respawns a
#     session that cannot start every couple of seconds.
set -u

LIB=${1:?usage: test-jit-agents.sh PATH/TO/lib/agents.sh}
[ -f "$LIB" ] || { echo "no such lib: $LIB" >&2; exit 2; }
LIB=$(cd "$(dirname "$LIB")" && pwd)/$(basename "$LIB")

BASH_BIN=$(command -v bash)
[ -n "$BASH_BIN" ] || { echo "no bash on PATH" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }
is() { # is LABEL EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "expected [$2], got [$3]"; fi
}

# A fresh sandbox per case: $HOME is where both the profile and the JIT
# bookkeeping live, so cases must not share one.
setup() {
  HOME=$work/home.$1
  rm -rf "$HOME"; mkdir -p "$HOME/.nix-profile/bin"
  export HOME
  # The bookkeeping paths are derived from $HOME, not from environment
  # knobs (scripts/check_backend_parity.py reads an assigned AGENT_BOX_*
  # name as a host contract), so pointing $HOME at a scratch dir is what
  # isolates a case.
  JIT_DIR="$HOME/.local/state/agent-box/jit"
  export AGENT_BOX_NIXPKGS="https://example.invalid/nixpkgs.tar.xz"
  export AGENT_BOX_NIX_BIN="$work/bin/nix"
  export AGENT_BOX_FLOCK_BIN="$work/bin/flock"
  export AGENT_BOX_AGENT_BINS="shell=/bin/bash"
  export NIX_CALLS="$HOME/nix-calls"
  : > "$NIX_CALLS"
  # shellcheck disable=SC1090
  . "$LIB"
}

mkdir -p "$work/bin"
# `nix` shim: records its argv, and installs a fake binary unless the case
# asked it to fail (NIX_FAIL).
cat > "$work/bin/nix" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "\$NIX_CALLS"
[ -n "\${NIX_FAIL:-}" ] && exit 1
# "\$AGENT_BOX_NIXPKGS#attr" is the last argument; map it back to a binary.
attr=\${@: -1}; attr=\${attr##*#}
case "\$attr" in
  claude-code) name=claude ;;
  codex) name=codex ;;
  *) exit 1 ;;
esac
printf '#!$BASH_BIN\n' > "\$HOME/.nix-profile/bin/\$name"
chmod +x "\$HOME/.nix-profile/bin/\$name"
exit 0
EOF
chmod +x "$work/bin/nix"
# flock shim: these tests are single-threaded, so taking the lock always
# succeeds. It records its argv, because the one property that matters
# here IS in the arguments — see the bounded-wait case below.
cat > "$work/bin/flock" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "\$HOME/flock-calls"
exit 0
EOF
chmod +x "$work/bin/flock"

# --- attribute mapping --------------------------------------------------
setup attr
is "agent_attr maps claude to the claude-code attribute" \
   "claude-code" "$(agent_attr claude)"
is "agent_attr maps codex to itself" "codex" "$(agent_attr codex)"
if agent_attr shell >/dev/null 2>&1; then
  no "agent_attr refuses 'shell'" "it returned success"
else
  ok "agent_attr refuses 'shell'"
fi

# --- the table wins -----------------------------------------------------
setup table
mkdir -p "$HOME/eager/bin"
printf '#!%s\n' "$BASH_BIN" > "$HOME/eager/bin/claude"
chmod +x "$HOME/eager/bin/claude"
export AGENT_BOX_AGENT_BINS="claude=$HOME/eager/bin/claude shell=$BASH_BIN"
printf '#!%s\n' "$BASH_BIN" > "$HOME/.nix-profile/bin/claude"
chmod +x "$HOME/.nix-profile/bin/claude"
is "an eagerly installed harness keeps its pinned store path" \
   "$HOME/eager/bin/claude" "$(agent_bin claude)"
is "shell still resolves from the table" "$BASH_BIN" "$(agent_bin shell)"

# "shell" is the login shell: it has no profile alternative to fall
# through to, so it must resolve from the table even when the path is
# gone — failing at exec, with the path in the message, beats vanishing
# into "agent is not installed".
setup shell_missing
export AGENT_BOX_AGENT_BINS="shell=$HOME/gone/bash"
is "shell resolves from the table even when the path is missing" \
   "$HOME/gone/bash" "$(agent_bin shell)"

# The native backend names "<profile>/bin/<agent>" whether or not the
# profile was built with that agent, so a table entry that is not there
# must fall through to the profile instead of shadowing it with a path
# that execs into nothing.
setup stale_table
export AGENT_BOX_AGENT_BINS="claude=$HOME/gone/bin/claude shell=$BASH_BIN"
printf '#!%s\n' "$BASH_BIN" > "$HOME/.nix-profile/bin/claude"
chmod +x "$HOME/.nix-profile/bin/claude"
is "a table entry naming a missing binary falls through to the profile" \
   "$HOME/.nix-profile/bin/claude" "$(agent_bin claude)"

# --- profile fallback ---------------------------------------------------
setup fallback
if agent_bin claude >/dev/null 2>&1; then
  no "an uninstalled harness does not resolve" "agent_bin succeeded"
else
  ok "an uninstalled harness does not resolve"
fi
printf '#!%s\n' "$BASH_BIN" > "$HOME/.nix-profile/bin/claude"
chmod +x "$HOME/.nix-profile/bin/claude"
is "a JIT-installed harness resolves from the user profile" \
   "$HOME/.nix-profile/bin/claude" "$(agent_bin claude)"

# --- a registry name can never become a path ----------------------------
setup hostile
mkdir -p "$HOME/evil"
printf '#!%s\n' "$BASH_BIN" > "$HOME/evil/x"; chmod +x "$HOME/evil/x"
for bad in "../evil/x" "/bin/sh" "claude; id" "" "opencode"; do
  if agent_bin "$bad" >/dev/null 2>&1; then
    no "agent_bin refuses [$bad]" "it resolved"
  else
    ok "agent_bin refuses [$bad]"
  fi
done

# --- a successful install ----------------------------------------------
setup install
if agent_install claude >/dev/null 2>&1; then ok "agent_install claude succeeds"
else no "agent_install claude succeeds" "it returned failure"; fi
is "the installed harness now resolves" \
   "$HOME/.nix-profile/bin/claude" "$(agent_bin claude)"
call=$(cat "$NIX_CALLS")
case "$call" in
  *"profile add"*) ok "it ran 'nix profile add'" ;;
  *) no "it ran 'nix profile add'" "argv was [$call]" ;;
esac
case "$call" in
  *--impure*) ok "the install is --impure (unfree claude-code needs it)" ;;
  *) no "the install is --impure" "argv was [$call]" ;;
esac
case "$call" in
  *"https://example.invalid/nixpkgs.tar.xz#claude-code"*)
    ok "it installs from the box's pinned nixpkgs, not the flake registry" ;;
  *) no "it installs from the box's pinned nixpkgs" "argv was [$call]" ;;
esac

# --- the standalone mirror points at the binary it is handed -----------
# The mirror is seeded at SESSION START now, not at install time (issue
# #572): start_session hands it the binary it is about to launch, so the
# argument is the contract these cases pin. Driving it through
# agent_install instead — as this case used to — would only prove the JIT
# path, which is the one path that was never broken.
setup codexmirror
export AGENT_BOX_AGENT_BINS="shell=/bin/bash"
printf '#!%s\n' "$BASH_BIN" > "$HOME/.nix-profile/bin/codex"
chmod +x "$HOME/.nix-profile/bin/codex"
mirror_codex_standalone "$HOME/.nix-profile/bin/codex"
if [ -x "$HOME/.codex/packages/standalone/current/codex" ]; then
  ok "the standalone layout RC pairing needs is created"
else
  no "the standalone layout RC pairing needs is created" \
     "no symlink at ~/.codex/packages/standalone/current/codex"
fi
is "current/codex resolves to the binary it was handed" \
   "$(readlink -f "$HOME/.nix-profile/bin/codex")" \
   "$(readlink -f "$HOME/.codex/packages/standalone/current/codex")"

# --- a codex nothing on the box can RESOLVE is still mirrored ----------
# The bug in issue #572: the settings page's Connections card runs its own
# `nix profile add` and the eager table names a path the runtime profile
# was built without, so at the moment the layout was seeded nothing could
# resolve a codex at all. Passing the binary explicitly is what makes the
# mirror independent of resolution — assert that with an AGENT_BOX_AGENT_BINS
# naming a codex that does not exist, which is exactly what a native box
# whose profile ships no harnesses has.
setup codexmirror_unresolvable
export AGENT_BOX_AGENT_BINS="codex=/nonexistent/profile/bin/codex shell=/bin/bash"
printf '#!%s\n' "$BASH_BIN" > "$HOME/card-installed-codex"
chmod +x "$HOME/card-installed-codex"
if agent_bin codex >/dev/null 2>&1; then
  no "the fixture really has an unresolvable codex" "agent_bin resolved one"
fi
mirror_codex_standalone "$HOME/card-installed-codex"
is "an explicitly passed codex is mirrored even when agent_bin resolves none" \
   "$HOME/card-installed-codex" \
   "$(readlink "$HOME/.codex/packages/standalone/agent-box-current/codex")"

# --- a bare call with no codex anywhere is a silent no-op --------------
# start_session only calls this from its codex branch, so the bare form is
# reached by nothing today — but it must not leave a half-built layout
# behind (a `current` symlink pointing at a codex that is not there would
# make `daemon start` fail with a dangling path instead of a clear one).
setup codexmirror_nocodex
export AGENT_BOX_AGENT_BINS="shell=/bin/bash"
if mirror_codex_standalone; then
  ok "a bare call with no codex installed returns success"
else
  no "a bare call with no codex installed returns success" "it returned failure"
fi
if [ -e "$HOME/.codex/packages/standalone/current" ]; then
  no "a bare call with no codex leaves no layout behind" \
     "current exists with no codex to point at"
else
  ok "a bare call with no codex leaves no layout behind"
fi

# --- the standalone mirror survives a pre-existing REAL directory ------
# issue #95: a curl-installed Codex with an unusual manual layout can leave
# `current` as a real directory instead of a symlink. `ln -sfn` cannot
# unlink a non-empty directory, so the naive version of this silently
# created `current/agent-box-current` INSIDE it and left the stale layout
# in place. A fresh VM never has a pre-existing directory there, so this
# case can only be caught here, not by the sessions VM test.
setup codexmirror_realdir
export AGENT_BOX_AGENT_BINS="shell=/bin/bash"
printf '#!%s\n' "$BASH_BIN" > "$HOME/.nix-profile/bin/codex"
chmod +x "$HOME/.nix-profile/bin/codex"
mkdir -p "$HOME/.codex/packages/standalone/current"
: > "$HOME/.codex/packages/standalone/current/stale-marker"
mirror_codex_standalone "$HOME/.nix-profile/bin/codex"
if [ -L "$HOME/.codex/packages/standalone/current" ]; then
  ok "a pre-existing real 'current' directory is replaced with a symlink"
else
  no "a pre-existing real 'current' directory is replaced with a symlink" \
     "current is still a real directory, not a symlink"
fi
if [ -x "$HOME/.codex/packages/standalone/current/codex" ]; then
  ok "the replaced symlink resolves to the mirrored codex binary"
else
  no "the replaced symlink resolves to the mirrored codex binary" \
     "no executable at current/codex"
fi
if [ -e "$HOME/.codex/packages/standalone/current/stale-marker" ]; then
  no "the stale directory's contents are gone, not nested underneath" \
     "stale-marker still resolves through current"
else
  ok "the stale directory's contents are gone, not nested underneath"
fi

# --- failure is rate-limited -------------------------------------------
setup cooldown
export NIX_FAIL=1
agent_install claude >/dev/null 2>&1 && no "a failing install reports failure" "it returned success"
is "the failure was one nix call" "1" "$(wc -l < "$NIX_CALLS")"
agent_install claude >/dev/null 2>&1
is "a second attempt inside the window does not call nix again" \
   "1" "$(wc -l < "$NIX_CALLS")"
# Wind the marker back past the retry window and it tries once more.
printf '%s\n' "$(( $(date +%s) - 100000 ))" > "$JIT_DIR/claude.failed"
agent_install claude >/dev/null 2>&1
is "an attempt after the window calls nix again" \
   "2" "$(wc -l < "$NIX_CALLS")"
# And a success clears the marker, so the next miss is not pre-poisoned.
# The attempt above FAILED, which re-armed the cooldown — wind it back
# again or this call never reaches nix at all (which is exactly what the
# rate limit is for).
unset NIX_FAIL
printf '%s\n' "$(( $(date +%s) - 100000 ))" > "$JIT_DIR/claude.failed"
agent_install claude >/dev/null 2>&1
if [ -e "$JIT_DIR/claude.failed" ]; then
  no "a successful install clears the cooldown marker" "marker still there"
else
  ok "a successful install clears the cooldown marker"
fi

# --- the install lock is BOUNDED ----------------------------------------
# start_session calls agent_install from the supervisor's reconcile loop,
# so an unbounded wait on a wedged install would stop it starting or
# reaping every OTHER session too.
setup lockwait
agent_install claude >/dev/null 2>&1
case "$(cat "$HOME/flock-calls" 2>/dev/null)" in
  *-w*) ok "the install lock waits with a timeout, never forever" ;;
  *) no "the install lock waits with a timeout, never forever" \
        "flock argv was [$(cat "$HOME/flock-calls" 2>/dev/null)]" ;;
esac

# --- nix is resolved at use, not baked in -------------------------------
# A native box cannot pin a store path (doing so baked the building host's
# nix into a generated file), so an unset AGENT_BOX_NIX_BIN must still find
# nix on PATH.
setup nixfrompath
unset AGENT_BOX_NIX_BIN
PATH=$work/bin:$PATH
export PATH
if agent_install claude >/dev/null 2>&1; then
  ok "with no AGENT_BOX_NIX_BIN the install resolves nix from PATH"
else
  no "with no AGENT_BOX_NIX_BIN the install resolves nix from PATH" \
     "agent_install failed"
fi

# --- no pinned nixpkgs = no install, and no crash -----------------------
setup nopin
unset AGENT_BOX_NIXPKGS
if agent_install claude >/dev/null 2>&1; then
  no "without AGENT_BOX_NIXPKGS the install refuses" "it returned success"
else
  ok "without AGENT_BOX_NIXPKGS the install refuses"
fi

echo
if [ "$fails" -eq 0 ]; then echo "all assertions passed"; else
  echo "$fails assertion(s) failed"; exit 1
fi
