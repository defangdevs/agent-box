#!/usr/bin/env bash
# Launch one agent-box on Google Compute Engine (PROTOTYPE).
#
# Designed to run in Cloud Shell (gcloud + python3 already there), but works
# anywhere with an authenticated gcloud. Creates, all named after --name:
#   <name>-net / <name>-subnet  dedicated VPC, 10.42.0.0/24 (same CIDR as AWS/Azure)
#   <name>-https                firewall: tcp/443 from --allow-cidr to the box only
#   <name>-iap-ssh              firewall: tcp/22 from Google's IAP range (only with --debug-ssh)
#   <name>-ip                   static regional external IPv4 (the box's URL)
#   <name>                      the VM: stock Ubuntu 24.04, NO service account
# Delete everything with teardown.sh --name <name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

name=agent-box
zone=us-central1-a
machine=e2-medium
disk=32
user=workspace
flake_ref=""
allow_cidr=0.0.0.0/0
sslip='["sslip.io"]'
debug_ssh=false
labels=agent-box=true
project="${CLOUDSDK_CORE_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
hash_file=""
password_hash=""
timeout_min=25

usage() { sed -n '2,13p' "$0"; echo "flags: --project --zone --name --machine-type --disk-gb --user --flake-ref --allow-cidr --sslip-domains JSON --debug-ssh --labels k=v,.. --password-hash HASH | --password-hash-file F --timeout-min N"; exit 2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project=$2; shift 2;;
    --zone) zone=$2; shift 2;;
    --name) name=$2; shift 2;;
    --machine-type) machine=$2; shift 2;;
    --disk-gb) disk=$2; shift 2;;
    --user) user=$2; shift 2;;
    --flake-ref) flake_ref=$2; shift 2;;
    --allow-cidr) allow_cidr=$2; shift 2;;
    --sslip-domains) sslip=$2; shift 2;;
    --debug-ssh) debug_ssh=true; shift;;
    --labels) labels="$labels,$2"; shift 2;;
    --password-hash-file) hash_file=$2; shift 2;;
    --password-hash) password_hash=$2; shift 2;;
    --timeout-min) timeout_min=$2; shift 2;;
    *) usage;;
  esac
done
[[ -n "$project" ]] || { echo "No project: pass --project or run 'gcloud config set project <id>'." >&2; exit 2; }
[[ "$name" =~ ^[a-z]([-a-z0-9]{0,40}[a-z0-9])?$ ]] || { echo "--name must be a lowercase GCE name (max 42 chars)" >&2; exit 2; }
region="${zone%-*}"
case "$machine" in
  t2a-*|c4a-*) image_family=ubuntu-2404-lts-arm64;;
  *) image_family=ubuntu-2404-lts-amd64;;
esac
case "$machine" in
  c4a-*|n4-*|c4-*) disk_type=hyperdisk-balanced;;
  *) disk_type=pd-balanced;;
esac
G=(gcloud --project "$project" --quiet)

# Pin the runtime to the commit this script was cloned at, so a box is
# reproducible (the AWS buttons are pinned at publish time; Azure's is not).
if [[ -z "$flake_ref" ]]; then
  if rev="$(git -C "$HERE" rev-parse HEAD 2>/dev/null)" && \
     git -C "$HERE" remote get-url origin 2>/dev/null | grep -q 'defangdevs/agent-box'; then
    flake_ref="github:defangdevs/agent-box/$rev"
    # A rev outside the default branch (a feature branch, later squash-merged)
    # leaves the box unable to update itself (issue #896): track the default
    # branch instead.
    git -C "$HERE" fetch -q origin 2>/dev/null || true
    if ! git -C "$HERE" merge-base --is-ancestor "$rev" refs/remotes/origin/HEAD 2>/dev/null; then
      flake_ref=github:defangdevs/agent-box
      echo "warning: $rev is not on origin's default branch; tracking the default branch instead ($flake_ref)" >&2
    fi
  else
    flake_ref=github:defangdevs/agent-box
    echo "note: not in an agent-box checkout; tracking the default branch ($flake_ref)" >&2
  fi
fi

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
password=""
if [[ -n "$password_hash" ]]; then
  printf '%s\n' "$password_hash" > "$work/hash"
  hash_file="$work/hash"
fi
if [[ -z "$hash_file" ]]; then
  # Generate the password here and send GCP only its argon2id hash: instance
  # metadata is readable by every project Viewer and by every process on the
  # box (same reasoning as the AWS launch page, issue #25).
  venv="${XDG_CACHE_HOME:-$HOME/.cache}/agent-box-gcp-venv"
  [[ -x "$venv/bin/python" ]] || { python3 -m venv "$venv" && "$venv/bin/pip" install -q argon2-cffi; }
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  # Caddy's defaults (golang.org/x/crypto/argon2 IDKey: t=1, m=46 MiB, p=1, 32-byte key).
  printf %s "$password" | "$venv/bin/python" -c '
import sys; from argon2 import PasswordHasher
print(PasswordHasher(time_cost=1, memory_cost=47104, parallelism=1, hash_len=32, salt_len=16).hash(sys.stdin.read()))' > "$work/hash"
  hash_file="$work/hash"
fi

# Only ask Service Usage when Compute does not answer: enabling needs
# serviceusage.services.enable, which a narrowly-scoped deployer may not hold
# even though the API is already on.
if ! "${G[@]}" compute zones describe "$zone" --format 'value(name)' >/dev/null 2>&1; then
  echo "== enabling the Compute Engine API"
  "${G[@]}" services enable compute.googleapis.com
fi

echo "== network"
"${G[@]}" compute networks describe "$name-net" >/dev/null 2>&1 || \
  "${G[@]}" compute networks create "$name-net" --subnet-mode=custom
"${G[@]}" compute networks subnets describe "$name-subnet" --region "$region" >/dev/null 2>&1 || \
  "${G[@]}" compute networks subnets create "$name-subnet" --network "$name-net" \
    --region "$region" --range 10.42.0.0/24
"${G[@]}" compute firewall-rules describe "$name-https" >/dev/null 2>&1 || \
  "${G[@]}" compute firewall-rules create "$name-https" --network "$name-net" \
    --direction INGRESS --allow tcp:443 --source-ranges "$allow_cidr" --target-tags "$name"
if [[ "$debug_ssh" = true ]]; then
  "${G[@]}" compute firewall-rules describe "$name-iap-ssh" >/dev/null 2>&1 || \
    "${G[@]}" compute firewall-rules create "$name-iap-ssh" --network "$name-net" \
      --direction INGRESS --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags "$name"
fi

echo "== static IPv4"
"${G[@]}" compute addresses describe "$name-ip" --region "$region" >/dev/null 2>&1 || \
  { "${G[@]}" compute addresses create "$name-ip" --region "$region" && \
    # `addresses create --labels` is beta-only (rejected by GA gcloud 588);
    # GA `addresses update --update-labels` is not.
    { "${G[@]}" compute addresses update "$name-ip" --region "$region" --update-labels "$labels" || true; }; }
ip="$("${G[@]}" compute addresses describe "$name-ip" --region "$region" --format 'value(address)')"

python3 "$HERE/render.py" --public-ip "$ip" \
  --user "$user" --flake-ref "$flake_ref" --sslip-domains "$sslip" < "$hash_file" > "$work/startup.sh"

echo "== VM $name ($machine, $image_family) at $ip"
"${G[@]}" compute instances create "$name" --zone "$zone" \
  --machine-type "$machine" \
  --image-family "$image_family" --image-project ubuntu-os-cloud \
  --boot-disk-size "${disk}GB" --boot-disk-type "$disk_type" \
  --subnet "$name-subnet" --address "$ip" --tags "$name" \
  --no-service-account --no-scopes \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --labels "$labels" \
  --metadata enable-guest-attributes=TRUE,enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
  --metadata-from-file startup-script="$work/startup.sh"

echo "== waiting for first boot (up to ${timeout_min} min)"
status=""
deadline=$(( $(date +%s) + timeout_min * 60 ))
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  status="$("${G[@]}" compute instances get-guest-attributes "$name" --zone "$zone" \
    --query-path agent-box/status --format 'value(value)' 2>/dev/null || true)"
  echo "   $(date -u +%T) ${status:-booting}"
  case "$status" in ready|ready-tls-pending|failed*) break;; esac
  sleep 15
done
case "$status" in
  ready|ready-tls-pending)
    echo
    echo "Your agent-box is up: https://$ip/$user/"
    for s in $(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])))' "$sslip"); do
      echo "  alias: https://${ip//./-}.$s/$user/"
    done
    [[ "$status" = ready ]] || echo "  (the TLS certificate was still being issued; give it a minute)"
    if [[ -n "$password" ]]; then
      echo "Sign in as '$user' with this password. It is shown once and GCP never saw it:"
      echo "  $password"
    fi
    echo "Delete it all: $HERE/teardown.sh --project $project --zone $zone --name $name"
    ;;
  *)
    echo "First boot did not finish (status: ${status:-none}). The log is on the box at /var/log/agent-box-bootstrap.log; the serial console has the startup-script output:" >&2
    echo "  gcloud compute instances get-serial-port-output $name --zone $zone --project $project | grep -i startup-script | tail -50" >&2
    exit 1;;
esac
