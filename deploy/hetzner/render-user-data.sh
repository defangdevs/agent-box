#!/usr/bin/env bash
# Render bootstrap.sh.in into the user data passed to Hetzner Cloud.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

user_name="${AGENT_BOX_USER_NAME:-workspace}"
flake_ref="${AGENT_BOX_FLAKE_REF:-}"
nix_installer="${AGENT_BOX_NIX_INSTALLER_URL:-https://install.determinate.systems/nix}"
password_hash="${AGENT_BOX_WEB_PASSWORD_HASH:-}"
suffixes_json="${AGENT_BOX_SSLIP_DOMAINS_JSON:-[\"sslip.io\"]}"
agents_md_file="${AGENT_BOX_AGENTS_MD_FILE:-$script_dir/AGENTS.md}"
bootstrap_template="${AGENT_BOX_BOOTSTRAP_TEMPLATE:-$script_dir/bootstrap.sh.in}"
image_runtime="${AGENT_BOX_IMAGE_INCLUDES_RUNTIME:-false}"

if [ -z "$flake_ref" ]; then
  repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
  revision="$(git -C "$repo_root" rev-parse HEAD)"
  flake_ref="github:defangdevs/agent-box/$revision"
fi

if [[ ! "$user_name" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  echo 'AGENT_BOX_USER_NAME must be a valid Linux user name' >&2
  exit 2
fi
if [[ -z "$flake_ref" || "$flake_ref" =~ [[:space:]] ]]; then
  echo 'AGENT_BOX_FLAKE_REF must be a non-empty flake reference without whitespace' >&2
  exit 2
fi
hash_pattern='^\$argon2id\$v=[0-9]+\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$'
if [[ ! "$password_hash" =~ $hash_pattern ]]; then
  echo 'AGENT_BOX_WEB_PASSWORD_HASH must be a Caddy-compatible argon2id hash' >&2
  exit 2
fi
if [ "$image_runtime" != true ] && [ "$image_runtime" != false ]; then
  echo 'AGENT_BOX_IMAGE_INCLUDES_RUNTIME must be true or false' >&2
  exit 2
fi
if [ ! -r "$agents_md_file" ]; then
  echo "AGENT_BOX_AGENTS_MD_FILE is not readable: $agents_md_file" >&2
  exit 2
fi
if [ ! -r "$bootstrap_template" ]; then
  echo "AGENT_BOX_BOOTSTRAP_TEMPLATE is not readable: $bootstrap_template" >&2
  exit 2
fi
if ! jq -e '
    type == "array" and length > 0 and
    all(.[]; type == "string" and
      test("^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")) and
    (unique | length) == length
  ' >/dev/null <<<"$suffixes_json"; then
  echo 'AGENT_BOX_SSLIP_DOMAINS_JSON must be a unique JSON array of DNS suffixes' >&2
  exit 2
fi

# Values interpolated here have already been constrained to YAML-safe DNS and
# Linux-name alphabets. Emit the same ordinary mapping shape as Lightsail and
# Azure: first boot rewrites the leading `domain:` line in place, and future
# `agentbox apply` runs keep reading the same valid document.
suffixes_yaml="$(jq -r 'join(", ")' <<<"$suffixes_json")"
config_yaml="$(printf '%s\n' \
  'domain: auto' \
  "domainSuffixes: [$suffixes_yaml]" \
  'agents: [claude, codex]' \
  'web:' \
  '  enable: true' \
  'users:' \
  "  $user_name:" \
  '    root: true')"

encode() {
  printf '%s' "$1" | base64 | tr -d '\n'
}

flake_ref_b64="$(encode "$flake_ref")"
nix_installer_b64="$(encode "$nix_installer")"
config_b64="$(encode "$config_yaml")"
agents_md_b64="$(base64 < "$agents_md_file" | tr -d '\n')"
password_hash_b64="$(encode "$password_hash")"

# Every replacement alphabet is base64 (or the literal true/false), so none
# can terminate the sed expression or introduce shell syntax.
sed \
  -e "s|@@FLAKE_REF_B64@@|$flake_ref_b64|g" \
  -e "s|@@NIX_INSTALLER_B64@@|$nix_installer_b64|g" \
  -e "s|@@CONFIG_B64@@|$config_b64|g" \
  -e "s|@@AGENTS_MD_B64@@|$agents_md_b64|g" \
  -e "s|@@WEB_PASSWORD_HASH_B64@@|$password_hash_b64|g" \
  -e "s|@@IMAGE_INCLUDES_RUNTIME@@|$image_runtime|g" \
  "$bootstrap_template"
