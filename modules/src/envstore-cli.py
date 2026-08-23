"""agent-box-envstore — read and write the box's KEY=value stores.

The format's one owner is src/lib/envstore.py; this is the door the callers
that are not Python use (issue #212). It is deliberately NOT on any PATH: the
wrappers that need it pin its store path as AGENT_BOX_ENVSTORE_BIN, the same
convention sessionCli already uses for flock and agent-box-profile, so a
caller cannot silently fall back to an older copy or to no copy at all.

Verbs, all of which take --file PATH or --profile NAME and otherwise act on
~/.config/agent-box/env:

  keys                 the key NAMES, sorted, one per line — never a value
  get KEY              one value, verbatim, no trailing newline added
  json                 the whole store as a JSON object, for jq callers
  set KEY=VALUE ...    add or replace; --stdin reads ONE value from stdin
  unset KEY ...        remove

`get` on a missing key exits 1 with nothing on stdout, so a shell caller can
write `v=$(... get K) || v=default`. Bad usage exits 2, matching the shell
CLIs.
"""
# The env-store library is spliced in ABOVE this file by the generated module
# (see envStoreCli in modules/agent-box.nix.in), so `os` and the envstore
# names are already bound here; only what the library does not import is
# imported again.
import argparse
import sys

PROFILE_NAME_MAX = 64


def die(message):
    sys.stderr.write(f"agent-box-envstore: {message}\n")
    raise SystemExit(2)


def valid_profile_name(name):
    if name == "" or len(name) > PROFILE_NAME_MAX:
        return False
    return all(char.isascii() and (char.isalnum() or char in "_-") for char in name)


def config_dir():
    # AGENT_BOX_CONFIG_DIR exists for the tests and for a caller that already
    # resolved the directory (the settings daemon is told its env file
    # outright); everything else follows $HOME, because this always runs as
    # the user whose store it is.
    override = os.environ.get("AGENT_BOX_CONFIG_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".config", "agent-box")


def target(args):
    """Return (path, header) for the store this invocation acts on."""
    if args.file:
        if args.profile:
            die("--file and --profile are mutually exclusive")
        # A bare --file gets the env-store header: the only other shape is a
        # profile, and that one names itself.
        return args.file, ENV_HEADER
    if args.profile:
        if not valid_profile_name(args.profile):
            die(
                f"invalid profile name '{args.profile}' (letters, digits, '_' "
                f"and '-', at most {PROFILE_NAME_MAX} characters)"
            )
        path = os.path.join(config_dir(), "profiles", f"{args.profile}.env")
        return path, profile_header(args.profile)
    return os.path.join(config_dir(), "env"), ENV_HEADER


def parse_assignments(items, from_stdin):
    if from_stdin:
        if len(items) != 1:
            die("--stdin takes exactly one KEY")
        key = items[0]
        if not valid_key(key):
            die(f"invalid key '{key}'")
        # The newline a shell here-doc or a `<file` redirect leaves at the end
        # is the terminator, not part of the secret — one is dropped, the rest
        # are kept. A PEM read with `--stdin < key.pem` therefore round-trips
        # byte-for-byte.
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        return [(key, value)]
    assignments = []
    for item in items:
        if "=" not in item:
            die(f"not a KEY=VALUE assignment: '{item}'")
        key, value = item.split("=", 1)
        if not valid_key(key):
            die(
                f"invalid key '{key}' (use letters, digits, underscore; not "
                "starting with a digit)"
            )
        assignments.append((key, value))
    return assignments


def main(argv):
    parser = argparse.ArgumentParser(prog="agent-box-envstore", add_help=True)
    parser.add_argument("--file", help="act on this file instead of the env store")
    parser.add_argument("--profile", help="act on ~/.config/agent-box/profiles/NAME.env")
    parser.add_argument("--stdin", action="store_true", help="`set`: read the value from stdin")
    parser.add_argument("verb", choices=("keys", "get", "json", "set", "unset"))
    parser.add_argument("args", nargs="*")
    args = parser.parse_args(argv)
    path, header = target(args)

    if args.verb == "keys":
        for key in keys(path):
            print(key)
        return 0
    if args.verb == "json":
        print(dumps(path))
        return 0
    if args.verb == "get":
        if len(args.args) != 1:
            die("get takes exactly one KEY")
        value = as_dict(load(path)).get(args.args[0])
        if value is None:
            return 1
        sys.stdout.write(value)
        return 0
    if args.verb == "set":
        if not args.args:
            die("set takes at least one KEY=VALUE")
        update(path, parse_assignments(args.args, args.stdin), header)
        return 0
    # unset
    if not args.args:
        die("unset takes at least one KEY")
    # No store, nothing to remove — and no store created to say so. The check
    # lives here, where the path is resolved, and not in a caller: a caller
    # that guessed the path would guess wrong whenever AGENT_BOX_CONFIG_DIR
    # moves it.
    if not os.path.exists(path):
        return 0
    for key in args.args:
        if not valid_key(key):
            die(f"invalid key '{key}'")
    update(path, [], header, drop=args.args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except EnvStoreError as error:
        die(str(error))
