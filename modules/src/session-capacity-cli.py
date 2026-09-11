import json as capacity_json
import sys


def capacity_main():
    try:
        with open(sys.argv[2], encoding="utf-8") as handle:
            sessions = capacity_json.load(handle)["sessions"]
    except FileNotFoundError:
        # No registry yet — first boot, before the supervisor's own seed
        # has run — is zero pending sessions, not an error. A read-only
        # capacity check must never be what CREATES the registry: a file
        # this call conjured up "exists" for the seed that runs after it,
        # and a seed never overwrites an existing file (issue #59), so the
        # NixOS-declared sessions would silently never get seeded.
        sessions = {}
    try:
        result = capacity_check(sessions, sys.argv[3:],
                                spawning=sys.argv[1] == "spawn")
        print(capacity_json.dumps(result))
        return 0
    except SessionCapacityError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (OSError, ValueError, KeyError,
            capacity_subprocess.TimeoutExpired) as exc:
        print("Cannot check session capacity: %s" % exc, file=sys.stderr)
        return 75


if __name__ == "__main__":
    sys.exit(capacity_main())
