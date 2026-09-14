set -u
# Build the shared nixpkgs git cache that every user's FIRST harness install
# reads (issue #669).
#
# This is the producing half. supervisor.sh has carried the consuming half
# since PR #670 and has been inert on every box ever since, because nothing
# has ever written the directory it reads.
#
# What it buys: a box's first `nix profile add` spends ~25s of its ~36s turning
# the pinned nixpkgs tarball into git objects - 54,075 blobs SHA-1'd and
# deflated, CPU-bound - before nix has read a line of any package definition.
# That work is byte-identical on every box, and it is the SAME revision for
# every harness, because agent_install resolves them all against
# $AGENT_BOX_NIXPKGS. Measured on a 2-vCPU aarch64 box: the eval below takes
# 27.6s against an empty cache and 1.4s against the one it leaves behind.
#
# Issue #669 asked for this to be baked into the VM image. There is no custom
# image build in this repo for either cloud - AWS tracks stock upstream NixOS
# AMIs and Azure deploys a stock marketplace image - so the box builds its own
# instead, once, in the background, from the ref it is actually pinned to.
# That has one property a baked image cannot have: it can never be stale
# relative to THIS box's own pin. It also does not close the image door - an
# image build that wants to bake the cache runs this same script, and the unit
# then finds the pin already current and exits.
NIX="${AGENT_BOX_NIX_BIN:?}"
SEED="${AGENT_BOX_NIXPKGS_CACHE_SEED:-/var/lib/agent-box/nixpkgs-cache}"
REF="${AGENT_BOX_NIXPKGS:-}"

# Everything written here is read by OTHER users - that is the entire point -
# so none of it may inherit a private umask from whoever started the unit.
umask 022

# No pin, nothing to build. Both renderers always set it, so this is the
# hand-run and the test, not a box.
[ -n "$REF" ] || exit 0

# The ordinary boot: one stat and one short read, then out. The pin is
# COMPARED rather than merely required to exist, so a box whose jitNixpkgs
# moved under it - a self-update advancing selfUpdate.agentNixpkgs - rebuilds
# against what its sessions will actually install, instead of handing every
# new user a cache for a revision the box has stopped using.
#
# That comparison is only as sharp as the ref is specific, and on a box with
# no agent-nixpkgs pin the ref is the MUTABLE channel URL. The string then
# never changes however far the channel moves, so the cache is never rebuilt
# and a new user gets one built for an older revision. That degrades rather
# than breaks: git dedupes ~92% of the objects, which the issue measured at
# 7.3s against 23.8s cold. Resolving the channel to its immutable release
# here would sharpen it, but that is the job of the pin the update service
# already maintains, and duplicating it would give the box two answers to
# one question.
if [ -d "$SEED/tarball-cache-v2" ] &&
   [ "$(cat "$SEED/pin" 2>/dev/null || :)" = "$REF" ]; then
  exit 0
fi

# Staged in a SIBLING of the seed, so publishing is a rename inside one
# directory rather than a 72 MiB copy across two.
STAGE="$SEED.staging"
rm -rf "$STAGE" || :
mkdir -p "$SEED" "$STAGE" || exit 1
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM

# `lib.version`, not a harness attribute. The git ingest is a property of the
# TARBALL rather than of whatever is evaluated out of it, so the cheapest
# attribute in the flake produces byte-identical objects - verified rather
# than assumed: a cache built this way resolves claude-code's outPath in 1.4s.
# Evaluating a harness instead would additionally need NIXPKGS_ALLOW_UNFREE
# and would pin this unit to the harness list for no gain.
#
# --impure mirrors how agent_install resolves the same ref. The experimental
# features are named explicitly because the nix.conf in force here is the
# box's, not a session's.
if ! XDG_CACHE_HOME="$STAGE" "$NIX" eval --impure --raw \
     --extra-experimental-features 'nix-command flakes' \
     "$REF#lib.version" > /dev/null; then
  echo "nixpkgs-cache: could not evaluate $REF" >&2
  exit 1
fi

# Both halves or neither: the packfiles are the objects, and the small
# fetcher-cache sqlite is the URL -> treeHash map. Handed only the packs, nix
# cannot learn the tree hash without redoing the entire ingest, so a seed
# missing the sqlite saves nobody anything.
if [ ! -d "$STAGE/nix/tarball-cache-v2" ] ||
   [ ! -f "$STAGE/nix/fetcher-cache-v4.sqlite" ]; then
  echo "nixpkgs-cache: nix left no usable cache under $STAGE/nix" >&2
  exit 1
fi

# Publication ORDER is load-bearing, and it is the same rule the consumer in
# supervisor.sh follows: tarball-cache-v2 is what both guards key on, so it is
# the last thing to land. A kill between any two steps here then leaves this
# script's own guard unsatisfied and the next boot redoes the whole thing -
# rather than leaving behind a directory that reads as "done" while sitting
# over a missing sqlite or a stale pin, which no later run would revisit.
mv -f "$STAGE/nix/fetcher-cache-v4.sqlite" "$SEED/fetcher-cache-v4.sqlite" || exit 1
{ printf '%s\n' "$REF" > "$STAGE/pin" && mv -f "$STAGE/pin" "$SEED/pin"; } || exit 1

# rename(2) refuses a non-empty target directory, so an existing cache moves
# aside first. The window in which neither is in place is one rename long, and
# a consumer landing inside it is a best-effort seed that simply does not
# happen on that start.
rm -rf "$SEED/.tarball-cache-v2.replacing" || :
if [ -d "$SEED/tarball-cache-v2" ] &&
   ! mv -T "$SEED/tarball-cache-v2" "$SEED/.tarball-cache-v2.replacing"; then
  exit 1
fi
mv -T "$STAGE/nix/tarball-cache-v2" "$SEED/tarball-cache-v2" || exit 1
rm -rf "$SEED/.tarball-cache-v2.replacing" || :

exit 0
