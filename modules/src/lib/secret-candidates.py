# Secret custody: advisory detection of plaintext secrets, never migration
# (issue #922).
#
# This file answers one question - "which names in this user's env store or
# profiles LOOK like secrets and are not yet in the broker?" - and nothing
# else. It never reads a value, never writes anywhere, and nothing that
# calls it is allowed to use the answer to copy a value into the broker.
# That is deliberate: a value that has been sitting in a plaintext env file
# has already been exposed (readable via /proc, possibly logged, possibly
# in a shell history or an old backup), so moving the SAME bytes into an
# encrypted store would launder that exposure rather than end it. The only
# real fix is rotating the credential at its source and setting a FRESH
# value with `agent-box-secret set`; this module exists to point that out
# loudly and repeatedly, not to paper over it with a one-time copy.
#
# Called from three places, all read-only: `agent-box-secret list` (the
# CLI), the settings page's env/profile editor, and a root-run advisory
# step in the box's own update path (agentbox apply / the NixOS activation
# equivalent) - chosen over "wait a couple of releases" specifically
# because a box's owner decides when it updates, so there is no shared
# clock to count releases against. Every one of the three calls this with
# a REAL root_key path or dir listing it can read; none of them call the
# broker to ask it to decrypt anything, because this never needs to.
#
# Spliced ABOVE secret-cli.py (and the settings daemon, and the apply-time
# report script) by the generated module, after lib/envstore.py, so load()
# already exists when this runs.
import os
import re

# A heuristic, not a classifier: suffix-shaped names a human would
# recognize as "probably a credential". False positives are fine and
# expected here (WEB_COOKIE_SECRET_* is not a user secret, for instance) -
# this only ever produces a hint for a human to judge, never an action.
# False negatives just mean a name using neither convention gets no hint,
# which is the same as today's behavior.
CANDIDATE_NAME_RE = re.compile(
    r"(^|_)(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIALS?|APIKEY|PAT)S?($|_)",
    re.IGNORECASE,
)


def looks_like_a_secret(name):
    return bool(CANDIDATE_NAME_RE.search(name))


def _names_in(path):
    return [key for key, _ in load(path)]


def list_profiles(home):
    """Every profile this user has, by name - a plain directory listing,
    the same set `agent-box-profile ls` would show."""
    profiles_dir = os.path.join(home, ".config", "agent-box", "profiles")
    try:
        entries = os.listdir(profiles_dir)
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.endswith(".env") and not entry.startswith("."):
            names.append(entry[:-len(".env")])
    return sorted(names)


def protected_names(store_root, namespace):
    """Names already in the broker's namespace for this user, read the way
    root can - straight off the record filenames, no decryption, no socket.
    A profile the broker has never seen (no directory yet) is simply empty.
    Callers without filesystem access to store_root (an ordinary CLI call,
    for instance) pass the names they already got from `agent-box-secret
    list` instead - see scan()'s `known` parameter."""
    try:
        entries = os.listdir(os.path.join(store_root, namespace))
    except OSError:
        return set()
    return {entry[:-len(".rec")] for entry in entries if entry.endswith(".rec")}


def scan(home, store_root=None, known_by_namespace=None, default_profile=None):
    """Every candidate name in this user's env store and profiles that is
    not already registered in the matching broker namespace.

    `store_root` (this user's /var/lib/agent-box-secrets/<user>, root-
    readable only) is used when the caller can read it directly - the
    apply-time report, say. A caller that cannot (an ordinary CLI
    invocation) instead passes `known_by_namespace`, a
    {namespace: set-of-names} map it already asked the broker for over its
    own socket. Giving neither simply reports every candidate as
    unprotected, which is still a safe (over-) estimate, never an under-
    one: the worst this function does wrong is mention a name that
    actually is already protected, not miss one that is not.

    `default_profile` is this user's CURRENT default profile name (there is
    no separate "_global" namespace any more - see resolve_profile), needed
    only to resolve the shared env file's own candidates against the right
    `store_root` directory. A caller with no default profile (or that does
    not know it) can leave this unset; the env file's candidates are then
    reported as unprotected rather than guessed at.

    Returns a list of {"name", "scope", "path"} dicts, "scope" being
    "env" (the box-wide file every session loads, default-profile
    sessions and every other profile's sessions alike) or "profile:NAME".
    """
    known_by_namespace = known_by_namespace or {}

    def already_known(namespace, name):
        if namespace in known_by_namespace:
            return name in known_by_namespace[namespace]
        if store_root:
            real_namespace = default_profile if namespace is None else namespace
            if real_namespace is not None:
                return name in protected_names(store_root, real_namespace)
        return False

    out = []
    config = os.path.join(home, ".config", "agent-box")
    env_path = os.path.join(config, "env")
    for name in _names_in(env_path):
        if looks_like_a_secret(name) and not already_known(None, name):
            out.append({"name": name, "scope": "env", "path": env_path})

    for profile in list_profiles(home):
        profile_path = os.path.join(config, "profiles", f"{profile}.env")
        for name in _names_in(profile_path):
            if looks_like_a_secret(name) and not already_known(profile, name):
                out.append({"name": name, "scope": f"profile:{profile}",
                           "path": profile_path})
    return out


HINT_LINES = (
    "These names look like credentials and are still stored in plain text:",
    "The exact bytes have already been exposed (every process of this user "
    "can read them), so copying them into agent-box-secret would not undo "
    "that - rotate each one at its source, then store the NEW value with "
    "`agent-box-secret set NAME --stdin` (add --profile NAME for a profile, "
    "not the shared env file).",
)


def format_hint(candidates):
    """The human-readable notice shared by the CLI, the apply-time report,
    and (as plain text for a tooltip) the settings page. Empty when there
    is nothing to report."""
    if not candidates:
        return ""
    lines = [HINT_LINES[0]]
    for item in candidates:
        where = "the shared env file" if item["scope"] == "env" else item["scope"]
        lines.append(f"  {item['name']}  ({where})")
    lines.append(HINT_LINES[1])
    return "\n".join(lines)
