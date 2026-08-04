set -euo pipefail
api() { curl -fsSL -H 'Accept: application/vnd.github+json' "$1"; }

# --- what would change? -----------------------------------------
target="$(api "https://api.github.com/repos/$REPO/commits/HEAD" | jq -r .sha)"
update_module=1
if [ "$target" = "$CURRENT_REV" ]; then
  update_module=0
else
  # Fast-forward only: a target that isn't strictly ahead of the
  # running rev means upstream history was rewritten or an older
  # (possibly vulnerable) rev is being replayed — refuse both.
  status="$(api "https://api.github.com/repos/$REPO/compare/$CURRENT_REV...$target" | jq -r .status)"
  if [ "$status" != "ahead" ]; then
    echo "refusing update: $target is '$status' of running rev $CURRENT_REV (need fast-forward)" >&2
    exit 1
  fi
fi

# Agent CLI pin: latest nixos-unstable channel release.
# channels.nixos.org redirects to the immutable releases.nixos.org
# URL — pin that, so the pin file stays reproducible.
release="$(curl -fsSLo /dev/null -w '%{url_effective}' https://channels.nixos.org/nixos-unstable)"
tarball="${release%/}/nixexprs.tar.xz"
update_agent=1
if [ -e "$AGENT_PIN_FILE" ] && grep -qF "$tarball" "$AGENT_PIN_FILE"; then
  update_agent=0
fi

if [ "$update_module" = 0 ] && [ "$update_agent" = 0 ]; then
  echo "already current: module at $target, agent nixpkgs at $release"
  exit 0
fi

# --- write the pins (with backups, so failure rolls back exactly
# what this run changed) ------------------------------------------
if [ "$update_module" = 1 ]; then
  module="$(mktemp)"
  trap 'rm -f "$module"' EXIT
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$target/modules/agent-box.nix" -o "$module"
  sha="sha256-$(openssl dgst -sha256 -binary "$module" | base64)"
  if [ -e "$PIN_FILE" ]; then
    cp "$PIN_FILE" "$PIN_FILE.prev"
  fi
  printf '{ rev = "%s"; sha256 = "%s"; }\n' "$target" "$sha" > "$PIN_FILE.tmp"
  mv "$PIN_FILE.tmp" "$PIN_FILE"
fi

if [ "$update_agent" = 1 ]; then
  # nix-prefetch-url --unpack both hashes the tarball and pre-warms
  # the store path fetchTarball wants, so the rebuild that follows
  # doesn't download it a second time.
  agent_sha="$(nix-prefetch-url --unpack "$tarball")"
  if [ -e "$AGENT_PIN_FILE" ]; then
    cp "$AGENT_PIN_FILE" "$AGENT_PIN_FILE.prev"
  fi
  printf '{ url = "%s"; sha256 = "%s"; }\n' "$tarball" "$agent_sha" > "$AGENT_PIN_FILE.tmp"
  mv "$AGENT_PIN_FILE.tmp" "$AGENT_PIN_FILE"
fi

wall "agent-box: updating (module: $REPO@$target, agent nixpkgs: $release) — agent sessions will restart if their services changed." || true
if /run/current-system/sw/bin/nixos-rebuild switch; then
  wall "agent-box: update to $target applied." || true
else
  # Roll back exactly the pins this run touched so the next trigger
  # retries cleanly instead of believing the failed state is current.
  if [ "$update_module" = 1 ]; then
    if [ -e "$PIN_FILE.prev" ]; then
      mv "$PIN_FILE.prev" "$PIN_FILE"
    else
      rm -f "$PIN_FILE"
    fi
  fi
  if [ "$update_agent" = 1 ]; then
    if [ -e "$AGENT_PIN_FILE.prev" ]; then
      mv "$AGENT_PIN_FILE.prev" "$AGENT_PIN_FILE"
    else
      rm -f "$AGENT_PIN_FILE"
    fi
  fi
  wall "agent-box: update to $target FAILED — pins rolled back, system unchanged. See: journalctl -u agent-box-update" || true
  exit 1
fi
