# The pinned local-webhook (webhook.py) the box RUNS — one home, read by both
# backends.
#
# The NixOS module used to hold these three constants as option defaults, which
# made them unreachable from a package build: `#runtime` is not a NixOS eval, so
# it was built with no pin at all and the native profile silently shipped none
# of the webhook payloads (issue #425). bin/assemble-module.py splices this file
# into the generated module verbatim, so the module stays a single
# self-contained file and the option defaults still read as literals there.
#
# Bumping the pin: change `rev`, then
#   nix-prefetch-url https://raw.githubusercontent.com/<repo>/<rev>/local-webhook/webhook.py
#   nix hash convert --hash-algo sha256 --to sri <base32>
# and put the SRI hash in `sha256`. Run `nix run .#assemble` afterwards — the
# generated module carries a copy of these values, and CI's
# module-generated-up-to-date check fails until it matches.
{
  repo = "defangdevs/local-channels";
  rev = "c0021f6c491d3710c498a3b8739ecd9d7932fe8d";
  sha256 = "sha256-2+lTrqU3+ZJSA5+CN9WQ1XbWoRe5tmaTmlVwEDPm6js=";
}
