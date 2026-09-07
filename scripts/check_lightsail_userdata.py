#!/usr/bin/env python3
"""Guard the Lightsail launch-script shell dialect.

Lightsail PREPENDS its own `#!/bin/sh` preamble to an instance's launch
script, so the shebang in `deploy/aws/lightsail-template.yaml` is only a comment
in the file cloud-init runs and the script starts life under dash. The
1-click template shipped for ten days with `set -euxo pipefail` on its
first executable line and died there on every launch ("Illegal option -o
pipefail"), 19 seconds into first boot, with nothing to show in CI: the
bug is a shell dialect, and cfn-lint only reads YAML.

This check keeps that from coming back. Everything up to and including
the bash re-exec guard must parse under `dash -n`, and no bashism may
appear before it.

The same class of bug bit the NATIVE template on its first live launch:
cloud-init runs the launch script with no HOME, Nix's profile snippet
dereferences `$HOME` unguarded, and under `set -u` the bootstrap aborted
40 s in with "HOME: unbound variable" — again invisible to cfn-lint and
to every render test, because it is a property of the environment
cloud-init provides. So the script must export HOME before it sources
anything from the Nix profile.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
# Discovered, not listed. Every Lightsail launch script reaches the instance
# through the same wrapper, so every one of them needs the same guard — and a
# hand-maintained list is how the guard came to skip the very template the bug
# shipped in: `lightsail-template.yaml` was never added to it, so CI stayed
# green over a 1-click template that could not boot (issue #390).
TEMPLATES = sorted(REPO.glob("deploy/aws/lightsail*template.yaml"))
GUARD = re.compile(r'exec\s+/bin/bash\s+"\$0"')
# Sourcing the Nix profile snippet, which reads $HOME with no default.
NIX_PROFILE_SOURCE = re.compile(r'^\s*\.\s+\S*/profile\.d/\S+\.sh')
HOME_SET = re.compile(r'^\s*(export\s+)?HOME=')
# Constructs dash does not have. Each one killed or would have killed the
# script before the re-exec guard could hand over to bash.
BASHISMS = (
    ("set -o pipefail", "pipefail is not a dash option"),
    ("> >(", "process substitution is bash-only"),
    ("[[", "[[ ]] is bash-only"),
    ("<<<", "here-strings are bash-only"),
)


def user_data_script(text: str) -> str:
    """Pull the Fn::Sub launch script out of the template.

    Done by hand rather than with a YAML loader so the short-form CFN tags
    (!Sub, !Ref, !Select) need no custom constructors.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "UserData:":
            break
    else:
        raise SystemExit("check: no UserData in this template")
    body, indent = [], None
    for line in lines[i + 1 :]:
        if not line.strip():
            body.append("")
            continue
        width = len(line) - len(line.lstrip())
        if indent is None:
            if line.lstrip().startswith(("!Sub", "Fn::Sub:", "|", "-")):
                continue  # the Fn::Sub header / block scalar marker
            indent = width
        elif width < indent:
            break  # dedent: the Fn::Sub variable map, not script any more
        body.append(line[indent:])
    return "\n".join(body)


def check(template: Path) -> int:
    script = user_data_script(template.read_text())
    lines = script.splitlines()

    guard_at = next((n for n, l in enumerate(lines) if GUARD.search(l)), None)
    if guard_at is None:
        print(
            "FAIL: the Lightsail launch script has no bash re-exec guard.\n"
            '       Add: if [ -z "${!BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi\n'
            "       Without it the script runs under dash, because Lightsail\n"
            "       prepends its own #!/bin/sh preamble to the launch script.",
            file=sys.stderr,
        )
        return 1

    prefix = lines[: guard_at + 1]
    for n, line in enumerate(prefix, start=1):
        if line.lstrip().startswith("#"):
            continue
        for token, why in BASHISMS:
            if token in line:
                print(
                    f"FAIL: line {n} uses a bashism BEFORE the re-exec guard "
                    f"(line {guard_at + 1}): {token!r} — {why}.\n"
                    f"       {line.strip()}",
                    file=sys.stderr,
                )
                return 1

    # cloud-init hands the launch script an environment with no HOME, and
    # the Nix profile snippet expands $HOME with no default — fatal under
    # `set -u`, which is how the first native launch died.
    source_at = next(
        (n for n, l in enumerate(lines) if NIX_PROFILE_SOURCE.search(l)), None
    )
    if source_at is not None:
        home_at = next((n for n, l in enumerate(lines) if HOME_SET.search(l)), None)
        if home_at is None or home_at > source_at:
            print(
                f"FAIL: line {source_at + 1} sources a Nix profile snippet "
                "before the script sets HOME.\n"
                '       Add: export HOME="${!HOME:-/root}"\n'
                "       cloud-init runs the launch script with no HOME, and the\n"
                "       snippet reads $HOME with no default — under `set -u` that\n"
                "       aborts the whole bootstrap.",
                file=sys.stderr,
            )
            return 1

    # CFN escapes: ${!VAR} means a literal ${VAR}; ${Param} is substituted at
    # deploy time. Neither should stop dash from parsing the prefix.
    posix = re.sub(r"\$\{!([^}]*)\}", r"${\1}", "\n".join(prefix))
    posix = re.sub(r"\$\{[A-Za-z][A-Za-z0-9]*\}", "PLACEHOLDER", posix)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(posix + "\n")
        path = fh.name
    for shell in ("dash", "sh"):
        try:
            done = subprocess.run([shell, "-n", path], capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if done.returncode != 0:
            print(
                f"FAIL: the launch script's first {len(prefix)} lines do not parse "
                f"under {shell}, so the re-exec guard is unreachable:\n{done.stderr}",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {template.name}: {len(prefix)} lines parse under "
              f"{shell}; guard on line {guard_at + 1}.")
        return 0
    print("FAIL: no dash or sh available to check the script prefix.", file=sys.stderr)
    return 1


PORTAL_BLOCK = re.compile(
    r"(install -d -m 0755 /etc/agent-box\n.*/etc/agent-box/config\.yaml)",
    re.S)


def check_written_config(template) -> int:
    """The config.yaml the launch script WRITES must be valid YAML, and the
    sed-escaping step that fills in portalIssuer/portalUser (#593) must
    actually pass through a value holding the '&' sed's replacement text
    treats specially, rather than splicing the placeholder into config.yaml
    in its place (see the comment beside esc_issuer in the template).

    `check` above proves the script's prefix parses under dash. This proves
    the fragment that writes the one file `agentbox apply` reads its
    declared state from actually behaves, by running it for real (as bash --
    the fragment uses `$(...)`, which the dash guard above never reaches)
    rather than reproducing its substitution by hand.
    """
    script = user_data_script(template.read_text())
    found = PORTAL_BLOCK.search(script)
    if not found:
        print(f"FAIL: {template.name}: no config.yaml write fragment to "
              "check.", file=sys.stderr)
        return 1
    block = re.sub(r"\$\{!([^}]*)\}", r"${\1}", found.group(1))
    block = block.replace("${Agent}", "claude").replace("${UserName}", "agent")

    rc = 0
    for label, (issuer, account) in {
        "handover on": ("https://station.example.com", "usr_2Nk9x"),
        "handover off": ("", ""),
        # '&' is IN PortalIssuer's AllowedPattern (a query string may have
        # one) but is sed replacement-text magic -- unescaped, this is
        # exactly the input that used to splice the placeholder into
        # config.yaml instead of the URL.
        "handover on, ampersand": (
            "https://station.example.com/cb?a=1&b=2", "usr_2Nk9x"),
    }.items():
        text = (block.replace("${PortalIssuerPlain}", issuer)
                     .replace("${PortalUserPlain}", account))
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            text = text.replace("/etc/agent-box", str(workdir))
            done = subprocess.run(["bash", "-c", text],
                                   capture_output=True, text=True)
            if done.returncode != 0:
                print(f"FAIL: {template.name}: config.yaml ({label}) write "
                      f"fragment failed:\n{done.stderr}", file=sys.stderr)
                rc = 1
                continue
            config_path = workdir / "config.yaml"
            try:
                data = yaml.safe_load(config_path.read_text())
            except yaml.YAMLError as exc:
                print(f"FAIL: {template.name}: config.yaml ({label}) is not "
                      f"valid YAML: {exc}", file=sys.stderr)
                rc = 1
                continue
        web = data.get("web") or {}
        user = (data.get("users") or {}).get("agent") or {}
        # web.enable and root hold either way: whatever handover does, the
        # box still has a front door and a root user.
        got = (web.get("enable"), user.get("root"),
               web.get("portalIssuer"), user.get("portalUser"))
        want = (True, True, issuer, account)
        if got != want:
            print(f"FAIL: {template.name}: config.yaml ({label}) landed as "
                  f"{got!r}, wanted {want!r}", file=sys.stderr)
            rc = 1
            continue
        print(f"OK: {template.name}: config.yaml ({label}) parses and "
              f"declares what it should.")
    return rc


def main() -> int:
    if not TEMPLATES:
        print(
            "FAIL: no Lightsail template found under aws/ — the glob that "
            "feeds this check matched nothing, which would pass vacuously.",
            file=sys.stderr,
        )
        return 1
    return max(max(check(t), check_written_config(t))
               for t in TEMPLATES)


if __name__ == "__main__":
    sys.exit(main())
