#!/usr/bin/env bash
# Bake an agent-box Azure image. Runs ONCE, as root, on a throwaway build VM
# created from the same stock Ubuntu marketplace image a real deployment uses
# (issue #697).
#
# What it is for: an Azure box spends 3m47s in its CustomScript bootstrap, and
# ~750 of that log's 855 lines are one `nix profile install ...#runtime`
# copying a 797 MiB closure out of cache.nixos.org. With the store already
# warm, that exact install takes 2.9s - measured on a live aarch64 box. So the
# image carries the closure and the bootstrap stops paying for it.
#
# What it bakes, and what it deliberately does NOT:
#
#   the STORE, yes. /nix/store is a content-addressed cache, so a box booting
#   from this image finds the paths it needs already there and downloads only
#   what has changed since. An image that has fallen behind master is
#   therefore slower, never wrong.
#
#   the PROFILE, no. /nix/var/nix/profiles/agent-box is box STATE, and
#   `nix profile install` refuses an element it already has. Baking it would
#   make the bootstrap fail on every boot from this image; leaving it out
#   means the bootstrap runs completely unchanged and simply finds its work
#   already done.
#
#   any agent-box CONFIGURATION, no. This script never runs `agentbox apply`,
#   so there is no /etc/agent-box, no user, no password hash, no host key and
#   no session state to leak into an image that other people deploy.
#
# The caller (.github/workflows/azure-image.yml) generalizes and captures the
# VM after this exits.
set -euxo pipefail

# The extension handler runs with an almost empty environment. Nix's own
# profile script dereferences $HOME unguarded, so it must exist before we
# source anything - the same trap the deploy bootstrap documents.
export HOME="${HOME:-/root}"
export DEBIAN_FRONTEND=noninteractive

NIXINSTALLER="${NIXINSTALLER:?the Determinate installer URL}"
FLAKEREF="${FLAKEREF:?the agent-box flake ref to warm the store from}"

# The build VM is freshly created, so cloud-init may still hold the dpkg lock.
cloud-init status --wait || true
APT="apt-get -o DPkg::Lock::Timeout=600 -qq"

# zram lives in a separate package on the linux-azure kernel (issue #435), and
# the marketplace image does not carry it. Baking it here means a deployed box
# has compressed swap without waiting for an apt round trip on first boot.
# Non-fatal: a kernel with no matching package must not fail an image build.
$APT update || true
$APT install -y "linux-modules-extra-$(uname -r)" || \
  echo "WARNING: linux-modules-extra unavailable for $(uname -r) (issue #435)"

# Nix as a package manager, exactly as the deploy bootstrap installs it, so
# the image and a from-scratch box differ in what is CACHED and in nothing
# else.
curl -fsSL "$NIXINSTALLER" | sh -s -- install linux --no-confirm --determinate
# `set +u` around a script we do not own: the installer's profile snippet is
# free to reference whatever the next release wants.
set +u
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
set -u

# Realize the runtime closure into the store. `nix build` rather than
# `nix profile install` for the reason in the header - and --out-link into
# /nix/var/nix/gcroots so the paths are a GC ROOT. Without that, the nix-gc
# timer a deployed box enables could reclaim the whole point of this image
# before anything has used it.
install -d -m 0755 /nix/var/nix/gcroots
nix build --print-out-paths \
  --out-link /nix/var/nix/gcroots/agent-box-image \
  "$FLAKEREF#runtime"

# Warm root's own flake/tarball cache too. First boot still has to EVALUATE
# the flake ref before it can notice the store is warm, and that evaluation
# fetches nixpkgs as a flake input and ingests it into a git cache - the same
# ~25s of CPU-bound object construction issue #669 measures for a user's
# first harness install. Cheap here, paid once, never again.
nix flake metadata "$FLAKEREF" > /dev/null || true

# Leave the image smaller and without this build's fingerprints.
$APT clean || true
rm -rf /var/lib/apt/lists/* || true
find /var/log -type f -exec truncate -s 0 {} + || true
rm -f /root/.bash_history || true

# Nothing box-specific should exist, because nothing here created any. Assert
# it rather than trust it: this image is deployed by other people, and a
# password hash or a host key baked into it would be shipped to all of them.
for leak in /etc/agent-box /var/lib/agent-box /home/agent; do
  if [ -e "$leak" ]; then
    echo "REFUSING: $leak exists in the image being baked" >&2
    exit 1
  fi
done

sync
echo BAKE_OK
