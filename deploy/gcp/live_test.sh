#!/usr/bin/env bash
# Live end-to-end test of the GCE prototype. Creates real resources and ALWAYS
# tears them down (EXIT trap). Prints a PASS/FAIL table and timings; never
# prints the password or its hash.
#
#   live_test.sh --project P --zone Z [--machine-type T] [--name N] [--skip-reboot]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
project=""; zone=europe-west2-a; machine=e2-medium
name="abx-gcp-test-$(date +%m%d%H%M)-$RANDOM"; skip_reboot=false
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project=$2; shift 2;;
    --zone) zone=$2; shift 2;;
    --machine-type) machine=$2; shift 2;;
    --name) name=$2; shift 2;;
    --skip-reboot) skip_reboot=true; shift;;
    *) echo "unknown flag $1" >&2; exit 2;;
  esac
done
[ -n "$project" ] || { echo "--project required" >&2; exit 2; }
G=(gcloud --project "$project" --quiet)
user=workspace
results=()
pass() { results+=("PASS $1"); echo "PASS $1"; }
fail() { results+=("FAIL $1${2:+ -- $2}"); echo "FAIL $1${2:+ -- $2}"; }
t0=$(date +%s)
since() { echo "$(( $(date +%s) - t0 ))s"; }

work="$(mktemp -d)"
cleanup() {
  echo "== teardown ($name)"
  if "$HERE/teardown.sh" --project "$project" --zone "$zone" --name "$name"; then
    pass "teardown: nothing left behind"
  else
    fail "teardown: resources left behind" "delete them by hand: $name*"
  fi
  rm -rf "$work"
  echo; echo "===== RESULTS ($name, $machine, $zone) ====="
  printf '%s\n' "${results[@]}"
}
trap cleanup EXIT

# Password + argon2id hash, generated here, kept in files, never echoed.
py=python3
if ! python3 -c 'import argon2' 2>/dev/null; then
  if python3 -m venv "$work/venv" 2>/dev/null && "$work/venv/bin/pip" install -q argon2-cffi 2>/dev/null; then
    py="$work/venv/bin/python"
  elif command -v uv >/dev/null; then
    uv venv -q "$work/venv" && uv pip install -q --python "$work/venv/bin/python" argon2-cffi && py="$work/venv/bin/python"
  else
    echo "need argon2-cffi (pip or uv)" >&2; exit 1
  fi
fi
python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")' > "$work/pw"
"$py" -c '
import sys; from argon2 import PasswordHasher
print(PasswordHasher(time_cost=1, memory_cost=47104, parallelism=1, hash_len=32, salt_len=16).hash(open(sys.argv[1]).read()))' "$work/pw" > "$work/hash"

echo "== launch $name"
if "$HERE/launch.sh" --project "$project" --zone "$zone" --name "$name" \
     --machine-type "$machine" --password-hash-file "$work/hash" \
     --labels purpose=agent-box-gcp-prototype,owner=sam-agent --timeout-min 30; then
  pass "launch: guest attribute reached ready ($(since))"
else
  fail "launch: did not reach ready ($(since))"
  "${G[@]}" compute instances get-serial-port-output "$name" --zone "$zone" 2>/dev/null \
    | grep -E 'startup-script|agent-box' | tail -80
  exit 1
fi
region="${zone%-*}"
ip="$("${G[@]}" compute addresses describe "$name-ip" --region "$region" --format 'value(address)')"
url="https://$ip/$user/"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url")"
[ "$code" = 401 ] && pass "web: verified TLS for the bare IP, 401 without credentials" || fail "web: unauthenticated GET" "got $code"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -u "$user:$(cat "$work/pw")" "$url")"
case "$code" in 2*|3*) pass "web: right password accepted ($code)";; *) fail "web: right password" "got $code";; esac
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -u "$user:wrong-password-xyz" "$url")"
[ "$code" = 401 ] && pass "web: wrong password refused" || fail "web: wrong password" "got $code"
alias_url="https://${ip//./-}.sslip.io/$user/"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$alias_url")"
case "$code" in 000) fail "web: sslip.io alias TLS" "000";; *) pass "web: sslip.io alias answers over verified TLS ($code)";; esac

if [ -f "$HERE/agent-box/scripts/ws_smoke.py" ]; then
  # Informational only: a fresh web-enabled box seeds no session (issues
  # #416/#468), so "no sessions" is the expected first-boot answer.
  "$py" -m pip install -q websocket-client >/dev/null 2>&1 || true
  "$py" "$HERE/agent-box/scripts/ws_smoke.py" "$url" "$(cat "$work/pw")" >"$work/ws.log" 2>&1
  echo "INFO ws_smoke.py exit $?: $(tail -3 "$work/ws.log" | tr '\n' ' ')"
fi

sa="$("${G[@]}" compute instances describe "$name" --zone "$zone" --format 'value(serviceAccounts)')"
[ -z "$sa" ] && pass "iam: no service account attached" || fail "iam: service account attached" "$sa"
md="$("${G[@]}" compute instances describe "$name" --zone "$zone" --format json)"
if grep -qF "$(cat "$work/pw")" <<<"$md"; then fail "secrets: plaintext password visible in instance metadata"; else pass "secrets: plaintext password not in instance metadata"; fi

serial="$("${G[@]}" compute instances get-serial-port-output "$name" --zone "$zone" 2>/dev/null)"
grep -q 'zram module present' <<<"$serial" && echo "INFO zram: in the base kernel modules on this image"
grep -q 'linux-modules-extra unavailable' <<<"$serial" && echo "INFO zram: linux-modules-extra unavailable (degraded, not broken)"

if [ "$skip_reboot" = false ]; then
  echo "== reboot (reset)"
  "${G[@]}" compute instances reset "$name" --zone "$zone" >/dev/null
  ok=false
  for _ in $(seq 1 40); do
    sleep 10
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")"
    [ "$code" = 401 ] && { ok=true; break; }
  done
  $ok && pass "reboot: URL back with the same certificate path ($(since))" || fail "reboot: URL not back" "last $code"
  serial="$("${G[@]}" compute instances get-serial-port-output "$name" --zone "$zone" 2>/dev/null)"
  grep -q 'already bootstrapped' <<<"$serial" && pass "reboot: startup-script guard short-circuited" || fail "reboot: guard line not seen in serial output"

  echo "== stop/start"
  "${G[@]}" compute instances stop "$name" --zone "$zone" >/dev/null && "${G[@]}" compute instances start "$name" --zone "$zone" >/dev/null
  ok=false
  for _ in $(seq 1 40); do
    sleep 10
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")"
    [ "$code" = 401 ] && { ok=true; break; }
  done
  $ok && pass "stop/start: same static IP, URL back ($(since))" || fail "stop/start: URL not back" "last $code"
fi
