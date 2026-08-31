#!/usr/bin/env python3
"""Guard the Azure Bicep template and the bootstrap script inside it.

Three separate failures this catches, none of which a schema linter can:

1. **JSON drift.** `azure/agent-box.json` is a build artifact of
   `azure/agent-box.bicep` and is what the README's "Deploy to Azure" button
   actually hands the portal. An edit to the Bicep that never reached the JSON
   ships nothing. So the check recompiles and compares.

   The comparison ignores every `_generator` block. Those carry the Bicep
   CLI's own version string and a version-derived `templateHash`, so two
   builds of the SAME source differ cosmetically whenever the compiling CLI
   differs — and it does differ: nixpkgs' `bicep` stamps a truncated version
   ("0.39") where the official binary stamps the full one. `.bicep-version`
   still pins what CI installs, so a real language-level change between
   versions is caught; this only stops the pin from being a tripwire for
   anyone compiling locally.

2. **A bootstrap that cannot run.** The script is a verbatim Bicep multi-line
   string with `@@PLACEHOLDER@@` markers swapped in by `replace()` — Bicep
   does not interpolate inside `'''...'''`, which is the whole point (a shell
   script is full of `${...}`), but it also means nothing type-checks the
   result. So the check performs the same substitutions and runs `bash -n`
   over what the VM would really execute, asserts no placeholder survived, and
   asserts the whole thing is ASCII. Non-ASCII matters: `az vm create`
   encodes `--custom-data` as latin-1 and aborts on an em-dash, and the same
   text is base64'd through several layers here.

   It also re-applies two guards the AWS twin learned the hard way (see
   `check_lightsail_userdata.py`): a bash re-exec before any bashism, and
   `HOME` exported before anything sources the Nix profile snippet, which
   dereferences `$HOME` unguarded and aborts under `set -u`.

3. **The password leaking into readable metadata.** The bootstrap carries the
   web terminal password. It is safe only because it travels in the
   extension's `protectedSettings`, which ARM encrypts and never returns from
   the API. Moving it to `settings` — the obvious "why is this not visible in
   the portal?" fix — would publish it to anyone with reader access. So the
   check fails if the script appears anywhere but `protectedSettings`, or if
   `webPassword` stops being a `securestring`.
"""
import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BICEP = REPO / "azure" / "agent-box.bicep"
COMPILED = REPO / "azure" / "agent-box.json"

GUARD = re.compile(r'exec\s+/bin/bash\s+"\$0"')
NIX_PROFILE_SOURCE = re.compile(r"^\s*\.\s+\S*/profile\.d/\S+\.sh")
HOME_SET = re.compile(r"^\s*(export\s+)?HOME=")
PLACEHOLDER = re.compile(r"@@[A-Z_]+@@")
BASHISMS = (
    ("set -o pipefail", "pipefail is not a dash option"),
    ("> >(", "process substitution is bash-only"),
    ("[[", "[[ ]] is bash-only"),
    ("<<<", "here-strings are bash-only"),
)

# A password that would break out of a single-quoted shell literal, and run the
# tail of itself as root, if the template ever substituted it raw. Bicep cannot
# constrain a parameter's characters — only its length — so nothing upstream
# rejects one of these. The template base64-encodes it instead, which is why
# SAMPLE carries the ENCODED form below and check_bootstrap asserts the
# plaintext never appears in the rendered script.
HOSTILE_PASSWORD = "p'; touch /tmp/pwned; '$x `id` \"q\""

# Values only need to be representative: this renders the script, it does not
# deploy it. They deliberately carry the punctuation a real parameter can, so a
# quoting mistake in the template shows up as a parse error.
SAMPLE = {
    "@@NIXINSTALLER@@": "https://install.determinate.systems/nix",
    "@@FLAKEREF@@": "github:defangdevs/agent-box/0123456789abcdef",
    "@@AGENT@@": "claude",
    "@@USER@@": "agent",
    "@@AGENTSMD@@": "## This box\n\n- A line with 'quotes' and $dollars.\n",
    "@@SEEDMAINSESSION@@": "false",
    "@@WEBPASSWORD@@": base64.b64encode(HOSTILE_PASSWORD.encode()).decode(),
}

# Which parameter's defaultValue feeds which marker. The script is rendered a
# second time from these, because a deployment that accepts the form as it
# stands is the commonest one there is - and the defaults are where a stray
# non-ASCII character is most likely to arrive unnoticed (agentsMd is prose).
# webPassword has no default: it is the one field the form always demands.
DEFAULT_OF = {
    "@@NIXINSTALLER@@": "nixInstallerUrl",
    "@@FLAKEREF@@": "agentBoxFlakeRef",
    "@@AGENT@@": "agent",
    "@@USER@@": "userName",
    "@@AGENTSMD@@": "agentsMd",
}


def strip_generator(node):
    """Drop every `_generator` block, at any depth, so two CLI versions compare."""
    if isinstance(node, dict):
        return {k: strip_generator(v) for k, v in node.items() if k != "_generator"}
    if isinstance(node, list):
        return [strip_generator(v) for v in node]
    return node


def bicep_cli():
    """`bicep` on PATH, else `az bicep`, else None."""
    if shutil.which("bicep"):
        return ["bicep"]
    if shutil.which("az"):
        return ["az", "bicep"]
    return None


def check_no_drift() -> int:
    cli = bicep_cli()
    if cli is None:
        print(
            "FAIL: neither `bicep` nor `az` is on PATH, so the committed JSON "
            "cannot be verified against its source.\n"
            "       Install the pinned CLI:\n"
            '         az bicep install --version "$(cat azure/.bicep-version)"\n'
            "       or pass --no-build to skip this one check.",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "rebuilt.json"
        # `bicep build FILE --outfile X` and `az bicep build --file FILE
        # --outfile X` take the input differently.
        cmd = cli + ["build"]
        cmd += [str(BICEP)] if cli == ["bicep"] else ["--file", str(BICEP)]
        cmd += ["--outfile", str(out)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            print(f"FAIL: bicep build failed:\n{done.stdout}{done.stderr}",
                  file=sys.stderr)
            return 1
        if done.stderr.strip():
            # Warnings are not fatal to the build but should not go unseen.
            print(done.stderr.strip(), file=sys.stderr)
        rebuilt = strip_generator(json.loads(out.read_text()))
    committed = strip_generator(json.loads(COMPILED.read_text()))
    if rebuilt != committed:
        print(
            f"FAIL: {COMPILED.relative_to(REPO)} does not match a fresh build of "
            f"{BICEP.relative_to(REPO)}.\n"
            "       Rebuild and commit both:\n"
            "         cd azure && az bicep build --file agent-box.bicep",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {COMPILED.relative_to(REPO)} matches a fresh build "
          "(ignoring _generator).")
    return 0


def render_bootstrap(template: dict, values: dict | None = None) -> str:
    try:
        script = template["variables"]["bootstrapTemplate"]
    except KeyError:
        raise SystemExit(
            "FAIL: the compiled template has no `bootstrapTemplate` variable — "
            "this check can no longer see the script it exists to guard."
        )
    for marker, value in (values or SAMPLE).items():
        script = script.replace(marker, value)
    return script


def check_chain_covers_markers(template: dict) -> int:
    """Every marker in the script must be substituted by the Bicep itself.

    Rendering with SAMPLE proves the script is well-formed once every marker is
    replaced — it cannot prove the TEMPLATE replaces them. Drop a `replace()`
    from the chain in the .bicep while leaving its marker in the script and the
    render here still substitutes it from SAMPLE and reports OK, while the
    deployed VM runs a literal `@@MARKER@@`. So compare the two directly: the
    compiled `bootstrap` variable is the whole `replace()` chain as one ARM
    expression, and a marker it substitutes appears in it quoted.
    """
    variables = template.get("variables", {})
    script = variables.get("bootstrapTemplate", "")
    chain = variables.get("bootstrap", "")
    missing = [m for m in sorted(set(PLACEHOLDER.findall(script)))
               if f"'{m}'" not in chain]
    if missing:
        print(
            f"FAIL: marker(s) {missing} appear in the bootstrap script but are "
            "not substituted by the template's replace() chain, so a deployed "
            "box would run the literal marker.\n"
            "       Add the missing replace() to `var bootstrap` in "
            "azure/agent-box.bicep (and its value to SAMPLE here).",
            file=sys.stderr,
        )
        return 1
    unused = [m for m in SAMPLE if m not in script]
    if unused:
        print(
            f"FAIL: SAMPLE define(s) {sorted(unused)}, which the script no "
            "longer uses. Drop them, or this check is testing a marker nothing "
            "renders.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: all {len(SAMPLE)} markers are substituted by the template's own "
          "replace() chain.")
    return 0


def defaults_render(template: dict) -> dict:
    """SAMPLE, but with every marker that has a parameter default taking it."""
    values = dict(SAMPLE)
    params = template.get("parameters", {})
    for marker, name in DEFAULT_OF.items():
        if name not in params:
            raise SystemExit(
                f"FAIL: parameter {name!r} is gone, but the check still maps "
                f"{marker} to it. Update DEFAULT_OF."
            )
        if "defaultValue" in params[name]:
            values[marker] = params[name]["defaultValue"]
    return values


def check_bootstrap(template: dict, values: dict, label: str) -> int:
    script = render_bootstrap(template, values)
    lines = script.splitlines()

    left = PLACEHOLDER.findall(script)
    if left:
        print(
            f"FAIL: placeholder(s) {sorted(set(left))} survive substitution — the "
            "Bicep replace() chain and the script have drifted apart, so the VM "
            "would run a literal marker.",
            file=sys.stderr,
        )
        return 1

    if HOSTILE_PASSWORD in script:
        print(
            f"FAIL: the rendered bootstrap ({label}) contains the web password "
            "in the clear. The template must substitute base64(webPassword), "
            "not the plaintext: Bicep cannot constrain a parameter's "
            "characters, so a password holding a single quote would close the "
            "shell literal it lands in and run its own tail as root during "
            "first boot.",
            file=sys.stderr,
        )
        return 1

    try:
        script.encode("ascii")
    except UnicodeEncodeError as exc:
        bad = script[exc.start : exc.end]
        line = script[: exc.start].count("\n") + 1
        print(
            f"FAIL: non-ASCII character {bad!r} on line {line} of the bootstrap "
            f"({label}). "
            "Keep it ASCII: the same text is base64'd through the template and "
            "the extension handler, and az has been seen to re-encode it "
            "(--custom-data is latin-1, which aborts on an em-dash).",
            file=sys.stderr,
        )
        return 1

    guard_at = next((n for n, l in enumerate(lines) if GUARD.search(l)), None)
    if guard_at is None:
        print(
            "FAIL: the bootstrap has no bash re-exec guard.\n"
            '       Add: if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi',
            file=sys.stderr,
        )
        return 1
    for n, line in enumerate(lines[: guard_at + 1], start=1):
        if line.lstrip().startswith("#"):
            continue
        for token, why in BASHISMS:
            if token in line:
                print(
                    f"FAIL: line {n} uses a bashism BEFORE the re-exec guard "
                    f"(line {guard_at + 1}): {token!r} - {why}.\n"
                    f"       {line.strip()}",
                    file=sys.stderr,
                )
                return 1

    source_at = next(
        (n for n, l in enumerate(lines) if NIX_PROFILE_SOURCE.search(l)), None
    )
    if source_at is not None:
        home_at = next((n for n, l in enumerate(lines) if HOME_SET.search(l)), None)
        if home_at is None or home_at > source_at:
            print(
                f"FAIL: line {source_at + 1} sources a Nix profile snippet before "
                "the script sets HOME.\n"
                '       Add: export HOME="${HOME:-/root}"\n'
                "       The extension handler runs the script with no HOME, and the "
                "snippet reads $HOME with no default - under `set -u` that aborts "
                "the whole bootstrap.",
                file=sys.stderr,
            )
            return 1

    if not shutil.which("bash"):
        print("FAIL: bash not available to parse the bootstrap.", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".sh") as fh:
        fh.write(script + "\n")
        fh.flush()
        done = subprocess.run(["bash", "-n", fh.name], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"FAIL: the rendered bootstrap ({label}) does not parse under bash:\n"
              f"{done.stderr}", file=sys.stderr)
        return 1
    print(f"OK: bootstrap ({label}) renders to {len(lines)} ASCII lines that parse "
          f"under bash; guard on line {guard_at + 1}.")
    return 0


def check_secrets(template: dict) -> int:
    kind = template.get("parameters", {}).get("webPassword", {}).get("type")
    if kind != "securestring":
        print(
            f"FAIL: parameter webPassword is {kind!r}, not 'securestring'. A plain "
            "string is recorded in the deployment history, where anyone with reader "
            "access on the resource group can read it back.",
            file=sys.stderr,
        )
        return 1

    chain = template.get("variables", {}).get("bootstrap", "")
    if "base64(parameters('webPassword'))" not in chain:
        print(
            "FAIL: the replace() chain does not substitute "
            "base64(parameters('webPassword')). A raw substitution puts the "
            "password inside a shell literal it can close - see "
            "HOSTILE_PASSWORD above.",
            file=sys.stderr,
        )
        return 1

    exposed = []
    for res in template.get("resources", []):
        if res.get("type") != "Microsoft.Compute/virtualMachines/extensions":
            continue
        props = res.get("properties", {})
        if "script" in (props.get("settings") or {}):
            exposed.append(res.get("name"))
        if "script" not in (props.get("protectedSettings") or {}):
            print(
                f"FAIL: extension {res.get('name')!r} has no script in "
                "protectedSettings - either it stopped running the bootstrap, or "
                "the bootstrap moved somewhere this check cannot vet.",
                file=sys.stderr,
            )
            return 1
    if exposed:
        print(
            f"FAIL: extension(s) {exposed} put the bootstrap in `settings`. That "
            "block is returned verbatim by the ARM API, so the web terminal "
            "password inside it becomes readable to anyone with reader access. It "
            "belongs in `protectedSettings`, which ARM encrypts and never returns.",
            file=sys.stderr,
        )
        return 1
    print("OK: webPassword is a securestring and the bootstrap stays in "
          "protectedSettings.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--no-build",
        action="store_true",
        help="skip the recompile/drift check (for machines with no Bicep CLI)",
    )
    args = ap.parse_args()

    for path in (BICEP, COMPILED):
        if not path.exists():
            print(f"FAIL: {path.relative_to(REPO)} is missing.", file=sys.stderr)
            return 1

    template = json.loads(COMPILED.read_text())
    rc = max(
        check_chain_covers_markers(template),
        check_bootstrap(template, SAMPLE, "hostile sample values"),
        check_bootstrap(template, defaults_render(template), "template defaults"),
        check_secrets(template),
    )
    if not args.no_build:
        rc = max(rc, check_no_drift())
    return rc


if __name__ == "__main__":
    sys.exit(main())
