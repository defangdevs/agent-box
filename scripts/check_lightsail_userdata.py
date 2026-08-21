#!/usr/bin/env python3
"""Guard the Lightsail launch-script shell dialect.

Lightsail PREPENDS its own `#!/bin/sh` preamble to an instance's launch
script, so the shebang in `aws/lightsail-template.yaml` is only a comment
in the file cloud-init runs and the script starts life under dash. The
1-click template shipped for ten days with `set -euxo pipefail` on its
first executable line and died there on every launch ("Illegal option -o
pipefail"), 19 seconds into first boot, with nothing to show in CI: the
bug is a shell dialect, and cfn-lint only reads YAML.

This check keeps that from coming back. Everything up to and including
the bash re-exec guard must parse under `dash -n`, and no bashism may
appear before it.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Every Lightsail launch script, infect-based or native: they all reach the
# instance through the same wrapper, so they all need the same guard.
TEMPLATES = [
    REPO / "aws" / "lightsail-native-template.yaml",
]
GUARD = re.compile(r'exec\s+/bin/bash\s+"\$0"')
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
            if line.lstrip().startswith(("!Sub", "|", "-")):
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


def main() -> int:
    return max(check(t) for t in TEMPLATES)


if __name__ == "__main__":
    sys.exit(main())
