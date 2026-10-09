#!/usr/bin/env bash
# Create one native Ubuntu agent-box in Hetzner Cloud.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: deploy/hetzner/deploy.sh [SERVER_NAME]

Required environment:
  HCLOUD_TOKEN                 Hetzner Cloud API token
  HCLOUD_SSH_KEY               Existing Hetzner SSH key name or ID
  AGENT_BOX_WEB_PASSWORD_HASH  Caddy-compatible argon2id password hash

Optional environment:
  HCLOUD_SERVER_TYPE           cx23 (default)
  HCLOUD_LOCATION              nbg1 (default)
  HCLOUD_IMAGE                 ubuntu-24.04 (default; snapshot ID is allowed)
  HCLOUD_ALLOW_CIDRS_JSON      ["0.0.0.0/0","::/0"] (default)
  HCLOUD_DEBUG_SSH             true (default)
  AGENT_BOX_USER_NAME          workspace (default)
  AGENT_BOX_FLAKE_REF          current checkout commit (default)
  AGENT_BOX_SSLIP_DOMAINS_JSON ["sslip.io"] (default)
  AGENT_BOX_IMAGE_INCLUDES_RUNTIME  false (default; true for a baked snapshot)
  AGENT_BOX_WAIT               true (default; wait for valid HTTPS)
USAGE
}

if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  usage
  exit 0
fi
if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN is required}"
: "${HCLOUD_SSH_KEY:?HCLOUD_SSH_KEY is required}"
: "${AGENT_BOX_WEB_PASSWORD_HASH:?AGENT_BOX_WEB_PASSWORD_HASH is required}"

for command_name in hcloud jq curl; do
  if ! command -v "$command_name" >/dev/null; then
    echo "missing required command: $command_name" >&2
    exit 2
  fi
done

server_name="${1:-${HCLOUD_SERVER_NAME:-agent-box}}"
server_type="${HCLOUD_SERVER_TYPE:-cx23}"
location="${HCLOUD_LOCATION:-nbg1}"
image="${HCLOUD_IMAGE:-ubuntu-24.04}"
allow_cidrs="${HCLOUD_ALLOW_CIDRS_JSON:-[\"0.0.0.0/0\",\"::/0\"]}"
debug_ssh="${HCLOUD_DEBUG_SSH:-true}"
wait_for_box="${AGENT_BOX_WAIT:-true}"
firewall_name="$server_name-agent-box"

if [[ ! "$server_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,50}[a-zA-Z0-9]$ ]]; then
  echo 'server name must be 2-52 letters, digits, dots, or hyphens' >&2
  exit 2
fi
if [ "$debug_ssh" != true ] && [ "$debug_ssh" != false ]; then
  echo 'HCLOUD_DEBUG_SSH must be true or false' >&2
  exit 2
fi
if [ "$wait_for_box" != true ] && [ "$wait_for_box" != false ]; then
  echo 'AGENT_BOX_WAIT must be true or false' >&2
  exit 2
fi
if ! jq -e 'type == "array" and length > 0 and all(.[]; type == "string")' \
    >/dev/null <<<"$allow_cidrs"; then
  echo 'HCLOUD_ALLOW_CIDRS_JSON must be a non-empty JSON array of CIDRs' >&2
  exit 2
fi
if hcloud server describe "$server_name" >/dev/null 2>&1; then
  echo "server already exists: $server_name" >&2
  exit 1
fi
if hcloud firewall describe "$firewall_name" >/dev/null 2>&1; then
  echo "firewall already exists: $firewall_name" >&2
  exit 1
fi

user_data="$(mktemp)"
firewall_id=''
server_created=false
cleanup() {
  status=$?
  trap - EXIT
  rm -f "$user_data"
  if [ "$status" -ne 0 ] && [ "$server_created" = false ] && [ -n "$firewall_id" ]; then
    hcloud firewall delete "$firewall_id" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

"$script_dir/render-user-data.sh" > "$user_data"
user_data_size="$(wc -c < "$user_data")"
if [ "$user_data_size" -gt 32768 ]; then
  echo "rendered user data is $user_data_size bytes; Hetzner allows 32768" >&2
  exit 1
fi

rules="$(jq -cn --argjson cidrs "$allow_cidrs" --argjson ssh "$debug_ssh" '
  [{direction:"in", protocol:"tcp", port:"443", source_ips:$cidrs,
    description:"agent-box HTTPS"}]
  + if $ssh then
      [{direction:"in", protocol:"tcp", port:"22", source_ips:$cidrs,
        description:"key-only debug SSH"}]
    else [] end
')"
firewall_json="$(printf '%s' "$rules" | hcloud firewall create \
  --name "$firewall_name" \
  --label agent-box=hetzner \
  --label managed-by=agent-box \
  --rules-file - \
  --output json)"
firewall_id="$(jq -er '.id // .firewall.id' <<<"$firewall_json")"

hcloud server create \
  --name "$server_name" \
  --type "$server_type" \
  --image "$image" \
  --location "$location" \
  --ssh-key "$HCLOUD_SSH_KEY" \
  --firewall "$firewall_id" \
  --label agent-box=hetzner \
  --label managed-by=agent-box \
  --user-data-from-file "$user_data" \
  --output json >/dev/null
server_created=true

server_json="$(hcloud server describe "$server_name" --output json)"
server_id="$(jq -er '.id' <<<"$server_json")"
public_ip="$(jq -er '.public_net.ipv4.ip' <<<"$server_json")"
user_name="${AGENT_BOX_USER_NAME:-workspace}"
domain_suffix="$(jq -er '.[0]' <<<"${AGENT_BOX_SSLIP_DOMAINS_JSON:-[\"sslip.io\"]}")"
domain="${public_ip//./-}.$domain_suffix"
url="https://$domain/$user_name/"

echo "created server $server_name ($server_id) at $public_ip"
echo "firewall: $firewall_name ($firewall_id)"
echo "bootstrap log: ssh root@$public_ip tail -f /var/log/agent-box-bootstrap.log"

if [ "$wait_for_box" = true ]; then
  echo "waiting for agent-box HTTPS: $url"
  ready=false
  for _ in $(seq 1 120); do
    status="$(curl -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout 5 --max-time 10 "$url" || true)"
    if [ "$status" = 401 ] || [ "$status" = 303 ] || [ "$status" = 200 ]; then
      ready=true
      break
    fi
    sleep 10
  done
  if [ "$ready" != true ]; then
    echo "agent-box did not become ready within 20 minutes; server retained for diagnostics" >&2
    exit 1
  fi
fi

echo "agent-box ready: $url"
