# The Defang CLI, pinned. ONE source of truth for both backends: the NixOS
# module names this file for agent-box-defang-cli.service's background
# install, and nix/runtime.nix ships it into the runtime profile's share
# dir so a native box's settings page can install it from the Defang card
# (issue #461).
#
# Not in nixpkgs, so there is no `attr` a card could fetch. DefangLabs/defang
# ships its own canonical packaging at pkgs/defang/cli.nix - `buildGo125Module`
# against the repo's own src/, with a vendorHash that is only ever valid for
# the go.sum sitting next to it. Fetching the whole source tree at one pinned
# tag and calling THEIR file, rather than re-deriving a build here, means that
# hash never needs maintaining: it travels with the tag. (The file beside it,
# pkgs/defang/default.nix - a fetchurl of the prebuilt release binary - is
# lib.warn-marked deprecated upstream and stuck on an old version; don't reach
# for that one.)
#
# The nixpkgs pin is NOT this box's nixpkgs, and that is the whole point. A
# derivation's output hash is a function of every input, nixpkgs included, so
# building cli.nix against whatever nixpkgs the host happens to have produces a
# DIFFERENT derivation than DefangLabs' release CI built, silently misses the
# binary cache, and compiles ~100 MB of Go instead - which is what OOM'd a
# 2 GiB box in issue #373. So this pins the revision DefangLabs/defang's own
# flake.lock pins at the tag below.
#
# Verified 2026-08-31: both architectures are prebuilt in the public cache, so
# an install substitutes rather than builds -
#   x86_64-linux   /nix/store/pxa0iq32xsp8pql6k2nn2gsgcll4fzf0-defang-cli-git
#   aarch64-linux  /nix/store/yayqldvzrg66lpajmcpnf9h5jvgvlxyd-defang-cli-git
# both HTTP 200 at https://defanglabs.cachix.org/<hash>.narinfo. The closure is
# 104.8 MiB over 4 paths, which is why it is fetched ON DEMAND and not shipped
# in the runtime profile.
#
# Bump all three pins together when moving to a new defang tag:
#   curl -sL https://raw.githubusercontent.com/DefangLabs/defang/vX.Y.Z/flake.lock \
#     | jq '.nodes.nixpkgs.locked | {url, sha256: .narHash}'
let
  defangSrc = builtins.fetchTarball {
    url = "https://github.com/DefangLabs/defang/archive/refs/tags/v3.15.0.tar.gz";
    sha256 = "sha256-9wfaHVqxJprJUoP5meQEgmRfV6kJugonmO714gaR1tc=";
  };
  defangPkgs = import (builtins.fetchTarball {
    url = "https://releases.nixos.org/nixpkgs/nixpkgs-26.11pre1057999.afe3d8ac4395/nixexprs.tar.xz";
    sha256 = "sha256-93GX5Q/npwBE92xpHlktxgGztuIP/2kwOMukz+qyJBk=";
  }) { system = builtins.currentSystem; };
in
defangPkgs.callPackage "${defangSrc}/pkgs/defang/cli.nix" { }
