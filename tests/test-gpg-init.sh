#!/usr/bin/env bash
set -euo pipefail

script=${1:?usage: test-gpg-init.sh /path/to/gpg-init.sh}
test_root=$(mktemp -d)
test_home="$test_root/home"
verify_home="$test_root/verify"
mkdir -m 0700 "$test_home" "$verify_home" "$test_home/downloads"

cleanup() {
  gpgconf --homedir "$test_home/.config/agent-box/gnupg" --kill all \
    >/dev/null 2>&1 || :
  gpgconf --homedir "$verify_home" --kill all >/dev/null 2>&1 || :
  rm -rf -- "$test_root"
}
trap cleanup EXIT

run_init() {
  HOME="$test_home" USER=alice AGENT_BOX_GPG_BIN="$(command -v gpg)" \
    bash "$script" box.example
}

run_init > "$test_root/first.log"
fingerprint=$(sed -n '1p' "$test_home/.config/agent-box/gpg-fingerprint")
case "$fingerprint" in
  (*[!0-9A-F]*|'')
    echo "invalid fingerprint: $fingerprint" >&2
    exit 1
    ;;
esac

test "$(stat -c %a "$test_home/.config/agent-box/gnupg")" = 700
test "$(stat -c %a "$test_home/.config/agent-box/gpg-public-key.asc")" = 644
test "$(stat -c %a "$test_home/downloads/agent-box-public-key.asc")" = 644
cmp "$test_home/.config/agent-box/gpg-public-key.asc" \
  "$test_home/downloads/agent-box-public-key.asc"

gpg --homedir "$test_home/.config/agent-box/gnupg" --batch --with-colons \
  --list-secret-keys "$fingerprint" \
  > "$test_root/secret-list"
awk -F: '$1 == "sec" && $4 == "22" && $12 ~ /c/ { found = 1 }
  END { exit(found ? 0 : 1) }' "$test_root/secret-list"
awk -F: '$1 == "ssb" && $4 == "18" && $12 ~ /e/ && $17 == "cv25519" {
    found = 1
  }
  END { exit(found ? 0 : 1) }' "$test_root/secret-list"

GNUPGHOME="$verify_home" gpg --batch --import \
  "$test_home/.config/agent-box/gpg-public-key.asc" >/dev/null 2>&1
GNUPGHOME="$verify_home" gpg --batch --with-colons --list-keys "$fingerprint" \
  > "$test_root/public-list"
awk -F: '$1 == "sub" && $4 == "18" && $12 ~ /e/ && $17 == "cv25519" {
    found = 1
  }
  END { exit(found ? 0 : 1) }' "$test_root/public-list"

# A sender using only the exported public key can encrypt, and only the
# provisioned private keyring can recover the bytes.
printf 'round-trip fixture\n' > "$test_root/plaintext"
GNUPGHOME="$verify_home" gpg --batch --quiet --yes --trust-model always \
  --recipient "$fingerprint" --output "$test_root/ciphertext.gpg" \
  --encrypt "$test_root/plaintext"
gpg --homedir "$test_home/.config/agent-box/gnupg" --batch --quiet --yes \
  --output "$test_root/decrypted" --decrypt "$test_root/ciphertext.gpg"
cmp "$test_root/plaintext" "$test_root/decrypted"

# Re-running must keep one identity and the same fingerprint.
run_init > "$test_root/second.log"
test "$(sed -n '1p' "$test_home/.config/agent-box/gpg-fingerprint")" = \
  "$fingerprint"
test "$(gpg --homedir "$test_home/.config/agent-box/gnupg" --batch \
  --with-colons --list-secret-keys |
  awk -F: '$1 == "sec" { count++ } END { print count + 0 }')" = 1

# A damaged marker is recoverable from the keyring and must not make a second
# primary key. This is the interrupted-publication path in gpg-init.sh.
printf 'not-a-fingerprint\n' > "$test_home/.config/agent-box/gpg-fingerprint"
run_init > "$test_root/recovered.log"
test "$(sed -n '1p' "$test_home/.config/agent-box/gpg-fingerprint")" = \
  "$fingerprint"
test "$(gpg --homedir "$test_home/.config/agent-box/gnupg" --batch \
  --with-colons --list-secret-keys |
  awk -F: '$1 == "sec" { count++ } END { print count + 0 }')" = 1

printf 'gpg recipient provisioning is cv25519, exportable, and idempotent\n'
