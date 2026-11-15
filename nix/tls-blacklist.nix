# NSS 3.131.1 removed these emSign roots from certdata.txt. buildcatrust
# rejects a blacklist entry that is absent from its input, so keep the
# explicit removal only for bundles that still contain those roots.
# https://github.com/NixOS/nixpkgs/issues/576485
# Other entries retain upstream's strict validation, including operator typos.
{ lib, version, roots }:
let
  removedIn = {
    "emSign ECC Root CA - G3" = "3.131.1";
    "emSign Root CA - C1" = "3.131.1";
    "emSign ECC Root CA - C3" = "3.131.1";
  };
in
lib.filter (name:
  !(builtins.hasAttr name removedIn) || lib.versionOlder version removedIn.${name}
) roots
