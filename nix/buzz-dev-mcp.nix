{ lib
, rustPlatform
, fetchFromGitHub
, pkg-config
, openssl
}:

rustPlatform.buildRustPackage rec {
  pname = "buzz-dev-mcp";
  version = "0.5.28";

  src = fetchFromGitHub {
    owner = "block";
    repo = "buzz";
    rev = "f87c10895ea35a9a2cea629fef1d7532a3fecbc5";
    hash = "sha256-NEsLQwcoQ2zTgznFgGFGNgYPnoclicNVycSEjl7I0Wo=";
  };

  cargoHash = "sha256-UQ8qNqoeDwkwhmVZebi++gb2GP61dr1Zv2QU9mJ6KJ8=";

  cargoBuildFlags = [ "-p" pname ];
  cargoTestFlags = [ "-p" pname ];
  cargoInstallFlags = [ "-p" pname ];

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ openssl ];

  # Cargo's Nix sandbox HOME is /homeless-shelter. One upstream path test
  # deliberately writes below HOME, so give the test suite a private one.
  preCheck = ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
  '';

  meta = {
    description = "Buzz MCP server with an embedded Buzz CLI";
    homepage = "https://github.com/block/buzz";
    license = lib.licenses.asl20;
    mainProgram = "buzz-dev-mcp";
    platforms = lib.platforms.linux;
  };
}
