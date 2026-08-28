## Your host: NixOS

- This box runs NixOS, so the system is BUILT from a configuration rather
  than installed into. `/etc/nixos/configuration.nix` is the source of
  truth and `nixos-rebuild switch` is what makes a change to it real.
  There is no system package manager to install into: use `nix profile
  add` (below) for tools you need, which lands them in your own profile
  and survives everything.
- Everything outside your home is the read-only Nix store plus generated
  /etc. A file you need to change that is not under $HOME almost always
  means editing the box's configuration and rebuilding - which is what
  the update path below does.
- Every rebuild is a generation and the previous one is still on disk, so
  a bad change is a rollback, not a reinstall.
