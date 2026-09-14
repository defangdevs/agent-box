#!/usr/bin/env bash
# Unit tests for `agent-box-nixpkgs-cache` (issue #669) - the script that
# builds the shared nixpkgs git cache supervisor.sh seeds each user from.
#
# What is worth pinning here is not that a happy path writes two files. It is
# the GUARDS and the PUBLICATION ORDER. This runs unattended at every boot,
# and the two ways to get it wrong are both silent:
#
#   - a guard that is too loose re-ingests 72 MiB and ~30s of CPU on every
#     boot of every box, forever, for nothing;
#   - a guard that is too tight, or a publish that lands its marker before
#     the payload, leaves a directory that reads as "done" sitting over a
#     missing sqlite or a stale pin. Nothing revisits it, so the box silently
#     keeps the slow path it was supposed to have lost - and the only symptom
#     is a first install that is 25s slower than the release notes claim.
#
# `nix` is a shim that records its argv and fabricates a cache, so there is no
# network and no nixpkgs here and the whole file runs natively on every
# architecture.
set -u

SCRIPT=${1:?usage: test-nixpkgs-cache.sh PATH/TO/nixpkgs-cache.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

BASH_BIN=$(command -v bash)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fails=0
ok() { printf 'ok   %s\n' "$1"; }
no() { printf 'FAIL %s\n     %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

SEED="$work/seed"
export AGENT_BOX_NIXPKGS_CACHE_SEED="$SEED"

# --- nix shim ----------------------------------------------------------
# Records argv, then fabricates exactly what the real `nix eval` leaves in
# XDG_CACHE_HOME: a tarball-cache-v2 tree and the fetcher-cache sqlite beside
# it. NIX_SHIM_FAILS makes it exit non-zero (no network on a first boot);
# NIX_SHIM_EMPTY makes it succeed and write nothing (the shape a future nix
# that renamed the cache would have).
mkdir -p "$work/bin"
cat > "$work/bin/nix" <<EOF
#!$BASH_BIN
printf '%s\n' "\$*" >> "$work/nix.log"
printf '%s\n' "\${XDG_CACHE_HOME:-(unset)}" >> "$work/nix.cachehome"
[ -n "\${NIX_SHIM_FAILS:-}" ] && exit 1
[ -n "\${NIX_SHIM_EMPTY:-}" ] && exit 0
mkdir -p "\$XDG_CACHE_HOME/nix/tarball-cache-v2/objects/pack"
printf 'pack for %s\n' "\${NIX_SHIM_REV:-one}" \
  > "\$XDG_CACHE_HOME/nix/tarball-cache-v2/objects/pack/p.pack"
printf 'sqlite for %s\n' "\${NIX_SHIM_REV:-one}" \
  > "\$XDG_CACHE_HOME/nix/fetcher-cache-v4.sqlite"
echo fake-version
exit 0
EOF
chmod +x "$work/bin/nix"
export AGENT_BOX_NIX_BIN="$work/bin/nix"

REF_ONE=https://example.invalid/nixos-1/nixexprs.tar.xz
REF_TWO=https://example.invalid/nixos-2/nixexprs.tar.xz

run() { # run [REF]
  : > "$work/nix.log"
  AGENT_BOX_NIXPKGS="${1-$REF_ONE}" \
    bash "$SCRIPT" > "$work/out" 2>&1
}
nix_ran() { [ -s "$work/nix.log" ]; }
pin() { cat "$SEED/pin" 2>/dev/null; }

# --- no pin: nothing to build ------------------------------------------
# Both renderers always set AGENT_BOX_NIXPKGS, so this is the hand-run - and
# it must not leave a half-made directory that the guard would later trust.
if AGENT_BOX_NIXPKGS="" bash "$SCRIPT" > "$work/out" 2>&1 &&
   [ ! -e "$SEED" ]; then
  ok "an empty ref exits 0 and creates nothing"
else no "an empty ref exits 0 and creates nothing" "$(cat "$work/out"; ls -a "$SEED" 2>&1)"; fi

# --- cold build ---------------------------------------------------------
if run && [ -d "$SEED/tarball-cache-v2" ] && [ -f "$SEED/fetcher-cache-v4.sqlite" ]; then
  ok "a cold box publishes both halves of the cache"
else no "a cold box publishes both halves of the cache" "$(cat "$work/out")"; fi

if [ "$(pin)" = "$REF_ONE" ]; then ok "the published pin is the ref it was built from"
else no "the published pin is the ref it was built from" "$(pin)"; fi

# The eval must be staged somewhere OTHER than the seed: nix writing straight
# into the published directory is what makes an interrupted run look complete.
if ! grep -qx "$SEED" "$work/nix.cachehome"; then
  ok "nix is never pointed at the published directory"
else no "nix is never pointed at the published directory" "$(cat "$work/nix.cachehome")"; fi

if [ -z "$(find "$work" -maxdepth 1 -name 'seed.staging*' -print -quit)" ]; then
  ok "the staging directory is cleaned up"
else no "the staging directory is cleaned up" "$(ls -a "$work")"; fi

# --- the ordinary boot: guard holds -------------------------------------
if run && ! nix_ran; then ok "an up-to-date pin does no work at all"
else no "an up-to-date pin does no work at all" "$(cat "$work/nix.log")"; fi

# --- a moved pin rebuilds ------------------------------------------------
# The box updated and jitNixpkgs advanced. A cache for a revision this box no
# longer installs is the case that must NOT be mistaken for "already seeded".
NIX_SHIM_REV=two run "$REF_TWO"
if nix_ran && [ "$(pin)" = "$REF_TWO" ] &&
   grep -q 'for two' "$SEED/tarball-cache-v2/objects/pack/p.pack"; then
  ok "a moved pin republishes the cache and the pin"
else no "a moved pin republishes the cache and the pin" "$(pin); $(cat "$work/out")"; fi

if [ ! -e "$SEED/.tarball-cache-v2.replacing" ]; then
  ok "replacing a cache leaves no leftover behind"
else no "replacing a cache leaves no leftover behind" "$(ls -a "$SEED")"; fi

# --- a cache with no pin file is not trusted -----------------------------
# An image that baked a cache without recording what it is built from. One
# rebuild is cheap; trusting an unknown revision forever is not.
rm -f "$SEED/pin"
if run && nix_ran && [ "$(pin)" = "$REF_ONE" ]; then
  ok "a cache with no pin file is rebuilt, not trusted"
else no "a cache with no pin file is rebuilt, not trusted" "$(cat "$work/out")"; fi

# --- failures publish nothing, and stay retryable ------------------------
rm -rf "$SEED"
if ! NIX_SHIM_FAILS=1 run; then ok "an eval that fails exits non-zero"
else no "an eval that fails exits non-zero" "$(cat "$work/out")"; fi

if [ ! -d "$SEED/tarball-cache-v2" ]; then
  ok "an eval that fails publishes no cache"
else no "an eval that fails publishes no cache" "$(ls -a "$SEED")"; fi

if ! NIX_SHIM_EMPTY=1 run; then ok "an eval leaving no cache exits non-zero"
else no "an eval leaving no cache exits non-zero" "$(cat "$work/out")"; fi

if [ ! -d "$SEED/tarball-cache-v2" ]; then
  ok "an eval leaving no cache publishes nothing"
else no "an eval leaving no cache publishes nothing" "$(ls -a "$SEED")"; fi

# The whole point of the guard being keyed on tarball-cache-v2: a run that
# died after the sqlite landed must be REDONE, not read as complete.
rm -rf "$SEED"; mkdir -p "$SEED"
printf 'interrupted\n' > "$SEED/fetcher-cache-v4.sqlite"
printf '%s\n' "$REF_ONE" > "$SEED/pin"
if run && nix_ran && [ -d "$SEED/tarball-cache-v2" ]; then
  ok "an interrupted publish is finished by the next run"
else no "an interrupted publish is finished by the next run" "$(cat "$work/out")"; fi

# --- the seed is readable by the users it exists for ---------------------
# It is copied by OTHER users' supervisors. A private umask on whatever
# started the unit would make every one of those copies fail, silently, and
# the box would just be slow forever.
rm -rf "$SEED"
( umask 077; run )
unreadable=$(find "$SEED" ! -perm -004 -print 2>/dev/null | head -5)
if [ -z "$unreadable" ]; then ok "a private umask still publishes a world-readable seed"
else no "a private umask still publishes a world-readable seed" "$unreadable"; fi

printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo "all checks passed" || echo "$fails failed")"
[ "$fails" -eq 0 ]
