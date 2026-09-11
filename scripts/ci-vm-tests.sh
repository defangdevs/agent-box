#!/usr/bin/env bash
# The lane's flake package supplies the exact check inventory it prepared.
set -euo pipefail

drivers=${1:?usage: ci-vm-tests.sh DRIVER_DIRECTORY JOBS}
jobs=${2:?usage: ci-vm-tests.sh DRIVER_DIRECTORY JOBS}
[[ "$jobs" == 1 || "$jobs" == 2 ]]
[[ -d "$drivers" ]]
mapfile -t names < <(find -L "$drivers" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
[[ ${#names[@]} -gt 0 ]]
checks=()
for name in "${names[@]}"; do
  checks+=(".#checks.x86_64-linux.$name")
done
exec nix build -L --keep-going --no-link --max-jobs "$jobs" --cores 1 "${checks[@]}"
