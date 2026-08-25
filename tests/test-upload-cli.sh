#!/usr/bin/env bash
# Unit tests for modules/src/upload-cli.sh (issue #368).
#
# These are the assertions that make the wrapper worth having. The guide used
# to carry this as a curl recipe, and prose cannot be tested — the recipe
# shipped leaking the token into argv and returning success on a 404 (PR #367
# review). Both are now regressions this file catches.
#
# gh and curl are shimmed, so nothing here touches the network or needs a
# token: the real endpoint has no sandbox, and a check that depends on GitHub
# being reachable is a check that goes red for reasons unrelated to the code.
set -u

SCRIPT=${1:?usage: test-upload-cli.sh PATH/TO/upload-cli.sh}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
# run() cds into the scratch dir, so a relative argument would vanish.
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

# The Nix sandbox has no /usr/bin/env, so the shims cannot use it in their
# shebang — point them at the bash that is already running this file.
BASH_BIN=$(command -v bash)
[ -n "$BASH_BIN" ] || { echo "no bash on PATH" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
shim=$work/bin
mkdir -p "$shim"

TOKEN="ghp_TESTTOKENvalue000000000000000000"

cat > "$shim/gh" <<EOF
#!$BASH_BIN
case "\$*" in
  "auth token") echo "$TOKEN" ;;
  "api repos/"*) echo 4242 ;;
  *"nameWithOwner"*) echo defangdevs/agent-box ;;
  *) echo "unexpected gh call: \$*" >&2; exit 1 ;;
esac
EOF

# Records its own argv and stdin so the tests can assert on how the script
# called it, then answers with whatever CURL_MODE asks for.
cat > "$shim/curl" <<EOF
#!$BASH_BIN
printf '%s\n' "\$@" > "$work/curl.argv"
cat > "$work/curl.stdin"
case "\${CURL_MODE:-ok}" in
  fail) echo 'curl: (22) The requested URL returned error: 500'; echo '{"message":"boom"}'; exit 22 ;;
  *) echo '{"url":"https://github.com/user-attachments/assets/deadbeef"}' ;;
esac
EOF

chmod +x "$shim/gh" "$shim/curl"
export PATH="$shim:$PATH"

printf 'not really a png but non-empty\n' > "$work/shot.png"

fails=0
# No grep/sed is used below, deliberately: the agent PATH on a NixOS box has
# neither (issue #372), and a test that cannot run on the machine it describes
# is not much of a test. bash pattern matching is always present.
contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has_line() {
  local want=$2 line
  while IFS= read -r line; do [ "$line" = "$want" ] && return 0; done < "$1"
  return 1
}
slurp() { cat "$1" 2>/dev/null; }
ok()   { echo "ok: $1"; }
bad()  { echo "FAIL: $1"; fails=$((fails + 1)); }
run()  { ( cd "$work" && bash "$SCRIPT" "$@" ); }

# --- 1. --help works and names the trap that costs the most time ------------
help=$(run --help 2>&1)
if [ $? -eq 0 ] && contains "$help" "404s until"; then
  ok "--help exits 0 and documents the activation delay"
else
  bad "--help must exit 0 and mention that the URL 404s until referenced"
fi

# --- 2. the happy path prints markdown on stdout ---------------------------
out=$(run shot.png --repo defangdevs/agent-box --alt before 2>/dev/null)
if [ "$out" = '![before](https://github.com/user-attachments/assets/deadbeef)' ]; then
  ok "prints the markdown embed on stdout"
else
  bad "expected a markdown embed, got: $out"
fi

# --- 3. the caveat goes to stderr, so stdout stays pipeable ----------------
err=$(run shot.png --repo defangdevs/agent-box 2>&1 >/dev/null)
if contains "$err" "404s until"; then
  ok "activation caveat goes to stderr, not stdout"
else
  bad "expected the activation caveat on stderr, got: $err"
fi

# --- 4. THE token must never reach curl's argv -----------------------------
# curl does not blank -H, and /proc has no hidepid, so a token in argv is
# readable by every other Linux user on the box — the one boundary agent-box
# actually enforces. This is the regression that matters most here.
if contains "$(slurp "$work/curl.argv")" "$TOKEN"; then
  bad "token leaked into curl argv — it must go in over stdin"
else
  ok "token never appears in curl's arguments"
fi
if contains "$(slurp "$work/curl.stdin")" "$TOKEN"; then
  ok "token is delivered on stdin"
else
  bad "token did not reach curl on stdin"
fi

# --- 5. the flags that make failure visible must still be there ------------
for flag in --fail-with-body -H; do
  if has_line "$work/curl.argv" "$flag"; then
    ok "curl is invoked with $flag"
  else
    bad "curl lost the $flag argument"
  fi
done
if has_line "$work/curl.argv" "@-"; then
  ok "header is read from stdin (-H @-)"
else
  bad "curl is no longer reading the auth header from stdin"
fi

# --- 6. an HTTP error must fail loudly, not return a body as a URL ---------
if out=$(CURL_MODE=fail run shot.png --repo defangdevs/agent-box 2>&1); then
  bad "an HTTP error exited 0 — the caller would paste the error as a URL"
else
  if contains "$out" "boom"; then
    ok "HTTP error exits non-zero and keeps the response body"
  else
    bad "HTTP error exited non-zero but swallowed the body: $out"
  fi
fi

# --- 7. --url prints the bare URL ------------------------------------------
out=$(run shot.png --repo defangdevs/agent-box --url 2>/dev/null)
if [ "$out" = "https://github.com/user-attachments/assets/deadbeef" ]; then
  ok "--url prints the bare URL"
else
  bad "--url should print just the URL, got: $out"
fi

# --- 8. input validation ---------------------------------------------------
if run missing.png --repo defangdevs/agent-box >/dev/null 2>&1; then
  bad "a missing file should not exit 0"
else
  ok "a missing file is rejected"
fi
: > "$work/empty.png"
if run empty.png --repo defangdevs/agent-box >/dev/null 2>&1; then
  bad "an empty file should not be uploaded"
else
  ok "an empty file is rejected"
fi
if run shot.png --bogus >/dev/null 2>&1; then
  bad "an unknown option should not exit 0"
else
  ok "an unknown option is rejected"
fi

# --- 9. a name with a space must not split the query -----------------------
cp "$work/shot.png" "$work/my shot.png"
run "my shot.png" --repo defangdevs/agent-box >/dev/null 2>&1
if contains "$(slurp "$work/curl.argv")" "name=my%20shot.png"; then
  ok "a filename with a space is urlencoded"
else
  bad "filename was not urlencoded; curl argv was: $(slurp "$work/curl.argv")"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "all upload-cli tests passed"
else
  echo "$fails upload-cli test(s) failed"
  exit 1
fi
