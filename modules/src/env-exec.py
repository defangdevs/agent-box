"""Session-spawn env loader (issue 89), then exec the agent.

Sessions are (re)created by the long-lived supervisor inside the agent unit,
so a unit-level EnvironmentFile= snapshot of the user's env file goes stale
the moment the settings page (or a hand edit) changes it — "restart the
sessions to apply" silently applied nothing. This wrapper re-reads the file at
EVERY session spawn and then execs the agent, making the file the single live
source for those keys (it is deliberately NOT in the unit's EnvironmentFile,
so a DELETED key disappears on restart too).

Python since issue #212: a value may now span lines, and the parser that
understands that lives in src/lib/envstore.py, spliced in above this file.
Values still reach the child as literal strings — nothing here is eval'd or
expanded, so a secret full of shell metacharacters cannot break or inject
anything.

Best effort by construction: a missing, unreadable or half-corrupt store
costs the keys it holds, never the session. The exec at the bottom is the only
step allowed to fail.
"""
# The library above already imported os; only what it does not is imported
# here.
import shutil
import subprocess
import sys


def load_into(environ, path, skip=()):
    for key, value in load(path):
        if key in skip:
            continue
        environ[key] = value


def main(argv):
    if not argv:
        sys.stderr.write("agent-box-env-exec: nothing to exec\n")
        return 2
    home = os.path.expanduser("~")
    config = os.path.join(home, ".config", "agent-box")

    # The per-user secrets file the settings page and `agent-box-session env`
    # manage.
    load_into(os.environ, os.path.join(config, "env"))

    # The agent profile's own environment (issue #321), on top of the file
    # above: every key in profiles/<name>.env that is not one of the reserved
    # LAUNCH keys agent-box-profile turns into harness arguments. The
    # supervisor passes the name through the tmux session environment, so this
    # applies at every spawn — an edited profile reaches the session on its
    # next restart, exactly like the file above.
    #
    # Convenience, NOT isolation: this process's environment is readable
    # through /proc/<pid>/environ by every other session of this user (issue
    # #135, wiki Users-vs-Sessions), so a secret in a profile is a secret all
    # of them have. What a profile buys is that sessions started with OTHER
    # profiles do not get it handed to them, not that they cannot reach it.
    profile = os.environ.get("AGENT_BOX_PROFILE", "")
    if profile and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in profile
    ):
        load_into(
            os.environ,
            os.path.join(config, "profiles", f"{profile}.env"),
            skip=PROFILE_RESERVED,
        )

    # The GitHub login this box acts as, for local-webhook's "@self" sender
    # mute (issue #261). Resolved HERE because this is the one process that
    # holds the token: the loop above just set it, and the identity is a
    # property of that token, not of the deployment. A value the env store set
    # wins (the resolver echoes it straight back); otherwise the resolver
    # answers from its cache, and only calls GitHub when the token changed.
    # --throttled because this runs at EVERY session start: one failed lookup
    # per token per hour is enough. It bounds how OFTEN the resolver calls
    # GitHub, though, not how long one call may take, so the timeout below is
    # what actually keeps a session from waiting on the network: a box whose
    # DNS or egress is broken must still start its sessions. Best effort — no
    # token, no network, no resolver or a slow one leaves the key unset, which
    # is what a box that writes nothing to GitHub wants anyway.
    resolver = shutil.which("agent-box-webhook-self")
    if resolver:
        try:
            result = subprocess.run(
                [resolver, "--throttled"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            login = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            login = ""
        if login:
            os.environ["LOCAL_WEBHOOK_SELF"] = login

    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        sys.stderr.write(f"agent-box-env-exec: cannot exec {argv[0]}: {error}\n")
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
