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
