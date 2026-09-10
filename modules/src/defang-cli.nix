# The Defang CLI, pinned. ONE source of truth for both backends: the NixOS
# module names this file for agent-box-defang-cli.service's background
# install, and nix/runtime.nix ships it into the runtime profile's share
# dir so a native box's settings page can install it from the Defang card
# (issue #461).
#
# Not in nixpkgs, so there is no `attr` a card could fetch. DefangLabs/defang
# ships its own flake, and `packages.<system>.defang-cli` in it is
# `buildGo125Module` against the repo's own src/ - so this asks for that
# output at a release TAG and nothing else.
#
# It used to re-implement the flake by hand: fetch the source tarball at the
# tag, fetch the nixpkgs revision the repo's flake.lock names, and
# callPackage pkgs/defang/cli.nix with it. That worked and needed THREE pins
# kept in step by hand ("Bump all three pins together", the note said). The
# flake carries its own lock, so the tag below is now the only pin, and the
# nixpkgs question answers itself.
#
# The nixpkgs the build sees is still NOT this box's, which was the whole
# point of the hand-written version and is preserved here: a derivation's
# output hash is a function of every input, so building cli.nix against
# whatever nixpkgs the host happens to have produces a DIFFERENT derivation
# than DefangLabs' release CI built, silently misses the binary cache, and
# compiles ~100 MB of Go instead - which is what OOM'd a 2 GiB box in issue
# #373. Going through the flake gets the repo's own locked nixpkgs by
# construction rather than by a hash somebody has to remember to move.
#
# Verified 2026-09-09, aarch64-linux: this expression and the hand-written
# one it replaces evaluate to the SAME store path,
# /nix/store/yayqldvzrg66lpajmcpnf9h5jvgvlxyd-defang-cli-git - the one the
# previous version of this file recorded as cache-warm at
# https://defanglabs.cachix.org. So the substituter still hits and no box
# starts compiling Go because of this change.
#
# getFlake, not fetchTarball + callPackage: both backends already enable the
# flakes feature box-wide (nix.settings.experimental-features on NixOS,
# --extra-experimental-features natively) and every caller of this file
# evaluates it with --impure, which is what an unlocked ref like a tag
# needs. builtins.currentSystem needs the same impurity and the previous
# version used it too.
#
# To move to a new defang release, change the tag. That is the whole edit.
(builtins.getFlake "github:DefangLabs/defang/v3.15.0")
  .packages.${builtins.currentSystem}.defang-cli
