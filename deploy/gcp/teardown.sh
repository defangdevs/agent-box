#!/usr/bin/env bash
# Delete everything launch.sh created for one --name (PROTOTYPE). Idempotent.
set -uo pipefail
name=agent-box; zone=us-central1-a
project="${CLOUDSDK_CORE_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project=$2; shift 2;;
    --zone) zone=$2; shift 2;;
    --name) name=$2; shift 2;;
    *) echo "flags: --project --zone --name" >&2; exit 2;;
  esac
done
region="${zone%-*}"
G=(gcloud --project "$project" --quiet)
"${G[@]}" compute instances delete "$name" --zone "$zone" --delete-disks=all || true
"${G[@]}" compute addresses delete "$name-ip" --region "$region" || true
for rule in "$name-https" "$name-iap-ssh"; do
  "${G[@]}" compute firewall-rules describe "$rule" >/dev/null 2>&1 && \
    { "${G[@]}" compute firewall-rules delete "$rule" || true; }
done
"${G[@]}" compute networks subnets delete "$name-subnet" --region "$region" || true
"${G[@]}" compute networks delete "$name-net" || true
left="$("${G[@]}" compute instances list --filter "name=$name" --format 'value(name)'; \
        "${G[@]}" compute addresses list --filter "name=$name-ip" --format 'value(name)'; \
        "${G[@]}" compute firewall-rules list --filter "name=($name-https $name-iap-ssh)" --format 'value(name)'; \
        "${G[@]}" compute networks list --filter "name=$name-net" --format 'value(name)')"
if [[ -n "$left" ]]; then echo "STILL PRESENT:"; echo "$left"; exit 1; fi
echo "all $name resources deleted"
