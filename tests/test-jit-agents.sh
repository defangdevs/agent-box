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

# --- codex gets its standalone mirror re-made ---------------------------
setup codexmirror
export AGENT_BOX_AGENT_BINS="shell=/bin/bash"
agent_install codex >/dev/null 2>&1
if [ -x "$HOME/.codex/packages/standalone/current/codex" ]; then
  ok "a JIT-installed codex gets the standalone layout RC pairing needs"
else
  no "a JIT-installed codex gets the standalone layout RC pairing needs" \
     "no symlink at ~/.codex/packages/standalone/current/codex"
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
mkdir -p "$HOME/.codex/packages/standalone/current"
: > "$HOME/.codex/packages/standalone/current/stale-marker"
agent_install codex >/dev/null 2>&1
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
