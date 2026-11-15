"""agent-box-secret-candidates-report - advisory-only, run on every apply
(issue #922).

Prints, for each configured user given on argv, which of their env-store
and profile names look like credentials and are not yet in the broker.
Never acts on what it finds (see lib/secret-candidates.py's header) and
never fails: a box owner decides when to run `agentbox apply` / the NixOS
switch, so there is no shared release clock to count against before
assuming everyone has moved a value over - this runs on EVERY apply
instead, for as long as there is something left to report, and naturally
stops printing once an owner has acted (or never prints anything on a box
that never had a plaintext-shaped name to begin with).

Root-only in practice (apply/activation runs as root), which is what lets
it read every configured user's $HOME and the root-owned secret store
directly - no socket, no decryption, just directory listings and the env-
store parser already used everywhere else. A user who does not exist yet
(a config pointed at a name useradd has not created) is skipped quietly:
this never blocks apply over an advisory step.

Exit status is always 0: this prints to stdout, which `agentbox apply` and
a NixOS activation script already surface to whoever ran the command, but
it is advisory and must never make either kind of apply fail.
"""
import pwd
import sys


def main(argv):
    any_found = False
    for user in argv:
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            continue
        store_root = f"/var/lib/agent-box-secrets/{user}"
        default_profile = None
        try:
            default_profile = resolve_profile(None, home=home)
        except SecretError:
            pass
        candidates = scan(home, store_root=store_root, default_profile=default_profile)
        hint = format_hint(candidates)
        if hint:
            if not any_found:
                sys.stdout.write(
                    "agent-box secrets: plaintext credential(s) found, not "
                    "yet protected (issue #922):\n\n")
                any_found = True
            sys.stdout.write(f"  [{user}]\n")
            for line in hint.splitlines():
                sys.stdout.write(f"  {line}\n")
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
