#!/usr/bin/env bash
# Keep the established 1+1+2 VM budget, with all build preparation done first.
set -euo pipefail

drivers=${1:?usage: ci-vm-tests.sh DRIVER_DIRECTORY}
sessions=(sessions)
webhook=(webhook)
rest=(connect containers memory-protection sessions-web settings-page ttyd-isolation web-surface)

# Fail closed if a new VM check has no lane, a lane duplicates a check, or
# a renamed check is still listed. The flake generates this directory from
# every check with a driver, independently of this scheduling policy.
diff -u <(find -L "$drivers" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort) \
  <(printf '%s\n' "${sessions[@]}" "${webhook[@]}" "${rest[@]}" | sort)

run_lane() {
  local jobs=$1
  shift
  local checks=()
  for check in "$@"; do
    checks+=(".#checks.x86_64-linux.$check")
  done
  nix build -L --keep-going --no-link --max-jobs "$jobs" --cores 1 "${checks[@]}"
}

run_lane 1 "${sessions[@]}" &
sessions_pid=$!
run_lane 1 "${webhook[@]}" &
webhook_pid=$!
run_lane 2 "${rest[@]}" &
rest_pid=$!

# Wait for every lane even after a failure; no result hides another's log.
status=0
for pid in "$sessions_pid" "$webhook_pid" "$rest_pid"; do
  wait "$pid" || status=1
done
exit "$status"
