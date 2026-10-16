import json as capacity_json
import sys


def capacity_main():
    if len(sys.argv) < 3:
        print("usage: agent-box-session-capacity "
              "check|spawn|hibernate-candidate|hibernate-verify REGISTRY [ARGS...]",
              file=sys.stderr)
        return 2
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
    command = sys.argv[1]
    try:
        if command == "hibernate-candidate":
            result = hibernation_candidate(sessions, excludes=sys.argv[3:])
            if result is None:
                return 1
            print(capacity_json.dumps(result))
            return 0
        if command == "hibernate-verify":
            if len(sys.argv) != 5:
                print("usage: agent-box-session-capacity hibernate-verify REGISTRY NAME TOKEN",
                      file=sys.stderr)
                return 2
            result = hibernation_candidate(
                sessions, name=sys.argv[3], token=sys.argv[4])
            if result is None:
                return 1
            print(capacity_json.dumps(result))
            return 0
        if command not in ("check", "spawn"):
            print("unknown capacity command: %s" % command, file=sys.stderr)
            return 2
        result = capacity_check(sessions, sys.argv[3:],
                                spawning=command == "spawn")
        print(capacity_json.dumps(result))
        return 0
    except SessionHibernationError as exc:
        print("Cannot inspect session quiescence: %s" % exc, file=sys.stderr)
        return 75
    except SessionCapacityError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (OSError, ValueError, KeyError,
            capacity_subprocess.TimeoutExpired) as exc:
        print("Cannot check session capacity: %s" % exc, file=sys.stderr)
        return 75


if __name__ == "__main__":
    sys.exit(capacity_main())
