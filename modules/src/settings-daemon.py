# Per-user settings daemon for agent-box (issue #36).
# (Run via pkgs.writers.writePython3Bin, which supplies the interpreter
# shebang; no #! line here so it stays lint-clean.)
#
# Runs AS THE AGENT USER (no root) — it only ever touches files the user
# already owns and only kills the user's own tmux session, so it crosses no
# privilege boundary. One instance per web-terminal user, bound to
# 127.0.0.1:<port>; Caddy reverse-proxies https://<domain>/<user>/settings*
# to it INSIDE that user's existing basic-auth block, so there is no new
# auth surface (see modules/agent-box.nix).
#
# Purpose: let the end user add/remove agent secrets (GH_TOKEN,
# ANTHROPIC_API_KEY, ...) WITHOUT a nixos-rebuild and WITHOUT ever typing the
# secret into the agent chat/terminal (which would leak into the transcript,
# tmux scrollback, and model context). The secret path is
# browser -> TLS (Caddy) -> this daemon -> ~/.config/agent-box/env (0600).
#
# The UI lists key NAMES only; it never renders a stored value. "Apply"
# kills the user's tmux sessions (same uid, via the PrivateTmp socket
# under TMUX_TMPDIR); the supervisor in the agent unit brings them
# back with the fresh environment.
#
# Sessions (issue 59): the daemon is also the web CRUD surface for the
# user-owned sessions.json — add/delete/restart sessions, managed on
# every user's settings page. For the primary web user
# (AGENT_BOX_HOME=1) the vhost root (/) additionally serves a tabbed
# terminal workspace (issue 119): one tab per session, each pane an
# iframe onto the per-session ttyd URL. The reconcile/respawn
# logic deliberately does NOT live here (a daemon crash or restart
# must never take the agent sessions down): the daemon only writes the
# file and kills the user's own tmux sessions; the supervisor in the
# hardened agent unit does the starting.
#
# Deliberately Python-3-stdlib only: no third-party imports, so it stays
# tiny and auditable and needs nothing beyond pkgs.python3.
#
# Listening (issue #49): under the module, systemd socket-activates the
# daemon on a pre-bound unix socket (0660 <user>:caddy — only the user and
# the caddy reverse-proxy can connect; localhost TCP was reachable by every
# local user). Without LISTEN_FDS (dev rigs, e2e runs) it falls back to
# binding 127.0.0.1:$AGENT_BOX_SETTINGS_PORT itself.
#
# Configuration comes from the environment (set by the systemd unit):
#   AGENT_BOX_SETTINGS_USER      the linux user name (display only)
#   AGENT_BOX_SETTINGS_ENV_FILE  path to the env file to manage
#   AGENT_BOX_SETTINGS_BASE      URL base path, e.g. /alice/settings
#   AGENT_BOX_SETTINGS_PORT      dev fallback TCP port on 127.0.0.1
#                                 (ignored when socket-activated)
#   AGENT_BOX_TMUX_SOCKET        tmux -L socket name (e.g. agent-box)
#   AGENT_BOX_TMUX_TMPDIR        TMUX_TMPDIR the agent's socket lives under
#   AGENT_BOX_TMUX_BIN           absolute path to the tmux binary
#   AGENT_BOX_SESSIONS_FILE      path to the user's sessions.json
#   AGENT_BOX_HOME               "1" = also serve the tabbed terminal
#                                 workspace at / (primary web user)
#   AGENT_BOX_AGENTS             comma-separated installed agent CLIs
#   AGENT_BOX_PASSWORD_CMD       no-argument sudo command that verifies
#                                 and replaces this user's web password
#   AGENT_BOX_WEBHOOK_SCRIPT     pinned local-webhook webhook.py (empty =
#                                 no webhook receiver, so no panel — but
#                                 the page still says which of "off" and
#                                 "misconfigured" it is)
#   AGENT_BOX_WEBHOOK_STATE_DIR  where its filter.*.json files live
#   AGENT_BOX_WEBHOOK_URL        the receiver endpoint senders POST to
#   AGENT_BOX_WEBHOOK_PYTHON     interpreter for that script
#   AGENT_BOX_CONNECT_BINS       "<id>=<binary>" pairs for the guided
#                                 sign-in cards (claude, codex, github)

import contextlib
import fcntl
import functools
import glob
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

# The env store's format lives in ONE place (issue #212): src/lib/envstore.py,
# which the generated module prepends to this file (see settingsDaemon in
# modules/agent-box.nix.in, the same seam envExecWrapper and envStoreCli use).
# This daemon is the file's main writer and does NOT shell out to the CLI: a
# secret must not travel through the argv of a helper process to get written.
# The names that library defines — KEY_RE, ENV_HEADER, PROFILE_RESERVED,
# as_dict/load/keys/update/profile_header — are therefore already bound here
# and are used below. PROFILE_RESERVED in particular is NOT redefined here:
# env-exec skips exactly those keys when it exports a profile's environment
# at spawn, so a second copy could classify a key as environment that the
# spawn path then refuses to export, and the profile's value would silently
# never reach the session.

USER = os.environ.get("AGENT_BOX_SETTINGS_USER", "agent")
ENV_FILE = os.environ["AGENT_BOX_SETTINGS_ENV_FILE"]
# Agent profiles (issue #321) live beside the env store, one file per profile.
# The preamble cache key reads this too: a watch's launch report names the
# profile AGENT_BOX_HOOK_PROFILE picks and whether it still exists, so
# creating or deleting a profile changes that report without touching the env
# file.
PROFILES_DIR = os.path.join(os.path.dirname(ENV_FILE), "profiles")
# The profile CLI, pinned by the unit (step 5 of #321). The page reads and
# writes profile FILES itself, with the same parser as the env store — but it
# never maps a profile onto harness arguments, because that mapping lives in
# exactly one place, `agent-box-profile launch` (#337). A second copy here
# would drift, and then what this page shows and what a session gets would
# disagree. Empty on a box whose unit predates this: the editor still works,
# only the resolved launch line and its warnings go missing.
PROFILE_BIN = os.environ.get("AGENT_BOX_PROFILE_BIN", "")
# Same charset and length the CLI's valid_name() enforces (profile-cli.sh):
# one file per profile, so the name is also a path component.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROFILE_RESERVED_LABELS = {
    "HARNESS": "Assistant",
    "MODEL": "Model",
    "EFFORT": "Reasoning level",
    "SYSTEM_PROMPT": "Instructions",
}
# PROFILE_RESERVED (the launch config) comes from the env store above, which
# is also what env-exec skips at spawn. Every OTHER key in a profile is
# session environment, and is listed by NAME only — a profile is exactly
# where a token ends up.
BASE = os.environ.get("AGENT_BOX_SETTINGS_BASE", "/settings").rstrip("/")
PORT = int(os.environ.get("AGENT_BOX_SETTINGS_PORT", "8080"))
TMUX_SOCKET = os.environ.get("AGENT_BOX_TMUX_SOCKET", "agent-box")
TMUX_TMPDIR = os.environ.get("AGENT_BOX_TMUX_TMPDIR", "")
TMUX_BIN = os.environ.get("AGENT_BOX_TMUX_BIN", "tmux")
# Sessions (issue 59): the daemon is the web CRUD surface for the
# user-owned sessions.json; the supervisor inside the agent unit
# reconciles tmux against it (starts within ~2s). The daemon only
# ever writes the file and kills the user's own tmux sessions.
SESSIONS_FILE = os.environ.get("AGENT_BOX_SESSIONS_FILE", "")
# Primary web user's daemon (Caddy proxies the vhost root here, behind
# the same cookie-or-basic auth as the terminal): GET / renders the
# tabbed terminal workspace and session CRUD lives at /sessions/*.
# The settings page keeps the session manager list plus secrets +
# danger zone.
HOME = os.environ.get("AGENT_BOX_HOME", "") == "1"
# This user's own space on the vhost: the workspace page at TERM_HOME, the
# settings page under it, and one path per session. The
# vhost root is a user picker that lands here — so a bookmark of the
# handed-out URL opens the box, not a single raw terminal.
TERM_BASE = "/" + urllib.parse.quote(
    os.environ.get("AGENT_BOX_SETTINGS_USER", "agent"), safe="")
TERM_HOME = TERM_BASE + "/"
# Every terminal user on this box, in the order the vhost lists them, so
# the root picker can name them. One entry (the norm) means there is
# nothing to pick and the root redirects straight into it.
WEB_USERS = [x for x in os.environ.get("AGENT_BOX_WEB_USERS", "").split(",") if x]
# Where session CRUD routes live, and the page they redirect back to.
SESS_BASE = "" if HOME else BASE
SESS_PAGE = TERM_HOME if HOME else BASE + "/"
AGENTS = [a for a in os.environ.get("AGENT_BOX_AGENTS", "claude").split(",") if a]
# Full sudo command line that triggers the box update (issue 54). Empty
# when selfUpdate is off, which hides the Update card and 404s the route.
UPDATE_CMD = os.environ.get("AGENT_BOX_UPDATE_CMD", "")
# Running agent-box git rev + GitHub owner/repo (set alongside
# UPDATE_CMD when selfUpdate is on) — shown on the Update card and
# used by its non-blocking GitHub update check.
REPO = os.environ.get("AGENT_BOX_REPO", "")
REV = os.environ.get("AGENT_BOX_REV", "")
# Read-only handles for the {BASE}/status progress endpoint the page
# polls after triggering an update: the update oneshot's unit name and
# a systemctl binary to `show` its state with. Both empty unless
# selfUpdate is on (no unit to watch) or on dev rigs without systemd;
# the endpoint then simply omits the update block. Querying unit state
# is unprivileged — no sudo, unlike the trigger in UPDATE_CMD.
UPDATE_UNIT = os.environ.get("AGENT_BOX_UPDATE_UNIT", "")
SYSTEMCTL = os.environ.get("AGENT_BOX_SYSTEMCTL", "")
# Full sudo command line that reboots the whole box. Set for the ROOT
# terminal user only — a reboot is the one Danger-zone action that
# crosses the user boundary (Restart all bounces the caller's own unit
# and needs no sudo at all), so on a multi-user box it must not be one
# user's power over another's sessions. Empty everywhere else, which
# hides the card and 404s the route.
#
# Its whole reason to exist: a kernel or libc patch installs on disk and
# takes effect at a boot, and unattended patching deliberately never
# reboots (see the box's apt policy). Without this the box has no way to
# finish those updates at all — nobody inside it can reboot.
REBOOT_CMD = os.environ.get("AGENT_BOX_REBOOT_CMD", "")
# The distro's own "a reboot would apply something" marker: Debian and
# Ubuntu's update machinery creates it, and the .pkgs file beside it
# lists what asked. Both are world-readable, so the card reports WHY a
# reboot is pending with no privilege at all. A NixOS box has neither
# file and simply shows the card with no reason line.
REBOOT_REQUIRED = "/run/reboot-required"
# Per-user, no-argument privileged helper (issue 91). Passwords are sent
# as JSON on stdin, never argv or environment, and helper output is never
# reflected into HTTP responses.
PASSWORD_CMD = os.environ.get("AGENT_BOX_PASSWORD_CMD", "")
# Webhook subscriptions panel (issue #227). The pinned local-webhook
# script plus the state directory its filter files live in; both set by
# the module only when the webhook receiver is enabled, so the panel and
# its routes simply do not exist otherwise. The daemon never parses or
# writes a filter file itself — it runs the script's own CLI, which owns
# topic parsing, TTL stamping, the atomic replace and its lock.
WEBHOOK_SCRIPT = os.environ.get("AGENT_BOX_WEBHOOK_SCRIPT", "")
WEBHOOK_STATE_DIR = os.environ.get("AGENT_BOX_WEBHOOK_STATE_DIR", "")
# Where a sender POSTs, printed on the panel so the URL to register is on
# the page instead of behind `agent-box-webhook url` in a session.
# Set by the module for a terminal user whenever the receiver is enabled,
# which is exactly when caddy serves that path; empty on a dev rig, and
# then the panel simply says nothing about the endpoint.
WEBHOOK_URL = os.environ.get("AGENT_BOX_WEBHOOK_URL", "")
# Interpreter for the above. The daemon's own is a fine fallback for a
# dev run; the module passes the same python the receiver unit uses.
WEBHOOK_PYTHON = os.environ.get("AGENT_BOX_WEBHOOK_PYTHON", "") or sys.executable
WEBHOOKS = bool(WEBHOOK_SCRIPT and WEBHOOK_STATE_DIR)
# Whether the box MEANT to have webhooks: every AGENT_BOX_WEBHOOK_* the
# unit passed, whether or not it passed the two the panel runs on. Read
# here with the rest rather than scanned when the page renders, so the
# panel's answer depends on what this unit was handed and not on whatever
# a later caller happens to have in its environment.
WEBHOOK_CONFIGURED = sorted(
    name for name, value in os.environ.items()
    if name.startswith("AGENT_BOX_WEBHOOK_") and value)
# The dispatch script the receiver runs on a match. Run here only in its
# --preamble mode, which spawns nothing: it prints the prompt the next
# match would start a session with, so the standing-watch panel shows what
# a watch DOES instead of restating why it exists (#259).
HOOK_SPAWN_CMD = os.environ.get("AGENT_BOX_HOOK_SPAWN_CMD", "")


def webhook_unavailable():
    """Why this box shows no webhook panel — or None when it shows one.

    Rendering nothing at all is how #425 came in: a fresh native box had
    no panel, and the page gave its operator no way to tell a feature that
    is OFF from one that is wired up wrong. An absence looks the same
    either way, so the report had to start with a bug hunt. Three states,
    and only the first is a choice anybody made:

      - Off. No webhook variable reaches this daemon at all, because the
        box renders no receiver. Nothing is broken; one line says so.
      - Half configured. Some webhook variable arrived and the ones this
        panel runs on did not. That is #426 exactly — the module set
        AGENT_BOX_WEBHOOK_SCRIPT on the settings unit, the native renderer
        set it only in the agent-box-webhook CLI wrapper, and the panel
        vanished on every native box.
      - Pinned at a path that is not there. #425's second half: the
        declared webhook.py had no owner and nothing installed it. Every
        route here forks that file, so the panel would render empty and
        each button would fail for a reason the page never states.
    """
    if not (WEBHOOK_CONFIGURED or HOOK_SPAWN_CMD):
        return ("Automations are not enabled for this agent-box. An "
                "administrator can enable webhook support in its "
                "configuration.")
    missing = [name for name, value in
               (("AGENT_BOX_WEBHOOK_SCRIPT", WEBHOOK_SCRIPT),
                ("AGENT_BOX_WEBHOOK_STATE_DIR", WEBHOOK_STATE_DIR))
               if not value]
    if missing:
        return ("Automations are unavailable because setup is incomplete: "
                "this page was given no "
                + " and no ".join("<code>%s</code>" % html.escape(name)
                                  for name in missing)
                + ". Report this message to the administrator.")
    if not os.path.isfile(WEBHOOK_SCRIPT):
        return ("Automations are unavailable because a required file "
                "<code>%s</code> is not there, so nothing on this page can "
                "run. Events are not arriving either. Report this message "
                "to the administrator."
                % html.escape(WEBHOOK_SCRIPT))
    return None


# Env var names (KEY_RE) come from the spliced env-store library above: the
# charset a shell / systemd EnvironmentFile accepts as a variable name, and
# the same one the CLIs enforce.
# Session names: same charset the supervisor and CLI enforce (they
# land in tmux -t targets and URLs). Every render and publish path filters
# through this, so a name it rejects is not merely unlisted — that session
# has no tab, no delete/restart button and no subscriptions row, while it
# keeps running and receiving events (issue #236).
#
# The bound is therefore whatever the name-minting paths can emit, not a
# round number: the dispatch wrapper's hook-<source key>-<4 hex> reaches 150
# characters for GitHub's own maxima (39-character owner + "/" + 100-
# character repo). Shortening a name to fit is NOT an option — two repos
# sharing a prefix would collapse onto one name — so the UI stretches
# instead (the tab label ellipsizes, with the full name as its tooltip).
# Mirrored by the CLI's NAME_MAX and the module's session-name assertion;
# it stays far below what a filter.<user>-<session>.json filename allows.
NAME_MAX = 150
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % NAME_MAX)
# Names a session may never take: each already means something else under
# TERM_BASE, and a session path is what the vhost sends everything ELSE
# there to. A session called "settings" would shadow the settings page —
# or, worse, be shadowed BY it and become unreachable with no hint why.
# "token" and "ws" are ttyd's own endpoints, reached without a trailing
# slash, which is the one shape a session path cannot be told apart from.
# Mirrored by the CLI's own check and by the module's session-name
# assertion, so a name is refused wherever it is typed.
RESERVED_NAMES = frozenset(("settings", "downloads", "webhook", "sessions",
                            "token", "ws"))
# Subscription topics: "source:owner/repo", or the prefix "source:owner/*"
# (local-webhook 0.13.0 dropped "*" and "source:*" as topics). The
# panel only ever posts back a topic it just rendered, and the CLI is
# exec'd as an argv list with no shell, so this is a sanity bound rather
# than a quoting defence — it keeps a malformed form value from reaching
# the subscription file at all.
TOPIC_RE = re.compile(r"^[A-Za-z0-9_.:/*-]{1,128}$")
# A webhook source name, as `agent-box-webhook setup` validates it
# (letters, digits, _ and -). The secret route resolves a name to a file
# inside the state dir, so this is the check that keeps a path out of it.
SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The agent user's home. A session's working directory defaults to it
# and the working-directory picker (below) browses within it: the
# daemon runs AS the user with ProtectHome=false, so it could read the
# whole tree, but a session only ever runs somewhere the user owns and
# confining the picker to $HOME keeps the web surface from doubling as
# a filesystem browser. systemd sets $HOME for User=; fall back to the
# passwd entry so a bare dev invocation still resolves it.
HOME_DIR = os.path.realpath(
    os.environ.get("HOME") or os.path.expanduser("~" + USER)
)


def session_url(name):
    """Where one session's terminal lives. SESSION_RE names are URL-safe
    as they stand; the trailing slash is what the vhost matches on."""
    return TERM_HOME + name + "/"


def resolve_browse_dir(raw):
    """Map a user-typed directory prefix to an absolute path CONFINED
    to HOME_DIR, or None if it escapes. "", "~" and "~/" mean HOME;
    "~/x" and absolute paths are honoured; anything else is relative to
    HOME. Only the directory portion is resolved — the caller lists its
    immediate children. realpath collapses .. and symlinks BEFORE the
    containment check, so neither can climb out of HOME."""
    raw = (raw or "").strip()
    if raw in ("", "~", "~/"):
        return HOME_DIR
    if raw.startswith("~/"):
        candidate = os.path.join(HOME_DIR, raw[2:])
    elif raw.startswith("/"):
        candidate = raw
    else:
        candidate = os.path.join(HOME_DIR, raw)
    candidate = os.path.realpath(candidate)
    if candidate == HOME_DIR or candidate.startswith(HOME_DIR + os.sep):
        return candidate
    return None


def list_subdirs(abs_dir):
    """Immediate subdirectory names of abs_dir, sorted case-folded and
    capped. is_dir() follows symlinks (a symlinked checkout is a valid
    cwd); an unreadable or non-directory path yields []."""
    try:
        entries = list(os.scandir(abs_dir))
    except OSError:
        return []
    names = []
    for entry in entries:
        try:
            if entry.is_dir():
                names.append(entry.name)
        except OSError:
            continue
    names.sort(key=str.lower)
    return names[:500]


def resolve_session_cwd(raw):
    """Turn the add form's working-directory field into the value
    stored in sessions.json, or raise ValueError with a user-facing
    message. HOME (the "~" default) is stored as None so the supervisor
    keeps its default-to-home behaviour and the file stays portable;
    any other directory is stored as an absolute path. The path must
    already exist — tmux new-session -c fails on a missing cwd."""
    abs_dir = resolve_browse_dir(raw)
    if abs_dir is None:
        raise ValueError("Working directory must be inside your home directory.")
    if not os.path.isdir(abs_dir):
        raise ValueError("Working directory does not exist: %s" % raw.strip())
    return None if abs_dir == HOME_DIR else abs_dir


def valid_password(password):
    """Accept password-manager symbols; form fields cannot contain LF/CR."""
    return 16 <= len(password) <= 64 and not any(
        char in password for char in "\r\n"
    )


def read_keys():
    """The sorted KEY names in the env file — never their values.

    The UI must not be able to surface a stored secret, so the page asks for
    names only. Reading and writing the file itself is the env-store
    library's job (issue #212), including the multi-line values a PEM needs.
    """
    return keys(ENV_FILE)


def set_key(key, value):
    update(ENV_FILE, [(key, value)], ENV_HEADER)


def delete_key(key):
    update(ENV_FILE, [], ENV_HEADER, drop=[key])


def profile_path(name):
    return os.path.join(PROFILES_DIR, name + ".env")


def read_profiles():
    """name -> {"reserved": {KEY: value}, "env": [KEY, ...]}.

    A profile file IS an env-store file in another directory, so it is read
    by the store's one parser (issue #212) rather than by a KEY=value loop of
    this page's own — which is what lets a SYSTEM_PROMPT span lines here, in
    `agent-box-profile show`, and at spawn, all three alike.

    Reserved values come back because the editor pre-fills them. The env half
    is names only, and no caller is given the values: `agent-box-profile show`
    and `env ls` both keep that rule, and a profile is where a token ends up.
    """
    out = {}
    try:
        names = sorted(os.listdir(PROFILES_DIR))
    except OSError:
        return out
    for entry in names:
        if not entry.endswith(".env"):
            continue
        name = entry[:-len(".env")]
        if not PROFILE_NAME_RE.match(name):
            continue
        data = as_dict(load(profile_path(name)))
        out[name] = {
            "reserved": {k: data.get(k, "") for k in PROFILE_RESERVED},
            "env": sorted(k for k in data if k not in PROFILE_RESERVED),
        }
    return out


def profile_write(name, assignments, drop=(), must_exist=False):
    """One read-modify-write on a profile, under the store's own lock.

    `must_exist` belongs to that same critical section, not to an
    `os.path.exists` in the caller: outside the lock a concurrent
    /profiles/delete lands between the check and the write, and the write
    recreates the profile that was just deleted. Returns False when it was
    gone.
    """
    return update(profile_path(name), assignments, profile_header(name),
                  drop=drop, must_exist=must_exist)


def profile_remove(name):
    """Delete under the SAME lock profile_write takes. Without it a save can
    read the file, this can unlink it, and the save can then write it back —
    a profile the operator deleted, quietly recreated."""
    path = profile_path(name)
    try:
        with locked(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    except OSError:
        # The lock itself is unavailable (the directory is gone, say). There
        # is then nothing to delete either.
        pass


def profile_launch(name, harness=""):
    """`agent-box-profile launch` for `name`, or None when it cannot answer.

    The ONE resolver (#337): the harness a profile starts and the arguments
    it starts with are its answer, not this page's. Used on write (so a
    profile that sets something its harness cannot use says so at once,
    rather than at the next session start) and on session add.
    """
    # Callers check PROFILE_BIN themselves before offering a profile at all
    # (see render_profile_options), so reaching here without one is a bug
    # rather than a state to report — but returning None would report it as
    # "that profile is gone", which is the wrong thing to go looking for.
    if not PROFILE_BIN:
        return None
    try:
        out = subprocess.run(
            [PROFILE_BIN, "launch", name] + ([harness] if harness else []),
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        resolved = json.loads(out.stdout)
    except ValueError:
        return None
    # json.loads takes any JSON value. A resolver that printed `[]`, `"x"` or
    # `null` would reach .get() below as an AttributeError — a 500 with a
    # traceback, in place of the answer this function exists to give.
    return resolved if isinstance(resolved, dict) else None


@contextlib.contextmanager
def sessions_lock():
    """Serialize one read_sessions -> write_sessions pair (issue #254).

    The protocol itself — the sidecar path, the primitive, the bound, and why
    a rename is not enough — is written down once, in
    modules/src/lib/registry.sh, which the four SHELL writers splice in. This
    is the fifth writer and the only one that is python, so it takes the same
    flock(2) on the same sidecar file through fcntl instead. What has to agree
    across the two languages is exactly that file's contract, and
    tests/test-registry.py checks it from both sides.

    Two things are true here and nowhere else. This daemon is a
    ThreadingHTTPServer, so it races ITSELF as well as the four shell writers
    (each fcntl.flock call opens its own description, so its own threads
    serialize too). And the loss it caused was worse than a lost row:
    reverting hasRun / boxSessionId / the cleared initialPrompt left a RUNNING
    session the supervisor treated as a first spawn next time it died,
    re-firing the kickoff prompt against a new id and orphaning the
    transcript. That particular cost is being taken off the table separately
    (issue #282): the supervisor keeps its own observations in
    ~/.local/state/agent-box/session/ and reads the registry copy only as a
    migration fallback, so what a lost update here can revert is intent —
    which the operator can see and re-state — and not the record of a
    conversation. The lock stays either way: intent is worth as much.

    fcntl precedent in this repo: password-helper.py's AUTH_ENV_LOCK.

    Best effort by design: if the lock cannot be created or taken, the body
    still runs — an unlockable registry must not make the web UI refuse to
    add or delete a session.
    """
    lock = None
    try:
        directory = os.path.dirname(SESSIONS_FILE) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # "a": create if absent, never truncate — the file is a lock, its
        # contents are irrelevant and other holders keep their offsets.
        lock = open(SESSIONS_FILE + ".lock", "a", encoding="utf-8")
        # Bounded like the shell writers' `flock -w 10`: a request thread must
        # never park forever behind a wedged holder. Timing out proceeds
        # unlocked, which is the pre-#254 behavior rather than a new failure.
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    lock.close()
                    lock = None
                    break
                time.sleep(0.05)
    except OSError:
        if lock is not None:
            lock.close()
            lock = None
    try:
        yield
    finally:
        if lock is not None:
            lock.close()


# What write_sessions stamps on a registry that carries no version of its
# own. It never OVERWRITES one it read: see write_sessions.
REGISTRY_VERSION = 1


class RegistryUnreadable(Exception):
    """SESSIONS_FILE is there but is not a session registry (issue #279).

    Raised by load_sessions so a read-modify-write can refuse. It is a
    distinct answer from "there are no sessions", and conflating the two was
    the bug: every mutation route wrote {} back plus its own edit, so ONE
    click on Add session republished the registry with a single entry and
    delisted every other session for good -- hasRun, boxSessionId and stopped
    gone with them. Running panes survived as unmanaged tmux sessions that
    nothing respawns, and re-adding a name started a fresh conversation.
    """


def load_sessions():
    """Read SESSIONS_FILE as (sessions, version), for a read-modify-write.

    Raises RegistryUnreadable when the file exists but cannot be trusted:
    unreadable, not JSON, or a document whose top level or `.sessions` is
    not an object. A MISSING file is not an error -- that is a first boot,
    and creating the registry is exactly what an add does.

    Entries are returned VERBATIM, including any whose value is not a dict.
    read_sessions drops those for display, but dropping them here would make
    the next write delete them, which is the same data loss in a smaller
    place; and refusing the whole write over one odd entry would leave the
    web UI permanently unable to add or delete a session, with no repair
    path that does not need a shell.

    `version` is whatever the file carried, so write_sessions can put the
    same value back instead of stamping v1 over a document a newer box
    wrote.
    """
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, REGISTRY_VERSION
    except OSError as exc:
        raise RegistryUnreadable(
            "cannot read %s: %s" % (SESSIONS_FILE, exc.strerror or exc))
    except ValueError as exc:
        raise RegistryUnreadable(
            "%s is not valid JSON: %s" % (SESSIONS_FILE, exc))
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        raise RegistryUnreadable(
            '%s carries no {"sessions": {...}} object' % SESSIONS_FILE)
    return dict(data["sessions"]), data.get("version", REGISTRY_VERSION)


def read_sessions():
    """Return the sessions dict from SESSIONS_FILE for DISPLAY ({} on any
    problem), with entries that are not objects dropped.

    Here "the registry does not parse" and "there are no sessions" really
    are the same answer, and deliberately so: a page that renders an empty
    list beats a 500 on every route that merely mentions a session, and the
    supervisor moves a corrupt registry aside within a tick or two, so the
    empty list is transient. A route that WRITES must use load_sessions
    instead (issue #279) -- publishing this answer back is what deleted the
    other sessions.

    Callers that render or publish names filter through SESSION_RE
    themselves.
    """
    try:
        sessions, _ = load_sessions()
    except RegistryUnreadable:
        return {}
    return {k: v for k, v in sessions.items() if isinstance(v, dict)}


def gen_session_name(base_name, sessions, cwd=None):
    """Auto-generate a unique session name from base_name and the directory.

    The first session for base_name gets the bare name ("claude"); a later
    one that would collide is named after the directory it works in
    ("portal", then "portal-2"). Users rarely care what a session is called
    (rename at runtime via /rename), so this spares them inventing one — but
    the name is also all a row shows about WHICH session it is, and a random
    "claude-a3f9" said nothing, so two auto-named sessions under one project
    tree were indistinguishable and the wrong transcript got downloaded
    (issue #277).

    `base_name` is the profile name when the session was started with one,
    else the agent — the caller (the /sessions/add handler) resolves that,
    since only it knows whether a profile was given. Either way it is
    already one of AGENTS/"shell" or a name PROFILE_NAME_RE has passed, so
    it already matches SESSION_RE; `cwd` is None for home (whose basename is
    the user's own name and names nothing) or an absolute path this daemon
    has resolved. Keeping the mirror in session-cli.sh's gen_name in step is
    deliberate: both creation paths must name a session the same way,
    profile included — two profiles on the same harness ("triage" and
    "reviewer", both claude) used to mint "claude" and "claude-2",
    indistinguishable for the reason issue #277 named in the first place.

    RESERVED_NAMES count as taken: the directory branch below would happily
    name a session after ~/settings or ~/ws, and that is precisely the name
    the vhost cannot route to a terminal.
    """
    taken = set(sessions) | RESERVED_NAMES
    if base_name not in taken:
        return base_name
    base = re.sub(r"[^A-Za-z0-9_-]", "-", os.path.basename((cwd or "").rstrip("/")))
    base = base.strip("-")
    if base and len(base) <= NAME_MAX - 2:
        if base not in taken:
            return base
        for suffix in range(2, 10):
            candidate = "%s-%d" % (base, suffix)
            if candidate not in taken:
                return candidate
    # No usable directory name, or nine sessions already work in that one:
    # random cannot collide the way a tenth "-N" guess would.
    for _ in range(1000):
        candidate = "%s-%s" % (base_name, secrets.token_hex(2))
        if candidate not in taken:
            return candidate
    # Astronomically unlikely fallback: a longer token can't be taken.
    return ("%s-%s" % (base_name, secrets.token_hex(8)))[:NAME_MAX]


def write_sessions(sessions, version=REGISTRY_VERSION):
    """Atomically rewrite SESSIONS_FILE (0600) with the given dict.

    Same tempfile-in-directory + os.replace dance as envstore.save. The
    supervisor in the agent unit picks the change up within ~2s.

    `version` is the one load_sessions read back, so a document written by a
    newer box keeps its own version rather than being silently downgraded to
    1 by whichever route the operator happened to press first (issue #279).
    This daemon does not understand a v2 registry, but stamping v1 on it
    tells every other reader that it does.
    """
    directory = os.path.dirname(SESSIONS_FILE) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": version, "sessions": sessions}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, SESSIONS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_harness_session(agent, remote_control):
    """Auto-start one bare session for `agent` the moment its connect card
    signs in (issue #504), so install+login leaves an actual running
    session behind rather than just a signed-in CLI nobody has started yet.

    Skipped if the user already has ANY session on that harness -- a repeat
    sign-in (a token refresh, "Sign in again") must not mint a second one
    every time the card cycles through "connected". No profile, no working
    directory: this is the bare pseudo-profile shape `shell` already has
    (see SHELL_PSEUDO_PROFILE), the one with no model, effort or prompt to
    carry.

    `remote_control` is the one thing that differs by harness. claude's rc
    is a flag on the ordinary TUI (supervisor.sh appends --remote-control),
    so this session is both a normal worker AND immediately visible to
    desktop/mobile. codex's rc replaces the process outright with the
    app-server pairing daemon (codex-remote-control.sh) -- pairing already
    happened in the connect card's own pane, so what closes issue #504's
    "a codex session running" is a real interactive session, which only
    remoteControl: false ever produces. Written directly here rather than
    through session-cli.sh/`/sessions/add`, both of which still hardcode
    remoteControl: true unconditionally -- neither writer offers a way to
    ask for false today.
    """
    if agent not in AGENTS:
        return
    try:
        with sessions_lock():
            sessions, version = load_sessions()
            if any(isinstance(s, dict) and s.get("agent") == agent
                   for s in sessions.values()):
                return
            name = gen_session_name(agent, sessions)
            sessions[name] = {
                "agent": agent,
                "skipPermissions": True,
                "remoteControl": remote_control,
                "remoteControlName": None,
                "workingDirectory": None,
                "extraArgs": [],
                "profile": None,
                "initialPrompt": None,
                "resumePrompt": None,
                "boxSessionId": None,
                "hasRun": False,
            }
            write_sessions(sessions, version)
    except (RegistryUnreadable, OSError):
        # Not the connect card's place to surface a broken registry, or a
        # write that failed outright (disk full, a permission problem) --
        # the session list already reports the former loudly (issue #279),
        # and sign-in itself still succeeded either way. The pane is
        # already reaped by the time this runs, so there is no in-flight
        # request to retry against; the next full sign-in cycle tries
        # again the same way a first one would.
        pass


_tmux_error_at = -1e9   # monotonic stamp of the last logged tmux OSError


def tmux(*args):
    """Run a tmux command against the user's own server; None on OSError."""
    env = dict(os.environ)
    if TMUX_TMPDIR:
        env["TMUX_TMPDIR"] = TMUX_TMPDIR
    try:
        return subprocess.run(
            [TMUX_BIN, "-L", TMUX_SOCKET] + list(args),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        # Missing/unrunnable tmux binary must not 500 the request. Rate
        # limited: the live feed's watcher runs this every second while a
        # page is open, and a permanently broken binary would otherwise
        # fill the journal with the same line.
        global _tmux_error_at
        now = time.monotonic()
        if now - _tmux_error_at > 60:
            _tmux_error_at = now
            sys.stderr.write("tmux: %s\n" % exc)
        return None


def live_sessions():
    proc = tmux("list-sessions", "-F", "#S")
    if proc is None or proc.returncode != 0:
        return set()
    return {line for line in proc.stdout.splitlines() if line}


def crashed_status(entry):
    """The exit status a crashed agent left on its registry entry, or None
    when the last pane did not crash.

    `died` is written by the pane epilogue (src/mark-stopped.sh) on a
    non-zero agent exit and cleared by the next spawn, so it describes the
    pane that is up NOW. It matters because tmux cannot: the crash branch
    leaves a post-mortem bash shell in the pane, which `tmux list-sessions`
    reports exactly like a running agent, and so did every surface built on
    it (issue #516).

    Read defensively -- the registry is edited by five programs in three
    languages, and a field this daemon only DISPLAYS must never be able to
    500 the page. A bool True is honoured as 0, this function's "crashed,
    status not recorded" -- the writer never records a 0, because a 0 exit
    is the clean one and takes the other branch -- so an entry written by
    hand still reads as dead rather than as healthy.
    """
    value = entry.get("died")
    if value is True:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def kill_session(name):
    """Kill one tmux session. The supervisor recreates it if it is still
    listed in sessions.json (= restart); delisting first makes it stay
    gone (= destroy)."""
    tmux("kill-session", "-t", "=" + name)


# --- Session transcripts (issue #248) --------------------------------
# Every agent keeps its conversation as a local JSONL file under $HOME, so
# handing one to the user is a file this daemon can already read — it runs as
# the owner, and the transcript is served from where the agent writes it.
# Nothing is copied into ~/downloads (issue #132): that dir is the agent's
# own hand-off drop, and a copy there would be a second, stale transcript
# with a lifetime nobody owns.
#
# What the daemon canNOT do is guess the filename from a launch id alone.
# That id — read from the supervisor's session state, with sessions.json's
# boxSessionId as the migration fallback (issue #282) — names the SEGMENT the
# session was last launched with, and both agents move off it:
#   claude — a clear, a compact or a resume keeps the process but opens a NEW
#     transcript under a new uuid; the SessionStart hook records that live id,
#     keyed by the launch id (issue #223). Follow the record, exactly as the
#     supervisor does when it picks a --resume target.
#   codex — the rollout file is named after codex's own session uuid, which
#     the box never chooses. The supervisor finds it by the "agent-box
#     session <id>" marker it stamps into the kickoff prompt; same marker
#     here.
# So the file offered is the one a respawn would resume. Sessions whose
# transcript cannot be located (a codex session started with no prompt, hence
# no marker; a `shell` session; an agent the box knows no layout for) simply
# have no download button — see transcript_of.
CLAUDE_PROJECTS_DIR = os.path.join(HOME_DIR, ".claude", "projects")
CODEX_SESSIONS_DIR = os.path.join(HOME_DIR, ".codex", "sessions")
LIVE_ID_DIR = os.path.join(
    HOME_DIR, ".local", "state", "agent-box", "live-session-id"
)
SESSION_STATE_DIR = os.path.join(
    HOME_DIR, ".local", "state", "agent-box", "session"
)
# Session ids, as claude's --session-id and the record filenames use them.
# Validated before it reaches a glob pattern or a path join, so it doubles as
# the path-safety check on a value read out of sessions.json.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Codex rollout scan bounds. The marker sits in the first user turn, so the
# head of the file is enough; the file cap keeps one page render off a box
# with months of codex history (newest first, so the cap only ever drops
# rollouts far older than any listed session).
CODEX_HEAD_BYTES = 64 * 1024
CODEX_SCAN_MAX = 400
# The marker as it appears in a rollout: "[agent-box session <uuid>]" inside
# the JSON-encoded kickoff prompt, so the brackets are not worth matching.
CODEX_MARKER_RE = re.compile(rb"agent-box session ([0-9a-fA-F-]{36})")
# Transcript lookups are re-run on every render of the sessions panel, and
# the live feed re-renders it on every session state change. Memoize per box
# id for a few seconds: long enough to collapse a render burst, short enough
# that a segment rotation or a fresh codex rollout shows up while the operator
# is still looking at the page.
TRANSCRIPT_TTL = 5.0
_transcript_cache = {}
_codex_index = (-1e9, {})


def claude_transcript(box_id):
    """Path of the transcript claude would resume for this launch id, or
    None. The live-id record is followed FIRST (one hop, like the
    supervisor): after a rotation — a clear, a compact or a resume — the
    launch id's transcript still exists, but it is the segment the session
    moved off."""
    ids = []
    live = read_live_id(box_id)
    if live and live != box_id:
        ids.append(live)
    ids.append(box_id)
    for sid in ids:
        # <projects>/<mangled cwd>/<id>.jsonl. The mangling is claude's, so
        # match on the id rather than recomputing the directory name; one
        # extra level covers a deeper layout (the supervisor's find allows
        # the same). The id is UUID-checked, so it carries no glob syntax.
        hits = glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", sid + ".jsonl"))
        hits += glob.glob(
            os.path.join(CLAUDE_PROJECTS_DIR, "*", "*", sid + ".jsonl")
        )
        newest = newest_file(hits)
        if newest:
            return newest
    return None


def codex_index():
    """box session id -> the codex rollout carrying its marker, for every
    rollout in the scan window.

    Built in ONE pass and shared by every codex session on the page: asking
    per session would re-read the same file heads once per row, and the
    expensive case is the session with NO rollout, which can only be
    answered by reading all of them. Newest first with setdefault, so a
    resumed session — which forks the rollout under a new name, keeping the
    marker — resolves to the fork still being written (the rule
    codex_rollout_uuid in the supervisor resumes by)."""
    global _codex_index
    now = time.monotonic()
    if now - _codex_index[0] < TRANSCRIPT_TTL:
        return _codex_index[1]
    index = {}
    for path in newest_first(CODEX_SESSIONS_DIR)[:CODEX_SCAN_MAX]:
        try:
            with open(path, "rb") as handle:
                head = handle.read(CODEX_HEAD_BYTES)
        except OSError:
            continue
        for match in CODEX_MARKER_RE.finditer(head):
            # Lower-cased on both sides: the marker carries whatever
            # sessions.json holds, and a UUID compares case-insensitively.
            index.setdefault(match.group(1).decode("ascii").lower(), path)
    _codex_index = (now, index)
    return index


def session_state_path(name):
    """Path of one session's supervisor-owned state file (issue #282), or
    None for a name this daemon will not touch.

    The one place this daemon spells that path — the supervisor's
    session_state_file and `agent-box-session`'s accessor of the same name
    are the other two. The key is the session NAME for now, which is a label
    and not an identity (it is meant to become editable, and it is minted
    from a file that can be stale); the key it should grow into is one a
    harness mints (issue #284). Routing every reader and writer through here
    is what keeps that re-key to one function per program.

    SESSION_RE is the path-safety check as well as the naming rule: the name
    reaches a path join, and callers take it from sessions.json."""
    return (
        os.path.join(SESSION_STATE_DIR, name + ".json")
        if SESSION_RE.match(name or "")
        else None
    )


def read_launch_id(name):
    """The id this session was last LAUNCHED with, per the supervisor's own
    record, or None when it has never recorded one.

    Attempt the read and handle the miss: the file is absent for a session
    that has not spawned yet, and for one that last spawned before #282
    shipped — the caller falls back to the registry copy for both."""
    path = session_state_path(name)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("launchSessionId")
    except (OSError, ValueError, AttributeError):
        return None
    value = value if isinstance(value, str) else ""
    return value if UUID_RE.match(value) else None


def read_live_id(box_id):
    """The live session id the SessionStart hook recorded for this launch
    id, or None. Both ids are UUID-checked: box_id names the record file,
    and the value read back names a transcript."""
    if not UUID_RE.match(box_id or ""):
        return None
    try:
        with open(os.path.join(LIVE_ID_DIR, box_id), "r", encoding="utf-8") as fh:
            value = fh.read(64).strip()
    except OSError:
        return None
    return value if UUID_RE.match(value) else None


def newest_file(paths):
    """The most recently modified of `paths`, skipping what cannot be
    stat'ed (a transcript deleted between glob and stat)."""
    best = None
    best_mtime = None
    for path in paths:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def newest_first(root):
    """Every .jsonl under `root`, most recently modified first. Codex nests
    its rollouts by date (sessions/YYYY/MM/DD/rollout-*.jsonl), so this
    walks rather than globbing a fixed depth."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                found.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [path for _mtime, path in found]


def transcript_of(name, entry):
    """(path, size) of one session's transcript, or None when there is
    none to offer. `name` and `entry` are its key and record in
    sessions.json — the caller has already name-checked the session, and
    nothing here comes from the request.

    The launch id comes from the supervisor's own state file, falling back
    to the registry copy for a session that last spawned before #282 (and
    for the id `agent-box-session add` mints up front)."""
    box_id = read_launch_id(name) or str((entry or {}).get("boxSessionId") or "")
    if not UUID_RE.match(box_id):
        # Never spawned, or a hand-written record: no id, so no transcript
        # can be attributed to this session (and guessing would hand over
        # a sibling's conversation).
        return None
    agent = str((entry or {}).get("agent") or "")
    now = time.monotonic()
    cached = _transcript_cache.get(box_id)
    if cached and now - cached[0] < TRANSCRIPT_TTL:
        # A cache HIT does not refresh the stamp: renders can arrive faster
        # than the TTL (the live feed re-renders on every state change), and
        # a stamp pushed forward by each of them would pin the first answer
        # for as long as the page stays open.
        path = cached[1]
    else:
        if agent == "claude":
            path = claude_transcript(box_id)
        elif agent == "codex":
            path = codex_index().get(box_id.lower())
        else:
            # shell sessions have no conversation, and an agent whose
            # on-disk layout the box does not know is not worth a guess
            # (issue #80 adds one when opencode support lands).
            path = None
        if len(_transcript_cache) > 256:
            # Sessions come and go (dispatched hook-* sessions especially),
            # so the keyspace grows for the life of the daemon. Nothing here
            # is worth an eviction policy: drop the lot and re-resolve.
            _transcript_cache.clear()
        _transcript_cache[box_id] = (now, path)
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return path, info.st_size, info.st_mtime


def human_size(size):
    """Byte count for a tooltip: whole KB/MB, since the point is only
    whether this is a short conversation or a long one."""
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%d KB" % round(size / 1024)
    return "%.1f MB" % (size / (1024 * 1024))


# --- What conversation a row holds (issue #277) -----------------------
# A row showed four things: name, agent, working directory and state. None of
# them says what the conversation IS, so two claude rows under one project
# tree read identically — and gen_name mints names like "claude-5109" that
# mean nothing on their own. An operator downloaded one session's transcript
# while wanting another's. The file the row already resolves for its size
# answers it: what the conversation was asked first.
TOPIC_SCAN_BYTES = 256 * 1024   # a long first tool result can precede turn one
TOPIC_MAX = 72                  # a row is one line, not a paragraph
_topic_cache = {}


def elide(text, limit):
    """`text` cut to `limit`, on a word boundary when there is one, with an
    ellipsis so a truncated prompt does not read as a complete one."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "\u2026"


def transcript_topic(path, size):
    """The opening user turn of a transcript, or "" when it has none yet.

    A found topic is cached for the life of the daemon: a transcript only ever
    grows, and its FIRST turn cannot change. NOT finding one is cached against
    the size instead, so the answer refreshes as the file grows (a session that
    has not been asked anything yet, or a codex rollout, whose records this
    does not model) without re-reading it on every render — and the live feed
    re-renders the panel on every session state change.
    """
    cached = _topic_cache.get(path)
    if cached and (cached[1] or cached[0] == size):
        return cached[1]
    try:
        with open(path, "rb") as handle:
            head = handle.read(TOPIC_SCAN_BYTES)
    except OSError:
        return ""
    topic = ""
    for line in head.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            # The agent can be mid-write on the last line, and a codex rollout
            # carries records this does not model. Neither is worth a label.
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if record.get("type") != "user" and message.get("role") != "user":
            continue
        body = message.get("content")
        if isinstance(body, list):
            # Content blocks: keep the text ones, drop images and tool results.
            body = " ".join(
                part.get("text", "") for part in body if isinstance(part, dict)
            )
        if not isinstance(body, str):
            continue
        text = " ".join(body.split())
        # A clear opens the next segment with the slash command itself, wrapped
        # in the local-command caveat; the operator's own prompt follows it.
        if not text or "<local-command" in text or "<command-" in text:
            continue
        topic = elide(text, TOPIC_MAX)
        break
    if len(_topic_cache) > 256:
        # Same reasoning as _transcript_cache: the keyspace grows with every
        # session that ever ran, and nothing here is worth an eviction policy.
        _topic_cache.clear()
    _topic_cache[path] = (size, topic)
    return topic


def when_written(mtime):
    """Local "MM-DD HH:MM" for a tooltip. The operator is choosing between
    conversations from this week, so the day matters and the year does not."""
    return time.strftime("%m-%d %H:%M", time.localtime(mtime))


def download_name(session, path):
    """Filename the browser saves the transcript as: the SESSION's name
    first (the only handle the operator recognises), then the agent's own
    filename, whose id is what a bug report or a resume needs. Filtered to
    a conservative charset so the value cannot break out of the
    Content-Disposition quoting."""
    stem = os.path.basename(path)
    name = "%s-%s" % (session, stem)
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:200]


# --- Webhook subscriptions (issue #227) ------------------------------
# Which webhooks are live, for which session, was readable only through
# the MCP tools or the CLI — i.e. only from inside a session, and only
# for THAT session. This is the same view for the operator, plus the
# delete that a flood makes urgent.
#
# local-webhook scopes itself by $LOCAL_WEBHOOK_SESSION, and that is an
# input rather than an identity: setting it per invocation is how one
# process reads or edits another session's subscriptions. The daemon
# runs as the user who owns those files, so no privilege is involved.
#
# Everything goes through the script's own CLI. The filter format is not
# this daemon's contract to keep: TTL stamps, the atomic replace and the
# in-process lock all live upstream, and a second writer here would be a
# second implementation of them.
_webhook_error_at = -1e9   # monotonic stamp of the last logged CLI failure


def webhook_key(name):
    """$LOCAL_WEBHOOK_SESSION for one session — what the supervisor puts
    in that session's tmux environment, so it names the same filter file
    the session itself writes. `name` empty means "no session": the user
    key, which owns no session filter and is used only to read the shared
    dispatch list."""
    return ("%s-%s" % (USER, name)) if name else USER


def webhook_state_dir():
    """Where the filter files live. AGENT_BOX_WEBHOOK_STATE_DIR is the
    module's answer and is set whenever a receiver exists; the rest is for
    a dev run, or for a box whose receiver was turned off after sessions
    had already left files behind."""
    return (
        WEBHOOK_STATE_DIR
        or os.environ.get("LOCAL_WEBHOOK_STATE_DIR")
        or os.path.join(HOME_DIR, ".local", "state", "local-webhook")
    )


def webhook_sources():
    """Which senders are set up: (default source, sorted names).

    Read straight from the receiver's sources.json rather than through a
    CLI, because this is not webhook.py's subscription format — it is the
    small file `agent-box-webhook setup` writes, and the only thing wanted
    here is which per-source paths exist to paste into a sender. The
    secret is deliberately not read: it lives in its own 0600 file, it is
    printed once by `setup`, and a page that echoed it would turn a
    browser tab into a place credentials leak from.

    No file (or an unreadable one) means no secret has been minted yet,
    which is exactly the state in which the endpoint rejects everything.
    """
    try:
        with open(os.path.join(webhook_state_dir(), "sources.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "", []
    if not isinstance(data, dict):
        return "", []
    sources = data.get("sources")
    names = sorted(sources) if isinstance(sources, dict) else []
    default = data.get("defaultSource")
    return (default if isinstance(default, str) else ""), names


def webhook_secret(source):
    """One source's HMAC secret, or "" if there is none to read.

    Same resolution as the CLI's own `secret_of`: `secretFile` (relative
    names are inside the state dir), else an inline `secret`. Duplicated
    here rather than shelled out to because `agent-box-webhook` is not on
    this daemon's PATH — it is the agent's CLI, in the agent's profile,
    and putting it there to read one file would widen what the web
    daemon can run for no gain.

    Why the page serves this at all: registering a webhook needs the URL
    AND the secret, and the operator doing it has a browser, not a shell.
    It is no new exposure — the same login already opens a terminal on
    this box, where the file is one `cat` away, and `agent-box-webhook
    setup` re-prints it on demand. It is fetched on click and never
    rendered into the page, so it stays out of screenshots, out of the
    live feed's DOM swaps, and out of the HTML the browser keeps.
    """
    if not source:
        return ""
    state = webhook_state_dir()
    try:
        with open(os.path.join(state, "sources.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        entry = (data.get("sources") or {}).get(source) or {}
    except (OSError, ValueError, AttributeError):
        return ""
    if not isinstance(entry, dict):
        return ""
    path = entry.get("secretFile")
    if isinstance(path, str) and path:
        if not os.path.isabs(path):
            path = os.path.join(state, path)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    inline = entry.get("secret")
    return inline.strip() if isinstance(inline, str) else ""


def webhook_secret_path(source):
    """The file a source's secret lives in, or "" if the page must not
    write it.

    Only the shape `agent-box-webhook setup` produces is rotatable from
    here: an entry with a `secretFile` inside the state dir, and no inline
    `secret` (which the receiver prefers over the file, so rotating the
    file under one would change nothing and claim it had). Anything else
    is a hand-written source, and editing sources.json is the CLI's job —
    it owns that format, with jq, in one place. The page refuses and says
    which command to run instead.
    """
    if not SOURCE_RE.match(source or ""):
        return ""
    state = webhook_state_dir()
    try:
        with open(os.path.join(state, "sources.json"), encoding="utf-8") as fh:
            entry = ((json.load(fh).get("sources") or {}).get(source) or {})
    except (OSError, ValueError, AttributeError):
        return ""
    if not isinstance(entry, dict) or entry.get("secret"):
        return ""
    path = entry.get("secretFile")
    if not isinstance(path, str) or not path:
        return ""
    full = os.path.realpath(path if os.path.isabs(path)
                            else os.path.join(state, path))
    # The path comes out of a file the user owns, so this is not a
    # privilege boundary — it is a bound on what a web POST can overwrite:
    # this source's own secret inside the state dir, nothing else.
    if os.path.dirname(full) != os.path.realpath(state):
        return ""
    return full


def webhook_rotate(source):
    """Mint a new secret for one source. True if it was replaced.

    Same 32 hex characters `agent-box-webhook setup` mints, from the same
    kernel CSPRNG, written 0600 through a tempfile in the same directory
    so a delivery mid-read never sees half a secret.

    A HARD cutover, deliberately: the receiver verifies against exactly
    one secret per source, so a delivery still signed with the old value
    is rejected from here on, and GitHub does not retry — the banner and
    the button's confirm say so, because an overlap is not ours to
    invent (defangdevs/local-channels#49).
    """
    path = webhook_secret_path(source)
    if not path:
        return False
    secret = secrets.token_hex(16)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".secret.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secret)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except OSError as exc:
        sys.stderr.write("webhook: rotate %s: %s\n" % (source, exc))
        return False
    return True


def prune_filter(name):
    """Drop one session's webhook filter file — the same cleanup
    'agent-box-session rm' does (#229). webhook.py reads
    filter.<LOCAL_WEBHOOK_SESSION>.json and the supervisor sets that to
    "<user>-<session>". True when a file was there to remove.

    Two callers, and they mean different things by it. Deleting a session
    prunes the file it leaves behind, which would otherwise go on claiming
    its topics. The panel's Unsubscribe all / Clear removes the file of a
    session that is still listed, unsubscribing it from everything at once:
    the only reachable cleanup for a filter that is broken, muted, or
    merely empty, since the CLI's verbs all take a topic that a file in
    those states does not have. Safe since local-webhook 0.13.0 — a
    session with no filter file receives nothing, so the worst case is
    subscribing again."""
    if not SESSION_RE.match(name):
        return False
    try:
        os.remove(os.path.join(webhook_state_dir(), "filter.%s.json" % webhook_key(name)))
    except OSError:
        return False   # never existed (never subscribed), or already gone
    return True


def webhook_cli(key, args):
    """Run the pinned webhook.py CLI scoped to one session key.

    Returns the completed process, or None if it could not run. PORT=0
    keeps this a pure state-file client: a CLI invocation must never bind
    the ingress the receiver daemon owns.
    """
    env = dict(os.environ)
    env["LOCAL_WEBHOOK_STATE_DIR"] = WEBHOOK_STATE_DIR
    env["LOCAL_WEBHOOK_SESSION"] = key
    env["LOCAL_WEBHOOK_PORT"] = "0"
    try:
        return subprocess.run(
            [WEBHOOK_PYTHON, WEBHOOK_SCRIPT] + list(args),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Same rate limit as tmux(): a page render forks one of these per
        # session, so a permanently broken pin must not fill the journal.
        global _webhook_error_at
        now = time.monotonic()
        if now - _webhook_error_at > 60:
            _webhook_error_at = now
            sys.stderr.write("webhook: %s\n" % exc)
        return None


def webhook_subscriptions(key):
    """The `subscriptions` view for one session key ({} on any problem).

    Carries the session's own topics plus the shared dispatch list, each
    entry already rendered with its note and expiry, plus filterState:
    why the topic list looks the way it does (local-webhook 0.13.0).
    """
    proc = webhook_cli(key, ["subscriptions"])
    if proc is None or proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def webhook_unsubscribe(key, topic, dispatch):
    """Drop one topic. True when the CLI reported success."""
    args = ["unsubscribe", topic]
    if dispatch:
        args += ["--deliver-to", "subagent"]
    proc = webhook_cli(key, args)
    return proc is not None and proc.returncode == 0


def hook_args_stamp():
    """(mtime_ns, size) of the env file, the profiles directory and the
    dispatch filter file.

    Part of hook_preamble's cache key. The dispatch script reports the
    hook-session arguments that file can override (#292), so its output is
    no longer a function of (topic, note) alone: an `agent-box-session env
    set` between two renders changes which model the page should show, and
    a cached answer would keep naming the old one for the life of the
    daemon. One stat per render is cheap; the fork it guards is not.

    The profiles directory is stamped for the same reason (#321): the report
    names the agent profile AGENT_BOX_HOOK_PROFILE picks, and whether that
    profile still exists decides whether the watch uses it at all. Creating
    or removing one does not touch the env file, and every write goes through
    a rename inside this directory, so its mtime moves on both.

    The dispatch filter file joins them because a watch can now name its own
    profile (spawnConfig.profile, issue #321): the report reads that file, so
    re-subscribing a watch with a different --profile has to re-render. Every
    write to it is an atomic rename, so the mtime moves.
    """
    stamps = []
    for path in (ENV_FILE, PROFILES_DIR, os.path.join(webhook_state_dir(), "filter.dispatch.json")):
        try:
            info = os.stat(path)
        except OSError:
            stamps.append(None)
        else:
            stamps.append((info.st_mtime_ns, info.st_size))
    return tuple(stamps)


@functools.lru_cache(maxsize=64)
def hook_preamble(topic, note, stamp):
    """What a match on this standing watch launches: the launch command,
    then the prompt the new session is given.

    Rendered by the dispatch script itself (--preamble), for the same
    reason the panel shells out to webhook.py for everything else: the
    text belongs to the thing that sends it. A copy here would drift, and
    a prompt the box no longer sends is worse than no prompt at all.

    `stamp` is hook_args_stamp() — not read here, only keyed on, so a
    changed env file renders fresh instead of serving the old model. For a
    given (topic, note, stamp) the answer is deterministic, so the
    per-second live feed re-render still costs one fork per watch per
    edit. "" when the module wired no command or the script failed — the
    caller then falls back to printing the note.
    """
    if not HOOK_SPAWN_CMD:
        return ""
    try:
        proc = subprocess.run(
            [HOOK_SPAWN_CMD, "--preamble", topic, note],
            # The script reads the dispatch file to find THIS watch's own
            # profile (#321); under socket activation nothing else puts the
            # state dir in this daemon's environment.
            env=dict(os.environ, LOCAL_WEBHOOK_STATE_DIR=webhook_state_dir()),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("webhook: preamble: %s\n" % exc)
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def webhook_entries(data, dispatch):
    """The topic entries of a `subscriptions` payload, session or
    dispatch side, as a list of dicts."""
    block = data.get("dispatch") if dispatch else data
    if not isinstance(block, dict):
        return []
    topics = block.get("topics")
    return [t for t in topics if isinstance(t, dict)] if isinstance(topics, list) else []


def webhook_view():
    """What the page renders: each session's own subscriptions, keyed by
    session name so the Sessions panel can fold them into that session's
    row, plus the standing watches, which belong to no session and get a
    panel of their own.

    A session with no topics receives nothing, whatever put it in that
    state — since local-webhook 0.13.0 there is no longer a way to be
    subscribed to everything by accident. What still differs is WHY, and
    an operator reading the row wants that:
      listening  topics, listed
      empty      subscribed once, then unsubscribed from everything
      absent     no filter file — this session has never subscribed
      invalid    a filter file that does not parse (a botched edit)
      off        enabled:false, so nothing is delivered whatever it lists
    """
    names = sorted(n for n in read_sessions() if SESSION_RE.match(n))
    sessions = {}
    watches = []
    seen_dispatch = False
    for name in names:
        data = webhook_subscriptions(webhook_key(name))
        if not seen_dispatch and data:
            watches = webhook_entries(data, dispatch=True)
            seen_dispatch = True
        topics = webhook_entries(data, dispatch=False)
        if not data:
            state = "unknown"
        elif data.get("enabled") is False:
            # The muted-everything flag. No tool writes it yet
            # (local-channels#23), but the fan-out honours it, so a
            # session carrying it is not listening whatever it lists.
            state = "off"
        elif topics:
            state = "listening"
        else:
            # Empty for one of several reasons; take upstream's own word for
            # which rather than restating the filter format here. "ok" means
            # a real, parseable topic list that is simply empty, and
            # "unconfigured" a file with no topics key at all — nothing is
            # delivered either way, so they share a row label.
            state = str(data.get("filterState") or "")
            state = state if state in ("absent", "invalid") else "empty"
        sessions[name] = {
            "name": name,
            "key": webhook_key(name),
            "state": state,
            "topics": topics,
            # Is there a filter file to remove? Every state but "absent"
            # has one; "unknown" means the CLI did not answer, so nothing
            # here can be said about it either way.
            "file": state not in ("absent", "unknown"),
        }
    if not seen_dispatch:
        # No sessions, or none answered: the standing watches are shared
        # and outlive every session, so read them under the user key.
        watches = webhook_entries(webhook_subscriptions(webhook_key("")), dispatch=True)
    return sessions, watches


# --- Guided sign-in (issues #207, #208, #313) ------------------------
# Onboarding used to mean finding the right terminal tab, reading a
# wrapped OSC-8 link out of a TUI, and pasting a code into a prompt that
# echoes nothing — and, for GitHub, leaving the box entirely to mint a
# token by hand. These cards remove that walk WITHOUT reimplementing any
# of it: every flow here is the vendor's own sign-in command
# (`claude auth login`, `codex login --device-auth`, `gh auth login
# --web`), run in a tmux session the user never has to find.
#
# The daemon reads the URL off that pane, renders it as a real link,
# types the code back in, and then asks the CLI ITSELF whether it is
# signed in (`claude auth status` answers JSON; the others answer a
# line). So no OAuth endpoint, client id, PKCE verifier or token ever
# lives here: the credential is written by the CLI to ~/.claude,
# ~/.codex or ~/.config/gh, exactly as it would have been from the
# terminal. Setting GH_TOKEN or ANTHROPIC_API_KEY by hand under
# Environment secrets keeps working, and keeps winning — every one of
# these CLIs prefers its environment variable over its stored
# credential, so the manual path stays the override it always was.
#
# Nothing in here is persisted by the daemon: the tmux session IS the
# state. That is why a card can be reconstructed after a reload, a
# daemon restart or from a second browser tab.
CONNECT_PREFIX = "_connect-"
# A pane this wide keeps a sign-in URL on ONE unwrapped line, so
# capture-pane reads it whole (the wrapped-URL problem the README
# documents for the terminal flow simply cannot happen here).
CONNECT_COLS = 512
CONNECT_ROWS = 40
# Device/user codes live 15 minutes; an abandoned pane is reaped at the
# same age rather than lingering with a code that can no longer work.
CONNECT_TTL = 900
# How long the pane stays after the CLI exits: its last screen is the
# only diagnostic a failed flow has.
CONNECT_LINGER = 60
# Claude Code's prompt treats one large stdin chunk as a PASTE and
# absorbs a trailing carriage return, so a real ~100-character code
# never submits when Enter rides along in the same write. Send Enter as
# a separate keypress after a settle delay (this exact failure is
# documented in raphaeltm/simple-agent-manager's setup-token driver).
CONNECT_ENTER_DELAY = 1.0
CONNECT_STATUS_TTL = 5.0
CONNECT_EXIT_RE = re.compile(r"\[agent-box\] exit=(\d+)")
CONNECT_URL_RE = re.compile(r"https://[^\s<>'\"`]+")
# The one-time code a device flow displays IN the pane (gh, codex).
# Claude's flow shows no code here — the user copies it from the browser.
#
# Matched by SHAPE, not by proximity to the word "code": hyphen-joined
# groups of upper-case alphanumerics, which is what both real CLIs print
# ("EC7A-D4B8" for gh, "56D0-6G7MP" for codex). Anchoring on the word
# instead read the codex card's prose back at the user — its first line
# is "…sign in with ChatGPT using device code authorization:", so
# "code authorization" rendered as the code AUTHORIZATION, while the real
# code, printed two lines further down under "Enter this one-time code",
# was never matched at all. Case-sensitive, and a hyphen is required, so
# ordinary words cannot pose as a code.
CONNECT_CODE_RE = re.compile(r"\b([0-9A-Z]{3,8}(?:-[0-9A-Z]{3,8}){1,3})\b")
# Upper-case hyphenated words these CLIs print that are NOT codes.
# Rendering one as if it were would send the user to the device page with
# a word to type.
CONNECT_PROSE = frozenset(("HERE", "NONE", "NULL", "TRUE", "FALSE",
                           "PROMPTED", "ABOVE", "BELOW", "ONE-TIME",
                           "SIGN-IN", "DEVICE-AUTH", "READ-ONLY"))
# What the user may paste back. Printable, no whitespace, bounded: the
# value is typed into a tmux pane as literal keys, never into a shell.
CONNECT_CODE_MAX = 512
CONNECT_CODE_OK = re.compile(r"^[\x21-\x7e]{4,%d}$" % CONNECT_CODE_MAX)
# Redaction for the one place pane text reaches the browser (the error
# line of a failed flow). These CLIs do not print credentials on the
# paths we drive, so this is a belt-and-braces guard, not the plan.
CONNECT_SECRET_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9._-]+|gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)"
)
CONNECT_ERROR_MAX = 240
# Pane lines that are never the diagnosis: the instructions, and the echo
# of the prompt the code was typed into (which carries the code itself —
# the user's own value, but not something to render back at them).
CONNECT_NOISE_RE = re.compile(
    r"(paste code|browser did|opening browser|press enter|first copy|"
    r"visit:|sign in)", re.IGNORECASE
)
# What a CLI's own complaint looks like, so the tail names the cause
# instead of whatever happened to be printed last.
CONNECT_BLAME_RE = re.compile(
    r"(error|failed|failure|denied|invalid|expired|refus|unable|cannot)",
    re.IGNORECASE
)

# Which CLI each card drives, as "<id>=<absolute binary>" pairs
# (AGENT_BOX_CONNECT_BINS, same convention as AGENT_BOX_AGENT_BINS).
# Absolute paths, not $PATH: this daemon's unit deliberately carries no
# agent PATH, and naming the binary also lets a VM test point an id at a
# stub. A flow whose id is absent here has no card.
# Where a card fetches its CLI from when the box does not ship it (issue
# #416) — the same pinned source the supervisor's lazy harness install
# uses, so a box that fetches claude from a session and gh from a card
# lands on one nixpkgs, not two. Empty disables the install offer rather
# than guessing at a channel.
CONNECT_NIXPKGS = os.environ.get("AGENT_BOX_NIXPKGS", "")
# The NixOS module PINS nix as a store path because it has one; a native
# box cannot, since resolving one at apply time would bake the building
# host's store path into a generated file and break on the next nix
# upgrade (bin/agentbox says so where it declines to set this). So the pin
# is optional and connect_nix_bin() resolves it at USE instead, the way
# lib/agents.sh already does for the supervisor's own lazy install.
CONNECT_NIX_PIN = os.environ.get("AGENT_BOX_NIX_BIN") or ""
# Where to look when nothing pinned it. A MULTI-USER nix install puts the
# binary in the default profile, which is on no PATH this daemon or its
# panes carry; a single-user install puts it in the user profile, which is
# on the pane's. Both layouts have to be covered by path, not by PATH.
CONNECT_NIX_FALLBACKS = (
    "/nix/var/nix/profiles/default/bin/nix",
    "~/.nix-profile/bin/nix",
)

CONNECT_BINS = {}
for _pair in os.environ.get("AGENT_BOX_CONNECT_BINS", "").split():
    if "=" in _pair:
        _flow_id, _flow_bin = _pair.split("=", 1)
        if _flow_id and _flow_bin:
            CONNECT_BINS[_flow_id] = _flow_bin

# The OTHER way a card can fetch its CLI: a pinned Nix expression file, as
# "<id>=<absolute path>" pairs (AGENT_BOX_CONNECT_EXPRS, same convention as
# CONNECT_BINS above). For a CLI that is IN nixpkgs, `attr` is the whole
# story and this is empty; this exists for one that is not, which today
# means defang (issue #461) and tomorrow whatever else #449's list grows
# that nixpkgs does not carry.
#
# It is a path rather than an inline expression because both backends
# already have somewhere to put a file that must not drift: the module
# writes it to the store, and the runtime profile ships it under
# share/agent-box/. One file, one set of pins, both backends.
CONNECT_EXPRS = {}
for _pair in os.environ.get("AGENT_BOX_CONNECT_EXPRS", "").split():
    if "=" in _pair:
        _flow_id, _flow_expr = _pair.split("=", 1)
        if _flow_id and _flow_expr:
            CONNECT_EXPRS[_flow_id] = _flow_expr


def parse_claude_status(proc):
    """`claude auth status` answers JSON — the one structured signal in
    the set, so success is never a guess about rendered prose.

    It reports "loggedIn" for an environment key too, and for ANY value of
    one: a stale ANTHROPIC_API_KEY answers `{"loggedIn": true,
    "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}` without
    the key ever being tried. The card cannot validate it — nothing local
    can — so it names the source instead of implying a checked account.
    """
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return (False, "")
    if not isinstance(data, dict) or not data.get("loggedIn"):
        return (False, "")
    source = str(data.get("apiKeySource") or "")
    if source and source != "none":
        return (True, "using " + source)
    who = str(data.get("email")
              or data.get("orgName")
              or data.get("authMethod")
              or "signed in")
    plan = str(data.get("subscriptionType") or "")
    return (True, "%s (%s)" % (who, plan) if plan else who)


def connect_detail(line):
    """The CLI's own status line, minus the "logged in" it opens with:
    the pill already says "Signed in", so repeating it there reads as
    "Signed in — Logged in to github.com …"."""
    text = line.strip()
    for prefix in ("logged in to ", "logged in as ", "logged in "):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text


def parse_codex_status(proc):
    """`codex login status` prints e.g. "Logged in using ChatGPT".

    Deliberately reported as "signed in locally": it is a LOCAL check, so
    it keeps saying that for credentials the backend has already
    invalidated (README, Codex sign-in). Pairing is what discovers those.
    """
    lines = [x.strip() for x in ((proc.stdout or "") + (proc.stderr or "")).splitlines()]
    first = next((x for x in lines if x), "")
    low = first.lower()
    if proc.returncode == 0 and "logged in" in low and "not logged in" not in low:
        return (True, connect_detail(first))
    return (False, "")


def parse_gh_status(proc):
    """`gh auth status` prints "Logged in to github.com account <login>
    (<source>)", where <source> names GH_TOKEN when the env store's key
    is what gh is using — which is exactly what the card must say."""
    text = (proc.stdout or "") + (proc.stderr or "")
    for raw in text.splitlines():
        line = raw.strip().lstrip("✓").strip()
        if line.lower().startswith("logged in to"):
            return (True, connect_detail(line))
    return (False, "")


def parse_defang_status(proc):
    """`defang whoami --json` prints the account as JSON on stdout and
    exits 0 when signed in; signed out is a non-zero exit with nothing on
    stdout ("Error: missing bearer token" goes to stderr instead), so the
    exit code is checked before anything is parsed as JSON.

    email/name come from a userinfo fetch the CLI only makes when it thinks
    it has a TTY (auth.go's `global.HasTty` gate), which a piped probe never
    has — so both are commonly absent even while signed in, and the pill
    falls back to the fields that are always there.
    """
    if proc.returncode != 0:
        return (False, "")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return (False, "")
    if not isinstance(data, dict):
        return (False, "")
    who = str(data.get("email") or data.get("name")
              or data.get("workspace") or "signed in")
    tier = str(data.get("subscriberTier") or "")
    return (True, "%s (%s)" % (who, tier) if tier else who)


CONNECT_PARSERS = {
    "claude": parse_claude_status,
    "codex": parse_codex_status,
    "gh": parse_gh_status,
    "defang": parse_defang_status,
}

# One row per flow, in render order. `start` and `status` are argv tails
# appended to the flow's binary; nothing is passed through a shell except
# the pane wrapper built in connect_start (via shlex.quote).
CONNECT_DEFS = [
    {
        "id": "claude",
        "binary": "claude",
        "attr": "claude-code",
        "label": "Claude Code",
        "note": "Sign in with your Claude subscription or Anthropic "
                "Console account.",
        "start": ["auth", "login"],
        "status": ["auth", "status"],
        "parse": "claude",
        "hosts": ("claude.com", "claude.ai", "anthropic.com"),
        "needs_code": True,
        "show_code": False,
        "unset": (),
        "shadow": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
        "prompt_re": None,
        "destructive": False,
    },
    {
        "id": "codex",
        "binary": "codex",
        "attr": "codex",
        "label": "Codex",
        "note": "Sign in with your OpenAI account. Follow the link and "
                "enter the code shown here.",
        "start": ["login", "--device-auth"],
        "status": ["login", "status"],
        "parse": "codex",
        "hosts": ("openai.com", "chatgpt.com"),
        "needs_code": False,
        "show_code": True,
        "unset": (),
        "shadow": ("OPENAI_API_KEY",),
        "prompt_re": None,
        # --device-auth DELETES stored credentials as it starts and does
        # not restore them if the flow is abandoned (README), so signing
        # in again is a destructive act and asks first.
        "destructive": True,
    },
    {
        "id": "github",
        "binary": "gh",
        "attr": "gh",
        "label": "GitHub",
        "note": "Uses GitHub's secure sign-in process. You will not need "
                "to create an app or paste a token.",
        "start": ["auth", "login", "--hostname", "github.com",
                  "--git-protocol", "https", "--web",
                  "--scopes", "repo,read:org,workflow"],
        "status": ["auth", "status", "--hostname", "github.com"],
        "parse": "gh",
        "hosts": ("github.com",),
        "needs_code": False,
        "show_code": True,
        # gh REFUSES to store a credential while GH_TOKEN is set in its
        # environment, so the SIGN-IN pane clears it (a login-time rule
        # only — the status probe above deliberately keeps it, because
        # that is what the sessions actually use).
        "unset": ("GH_TOKEN", "GITHUB_TOKEN"),
        "shadow": ("GH_TOKEN", "GITHUB_TOKEN"),
        # gh asks TWO keypress questions, and both have to be answered or
        # the flow wedges. "Authenticate Git with your GitHub
        # credentials?" comes FIRST, before gh contacts GitHub at all, and
        # (contrary to connect_server_path's earlier hope) it is asked
        # whether or not git is on PATH — so matching only "Press Enter"
        # left gh blocked on it, no device code was ever printed, and the
        # card hung at "Starting the sign-in…" (agent-box#400). Enter
        # accepts the default (Yes), which is what signing in via gh
        # wants. "Press Enter to open github.com…" is the second.
        "prompt_re": re.compile(
            r"Authenticate Git with your GitHub credentials|Press Enter",
            re.IGNORECASE),
        "destructive": False,
    },
    {
        "id": "defang",
        "binary": "defang",
        # No nixpkgs attribute: defang is not in nixpkgs. It is fetched from
        # a pinned expression instead, named by AGENT_BOX_CONNECT_EXPRS, so
        # this card is installable on both backends (issue #461).
        "attr": None,
        "label": "Defang Deploy",
        "note": "Opens Defang's secure sign-in page in your browser.",
        "start": ["login", "--non-interactive=false"],
        "status": ["whoami", "--json"],
        "parse": "defang",
        "hosts": ("defang.io",),
        # The CLI polls the auth server on its own (auth.go's
        # StartAuthCodeFlow) until the browser tab approves it — unlike
        # claude, nothing is ever shown for the user to type back here.
        "needs_code": False,
        "show_code": False,
        "unset": ("DEFANG_ACCESS_TOKEN",),
        "shadow": ("DEFANG_ACCESS_TOKEN",),
        "prompt_re": None,
        "destructive": False,
    },
]

_connect_status_cache = {}
_connect_probing = set()
_connect_lock = threading.Lock()


def connect_flows():
    """Every card this box can show, installed or not (issue #416).

    A card used to exist only where its binary did, which made the lazy
    box a chicken-and-egg: with the CLIs out of the closure there was no
    GitHub card to press, and pressing it is how you would get gh. So the
    card list is CONNECT_DEFS, and the binary is per-card STATE.

    Resolution order mirrors the supervisor's agent_bin: the eval-time
    table first (an eagerly installed CLI keeps its pinned store path),
    then this user's own profile, which is where a lazy install lands and
    is already first on a session's PATH. flow["bin"] is None when
    neither has it — the card then offers to install it.

    Mirroring agent_bin includes its existence check, which matters more
    now than it reads. A table entry can name a binary that is not there:
    the native backend builds the table from a fixed layout
    ("<profile>/bin/claude") whether or not the profile was built with that
    harness, and the module names defang at the path its BACKGROUND unit
    will eventually install it to (issue #373). Taking such an entry at its
    word reported "Signed in?"-able state for a CLI that cannot be
    executed, and shadowed the profile copy a lazy install had just put
    down.
    """
    flows = []
    for spec in CONNECT_DEFS:
        flow = dict(spec)
        # Where this card would fetch the CLI from, if it is missing: a
        # nixpkgs attribute, or a pinned expression file for a CLI nixpkgs
        # does not carry (issue #461). Either one makes the card live.
        flow["expr"] = CONNECT_EXPRS.get(spec["id"])
        fetchable = bool(spec["attr"] or flow["expr"])
        binary = CONNECT_BINS.get(spec["id"])
        if binary and not os.access(binary, os.X_OK):
            binary = None
        if not binary and fetchable:
            candidate = os.path.join(
                os.path.expanduser("~"), ".nix-profile", "bin", spec["binary"])
            if os.access(candidate, os.X_OK):
                binary = candidate
        flow["bin"] = binary
        # Never a DEAD card: one whose CLI is absent and which cannot be
        # installed from here has no button and nothing to say, which is
        # worse than not being there (the reasoning agentbox already
        # applied when it left defang out of a native box's cards). A card
        # therefore appears when the CLI is here, OR when pressing it
        # would get it — from nixpkgs, or from a pinned expression.
        if binary or fetchable:
            flows.append(flow)
    return flows


def connect_flow(flow_id):
    for flow in connect_flows():
        if flow["id"] == flow_id:
            return flow
    return None


def connect_pane(flow_id):
    return CONNECT_PREFIX + flow_id


def connect_target(flow_id):
    """The flow's pane, as a tmux TARGET-PANE.

    The trailing colon is load-bearing: `=name` is an exact SESSION
    target, and capture-pane/send-keys resolve a pane, so they answer
    "can't find pane" for it. `=name:` is that session's active pane,
    which is the only pane these sessions ever have.
    """
    return "=" + connect_pane(flow_id) + ":"


def connect_status_env(flow):
    """The environment a SESSION would give this CLI.

    The card answers "are my sessions signed in?", and for these CLIs an
    environment variable answers that before any stored credential does.
    This daemon's unit does not load the env store (the supervisor's spawn
    wrapper does, per session), so a probe run with our own environment
    would report "not signed in" on a box whose sessions are perfectly
    authenticated through a hand-set GH_TOKEN. Lift exactly the keys this
    flow cares about out of the store, so the pill matches what the
    terminal would say. No value is ever rendered.
    """
    env = dict(os.environ)
    stored = as_dict(load(ENV_FILE))
    for key in flow["shadow"]:
        if key in stored:
            env[key] = stored[key]
    return env


def connect_run(flow, args, timeout=15):
    """Run one of the flow's own subcommands. None on any failure —
    a missing binary or a hung status call must not 500 the page."""
    # Not installed yet (issue #416): there is nothing to ask, and asking
    # would be a TypeError on None rather than the "no" the card wants.
    if not flow["bin"]:
        return None
    try:
        return subprocess.run(
            [flow["bin"]] + list(args),
            env=connect_status_env(flow),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def connect_probe(flow):
    """Ask one CLI whether it is signed in, off the request path."""
    try:
        proc = connect_run(flow, flow["status"])
        value = (False, "") if proc is None else CONNECT_PARSERS[flow["parse"]](proc)
        with _connect_lock:
            _connect_status_cache[flow["id"]] = (time.monotonic(), value)
    finally:
        with _connect_lock:
            _connect_probing.discard(flow["id"])


def connect_expire(flow_id):
    """Mark this flow's cached status stale, keeping the value.

    Used the moment the CLI exits: the cached answer predates the sign-in
    it just finished, so the next read must re-probe. The value is kept so
    the card still has something to render, and the stamp — not the entry —
    is what is dropped, so a probe landing concurrently is never lost.
    """
    with _connect_lock:
        hit = _connect_status_cache.get(flow_id)
        if hit:
            _connect_status_cache[flow_id] = (0.0, hit[1])


def connect_fresh(flow_id):
    """True when this flow's cached status is younger than the TTL."""
    with _connect_lock:
        hit = _connect_status_cache.get(flow_id)
    return bool(hit) and time.monotonic() - hit[0] < CONNECT_STATUS_TTL


def connect_status(flow):
    """Cached (connected, detail), or None while nothing is known yet.

    This NEVER blocks the request. A render asks every card, each answer
    forks a real CLI, and `gh auth status` talks to GitHub — so a slow or
    unreachable network held the whole settings page, past the 10s client
    timeout, for a page that has nothing to do with these cards (caught by
    settings-page.nix, not by connect.nix, which stubs the CLIs). A stale
    answer plus a background refresh is the right trade for a status pill:
    the page polls, and the truth lands a beat later.
    """
    now = time.monotonic()
    with _connect_lock:
        hit = _connect_status_cache.get(flow["id"])
        stale = not hit or now - hit[0] >= CONNECT_STATUS_TTL
        if stale and flow["id"] not in _connect_probing:
            _connect_probing.add(flow["id"])
            threading.Thread(
                target=connect_probe, args=(flow,), daemon=True
            ).start()
    return hit[1] if hit else None


def tmux_sessions():
    """(server_up, {session names}) from ONE `tmux list-sessions`.

    Both facts are needed for every card, and each call is a process:
    "is the server up" is not the same question as "is this pane live",
    but it is the same fork.
    """
    proc = tmux("list-sessions", "-F", "#S")
    if proc is None or proc.returncode != 0:
        return (False, set())
    return (True, {line for line in proc.stdout.splitlines() if line})


def tmux_server_up():
    """True when the user's tmux server is already running.

    connect_start REFUSES to start one. `tmux new-session` starts a
    server when none is running, and that server outlives the request —
    so a pane created with no server would make THIS daemon the parent of
    every session the supervisor later spawns, moving the agents out of
    the hardened agent unit's namespace and cgroup and into the settings
    daemon's (which, unlike theirs, keeps NoNewPrivileges off for its
    sudo'd password helper). The sign-in pane must be a child of the
    agent unit's server or it must not exist.
    """
    return tmux_sessions()[0]


def connect_capture(flow_id):
    """The pane's text, wrapped lines rejoined, scrollback included."""
    proc = tmux("capture-pane", "-p", "-J", "-S", "-200",
                "-t", connect_target(flow_id))
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def connect_age(flow_id):
    proc = tmux("display-message", "-p", "-t", connect_target(flow_id),
                "#{session_created}")
    if proc is None or proc.returncode != 0:
        return None
    try:
        return max(0.0, time.time() - int(proc.stdout.strip()))
    except ValueError:
        return None


def connect_trusted_url(text, hosts):
    """The first https URL on a host this flow is allowed to send the
    user to. Host-anchored on purpose: the page turns this into a link
    the user is asked to trust, so a future CLI change (or anything else
    that reaches the pane) must not be able to pick the destination."""
    for match in CONNECT_URL_RE.finditer(text or ""):
        candidate = match.group(0)
        while candidate and candidate[-1] in ").,;:'\"":
            candidate = candidate[:-1]
        try:
            parsed = urllib.parse.urlsplit(candidate)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            continue
        if any(host == h or host.endswith("." + h) for h in hosts):
            return candidate
    return None


def connect_user_code(text):
    """The one-time code a device flow prints in the pane. Searched with
    URLs removed, so a `code=` query parameter cannot pose as one."""
    stripped = CONNECT_URL_RE.sub(" ", text or "")
    for match in CONNECT_CODE_RE.finditer(stripped):
        code = match.group(1).upper()
        if code not in CONNECT_PROSE:
            return code
    return None


def connect_error(text):
    """A short, redacted tail of the pane for a flow that ended without
    signing in — the CLI's own words are the only diagnostic there is."""
    lines = []
    for raw in (text or "").splitlines():
        line = CONNECT_EXIT_RE.sub("", raw).strip()
        line = "".join(ch for ch in line if ch == " " or ch.isprintable())
        if line:
            lines.append(line)
    lines = [x for x in lines
             if not CONNECT_NOISE_RE.search(x) and not CONNECT_URL_RE.search(x)]
    blamed = [x for x in lines if CONNECT_BLAME_RE.search(x)]
    picked = (blamed or lines)[-2:]
    if not picked:
        return ""
    tail = CONNECT_SECRET_RE.sub("[redacted]", " ".join(picked))
    return tail[:CONNECT_ERROR_MAX]


def connect_state(flow, keys=None, tmux_state=None):
    """What the card shows, derived from the CLI and the pane — never
    from anything the daemon stored."""
    flow_id = flow["id"]
    server_up, live = tmux_sessions() if tmux_state is None else tmux_state
    running = connect_pane(flow_id) in live
    status = connect_status(flow)
    connected, detail = status if status is not None else (False, "")
    url = code = error = None
    if running:
        # A LIVE pane owns the card; the status answer does not get to
        # overrule it. Reaping a pane because the cached status still says
        # "connected" killed every "Sign in again" — the card says signed
        # in, which is exactly why the user pressed the button, so the
        # pane died within milliseconds of starting (caught by
        # connect.nix's cancel subtest: "claude stuck in idle").
        text = connect_capture(flow_id) or ""
        exited = CONNECT_EXIT_RE.search(text)
        if exited:
            # The CLI is done, and its exit code beats a cached status that
            # predates the exchange: a 0 means it believes the sign-in
            # worked, so hold the card there until a FRESH probe agrees
            # rather than flashing "Not signed in" at the moment of
            # success.
            if exited.group(1) != "0":
                state = "failed"
                error = connect_error(text)
            elif connected and connect_fresh(flow_id):
                connect_cancel(flow_id)
                running = False
                state = "connected"
                # One bare session per harness, started the moment sign-in
                # lands (issue #504) -- codex gets an interactive worker
                # session (remote_control=False) since pairing for
                # phone/desktop already happened in this flow's own pane;
                # claude's rc is a flag on the same worker session, so one
                # session covers both being usable AND remote-visible.
                if flow_id == "claude":
                    ensure_harness_session("claude", remote_control=True)
                elif flow_id == "codex":
                    ensure_harness_session("codex", remote_control=False)
            else:
                connect_expire(flow_id)
                state = "exchanging"
        elif (connect_age(flow_id) or 0) > CONNECT_TTL:
            connect_cancel(flow_id)
            state = "expired"
        else:
            if flow["prompt_re"] is not None:
                connect_answer_prompt(flow, text)
            url = connect_trusted_url(text, flow["hosts"])
            code = connect_user_code(text) if flow["show_code"] else None
            state = "waiting" if url else "starting"
    else:
        # "checking" is not "signed out": saying the latter before the CLI
        # has answered would invite a sign-in the box does not need.
        state = ("connected" if connected
                 else ("idle" if status is not None else "checking"))
    keys = read_keys() if keys is None else keys
    shadow = [k for k in flow["shadow"] if k in keys]
    return {
        "id": flow_id,
        "label": flow["label"],
        "note": flow["note"],
        "state": state,
        "detail": detail,
        "url": url,
        "code": code,
        "error": error,
        "needs_code": flow["needs_code"],
        # False means the CLI is not on this box yet, so the button offers
        # to fetch it first (issue #416). A card with neither an `attr` nor
        # a pinned expression can never be installed from here, so it
        # reports itself installed only when it really is.
        "installed": bool(flow["bin"]),
        "installable": bool(flow["attr"] or flow["expr"]),
        # No tmux server means no session to sign in from, and starting
        # one HERE is exactly what tmux_server_up() explains we must not
        # do — so the card says so instead of offering a dead button.
        "blocked": not server_up,
        "destructive": flow["destructive"],
        "shadow": shadow,
    }


def connect_nix_bin(server_path=None):
    """The nix an install runs with, resolved at USE (issue #544).

    The old fallback was the bare name "nix", left for the pane's shell to
    resolve. On a native box that never works: the pane's PATH is the tmux
    server's (see connect_server_path), which carries the agent profile and
    the distro's bin directories and NOT
    /nix/var/nix/profiles/default/bin, where a multi-user install puts nix.
    So every card that had to fetch its CLI first ran "nix: not found" —
    the install could not start at all, on any native box, for claude,
    codex and defang alike. An interactive shell finds nix there only
    because /etc/profile's nix-daemon.sh prepends that directory, and this
    script is run by a non-interactive `sh -c` that sources nothing.

    Order follows lib/agents.sh, which resolves the same binary for the
    supervisor's lazy install: the pin, then PATH (the pane's first, since
    that is the one the install will actually run under), then the two
    standard install layouts. Returning None is a real answer — the caller
    fails the card with something the operator can read, rather than
    emitting a script that cannot work.

    The pin is preferred but not TRUSTED - it goes through the same
    executable check as every other candidate, so a store path the garbage
    collector has since taken falls through to a nix that is really there
    rather than pinning the card to a script that cannot run. This is one
    place stricter than agents.sh, which takes its pin on faith: the shell
    has no cheap way to say "checked, and missing" other than the error it
    would print anyway, and this resolver does, so the operator gets "no
    nix on this box" instead of "nix: not found" from inside a pane.
    """
    candidates = [CONNECT_NIX_PIN] if CONNECT_NIX_PIN else []
    candidates += [os.path.join(entry, "nix")
                   for entry in (server_path or "").split(os.pathsep) if entry]
    found = shutil.which("nix")
    if found:
        candidates.append(found)
    candidates.extend(os.path.expanduser(p) for p in CONNECT_NIX_FALLBACKS)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def connect_server_path():
    """The PATH a SESSION's pane gets, read off the tmux server.

    A pane this daemon creates inherits the tmux CLIENT's environment —
    ours — and this unit deliberately carries no agent PATH (coreutils,
    findutils, grep, sed, systemd; no git, no gh, no node). That is not a
    cosmetic difference: `gh auth login` shells out to `git` to set up the
    credential helper /etc/gitconfig configures for github.com, and with
    no git on PATH that step cannot run. gh asks "Authenticate Git with
    your GitHub credentials? (Y/n)" regardless of whether git is present
    (so PATH alone does not settle it — the daemon answers that keypress,
    see the github flow's prompt_re and agent-box#400); PATH is what lets
    the answer actually configure git rather than fail.

    The tmux server's global environment IS the agent unit's, so a pane
    started with it looks like the terminal the user would have used. Only
    PATH is lifted: the sign-in pane deliberately carries no session
    secrets (see `unset`), and the env store is the supervisor's job.
    """
    proc = tmux("show-environment", "-g", "PATH")
    if proc is None or proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        # "PATH=..." when set; "-PATH" when the server has it unset.
        if line.startswith("PATH="):
            value = line[len("PATH="):]
            return value or None
    return None


def connect_start(flow):
    """Start the flow's own sign-in command in its own tmux session."""
    flow_id = flow["id"]
    if connect_pane(flow_id) in live_sessions():
        # A listed pane is one of two things: a sign-in genuinely in
        # flight, or a FINISHED one still lingering in CONNECT_LINGER's
        # sleep (the wrapper holds the last screen readable after the CLI
        # exits). Only the first is idempotent. The second is a corpse: a
        # sign-in that FAILED (the provider's token endpoint 500'd, a
        # rejected code) would otherwise pin the card to "failed" and make
        # Start a no-op for the whole linger window, with no way to retry
        # (agent-box#400). Reap the corpse and start fresh; a pane with no
        # exit marker yet is still in flight, so leave it be.
        if not CONNECT_EXIT_RE.search(connect_capture(flow_id) or ""):
            return connect_state(flow)      # in flight: idempotent
        connect_cancel(flow_id)
        for _ in range(50):                 # let tmux free the name (~1s max)
            if connect_pane(flow_id) not in live_sessions():
                break
            time.sleep(0.02)
    if not tmux_server_up():
        state = connect_state(flow)
        state["state"] = "failed"
        state["error"] = ("No terminal session is running, so there is "
                          "nowhere to sign in from. Start a session first.")
        return state
    # Not installed yet (issue #416): fetch it first, in THIS pane, so the
    # download has somewhere visible to happen. The card is already
    # watching this pane, so progress and any failure land where the user
    # is looking rather than in a journal they cannot read.
    #
    # The binary it will land at is the same path connect_flows would have
    # resolved, so the sign-in half needs no special case.
    binary = flow["bin"]
    prelude = ""
    # Read before the prelude is built, not after: the install half needs
    # the same PATH the CLI half will run under, to resolve nix against it.
    server_path = connect_server_path()
    if not binary:
        if not flow["attr"] and not flow["expr"]:
            state = connect_state(flow)
            state["state"] = "failed"
            state["error"] = ("%s is not installed on this box, and cannot "
                              "be installed from here." % flow["label"])
            return state
        # Only the `attr` path needs a package source. A card with a pinned
        # expression does not, so it must not be failed for the lack of one
        # — the source selection below already prefers the expression, and
        # a guard that fired first would contradict it.
        if flow["attr"] and not flow["expr"] and not CONNECT_NIXPKGS:
            state = connect_state(flow)
            state["state"] = "failed"
            state["error"] = ("%s is not installed, and this box has no "
                              "package source configured to fetch it from."
                              % flow["label"])
            return state
        nix_bin = connect_nix_bin(server_path)
        if not nix_bin:
            state = connect_state(flow)
            state["state"] = "failed"
            state["error"] = ("%s is not installed, and no nix was found on "
                              "this box to install it with." % flow["label"])
            return state
        # An expression wins over an attr when a card somehow has both: it
        # is the PINNED one, and pinning is the only reason a card carries
        # an expression at all.
        if flow["expr"]:
            source = ["--file", flow["expr"], ""]
        else:
            source = ["%s#%s" % (CONNECT_NIXPKGS, flow["attr"])]
        binary = os.path.join(
            os.path.expanduser("~"), ".nix-profile", "bin", flow["binary"])
        # --profile names the profile explicitly for the same reason the
        # path above does: ~/.nix-profile is what a session's PATH already
        # looks in, so an install landing anywhere else would succeed and
        # still leave the card saying "not installed".
        # Chained with && and NOT "|| exit 1", which is what the card was
        # really hiding behind (issue #544): `exit` leaves the whole shell,
        # so a failed install skipped the exit marker AND the linger sleep
        # below. tmux then dropped the session within milliseconds,
        # connect_state found no pane, and the card fell back to "Not
        # installed" with error None — every install failure, whatever its
        # cause, looked exactly like the button doing nothing. Falling
        # through to the marker instead lets $? carry the install's own
        # status, so the card reaches "failed" and shows connect_error().
        prelude = (
            "echo '[agent-box] fetching %s — first use on this box, this "
            "can take a few minutes'; NIXPKGS_ALLOW_UNFREE=1 %s profile add "
            "--impure --profile %s %s && " % (
                flow["label"],
                shlex.quote(nix_bin),
                shlex.quote(os.path.join(os.path.expanduser("~"),
                                         ".nix-profile")),
                " ".join(shlex.quote(a) for a in source)))
    inner = " ".join(shlex.quote(a) for a in [binary] + flow["start"])
    if flow["unset"]:
        inner = ("env " + " ".join("-u " + k for k in flow["unset"]) + " " + inner)
    inner = prelude + inner
    # The exit marker turns "the CLI is done" into something the state
    # machine can see, and the sleep keeps the final screen readable
    # until then. Both are shell-level, so no CLI has to cooperate.
    script = "%s; printf '\\n[agent-box] exit=%%s\\n' \"$?\"; sleep %d" % (
        inner, CONNECT_LINGER)
    # In the script, not `new-session -e PATH=...`: tmux honours -e for any
    # other variable but the pane's PATH comes from the CLIENT regardless
    # (measured on tmux 3.6a — an -e FOO reaches the pane while an -e PATH
    # beside it is dropped), and this client is the settings daemon.
    if server_path:
        script = "export PATH=%s; %s" % (shlex.quote(server_path), script)
    tmux("new-session", "-d", "-s", connect_pane(flow_id),
         "-x", str(CONNECT_COLS), "-y", str(CONNECT_ROWS), script)
    return connect_state(flow)


def connect_answer_prompt(flow, text):
    """Press Enter for the user when the CLI is sitting on a keypress it
    waits for (gh's web flow does this before it starts polling).

    Answered from the pane's CURRENT tail on every render rather than in a
    bounded loop at start-up: the loop blocked the POST for its whole
    window and then gave up for good, so a CLI that took longer than that
    to print its prompt — a slow device-code request is all it takes — was
    left holding a keypress nobody would ever send, showing the user a URL
    and a code for a flow that had not started polling and never would.

    Only the last non-empty line is matched, so a prompt that has already
    been answered (its line is no longer the tail) is not answered twice,
    and one that is still waiting gets another keypress on the next poll.
    """
    line = next((x for x in reversed((text or "").splitlines()) if x.strip()), "")
    if not flow["prompt_re"].search(line):
        return False
    tmux("send-keys", "-t", connect_target(flow["id"]), "C-m")
    return True


def connect_send_code(flow, code):
    """Type the code the user pasted into the pane, then submit it."""
    flow_id = flow["id"]
    if connect_pane(flow_id) not in live_sessions():
        return connect_state(flow)
    target = connect_target(flow_id)
    tmux("send-keys", "-t", target, "-l", code)
    time.sleep(CONNECT_ENTER_DELAY)
    tmux("send-keys", "-t", target, "C-m")
    return connect_state(flow)


def connect_cancel(flow_id):
    tmux("kill-session", "-t", "=" + connect_pane(flow_id))


def find_supervisor_pids():
    """PIDs of this user's session supervisor — the agent unit's main
    process (the shared src/supervisor.sh store script; issue #154 Phase 2
    made it user-independent, so the uid restriction below is what scopes
    the match to OUR unit). Matched by an argv element ending in
    "agent-box-supervisor"."""
    marker = "agent-box-supervisor"
    uid = os.getuid()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat("/proc/" + entry).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue  # process raced away
        if any(a.decode("utf-8", "replace").endswith(marker) for a in argv):
            pids.append(int(entry))
    return pids


def restart_all():
    """Bounce the WHOLE agent unit, no sudo needed: SIGTERM the
    supervisor (the unit's main process, our own uid). systemd then
    tears the session tree down and Restart=always brings the unit
    back with freshly read EnvironmentFiles — unit env is a
    start-time snapshot, so this is the lever that applies changes to
    host-configured environmentFiles (issue 89). Per-session restarts
    stay cheap: the spawn wrapper re-reads the user env file anyway.
    Dev rigs without the unit fall back to bouncing the sessions."""
    pids = find_supervisor_pids()
    if not pids:
        for name in read_sessions():
            if SESSION_RE.match(name):
                kill_session(name)
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            sys.stderr.write("restart_all: pid %d: %s\n" % (pid, exc))


def update_box():
    """Trigger the box update oneshot via the allowlisted sudo command.
    --no-block (baked into UPDATE_CMD) means this returns immediately;
    the rebuild may later restart this very daemon.
    """
    try:
        proc = subprocess.run(
            UPDATE_CMD.split(),
            check=False,
            capture_output=True,
        )
        # rc only — never log request bodies or command output wholesale.
        sys.stderr.write("update_box: trigger rc=%d\n" % proc.returncode)
    except OSError as exc:
        sys.stderr.write("update_box: %s\n" % exc)


def reboot_box():
    """Trigger the reboot through the allowlisted sudo command.

    The caller sends its HTTP response BEFORE calling this (see the
    /reboot route): --no-block returns immediately, but systemd starts
    stopping units at once, and this daemon is one of them — a response
    written afterwards can lose the race with its own socket going away.
    """
    try:
        proc = subprocess.run(REBOOT_CMD.split(), check=False, capture_output=True)
        sys.stderr.write("reboot_box: trigger rc=%d\n" % proc.returncode)
    except OSError as exc:
        sys.stderr.write("reboot_box: %s\n" % exc)


def reboot_pending():
    """What the distro says a reboot would apply: (pending, packages).

    Read, never inferred: `pending` is the existence of the marker file
    the package manager itself creates, and `packages` is the list it
    wrote beside it. A box that has never needed a reboot has neither
    file, which is not an error — it is the answer.
    """
    if not os.path.exists(REBOOT_REQUIRED):
        return False, []
    try:
        with open(REBOOT_REQUIRED + ".pkgs", encoding="utf-8") as fh:
            names = [line.strip() for line in fh if line.strip()]
    except OSError:
        names = []
    # Deduplicated (the file repeats a package that asked twice) and
    # capped: this is a one-line reason on a card, not a manifest.
    seen, packages = set(), []
    for name in names:
        if name not in seen and len(packages) < 6:
            seen.add(name)
            packages.append(name)
    return True, packages


def update_service_state():
    """Read-only state of the box update oneshot, for the UI progress
    line. Returns None when self-update is off (no unit wired) or
    systemctl is unavailable (dev rigs) — the caller then omits the
    update block. `since` is the run's monotonic start time (usec since
    boot, 0 if it never ran): the page captures it before triggering
    and waits for a strictly newer value, which is stable even though
    the rebuild may restart this daemon (same boot). No privilege
    needed — `systemctl show` is a world-readable query."""
    if not UPDATE_UNIT or not SYSTEMCTL:
        return None
    try:
        proc = subprocess.run(
            [SYSTEMCTL, "show", UPDATE_UNIT, "--property",
             "ActiveState,Result,ExecMainStartTimestampMonotonic"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    props = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    try:
        since = int(props.get("ExecMainStartTimestampMonotonic", "0") or "0")
    except ValueError:
        since = 0
    return {
        "active": props.get("ActiveState", ""),
        "result": props.get("Result", ""),
        "since": since,
    }


def session_counts():
    """How many configured sessions are currently live — the signal the
    page watches to confirm a 'Restart all' has bounced and recovered.
    Stopped sessions are excluded on both sides: the supervisor will not
    bring them up, so counting them would hold 'live < configured' (and
    the page's restart spinner) open forever."""
    configured = [
        n for n, v in read_sessions().items()
        if SESSION_RE.match(n) and not v.get("stopped")
    ]
    live = live_sessions()
    return {
        "configured": len(configured),
        "live": sum(1 for n in configured if n in live),
    }


def status_payload():
    """Compact JSON the settings page long-polls for restart/update
    progress, and connectPoll checks before fetching the full page.
    Never includes secret values or command output."""
    payload = {"rev": REV, "sessions": session_counts()}
    update = update_service_state()
    if update is not None:
        payload["update"] = update
    if connect_flows():
        payload["connect_fp"] = connect_fingerprint()
    return payload


# --- Live session feed -----------------------------------------------
# Sessions change from outside whichever page you happen to be looking
# at: the agent-box-session CLI, an agent adding its own helper, a
# second browser tab, or the supervisor bringing a listed session up.
# Both pages used to learn about that from their OWN posts plus a short
# poll burst only, so anything else stayed invisible until a reload.
#
# {SESS_BASE}/sessions/events fixes that with a Server-Sent Events
# stream: one frame per change, carrying a fingerprint of the session
# state. The page compares it with what it last rendered and, when they
# differ, re-fetches itself and patches the tab bar / session list in
# place (the same swap its own form posts do). Only the digest crosses
# the stream — never session data, so nothing here can leak argv, cwd
# or env. Same route prefix as the rest of session CRUD, hence the same
# auth gate; ?poll=1 returns the fingerprint as plain JSON for clients
# that cannot hold a stream open.
EVENTS_TICK = 1.0         # how often the watcher samples session state
EVENTS_SLICE = 1.0        # how often a stream re-checks that its client is there
EVENTS_KEEPALIVE = 20.0   # comment frame so idle proxies keep the stream
EVENTS_MAX_STREAMS = 8    # a stream costs a thread; past this, clients poll


def session_view():
    """The session state the pages actually render: order, name, agent,
    working directory, live-or-starting, stopped, died.

    Deliberately not the whole of sessions.json — the supervisor still
    mirrors its bookkeeping into that file for one release (hasRun,
    boxSessionId, clearing initialPrompt on first spawn; issue #282) and
    those rewrites must not read as a change worth re-rendering for.
    """
    entries = {n: v for n, v in read_sessions().items() if SESSION_RE.match(n)}
    live = live_sessions()
    return [
        [
            name,
            str(entries[name].get("agent") or "?"),
            str(entries[name].get("workingDirectory") or ""),
            name in live,
            bool(entries[name].get("stopped")),
            # A crash changes no other field here -- the pane stays live and
            # nothing is stopped -- so without this the feed would never push
            # the frame that repaints the row as Died (issue #516).
            crashed_status(entries[name]),
        ]
        for name in entries
    ]


def session_fingerprint():
    """Short digest of session_view(): the token the feed pushes and the
    page compares against the state it was rendered from."""
    raw = json.dumps(session_view(), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class SessionWatcher:
    """One sampler thread shared by every open stream.

    A sample forks `tmux list-sessions`, so sampling per client would
    scale with open browser tabs; instead a single thread samples while
    at least one stream is connected and hands them all the same
    fingerprint through a condition variable. With nothing connected the
    thread exits, so an idle box spawns nothing at all.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.fingerprint = ""
        self.seq = 0
        self.streams = 0
        self.thread = None

    def subscribe(self):
        """Reserve a stream slot. Returns (sequence, fingerprint) to start
        from, or None when EVENTS_MAX_STREAMS are already open. The
        fingerprint is empty until the first sample lands."""
        with self.cond:
            if self.streams >= EVENTS_MAX_STREAMS:
                return None
            self.streams += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._sample, daemon=True)
                self.thread.start()
            return self.seq, self.fingerprint

    def release(self):
        with self.cond:
            self.streams -= 1

    def wait(self, seq, timeout):
        """Block until the fingerprint changes or timeout expires, then
        return (sequence, fingerprint). An unchanged sequence means the
        caller timed out and should send a keep-alive."""
        with self.cond:
            if self.seq == seq:
                self.cond.wait(timeout)
            return self.seq, self.fingerprint

    def _sample(self):
        while True:
            with self.cond:
                if not self.streams:
                    # Last stream left: stop sampling. Holding the lock
                    # across the check makes this safe against a
                    # subscribe() racing in to start a fresh thread.
                    self.thread = None
                    return
            current = session_fingerprint()
            with self.cond:
                if current != self.fingerprint:
                    self.fingerprint = current
                    self.seq += 1
                    self.cond.notify_all()
            time.sleep(EVENTS_TICK)


WATCHER = SessionWatcher()


def change_password(previous, new):
    """Ask the root helper to verify and rotate the web credentials.

    Return 0 on success, 2 for a wrong current password, and another
    nonzero value for an operational failure. Passwords cross sudo on
    stdin only; neither argv, the environment nor the journal sees them.
    """
    try:
        proc = subprocess.run(
            PASSWORD_CMD.split(),
            input=json.dumps({"previous": previous, "new": new}),
            text=True,
            check=False,
            capture_output=True,
        )
        sys.stderr.write("change_password: helper rc=%d\n" % proc.returncode)
        return proc.returncode
    except OSError as exc:
        sys.stderr.write("change_password: %s\n" % exc)
        return 5


# The mascot (issue #185): a potato wired to an aperture optic. Embedded
# from docs/potato.svg at assemble time, so the landing page and a live
# box cannot drift to two different marks — that file is the one source,
# this is the only copy of it that can ship (a deployed box fetches the
# module as a SINGLE file, so it cannot serve a sibling asset).
#
# Whimsy belongs on human surfaces only: nothing about the mascot goes
# near the agent's spawn preamble, where it would cost tokens on every
# session and read as an instruction.
POTATO_SVG = """\
@@include:../../docs/potato.svg@@
"""
# ... and as a favicon. Inline data: URI rather than a route, so the page
# stays self-contained and the tab icon needs no second request. Before
# this the workspace tab was blank, which made several open boxes
# indistinguishable.
FAVICON = "data:image/svg+xml," + urllib.parse.quote(POTATO_SVG)

# Page skeleton. HEAD_TPL and BODY go through str.format (hence no
# literal braces in them); STYLE and SCRIPT are plain strings so CSS/JS
# braces need no doubling. The layout mirrors GitHub's environment-
# secrets settings: section header with an action button on the right,
# then a bordered table (header row + one row per item) with icon
# buttons per row. SCRIPT is progressive enhancement only — without JS
# the plain form POST + 303 redirect flow still works, the add/edit
# forms just render expanded.
HEAD_TPL = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="agent-box-events" content="{events}" data-fp="{fp}">
<link rel="icon" href="{favicon}">
<title>{title}</title>
"""

STYLE = """<style>
  @@include:settings.css@@
</style>
"""

# Shared by the settings-page and workspace add forms so their layout,
# accessibility, and autocomplete behaviour cannot drift apart.
NEW_SESSION_FIELDS_TPL = """<div class="row new-session-row">
  <select id="profile-picker" name="profile" aria-label="Profile" required
          title="Profile: saved assistant, model, reasoning level and instructions">{profiles}</select>
  <span class="cwd-control">
    <label for="new-session-cwd">Starting folder</label>
    <span class="combo">
      <input id="new-session-cwd" type="text" name="cwd" value="~" class="cwd"
             placeholder="~" autocomplete="off" autocapitalize="off"
             autocorrect="off" spellcheck="false"
             data-dir-input data-dir-base="{action_base}"
             aria-label="Starting folder" aria-autocomplete="list"
             title="Starting folder (defaults to your home folder)">
      <ul class="ac" hidden></ul>
    </span>
  </span>
  <button type="submit" class="btn">Add session</button>
</div>
<p class="note">The profile decides which assistant runs, and its model,
reasoning level and instructions. Choose where the session starts too: the
default is your home folder (<code>~</code>).</p>
<div class="row prompt-row">
  <textarea name="prompt" rows="2"
            placeholder="starting task (optional) &mdash; what this session should work on first"></textarea>
</div>"""

# The session manager <section> on the settings page (every user,
# including the primary one — the HOME root page is the tabbed
# terminal workspace, not a manager). {action_base} is SESS_BASE, so
# the forms post to wherever the session routes actually live; the
# hidden back=settings field makes their redirects land back here
# rather than on SESS_PAGE (issue #119).
SESSIONS_SECTION_TPL = """<section>
    <div class="sec-head">
      <h2>Sessions</h2>
      <button type="button" class="btn" data-toggle="session-editor">Add session</button>
    </div>
    <p class="note">Each session opens a separate terminal tab for an AI
    assistant or shell. New sessions are usually ready within a few seconds.
    Select one to open it.</p>
    <div id="session-editor" class="editor">
      <form method="post" action="{action_base}/sessions/add">
        <input type="hidden" name="back" value="settings">
        {new_session_fields}
      </form>
    </div>
    <div id="sessions-list">{sessions}</div>
  </section>"""

# The agent-profiles panel (issue #321, step 5), settings page only — like
# the webhook panel, the workspace root is a terminal and not a manager.
#
# It sits directly UNDER Sessions on purpose: a profile has no life of its
# own, it is a thing you pick in the row above, and the two panels read in
# the order the work happens. The note says which way the override runs,
# because the session row now has two controls that both name a harness.
PROFILES_SECTION_TPL = """<section>
    <div class="sec-head">
      <h2>Profiles</h2>
      <button type="button" class="btn" data-toggle="profile-editor">Add profile</button>
    </div>
    <p class="note">Profiles save how an assistant should start, including
    its model, reasoning level and extra instructions. Choose one when adding
    a session or use one for sessions started automatically. Changes apply
    only to new sessions.</p>
    <div id="profile-editor" class="editor">
      <form method="post" action="{base}/profiles/set">
        <div class="row profile-row">
          <input type="text" name="name" placeholder="profile name" class="pname"
                 pattern="[A-Za-z0-9_-]{{1,64}}" required autocomplete="off"
                 aria-label="Profile name"
                 title="Letters, digits, underscore and hyphen; at most 64 characters">
          <select name="HARNESS" aria-label="Assistant" required>{harnesses}</select>
          <input type="text" name="MODEL" placeholder="model (optional)" class="pmodel"
                 autocomplete="off" aria-label="Model">
          <select name="EFFORT" aria-label="Reasoning level">{effort}</select>
        </div>
        <div class="row prompt-row">
          <textarea name="SYSTEM_PROMPT" rows="2" spellcheck="false"
                    placeholder="instructions (optional) &mdash; used for every session with this profile"></textarea>
        </div>
        <div class="row env-row">
          <input type="text" name="key" placeholder="KEY_NAME (optional)"
                 pattern="[A-Za-z_][A-Za-z0-9_]*" autocomplete="off"
                 aria-label="Environment setting name">
          <input type="password" name="value" placeholder="value"
                 autocomplete="off" aria-label="Value for that setting">
        </div>
        <p class="note">Saving a profile with the same name replaces it.
        Leave an optional field blank to remove its current value. Add a
        starting environment setting above, or more from the profile&rsquo;s
        own row below once it is saved.</p>
        <div class="row profile-actions">
          <button type="submit" class="btn">Save</button>
        </div>
      </form>
    </div>
    <div id="profiles-list">{profiles}</div>
  </section>"""

# The webhook panel (issue #227), settings page only: the workspace root
# is a terminal, not a manager. Hidden entirely when the box serves no
# webhook receiver. Two halves, in the order the work happens: the
# endpoint a sender has to be pointed at, then the standing watches a
# delivery starts a session from. A session's OWN subscriptions are in
# neither — they fold open under that session's row in the panel above.
#
# Called "Webhook", not "Standing watches": the panel now answers "where
# do deliveries come in" as well as "what do they start", and the list's
# own header still says which of the two it is.
WEBHOOKS_SECTION_TPL = """<section>
    <div class="sec-head">
      <h2>Automations</h2>
    </div>
    <div id="webhook-endpoint">{endpoint}</div>
    <p class="note">Automatic session rules start a new session when a
    matching event arrives. Event notifications sent to existing sessions
    appear under Sessions above. Changes apply to the next event.</p>
    <div id="webhooks-list">{webhooks}</div>
  </section>"""

# What stands in for the panel when there is no panel (#425). One heading
# and one sentence: an operator who came here looking for the webhook
# section finds it, and finds out in the same glance whether the feature
# is off or the box is wrong. See webhook_unavailable() for the states.
WEBHOOK_UNAVAILABLE_TPL = """<section>
    <div class="sec-head">
      <h2>Automations</h2>
    </div>
    <p class="note">{text}</p>
  </section>"""

# The endpoint half. Its own note, so the whole thing can be left out on a
# box that serves no endpoint (no AGENT_BOX_WEBHOOK_URL) without leaving a
# paragraph pointing at a URL that is not there.
#
# What a webhook IS, in plain words and without assuming GitHub: the
# receiver verifies an HMAC over the body and nothing else, so a repo, a
# CI system, a payment processor and a home-grown script are all the same
# kind of sender to this box. Shared by both states below, because the
# answer must not depend on whether a sender happens to be set up yet.
WEBHOOK_LEAD = ("Connect GitHub or another service so agent-box can respond "
                "when something happens &mdash; for example, when a review "
                "is submitted or a build finishes.")

# The table is for senders that ARE set up. Everything a reader needs in
# order to make sense of the rows goes in this paragraph above it, never
# in a row of its own: `.tbl li` is a flex row, so prose dropped into the
# list is laid out as though its sentences were columns.
WEBHOOK_ENDPOINT_TPL = """<p class="note">%s To connect GitHub, copy the
    webhook URL and secret below into the repository's Settings &rarr;
    Webhooks &rarr; Add webhook page. Select <code>application/json</code>
    as the content type. Both values are required.</p>
    {rows}""" % WEBHOOK_LEAD

# No sender configured: no table at all, because an empty one is a header
# over a paragraph of prose. The command NAMES its source rather than
# leaning on the `github` default — the panel is source-neutral, and a
# reader connecting Stripe should not have to discover that the argument
# exists.
WEBHOOK_ENDPOINT_EMPTY_TPL = """<p class="note">%s</p>
    <p class="note">No service is connected yet. To add one, open a session
    and run <code>agent-box-webhook setup SOURCE</code>, replacing SOURCE with
    the service name &mdash; for example, <code>github</code> or
    <code>stripe</code>.</p>""" % WEBHOOK_LEAD

# Guided sign-in (issues #207, #208, #313). Hidden entirely when the box
# passed no AGENT_BOX_CONNECT_BINS. data-busy tells the page whether a
# flow is mid-flight, the only thing that decides whether to poll at all;
# data-status + data-fp let it check {BASE}/status's tiny fingerprint
# before paying for a full page fetch - connectPoll used to re-fetch the
# whole page unconditionally every 2.5s while busy, regardless of whether
# anything had actually changed.
CONNECT_SECTION_TPL = """<section>
    <div class="sec-head">
      <h2>Connections</h2>
    </div>
    <p class="note">Connect the accounts your assistants can use. Sign-in is
    handled securely by each provider, and this page does not display your
    credentials. API keys added manually below take priority.</p>
    <div id="connect-list" data-busy="{busy}" data-status="{status_url}"
         data-fp="{fp}">{cards}</div>
  </section>"""

# The user's landing page (HOME mode): a tabbed terminal workspace
# (issue #119) — one tab per session, the active one shown in an iframe
# onto that session's own path (/<user>/<session>/; same origin, so the
# auth cookie and its WebSocket upgrade work unchanged). Tabs are plain
# ?tab= links so the page works without JS (each click re-renders with
# the other terminal); SCRIPT upgrades that to
# client-side switching with background tabs kept attached. Session
# CRUD beyond "add" lives on the settings page.
#
# The add/delete banner is the FIRST element, above the tab bar (issue
# #188): it is page-level feedback, and sitting between the tabs and
# the panes it both prised the tab bar away from the terminal it labels
# and read as a message from the session in that pane.
#
# The (i) hint (issue #327): tmux's `mouse on` (issue #265) claims every
# button and drag for itself, not just the wheel — there is no tmux/xterm
# mouse-tracking mode that reports wheel events alone. So a plain
# click-drag now paints tmux's own copy-mode highlight instead of a
# browser selection, and it vanishes on mouse-up without reaching the
# clipboard (ttyd's bundled xterm.js has no OSC 52 support, so nothing
# server-side can bridge tmux's paste buffer to the browser clipboard
# either). xterm.js's own fallback is holding Shift (Option on a Mac)
# while dragging, which forces its native DOM selection — and that in
# turn is what ttyd auto-copies on selection change. On Mac that fallback
# also needed a ttyd flag (macOptionClickForcesSelection=true, next to
# the ttyd ExecStart below) since xterm.js ships it off by default; this
# hint is the other half — telling people the gesture exists at all.
# The gesture is spelled out TWICE in the span below, in `title` and in
# `aria-label`, and that is not a copy-paste slip: `title` is the pointer
# tooltip, while an `aria-label` REPLACES it as the accessible name and
# `title` is not read as a description once a name exists. A short name of
# its own ("Terminal mouse tip") would therefore be the whole of what a
# screen reader ever announced, so both attributes carry the instructions.
#
# Both trailing icons are inline SVG (ICON_INFO/ICON_GEAR below), not the
# text glyphs U+24D8/U+2699 they used to be. Two reasons, and the second
# is the one a desktop browser hides: U+2699 GEAR carries
# Emoji_Presentation, so iOS and Android substitute their COLOR emoji
# font for it — the settings control turned up as a shaded 3D gear next
# to a flat monochrome (i), visibly bigger and in the wrong palette,
# while the same page on a desktop looked merely a little uneven. Nothing
# in CSS fixes that: font-size only scales the emoji, and the glyph a
# font hands back is not ours to pick. Sized in px as SVG, the pair is
# the same 18px box everywhere and inherits currentColor like every
# other icon on the page.
HOME_BODY = """<body class="ws">
<div id="msg-slot">{message}</div>
<nav class="tabs" id="tab-bar" aria-label="Sessions" data-term-base="{term_base}"
     data-sess-base="{action_base}">
  {tabs}
  <button type="button" class="btn add" data-toggle="session-editor"
          title="New session" aria-label="New session">+</button>
  <span class="spacer"></span>
  <span class="hint" tabindex="0"
        aria-label="Terminal mouse tip. Mouse wheel scrolls the pane's history. Hold Shift (Option on a Mac) while clicking and dragging to select and copy text."
        title="Mouse wheel scrolls the pane's history. Hold Shift (Option on a Mac) while clicking and dragging to select and copy text.">{icon_info}</span>
  <a class="gear" href="{base}/" title="Settings" aria-label="Settings">{icon_gear}</a>
</nav>
<div id="session-editor" class="editor">
  <form method="post" action="{action_base}/sessions/add">
    <input type="hidden" name="back" value="workspace">
    {new_session_fields}
  </form>
</div>
<div class="panes" id="panes">{pane}</div>
</body>
</html>
"""

BODY = """<main>
  <a class="repo" href="https://github.com/defangdevs/agent-box" title="agent-box on GitHub" aria-label="agent-box on GitHub">
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
      0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52
      -.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2
      -3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21
      2.2.82A7.65 7.65 0 0 1 8 3.86c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82
      2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75
      -3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01
      8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/>
    </svg>
    GitHub
  </a>
  <a class="back" href="{term_home}">&larr; workspace</a>
  <h1><span class="mark">{mark}</span>Settings</h1>
  <div id="msg-slot">{message}</div>
  {sessions_section}
  {profiles_section}
  {webhooks_section}
  {connect_section}
  <section>
    <div class="sec-head">
      <h2>API keys and secrets</h2>
      <button type="button" class="btn" data-toggle="secret-editor">Add secret</button>
    </div>
    <p class="note">Store API keys and other credentials used by your
    sessions. Values are hidden after you save them. Restart a session before
    expecting a change to take effect.</p>
    <div id="secret-editor" class="editor">
      <form id="secret-form" method="post" action="{base}/set">
        <div class="row">
          <input type="text" name="key" placeholder="KEY_NAME"
                 pattern="[A-Za-z_][A-Za-z0-9_]*" required
                 title="Letters, digits and underscores; must not start with a digit">
          <textarea name="value" class="secret-value" rows="2" spellcheck="false"
                    placeholder="value"
                    autocomplete="off" required></textarea>
          <button type="submit" class="btn">Save</button>
        </div>
        <p class="note">Saving a secret with the same name replaces its
        current value. Stored values cannot be viewed from this page.
        Multi-line values are supported.</p>
      </form>
    </div>
    <div id="secrets-list">{keys}</div>
  </section>
  {password_section}
  <section>
    <h2>Maintenance</h2>
    <ul class="tbl danger">
      <li>
        <span class="dz"><strong>Restart all sessions</strong>
        <span class="note">Stops and restarts every running session. Any
        work that has not been saved will be lost. Sessions you stopped
        manually will remain stopped.
        <span id="restart-status" class="update-state" aria-live="polite"></span></span></span>
        <form method="post" action="{base}/restart" data-poll="restart"
              data-status="{base}/status"
              onsubmit="return confirm('Restart all sessions? Any work that has not been saved will be lost.');">
          <button type="submit" class="btn danger-btn">Restart all sessions</button>
        </form>
      </li>
      {update_row}
      {reboot_row}
    </ul>
  </section>
</main>
</html>
"""

PASSWORD_SECTION = """<section>
    <div class="sec-head">
      <h2>Account</h2>
      <button type="button" class="btn" data-toggle="password-editor">Change password</button>
    </div>
    <p class="note">Change the password used to open agent-box. You will be
    signed out on every device.</p>
    <div id="password-editor" class="editor">
      <form method="post" action="{base}/password" data-native>
        <div class="fields">
          <label class="field">Current password
            <input type="password" name="previous_password"
                   autocomplete="current-password" required>
          </label>
          <label class="field">New password
            <input type="password" name="new_password"
                   autocomplete="new-password" minlength="16" maxlength="64" required>
          </label>
          <label class="field">Confirm new password
            <input type="password" name="confirm_password"
                   autocomplete="new-password" minlength="16" maxlength="64" required>
          </label>
          <p class="note">Use 16&ndash;64 characters. Symbols generated
          by password managers are supported.</p>
          <div><button type="submit" class="btn">Update password</button></div>
        </div>
      </form>
    </div>
  </section>"""

UPDATE_ROW = """<li>
        <span class="dz"><strong>Update agent-box</strong>
        <span class="note">Updates agent-box and its AI tools, refreshes
        configuration, and restarts affected services. This usually takes a
        few minutes, and sessions may restart.{update_line}</span></span>
        <form method="post" action="{base}/update" data-poll="update"
              data-status="{base}/status"
              onsubmit="return confirm('Update agent-box now? Sessions may restart if needed.');">
          <button type="submit" class="btn danger-btn">Update agent-box</button>
        </form>
      </li>"""

# Last in Maintenance, because it is the widest: the machine, not a
# service. The note says what comes back by itself, so the choice is
# about the minute of downtime rather than about losing the box.
REBOOT_ROW = """<li>
        <span class="dz"><strong>Restart agent-box</strong>
        <span class="note">Makes agent-box unavailable for about a minute.
        Running sessions will stop, and any work that has not been saved will
        be lost. Use this when an update says a restart is required.{reboot_line}
        <span id="reboot-status" class="update-state" aria-live="polite"></span></span></span>
        <form method="post" action="{base}/reboot" data-poll="reboot"
              data-status="{base}/status"
              onsubmit="return confirm('Restart agent-box now? It will be unavailable for about a minute, and unsaved work will be lost.');">
          <button type="submit" class="btn danger-btn">Restart agent-box</button>
        </form>
      </li>"""

# Octicons (MIT) inlined so the page stays a single self-contained
# response.
ICON_INFO = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Z'
    'M6.5 7.75A.75.75 0 0 1 7.25 7h1a.75.75 0 0 1 .75.75v2.75h.25a.75.75 0 0 1 0 1.5h-2a.75.75 '
    '0 0 1 0-1.5h.25v-2h-.25a.75.75 0 0 1-.75-.75ZM8 6a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z"/></svg>'
)
ICON_GEAR = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M8 0a8.2 8.2 0 0 1 .701.031C9.444.095 9.99.645 10.16 1.29l.288 1.107c.018.066.079'
    '.158.212.224.231.114.454.243.668.386.123.082.233.09.299.071l1.103-.303c.644-.176 1.392.021 '
    '1.82.63.27.385.506.792.704 1.218.315.675.111 1.422-.364 1.891l-.814.806c-.049.048-.098.147'
    '-.088.294.016.257.016.515 0 .772-.01.147.038.246.088.294l.814.806c.475.469.679 1.216.364 '
    '1.891a7.977 7.977 0 0 1-.704 1.217c-.428.61-1.176.807-1.82.63l-1.102-.302c-.067-.019-.177'
    '-.011-.3.071a5.909 5.909 0 0 1-.668.386c-.133.066-.194.158-.211.224l-.29 1.106c-.168.646'
    '-.715 1.196-1.458 1.26a8.006 8.006 0 0 1-1.402 0c-.743-.064-1.289-.614-1.458-1.26l-.289'
    '-1.106c-.018-.066-.079-.158-.212-.224a5.738 5.738 0 0 1-.668-.386c-.123-.082-.233-.09-.299'
    '-.071l-1.103.303c-.644.176-1.392-.021-1.82-.63a8.12 8.12 0 0 1-.704-1.218c-.315-.675-.111'
    '-1.422.363-1.891l.815-.806c.05-.048.098-.147.088-.294a6.214 6.214 0 0 1 0-.772c.01-.147'
    '-.038-.246-.088-.294l-.815-.806C.635 6.045.431 5.298.746 4.623a7.92 7.92 0 0 1 .704-1.217'
    'c.428-.61 1.176-.807 1.82-.63l1.102.302c.067.019.177.011.3-.071.214-.143.437-.272.668-.386'
    '.133-.066.194-.158.211-.224l.29-1.106C6.009.645 6.556.095 7.299.03 7.53.01 7.764 0 8 0Z'
    'm-.571 1.525c-.036.003-.108.036-.137.146l-.289 1.105c-.147.561-.549.967-.998 1.189-.173.086'
    '-.34.183-.5.29-.417.278-.97.423-1.529.27l-1.103-.303c-.109-.03-.175.016-.195.045-.22.312'
    '-.412.644-.573.99-.014.031-.021.11.059.19l.815.806c.411.406.562.957.53 1.456a4.709 4.709 0 '
    '0 0 0 .582c.032.499-.119 1.05-.53 1.456l-.815.806c-.081.08-.073.159-.059.19.162.346.353.677'
    '.573.989.02.03.085.076.195.046l1.102-.303c.56-.153 1.113-.008 1.53.27.161.107.328.204.501'
    '.29.447.222.85.629.997 1.189l.289 1.105c.029.109.101.143.137.146a6.6 6.6 0 0 0 1.142 0c.036'
    '-.003.108-.036.137-.146l.289-1.105c.147-.561.549-.967.998-1.189.173-.086.34-.183.5-.29.417'
    '-.278.97-.423 1.529-.27l1.103.303c.109.029.175-.016.195-.045.22-.313.411-.644.573-.99.014'
    '-.031.021-.11-.059-.19l-.815-.806c-.411-.406-.562-.957-.53-1.456a4.709 4.709 0 0 0 0-.582c'
    '-.032-.499.119-1.05.53-1.456l.815-.806c.081-.08.073-.159.059-.19a6.464 6.464 0 0 0-.573'
    '-.989c-.02-.03-.085-.076-.195-.046l-1.102.303c-.56.153-1.113.008-1.53-.27a4.44 4.44 0 0 0'
    '-.501-.29c-.447-.222-.85-.629-.997-1.189l-.289-1.105c-.029-.11-.101-.143-.137-.146a6.6 6.6 '
    '0 0 0-1.142 0ZM11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM9.5 8a1.5 1.5 0 1 0-3.001.001A1.5 1.5 0 0 '
    '0 9.5 8Z"/></svg>'
)
ICON_LOCK = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M4 4a4 4 0 0 1 8 0v2h.25c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0 1 12.25 15'
    'h-8.5A1.75 1.75 0 0 1 2 13.25v-5.5C2 6.784 2.784 6 3.75 6H4Zm8.25 3.5h-8.5a.25.25 0 0 0'
    '-.25.25v5.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25v-5.5a.25.25 0 0 0-.25-.25Z'
    'M10.5 6V4a2.5 2.5 0 1 0-5 0v2Z"/></svg>'
)
ICON_PENCIL = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 '
    '8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235'
    '-.547.445-.758l8.61-8.61Zm.176 4.823L9.75 4.81l-6.286 6.287a.253.253 0 0 0-.064.108l'
    '-.558 1.953 1.953-.558a.253.253 0 0 0 .108-.064Zm1.238-3.763a.25.25 0 0 0-.354 0L10.811 '
    '3.75l1.439 1.44 1.263-1.263a.25.25 0 0 0 0-.354Z"/></svg>'
)
ICON_DOWNLOAD = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M7.47 10.78a.75.75 0 0 0 1.06 0l3.75-3.75a.749.749 0 0 0-.326-1.275.749.749 0 0 0'
    '-.734.215L8.75 8.689V1.75a.75.75 0 0 0-1.5 0v6.939L4.78 5.97a.749.749 0 0 0-1.275.326.749'
    '.749 0 0 0 .215.734ZM3.75 13a.75.75 0 0 0 0 1.5h8.5a.75.75 0 0 0 0-1.5Z"/></svg>'
)
# What the secret row shows until someone asks for the value. Eight
# bullets, not the real length: even a length is a hint nobody needs on a
# page that may be on screen in a call.
SECRET_MASK = "&bull;" * 8

ICON_COPY = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5'
    'c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 '
    '9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25Z"/>'
    '<path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 '
    '14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25'
    'h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z"/></svg>'
)
ICON_CHECK = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L1.72 9.78a.751.751 '
    '0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 11.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>'
)
ICON_TRASH = (
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<path d="M11 1.75V3h2.25a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5H5V1.75C5 .784 5.784 '
    '0 6.75 0h2.5C10.216 0 11 .784 11 1.75ZM4.496 6.675l.66 6.6a.25.25 0 0 0 .249.225h5.19'
    'a.25.25 0 0 0 .249-.225l.66-6.6a.75.75 0 0 1 1.492.149l-.66 6.6A1.748 1.748 0 0 1 '
    '10.595 15h-5.19a1.75 1.75 0 0 1-1.741-1.575l-.66-6.6a.75.75 0 1 1 1.492-.15ZM6.5 1.75'
    'V3h3V1.75a.25.25 0 0 0-.25-.25h-2.5a.25.25 0 0 0-.25.25Z"/></svg>'
)

# Progressive enhancement: submit forms via fetch and patch the three
# swap regions (message, secrets list, sessions list) in place, so
# changes show up without a page reload; poll briefly while a session
# is still "starting" so the state flips to "live" on its own. The
# inline confirm() guards run before the submit event reaches us — a
# dismissed dialog cancels the event, so we only see accepted ones.
# Idiomorph (0BSD) morphs the live DOM toward re-rendered HTML instead of
# replacing it, which is what lets a swap keep focus, caret and scroll. It is
# vendored rather than fetched: the pages load no external asset, and a
# deployed box fetches this module as a single file (issue #51), so there is
# no second request to make. modules/src/vendor/vendor.json is its pin —
# `python3 scripts/check_vendor.py` verifies it and reports upstream releases.
# Its own <script> element, not a concatenation: a minified bundle that
# happens to end without a semicolon would otherwise let ASI glue it onto the
# IIFE that opens settings.js, turning the pair into a call. Two elements are
# parsed independently, so an upstream bump cannot introduce that. The `var`
# still lands on window, which is how settings.js reaches it.
SCRIPT = """<script>
@@include:vendor/idiomorph.min.js@@
</script>
<script>
@@include:settings.js@@
</script>
"""


def render_keys(keys):
    base = html.escape(BASE)
    rows = []
    for key in keys:
        safe = html.escape(key)
        rows.append(
            f'<li><span class="nm">{ICON_LOCK}<code>{safe}</code></span>'
            f'<span class="acts">'
            f'<button type="button" class="icon" data-edit="{safe}" '
            f'aria-label="Edit" title="Update {safe}">{ICON_PENCIL}</button>'
            f'<form class="inline" method="post" action="{base}/delete" '
            f'onsubmit="return confirm(\'Delete {safe}?\');">'
            f'<input type="hidden" name="key" value="{safe}">'
            f'<button type="submit" class="icon idanger" aria-label="Delete" '
            f'title="Delete {safe}">{ICON_TRASH}</button></form>'
            f'</span></li>'
        )
    body = "".join(rows) if rows else '<li class="empty">No secrets yet.</li>'
    return '<ul class="tbl"><li class="tbl-head">Name</li>' + body + "</ul>"


# The value the profile picker posts for a bare shell session. A profile
# NAME can only hold [A-Za-z0-9_-], so a leading ":" cannot collide with a
# real profile - including one somebody names "shell", which is legal (what
# #493 refuses is HARNESS=shell, not the name).
SHELL_PSEUDO_PROFILE = ":shell"


def render_profile_options(profiles):
    """The session row's profile <select> - now the ONLY control on it
    (issue #493).

    It used to sit beside an assistant <select> and override it. That pair
    could not express what a box actually is any more: a box installs its
    assistants on demand, so there is no default one for the other select to
    preselect, and two controls that both name an assistant made "which of
    these two did I just change" a question the page could not answer.

    So the profile is the whole answer, and every entry here starts a
    session on its own terms. `shell` is offered as a pseudo-profile rather
    than a profile file: it is a session KIND with no model, reasoning level
    or instructions to save, which is the same reason agent-box-profile
    refuses it as a HARNESS.

    Nothing is preselected. The box no longer has a default assistant, so
    the page asks rather than guessing - the rule the profile editor's own
    picker already follows.

    Without a resolver there is no picker at all. The settings unit is
    socket activated with stopIfChanged = false, so a daemon that survived
    an update can still be running on the environment it started with - and
    on one that predates AGENT_BOX_PROFILE_BIN every pick would fail. Such a
    daemon can still start a shell, which is the one entry needing no
    resolver, and says why the rest are missing.
    """
    shell_opt = ('<option value="%s">shell (no assistant)</option>'
                 % SHELL_PSEUDO_PROFILE)
    if not PROFILE_BIN:
        return ('<option value="" disabled selected>Restart the settings '
                'service to list profiles</option>' + shell_opt)
    items = ['<option value="" disabled selected>Choose a profile</option>']
    for name in sorted(profiles):
        safe = html.escape(name)
        harness = html.escape(profiles[name]["reserved"].get("HARNESS") or "")
        label = f"{safe} ({harness})" if harness else safe
        items.append(f'<option value="{safe}">{label}</option>')
    items.append(shell_opt)
    return "".join(items)


# The harnesses a PROFILE may name: the installed ones, minus "shell"
# (issue #493). A profile is a worker - a harness plus the model, effort and
# prompt that tell two workers on it apart - and "shell" has none of those,
# so a profile built around it configures nothing. It stays in AGENTS,
# because a shell SESSION is still a session kind; only the profile editor
# refuses it. agent-box-profile applies the same rule, so the page cannot
# offer a value the CLI would reject.
PROFILE_AGENTS = [a for a in AGENTS if a != "shell"]


def render_harness_options(selected=""):
    """The profile editor's assistant <select>.

    It has a blank entry, but that entry is a PROMPT and not a value: a
    profile must name its assistant (issue #493). There used to be a
    "default assistant" option here, resolving through the box-wide default
    agent the same way a session with no --harness does. A box now starts with
    no assistant installed and installs them on demand, so "the default one"
    names nothing anybody chose - and a profile that resolved through it
    would quietly become a different worker the day the box's configuration
    changed. So the blank entry is unselectable: the form asks, and the
    resolver refuses a profile that never answered.
    """
    blank_sel = "" if selected else " selected"
    items = ['<option value="" disabled%s>Choose an assistant</option>'
             % blank_sel]
    for agent in PROFILE_AGENTS:
        safe = html.escape(agent)
        sel = " selected" if agent == selected else ""
        items.append(f'<option value="{safe}"{sel}>{safe}</option>')
    return "".join(items)


# The reasoning-level values a profile's EFFORT may hold (issue #493's
# layout checklist: a picker, not free text). These are the labels claude's
# own --effort accepts; agent-box-profile passes the same string straight
# through to codex as `-c model_reasoning_effort=VALUE`, so one list serves
# both today's harnesses without the picker needing to know which one a
# profile names. A harness that cannot use EFFORT at all (there is none
# yet) would simply never be asked, exactly as a blank free-text field
# never was.
PROFILE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def render_effort_options(selected=""):
    """The profile editor's reasoning-level <select>. Blank is a real,
    selectable value ("default") rather than a prompt: it means "start this
    profile's harness with no --effort of its own", the same thing an empty
    free-text field used to mean. A SELECTED value outside the known list
    (set by an older free-text field, or by `agent-box-profile` directly) is
    kept as its own option rather than silently dropped - the alternative is
    a select that shows "default" while the stored profile still carries the
    old value, so hitting Save with nothing touched would erase it.
    """
    blank_sel = "" if selected else " selected"
    items = ['<option value=""%s>default</option>' % blank_sel]
    known = selected in PROFILE_EFFORT_LEVELS
    if selected and not known:
        safe = html.escape(selected)
        items.append(f'<option value="{safe}" selected>{safe}</option>')
    for level in PROFILE_EFFORT_LEVELS:
        sel = " selected" if level == selected else ""
        items.append(f'<option value="{level}"{sel}>{level}</option>')
    return "".join(items)


def render_profiles(profiles):
    """The profiles list. Each row folds open onto its launch config and its
    environment KEY NAMES — never a value, the rule `agent-box-profile show`
    and `env ls` already keep, and the one that matters most here because a
    profile is exactly where a token ends up."""
    base = html.escape(BASE)
    rows = []
    for name in sorted(profiles):
        safe = html.escape(name)
        res = profiles[name]["reserved"]
        # The summary line answers "what worker is this" without a click:
        # the harness, then whatever narrows it.
        bits = []
        for key in ("HARNESS", "MODEL", "EFFORT"):
            if res.get(key):
                bits.append(html.escape(res[key]))
        if res.get("SYSTEM_PROMPT"):
            bits.append("instructions")
        env_keys = profiles[name]["env"]
        if env_keys:
            bits.append("%d custom setting%s" % (
                len(env_keys), "" if len(env_keys) == 1 else "s"))
        # Each bit was escaped as it went in, so the join must NOT be
        # escaped again: a MODEL holding "&" or "<" would render as visible
        # entity text ("&amp;lt;").
        meta = " · ".join(bits) if bits else "empty"
        # Environment keys, by name. Each gets its own delete button rather
        # than a form field to blank: dropping a key and setting it to the
        # empty string are different states in the store, and only one of
        # them is what "remove this" means.
        chips = []
        for key in env_keys:
            k = html.escape(key)
            chips.append(
                f'<li><span class="nm">{ICON_LOCK}<code>{k}</code></span>'
                f'<span class="acts"><form class="inline" method="post" '
                f'action="{base}/profiles/delkey" '
                f'onsubmit="return confirm(\'Remove {k} from profile {safe}?\');">'
                f'<input type="hidden" name="name" value="{safe}">'
                f'<input type="hidden" name="key" value="{k}">'
                f'<button type="submit" class="icon idanger" aria-label="Remove" '
                f'title="Remove {k} from {safe}">{ICON_TRASH}</button></form>'
                f'</span></li>'
            )
        env_list = ('<ul class="tbl subs">' + "".join(chips) + "</ul>") if chips else (
            '<p class="note">No custom settings.</p>')
        # Pre-filled edit form: the launch fields come back as they are
        # stored, so an edit is an edit and not a retype. SYSTEM_PROMPT is a
        # textarea for the same reason it is one above — it can span lines.
        prompt_val = html.escape(res.get("SYSTEM_PROMPT") or "")
        rows.append(
            f'<li class="foldrow prof-row"><details><summary>'
            f'<span class="nm"><code>{safe}</code></span>'
            f'<span class="meta">{meta}</span>'
            f'<span class="acts"><form class="inline" method="post" '
            f'action="{base}/profiles/delete" '
            f'onsubmit="return confirm(\'Delete profile {safe}? Sessions already '
            f'running keep what they started with.\');">'
            f'<input type="hidden" name="name" value="{safe}">'
            f'<button type="submit" class="icon idanger" aria-label="Delete" '
            f'title="Delete profile {safe}">{ICON_TRASH}</button></form></span>'
            f'</summary><div class="fold">'
            f'<form method="post" action="{base}/profiles/set">'
            f'<input type="hidden" name="name" value="{safe}">'
            f'<div class="row profile-row">'
            f'<select name="HARNESS" aria-label="Assistant for {safe}" required>'
            f'{render_harness_options(res.get("HARNESS") or "")}</select>'
            f'<input type="text" name="MODEL" value="{html.escape(res.get("MODEL") or "")}" '
            f'placeholder="model" class="pmodel" autocomplete="off" '
            f'aria-label="Model for {safe}">'
            f'<select name="EFFORT" aria-label="Reasoning level for {safe}">'
            f'{render_effort_options(res.get("EFFORT") or "")}</select>'
            f'<button type="submit" class="btn">Save</button></div>'
            f'<div class="row prompt-row"><textarea name="SYSTEM_PROMPT" rows="2" '
            f'spellcheck="false" placeholder="instructions" '
            f'aria-label="Instructions for {safe}">{prompt_val}</textarea></div>'
            f'</form>'
            f'<p class="note">These values are added whenever a session starts '
            f'with this profile. They are hidden here, but sessions under the '
            f'same account can access one another&rsquo;s values. Do not use '
            f'profiles to separate trusted and untrusted work.</p>'
            f'{env_list}'
            f'<form class="row" method="post" action="{base}/profiles/setkey">'
            f'<input type="hidden" name="name" value="{safe}">'
            f'<input type="text" name="key" placeholder="KEY_NAME" required '
            f'pattern="[A-Za-z_][A-Za-z0-9_]*" autocomplete="off" '
            f'aria-label="Setting name for {safe}">'
            f'<input type="password" name="value" placeholder="value" required '
            f'autocomplete="off" aria-label="Value for that key">'
            f'<button type="submit" class="btn">Add</button></form>'
            f'</div></details></li>'
        )
    body = "".join(rows) if rows else (
        '<li class="empty">No profiles yet. Add one, or start a session '
        'with &quot;shell (no assistant)&quot;.</li>')
    return '<ul class="tbl"><li class="tbl-head">Profile</li>' + body + "</ul>"


def display_cwd(value):
    """Compact working-directory label for a session row: "~" for the
    default (stored None), and an absolute path is shown home-relative
    (~/foo) when it sits under HOME."""
    if not value:
        return "~"
    if value == HOME_DIR:
        return "~"
    if value.startswith(HOME_DIR + os.sep):
        return "~/" + value[len(HOME_DIR) + 1:]
    return value


def render_sessions(subs=None):
    """The Sessions panel. With `subs` (name -> webhook_view() entry) each
    row folds open onto that session's own subscriptions: a subscription
    is read as "what does THIS session receive", and a separate list of
    rows tagged with a session name made the reader do that join by eye."""
    entries = {n: v for n, v in read_sessions().items() if SESSION_RE.match(n)}
    base = html.escape(SESS_BASE)
    term = html.escape(TERM_HOME)
    if not entries:
        body = '<li class="empty">No sessions defined.</li>'
    else:
        live = live_sessions()
        items = []
        for name in sorted(entries):
            safe = html.escape(name)
            agent = html.escape(str(entries[name].get("agent") or "?"))
            # Which WORKER this session is, when it is more than a bare
            # harness (#321). The registry records the profile NAME, so this
            # answers "what was it started as" even after the profile itself
            # was edited or deleted — which is the honest answer, since the
            # arguments it is running with were resolved at create time.
            prof = entries[name].get("profile")
            prof_tag = ""
            if prof:
                # Its own balanced fragment, not a `</span><span…` splice
                # appended to `agent`: that spliced form was correct only
                # while the caller below wrapped `agent` in exactly one
                # <span>, so moving `agent` into an attribute or a different
                # element would have emitted broken markup with nothing to
                # say so.
                pf = html.escape(str(prof))
                prof_tag = (f'<span class="meta prof-tag" '
                            f'title="Agent profile it was started with">'
                            f'{pf}</span>')
            cwd = html.escape(display_cwd(entries[name].get("workingDirectory")))
            # stopped = listed but deliberately down (clean agent exit or
            # agent-box-session stop); the same route revives it.
            #
            # died = the agent CRASHED and the pane it left behind is a
            # post-mortem shell (issue #516). tmux reports that pane as a
            # live session, so liveness alone said "Running" about a session
            # with no agent in it at all -- which is how a codex Remote
            # Control daemon died on the deployed box with every surface
            # still claiming it was up. Only meaningful while the session IS
            # live: once the pane goes, the supervisor is bringing it back
            # and "Starting" is the true answer.
            dead = crashed_status(entries[name])
            if name in live:
                state = "died" if dead is not None else "live"
            elif entries[name].get("stopped"):
                state = "stopped"
            else:
                state = "starting"
            state_label = {
                "live": "Running",
                "starting": "Starting",
                "stopped": "Stopped",
                "died": "Died",
            }[state]
            # The exit status is all anyone knows about the crash without
            # attaching to the pane, so it goes where the state is read.
            state_tip = ""
            if state == "died":
                how = ("with status %d" % dead) if dead else "with an error"
                state_tip = (
                    ' title="The agent exited %s. The pane is a post-mortem '
                    'shell &mdash; restart it to bring the agent back."' % how
                )
            # One route, two verbs: /sessions/restart clears the stopped
            # flag and kills the pane, so on a session that is already down
            # it only STARTS one. Nothing is running to lose there, and
            # calling that "Restart" behind a "work is lost" prompt asked
            # the operator to accept a risk that does not exist (#241).
            if state == "stopped":
                verb, guard = "Start", ""
            elif state == "died":
                # Same reasoning as the stopped row (#241): the confirm asks
                # the operator to accept losing work that is not there. The
                # verb stays Restart, because the pane IS there and this
                # replaces it.
                verb, guard = "Restart", ""
            else:
                verb = "Restart"
                guard = (f' onsubmit="return confirm(\'Restart {safe}? Any '
                         f'work that has not been saved will be lost.\');"')
            # Download the session's own transcript (issue #248), when the
            # daemon can find one. A GET on a read-only route, so it is a
            # link and not a form — and no button at all for a session with
            # nothing to download, rather than one that 404s. download=
            # keeps the browser from rendering the JSONL in the tab. Safe
            # inside the row's <summary>: a click whose activation target is
            # the link never reaches the summary's own toggle (measured in
            # chromium, and the same rule the Restart button relies on).
            found = transcript_of(name, entries[name])
            topic = transcript_topic(found[0], found[1]) if found else ""
            if found:
                # Both halves of "which conversation is this": the opening
                # prompt and when the file was last appended to. html.escape
                # is not decoration here — the topic is a prompt the operator
                # typed, so it reaches an attribute as data (issue #277).
                detail = "%s, last written %s" % (
                    human_size(found[1]), when_written(found[2]))
                if topic:
                    tip = 'Download transcript "%s" (%s)' % (topic, detail)
                else:
                    tip = "Download transcript (%s)" % detail
                tip = html.escape(tip)
                download = (
                    f'<a class="icon" href="{base}/sessions/transcript?name={safe}" '
                    f'download aria-label="{tip}" title="{tip}">{ICON_DOWNLOAD}</a>'
                )
            else:
                download = ""
            # The same answer on the row itself, so choosing does not need a
            # hover: CSS ellipsizes it rather than pushing the actions out.
            if topic:
                shown = html.escape(topic)
                subject = ('<span class="meta topic" title="Conversation: '
                           f'{shown}">{shown}</span>')
            else:
                subject = ""
            row = (
                # The name deep-links into that session's own path. No
                # userinfo in the href (issue 56).
                f'<span class="nm">'
                f'<a class="sess" href="{term}{safe}/"><code>{safe}</code></a>'
                f'<span class="meta">{agent}</span>'
                f'{prof_tag}'
                f'<span class="meta" title="Starting folder"><code>{cwd}</code></span>'
                f'{subject}'
                f'<span class="state" data-state="{state}"{state_tip}>{state_label}</span>'
                f'{render_subs_chip(subs, name)}</span>'
                f'<span class="acts">'
                f'{download}'
                f'<form class="inline" method="post" '
                f'action="{base}/sessions/restart"{guard}>'
                f'<input type="hidden" name="name" value="{safe}">'
                f'<input type="hidden" name="back" value="settings">'
                f'<button type="submit" class="btn small">{verb}</button></form>'
                f'<form class="inline" method="post" action="{base}/sessions/delete" '
                f'onsubmit="return confirm(\'Delete session {safe}? Its running '
                f'assistant will stop.\');">'
                f'<input type="hidden" name="name" value="{safe}">'
                f'<input type="hidden" name="back" value="settings">'
                f'<button type="submit" class="icon idanger" aria-label="Delete" '
                f'title="Delete {safe}">{ICON_TRASH}</button></form>'
                f'</span>'
            )
            if subs is None:
                items.append(f"<li>{row}</li>")
            else:
                # data-fold is the handle the live feed's DOM swap restores
                # an open row by; without it every session state change
                # would shut a fold the operator had just opened.
                items.append(
                    f'<li class="foldrow"><details data-fold="subs-{safe}">'
                    f'<summary>{row}</summary>'
                    f'{render_session_subs(subs.get(name), name)}'
                    f'</details></li>'
                )
        body = "".join(items)
    return '<ul class="tbl"><li class="tbl-head">Session</li>' + body + "</ul>"


WEBHOOK_STATES = {
    # label, and the note the operator needs to read the row correctly.
    # Every state but "listening" delivers nothing; the note says how it
    # got there, because "unsubscribed from everything" and "never
    # subscribed" invite different next moves.
    #
    # None of it names the filter FILE. That a subscription is a line in a
    # JSON file is this daemon's business and webhook.py's; the operator
    # reading the row is being told what the session receives.
    "listening": ("receiving events", ""),
    "empty": (
        "event notifications off", "This session is not receiving events."),
    "absent": (
        "event notifications off",
        "This session has not been connected to any events."),
    "invalid": (
        "event setup error",
        "This session's event settings cannot be read, so it is not receiving "
        "events. Subscribe again to replace them, or select Clear to remove "
        "them."),
    "off": (
        "event notifications paused", "Events are paused for this session."),
    "unknown": (
        "event status unavailable",
        "Could not read this session's event settings."),
}


def render_subs_chip(subs, name):
    """The one thing a session's row says about its webhooks while folded:
    how many topics it receives, or which kind of nothing."""
    if subs is None:
        return ""
    sub = subs.get(name)
    if sub is None:
        return ""
    count = len(sub["topics"])
    if sub["state"] == "listening":
        label = "1 event notification" if count == 1 else "%d event notifications" % count
    else:
        # A muted session HAS topics and receives none of them, so the
        # count would be the one thing the row must not say.
        label = WEBHOOK_STATES.get(sub["state"], WEBHOOK_STATES["unknown"])[0]
    return f'<span class="meta subs-chip">{html.escape(label)}</span>'


def display_event_expiry(value):
    """Turn the receiver's terse duration into a self-explanatory label."""
    value = str(value or "").strip()
    if not value:
        return ""
    if value.lower().startswith("never"):
        return "Always active"
    return "Expires in " + value


def render_session_subs(sub, name):
    """The fold under one session row: its topics, and the state line that
    says why there are none. Its last row drops the whole filter file,
    which is the only cleanup an empty or unparseable one has — the CLI's
    unsubscribe takes a topic, and neither of those has one to name."""
    safe = html.escape(name)
    # Only the hint: the state's LABEL is already the chip on the summary
    # line, and a fold that opens onto the word it was folded under says
    # nothing.
    hint = WEBHOOK_STATES.get(
        (sub or {}).get("state") or "unknown", WEBHOOK_STATES["unknown"])[1]
    rows = []
    for entry in (sub or {}).get("topics") or []:
        topic = str(entry.get("topic") or "")
        if topic:
            rows.append(render_webhook_row(
                topic,
                [display_event_expiry(entry.get("expiresIn"))],
                str(entry.get("note") or ""),
                sub["key"],
                dispatch=False,
            ))
    if sub and sub["file"]:
        # Two labels, because one word cannot be honest about both cases:
        # a session with topics is being unsubscribed from them, and one
        # without is having a leftover state cleared. Neither says "file".
        count = len(sub["topics"])
        if count:
            label = "Stop all notifications"
            ask = ("Stop all event notifications for %s? This removes %d rule%s."
                   % (name, count, "" if count == 1 else "s"))
        else:
            label = "Clear"
            ask = "Clear the unreadable event settings for %s?" % name
        forget = (
            f'<form class="inline" method="post" action="{html.escape(BASE)}/webhooks/forget" '
            f'onsubmit="return confirm(\'{html.escape(ask, quote=True)}\');">'
            f'<input type="hidden" name="name" value="{safe}">'
            f'<button type="submit" class="btn small danger-btn" '
            f'title="{html.escape(ask, quote=True)}">{label}</button></form>'
        )
    else:
        forget = ""
    # A listening session needs no hint: the topics are right above and
    # the button says what it does, so the last row is just the action.
    note = f'<span class="note wh-note">{html.escape(hint)}</span>' if hint else ""
    rows.append(
        f'<li class="sub-state"><span class="nm wh">{note}</span>'
        f'<span class="acts">{forget}</span></li>'
    )
    return '<ul class="tbl subs">' + "".join(rows) + "</ul>"


def render_webhook_row(topic, meta, note, key, dispatch, fold=""):
    """One subscription row: what it is, why it exists, and its delete.
    Every row this renders names a topic, so every row can drop it.

    `fold` is text the row hides behind a disclosure — what a standing watch
    launches (#259): the agent CLI and its arguments, then the prompt. Too
    long to sit on the row itself and too useful to leave off the page."""
    base = html.escape(BASE)
    safe_topic = html.escape(topic)
    bits = "".join(
        f'<span class="meta">{html.escape(b)}</span>' for b in meta if b
    )
    note_html = (
        f'<span class="note wh-note">{html.escape(note)}</span>' if note else ""
    )
    item_name = "automatic session rule" if dispatch else "event notification rule"
    row = (
        f'<span class="nm wh"><code>{safe_topic}</code>{bits}{note_html}</span>'
        f'<span class="acts">'
        f'<form class="inline" method="post" action="{base}/webhooks/unsubscribe" '
        f'onsubmit="return confirm(\'Delete the {item_name} for {safe_topic}?\');">'
        f'<input type="hidden" name="topic" value="{safe_topic}">'
        f'<input type="hidden" name="key" value="{html.escape(key)}">'
        f'<input type="hidden" name="dispatch" value="{"1" if dispatch else ""}">'
        f'<button type="submit" class="icon idanger" aria-label="Delete" '
        f'title="Delete {item_name} for {safe_topic}">{ICON_TRASH}</button></form>'
        f'</span>'
    )
    if not fold:
        return f"<li>{row}</li>"
    # Same fold shape as a session row (data-fold and all, so the live
    # feed's DOM swap restores an open one). The label inside says which
    # parts of the prompt an event fills in; without it the placeholders
    # read as text the session would really receive.
    return (
        f'<li class="foldrow"><details data-fold="watch-{safe_topic}">'
        f'<summary title="Show what this rule starts">{row}</summary>'
        f'<div class="wh-prompt"><p class="note">A matching event starts a new '
        f'session with the setup and instructions shown below. Text in '
        f'&lt;&hellip;&gt; comes from the event.</p>'
        f'<pre>{html.escape(fold)}</pre></div>'
        f'</details></li>'
    )


def copy_button(what, value=None, secret_url=None):
    """A one-click copy for a value the operator has to paste elsewhere.

    Either `value` — text already in the page, copied straight from the
    attribute — or `secret_url`, a route the button fetches on the click
    (which is how a secret gets copied without being rendered).

    Takes VALUES and escapes them here, rather than an `attrs` fragment
    the caller assembles: a parameter that is raw markup makes escaping
    the caller's job to remember every time, on a button whose whole
    purpose is handling credentials (#421 review). Both icons ship inside
    it and CSS picks which one shows, so the "copied" tick needs no icon
    markup in the script."""
    attr = ('data-copy="%s"' % html.escape(value, quote=True) if value
            else 'data-secret-url="%s"' % html.escape(secret_url or "", quote=True))
    return (
        '<button type="button" class="icon icopy" %s '
        'aria-label="Copy %s" title="Copy to clipboard">'
        '<span class="ci">%s</span><span class="co">%s</span></button>'
        % (attr, html.escape(what, quote=True), ICON_COPY, ICON_CHECK)
    )


def rotate_form(source):
    """Replace one source's secret, from the page.

    A form, not a fetch button: with no JS it still works, like every
    other action here. The confirm() is not ceremony — this is a hard
    cutover (see webhook_rotate()), so the dialog is where someone finds
    out that the sender has to be updated before it says the words on a
    banner they have already triggered."""
    safe = html.escape(source)
    return (
        '<form class="inline" method="post" action="%s/webhooks/rotate" '
        'onsubmit="return confirm(\'Replace the %s secret? Events will stop '
        'until you update the connected service, and some missed events may '
        'not be retried.\');">'
        '<input type="hidden" name="source" value="%s">'
        '<button type="submit" class="icon ismall" '
        'aria-label="Replace the %s secret" '
        'title="Create a new secret">Replace</button></form>'
        % (html.escape(BASE), safe, safe, safe)
    )


def render_webhook_endpoint():
    """What to register in the sender: the payload URL and the secret,
    per configured source, each with a copy button.

    Both were previously reachable only by running `agent-box-webhook
    setup`/`url` inside a session, which is the wrong place for them:
    registering a webhook is a browser job, done with the sender's own
    settings open in the next tab, by an operator who may have no shell
    here at all — and the two values are pasted, not read, so the button
    matters as much as the value.

    A row per configured source, because the per-source path is what the
    sender's Payload URL field wants, and the default source's row says
    so — a URL registered with no source on the end routes there. With no
    source configured there is nothing to paste yet, and saying so is
    more use than printing a URL that rejects every delivery.

    The secret is NOT rendered: the row shows a mask, and Show/Copy fetch
    it from {BASE}/webhooks/secret on the click. Same reasoning as
    webhook_secret() — the value is the operator's to have, but a value
    that is not in the HTML cannot leak through a screenshot of this page
    or through the live feed's DOM swaps."""
    if not WEBHOOK_URL:
        return ""
    # Raw values, escaped at the point of use — copy_button() and
    # rotate_form() take values, not markup, and escape their own.
    endpoint = WEBHOOK_URL.rstrip("/")
    base = html.escape(endpoint)
    default, names = webhook_sources()
    rows = []
    for name in names:
        safe = html.escape(name)
        secret_route = (BASE + "/webhooks/secret?source="
                        + urllib.parse.quote(name, safe=""))
        # Naming the default is not decoration: a URL registered with no
        # source on the end routes to it, so this is the one row that also
        # explains an endpoint someone pasted bare months ago.
        tag = (" &middot; default service"
               if name == default else "")
        rows.append(
            '<li><span class="nm wh"><code>%s/%s</code>'
            '<span class="meta">%s%s</span>'
            '<span class="wh-secret"><span class="meta">Secret</span>'
            '<code data-secret-out>%s</code>'
            '<button type="button" class="icon ismall" data-reveal '
            'data-secret-url="%s" aria-label="Show the %s secret" '
            'title="Show the secret">Show</button>%s%s</span>'
            '</span>'
            '<span class="acts">%s</span></li>'
            % (base, safe, safe, tag,
               SECRET_MASK, html.escape(secret_route, quote=True), safe,
               copy_button("the %s secret" % name, secret_url=secret_route),
               rotate_form(name),
               copy_button("the %s payload URL" % name,
                           value="%s/%s" % (endpoint, name)))
        )
    if not rows:
        # Prose, not a one-row table: the table's job is to list the
        # senders that exist, and there are none to list.
        return WEBHOOK_ENDPOINT_EMPTY_TPL.format(base=base)
    return WEBHOOK_ENDPOINT_TPL.format(
        rows=('<ul class="tbl"><li class="tbl-head">Webhook URL</li>'
              + "".join(rows) + "</ul>")
    )


def render_webhooks(watches):
    """The standing watches. Session subscriptions are NOT here: they
    belong to a session and are folded into its row above. A standing
    watch belongs to no session — it spawns one — so this list is where
    it can be seen at all.

    The note is NOT on the row (#259). A watch note is written for the
    session the watch spawns — the dispatcher quotes it into that session's
    prompt — so on the row it is a paragraph of someone else's briefing,
    and the panel's own description already says what a standing watch is.
    The fold carries the whole prompt instead, note included, in the frame
    that explains why that wording exists."""
    rows = []
    stamp = hook_args_stamp()
    for entry in watches:
        topic = str(entry.get("topic") or "")
        if not topic:
            continue
        note = str(entry.get("note") or "")
        prompt = hook_preamble(topic, note, stamp)
        rows.append(render_webhook_row(
            topic,
            [display_event_expiry(entry.get("expiresIn"))],
            # Only when the prompt could not be rendered: then the note is
            # the one thing left that says why this watch exists.
            "" if prompt else note,
            # A standing watch is shared, so any key edits the same list.
            webhook_key(""),
            dispatch=True,
            fold=prompt,
        ))
    if not rows:
        rows.append('<li class="empty">No automatic session rules.</li>')
    return ('<ul class="tbl"><li class="tbl-head">Automatic session rule</li>'
            + "".join(rows) + "</ul>")


def render_reboot_line():
    """Why a reboot is worth pressing, or nothing at all.

    A red button with no reason gets pressed out of curiosity; one that
    answers a question the box is already asking gets pressed when it
    should be. So the card stays silent until the distro says a reboot
    would apply something, and then names what.
    """
    pending, packages = reboot_pending()
    if not pending:
        return ""
    reason = ""
    if packages:
        reason = " (" + html.escape(", ".join(packages)) + ")"
    return ("<br><strong>A restart is required</strong>" + reason
            + " &mdash; restart agent-box to finish installing the update.")


def render_update_line():
    """Running rev plus a progressively enhanced GitHub update status.

    REV is a full git sha; the label shows the usual short form. Empty
    when the module didn't pass a rev (selfUpdate off — but then the
    whole Update card is hidden anyway). Without JavaScript, the user
    still gets a direct GitHub comparison link.
    """
    if not REV:
        return ""
    label = f"<code>{html.escape(REV[:12])}</code>"
    if REPO:
        url = html.escape(f"https://github.com/{REPO}/commit/{REV}")
        label = f'<a href="{url}">{label}</a>'
    line = " Installed version: " + label + "."
    if not REPO:
        return line
    repo = html.escape(REPO)
    rev = html.escape(REV)
    compare_url = html.escape(f"https://github.com/{REPO}/compare/{REV}...HEAD")
    return (
        line
        + f' <span id="update-status" class="update-state" aria-live="polite" '
          f'data-repo="{repo}" data-rev="{rev}" data-compare-url="{compare_url}">'
          f'<a href="{compare_url}">Check GitHub for changes</a>.</span>'
    )


def render_connect_card(state):
    """One flow's row, plus the wizard step under it while a sign-in is
    in flight. Rendered server-side on purpose: the tmux session is the
    only state, so a reload, a second tab or a daemon restart all show
    the same step — and the page keeps working without JavaScript.

    The row folds like a session row (issue #449): the connection list
    only grows, so a card with nothing to say must take one line, not a
    full paragraph. It opens by default only while it needs the operator
    right now — see the `open_now` rule below — so pressing "Sign in"
    never lands on a page where the wizard it just started is invisible
    until the row is expanded by hand."""
    base = html.escape(BASE)
    flow_id = html.escape(state["id"])
    pill = {
        "connected": ("live", "Signed in" + (" — " + html.escape(state["detail"])
                                             if state["detail"] else "")),
        "waiting": ("starting", "Waiting for you"),
        "starting": ("starting", "Starting…"),
        "failed": ("failed", "Not signed in"),
        "expired": ("failed", "Sign-in expired"),
        "exchanging": ("starting", "Finishing sign-in&hellip;"),
        "idle": ("stopped", "Not signed in"),
        "checking": ("stopped", "Checking&hellip;"),
    }[state["state"]]
    if not state["installed"] and state["state"] in ("idle", "checking",
                                                     "failed"):
        # "Not signed in" would be a half-truth for a CLI that is not even
        # here; the distinction matters because the fix is different (wait
        # for a download, not go through an OAuth flow).
        pill = ("stopped", "Not installed")
    if state["state"] in ("waiting", "starting", "exchanging"):
        label, confirm = None, False
    elif state["blocked"]:
        label, confirm = None, False
    elif state["state"] == "connected":
        label, confirm = "Sign in again", state["destructive"]
    elif not state["installed"]:
        # The CLI is not on this box yet (issue #416). Same button, same
        # pane — it just fetches first, and the label says so rather than
        # letting a several-minute download look like a hung sign-in.
        # A missing CLI is not a missing credential: a harness is fetched
        # on demand and its stored token outlives the binary, so a
        # destructive flow mid-probe keeps its guard here for the same
        # reason it does below — an unanswered probe is a guess, and a
        # guess must not be what skips the confirmation.
        label, confirm = ("Install & sign in",
                          state["state"] == "checking" and state["destructive"]) \
            if state["installable"] else (None, False)
    else:
        # "checking" falls in here too, on purpose. connect_status() never
        # blocks the render (a slow or unreachable network must not hold
        # the whole settings page hostage for a status pill), so EVERY
        # card starts "checking" on a cold cache and reverts to it whenever
        # CONNECT_STATUS_TTL lapses between renders — which is often, since
        # a probe is only as fresh as the last page load. The button used
        # to disappear for that whole window: nothing to click, no error,
        # no request, until the poll a beat later swapped the real button
        # back in. A quick operator hits that gap on nearly every reload,
        # and unevenly — gh's probe answers faster than claude's or
        # codex's, which is why only the GitHub card ever seemed to work.
        # connect_start() does not read this cache either way, so offering
        # the button is safe; the one thing it must not do is skip a
        # destructive flow's confirmation on the strength of a guess, so
        # that guard stays armed until the probe actually clears it.
        label, confirm = "Sign in", (state["state"] == "checking" and state["destructive"])
    action = ""
    if label:
        guard = ""
        if confirm:
            guard = (' onsubmit="return confirm(\'Sign in again? The current '
                     'credential is dropped as the new sign-in starts.\');"')
        action = (
            f'<form class="inline" method="post" action="{base}/connect/start"{guard}>'
            f'<input type="hidden" name="flow" value="{flow_id}">'
            f'<button type="submit" class="btn small">{label}</button></form>'
        )
    head = (
        f'<div class="conn-head"><strong>{html.escape(state["label"])}</strong>'
        f'<span class="acts"><span class="state" data-state="{pill[0]}">{pill[1]}</span>'
        f'{action}</span></div>'
    )
    note = state["note"]
    if not state["installed"] and state["installable"]:
        note += (' The first sign-in also installs this tool and may take a '
                 'few minutes.')
    step = render_connect_step(state)
    if state["blocked"]:
        step = ('<div class="conn-step"><p class="note">Add a session above '
                'before signing in.</p></div>')
    warn = ""
    if state["shadow"]:
        keys = ", ".join("<code>%s</code>" % html.escape(k) for k in state["shadow"])
        warn = (
            f'<div class="conn-step"><p class="note conn-warn">{keys} is set '
            f'under API keys and secrets. Sessions will use that value instead '
            f'of the account shown here.</p></div>'
        )
    # Open on anything the operator has to act on or read right now: a flow
    # mid-run, a fresh error, the "no session to sign in from" note, or a
    # shadow-env warning (it renders inside this same <details>, so a
    # closed card would hide it). Idle and connected cards with nothing
    # else to say stay closed (issue #449).
    open_now = (
        state["state"] in ("waiting", "starting", "checking", "exchanging")
        or (state["state"] in ("failed", "expired") and state["error"])
        or state["blocked"]
        or state["shadow"]
    )
    return (
        f'<li class="foldrow conn-row">'
        f'<details data-fold="conn-{flow_id}"{" open" if open_now else ""}>'
        f'<summary>{head}</summary><p class="note">{note}</p>{step}{warn}'
        f'</details></li>'
    )


def render_connect_step(state):
    """The wizard body: what the user has to do right now."""
    base = html.escape(BASE)
    flow_id = html.escape(state["id"])
    cancel = (
        f'<form class="inline" method="post" action="{base}/connect/cancel">'
        f'<input type="hidden" name="flow" value="{flow_id}">'
        f'<button type="submit" class="btn small">Cancel</button></form>'
    )
    if state["state"] == "starting":
        return (f'<div class="conn-step"><p class="note">Starting the '
                f'sign-in&hellip;</p>{cancel}</div>')
    if state["state"] == "exchanging":
        return ('<div class="conn-step"><p class="note">Sign-in complete. '
                'Checking the connection&hellip;</p></div>')
    if state["state"] in ("failed", "expired") and state["error"]:
        return (f'<div class="conn-step"><p class="note conn-error">'
                f'{html.escape(state["error"])}</p></div>')
    if state["state"] != "waiting":
        return ""
    url = html.escape(state["url"], quote=True)
    parts = [
        f'<p class="note"><strong>1.</strong> '
        f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
        f'Open the sign-in page</a> and approve the request.</p>'
    ]
    if state["code"]:
        parts.append(
            f'<p class="note"><strong>2.</strong> Enter this code on that '
            f'page: <code class="conn-code">{html.escape(state["code"])}</code>'
            f'{copy_button("the pairing code", value=state["code"])}</p>'
        )
    if state["needs_code"]:
        step = "3" if state["code"] else "2"
        parts.append(
            f'<form method="post" action="{base}/connect/code" class="row conn-form">'
            f'<input type="hidden" name="flow" value="{flow_id}">'
            f'<label class="field conn-field"><span class="note">'
            f'<strong>{step}.</strong> Paste the code the page gives you back'
            f'</span>'
            f'<input type="password" name="code" autocomplete="off" required '
            f'placeholder="code from the sign-in page"></label>'
            f'<button type="submit" class="btn">Submit code</button></form>'
        )
    parts.append(cancel)
    return '<div class="conn-step">' + "".join(parts) + "</div>"


def connect_fingerprint(states=None):
    """Short digest of every connect flow's state - what connectPoll
    compares {BASE}/status's answer against before paying for the full
    page fetch that used to run unconditionally every 2.5s while busy."""
    if states is None:
        keys = read_keys()
        tmux_state = tmux_sessions()
        states = [connect_state(flow, keys=keys, tmux_state=tmux_state)
                  for flow in connect_flows()]
    raw = json.dumps(states, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def render_connect():
    """Every card, plus the busy flag and fingerprint the page polls on."""
    keys = read_keys()
    tmux_state = tmux_sessions()
    states = [connect_state(flow, keys=keys, tmux_state=tmux_state)
              for flow in connect_flows()]
    busy = "1" if any(
        s["state"] in ("starting", "waiting", "checking", "exchanging")
        for s in states
    ) else "0"
    cards = "".join(render_connect_card(s) for s in states)
    return CONNECT_SECTION_TPL.format(
        cards='<ul class="tbl">' + cards + "</ul>", busy=busy,
        status_url=html.escape(BASE) + "/status",
        fp=connect_fingerprint(states))


def render_sessions_section(subs=None, profiles=None):
    return SESSIONS_SECTION_TPL.format(
        action_base=html.escape(SESS_BASE),
        new_session_fields=render_new_session_fields(profiles),
        sessions=render_sessions(subs),
    )


def render_new_session_fields(profiles=None):
    """Both add-session forms, settings page and workspace, come from here so
    their layout cannot drift (that is why the template is shared). The
    profile picker is passed IN rather than read here: the settings page
    already reads the profiles for its own panel, and one page render must
    not scan the directory twice."""
    if profiles is None:
        profiles = read_profiles()
    return NEW_SESSION_FIELDS_TPL.format(
        action_base=html.escape(SESS_BASE),
        profiles=render_profile_options(profiles),
    )


def pane_state(name, live, stopped):
    """Which of the three panes a session gets — the ONE derivation, read
    by the tab bar and by render_pane alike, and stamped into the markup
    so SCRIPT never has to re-derive it.

    It answers "is the pane attachable", which is not the question the
    tab's coloured dot answers. A session whose AGENT crashed still has a
    live tmux session holding the post-mortem shell, so its dot says
    `died` while its pane is `live` (issue #516). The client used to infer
    the pane from the dot and so tore that shell out on the next live-feed
    refresh, replacing the one surface that says WHY the agent died with a
    "starting…" placeholder (CodeRabbit on PR #522)."""
    if name in live:
        return "live"
    return "stopped" if name in stopped else "starting"


def render_tabs(names, live, stopped, died, selected):
    """The workspace tab bar. File order, not sorted: sessions.json
    preserves insertion order, so a new session appears as the
    rightmost tab, like any terminal app. The dot-only .state span
    reuses the list styling (its ::before is the dot).

    Each tab carries a close (x) button posting to the same
    /sessions/delete route the settings page uses (no back= field, so
    it redirects to the workspace). The button is a SIBLING of the tab
    link, not a child: a <form> inside an <a> is invalid markup, and
    keeping them apart also stops the tab-select click delegation from
    swallowing the close click. Closing kills a live agent and the
    button sits a few pixels from the session name, so SCRIPT arms it
    on the first click and only submits on the second.

    The name gets its own span so a long one (a dispatched
    hook-<owner/repo>-<hex> runs to 150 characters, and names are never
    shortened to fit — issue #236) ellipsizes instead of pushing the
    other tabs out of the bar; title carries it in full."""
    items = []
    base = html.escape(SESS_BASE)
    home = html.escape(TERM_HOME)
    for name in names:
        safe = html.escape(name)
        cur = ' aria-current="page"' if name == selected else ""
        if name in live:
            # The pane is up either way; whether an AGENT is in it is what
            # the dot has to say (issue #516).
            state = "died" if name in died else "live"
        elif name in stopped:
            state = "stopped"
        else:
            state = "starting"
        # The dot is the only cue this bar has room for, so a died session
        # gets the words in the tooltip: the row on the settings page is the
        # other place that says it, and neither is where the operator is
        # looking when a session dies under them.
        tip = (safe + " &mdash; the agent died") if state == "died" else safe
        # The dot's state and the PANE's state are different questions, and
        # the dot carries both answers side by side: `died` says the agent
        # is gone, `live` says its tmux session is still attachable. SCRIPT
        # reads data-pane-state and never guesses the second from the first
        # (issue #516). It rides on this span rather than on the <a> so that
        # the tab's own attribute order stays as it was.
        ps = pane_state(name, live, stopped)
        items.append(
            f'<span class="tab-wrap">'
            f'<a class="tab" data-tab="{safe}" href="{home}?tab={safe}"{cur}'
            f' title="{tip}">'
            f'<span class="state" data-state="{state}"'
            f' data-pane-state="{ps}"></span>'
            f'<span class="tab-name">{safe}</span></a>'
            f'<form class="tab-close" method="post" action="{base}/sessions/delete">'
            f'<input type="hidden" name="name" value="{safe}">'
            f'<button type="submit" class="tab-x" data-close="{safe}" '
            f'aria-label="Close {safe}" title="Close {safe}">&times;</button>'
            f'</form></span>'
        )
    if not items:
        items.append('<span class="tab-empty">No sessions yet.</span>')
    return "".join(items)


def render_msg(message, page, kind="ok"):
    """The page-level feedback banner ("Session added", "Key saved"…),
    with a dismiss (x) that works both ways: it is a LINK back to
    `page` — the same page minus the ?ok= that produced the banner —
    so a scriptless browser dismisses it by re-rendering, while SCRIPT
    intercepts the click and only removes the element. That
    distinction matters on the workspace, where an actual navigation
    would tear down every attached terminal iframe.

    Dismissal is manual only (issue #246). The banner sits in normal
    flow, so its removal resizes the panes below it; a move the user
    asked for reads as a response, the same move on a timer reads as
    the page lurching on its own.

    `kind` ("ok" / "warn" / "error") picks the CSS palette via
    data-kind - a failure used to render in the same green as a
    success, indistinguishable at a glance (issue #549). An error
    also gets role="alert" (assertive) instead of role="status"
    (polite). Every kind gets a copy button, since the operator has
    no shell here to select the banner text from and may need to
    paste it elsewhere, whichever kind it is."""
    if not message:
        return ""
    role = "alert" if kind == "error" else "status"
    return (
        f'<div class="msg" data-kind="{kind}" role="{role}">'
        f'<span class="msg-text">{html.escape(message)}</span>'
        f'{copy_button("this message", value=message)}'
        f'<a class="msg-x" href="{html.escape(page, quote=True)}" '
        f'aria-label="Dismiss" title="Dismiss">&times;</a>'
        f'</div>'
    )


def render_pane(selected, live, stopped):
    """The server-rendered pane: only the SELECTED session, and only
    when its tmux session is already live — the ttyd attach wrapper
    greets a not-yet-started session with an error and exits, so a
    starting session gets a placeholder instead (SCRIPT swaps in the
    iframe once the state flips; without JS, reloading does). A stopped
    session gets an honest placeholder: nothing is coming up until a
    start revives it.

    data-ph records which of the three states the pane was built for —
    on the iframe too, so SCRIPT can tell a terminal that is still wired
    to a live tmux session from one whose session has since gone (issue
    #241)."""
    if selected is None:
        return '<div class="pane placeholder active">No session selected.</div>'
    safe = html.escape(selected)
    state = pane_state(selected, live, stopped)
    if state == "stopped":
        # The Start button lives HERE, not only in the settings page's session
        # row: this pane is what the operator is looking at when they find out
        # the session is down, and sending them to another page to press a
        # button that changes THIS pane is a detour with a reload at the end
        # of it. Same route the row posts to, and back=workspace, so it
        # returns to the workspace with this session's tab selected. Named
        # explicitly rather than left to SESS_PAGE's default: /<user>/ is a
        # workspace for EVERY user, while that default is the settings page
        # for anyone but the primary one (CodeRabbit on PR #452).
        return (f'<div class="pane placeholder active" data-pane="{safe}" '
                f'data-ph="stopped">'
                f'<span class="ph-msg">{safe} is stopped &mdash; nothing '
                f'starts it on its own.</span>'
                f'<form class="inline" method="post" '
                f'action="{html.escape(SESS_BASE)}/sessions/restart">'
                f'<input type="hidden" name="name" value="{safe}">'
                f'<input type="hidden" name="back" value="workspace">'
                f'<button type="submit" class="btn small">Start</button>'
                f'</form></div>')
    if state == "starting":
        return (f'<div class="pane placeholder active" data-pane="{safe}" '
                f'data-ph="starting">{safe} is starting&hellip; '
                f'reload in a few seconds.</div>')
    return (f'<iframe class="pane active" data-pane="{safe}" data-ph="live" '
            f'src="{html.escape(session_url(selected))}" title="{safe} terminal" '
            f'allow="clipboard-read; clipboard-write"></iframe>')


def render_head(title):
    """Page head, carrying the live feed's handle: where to stream from,
    and the fingerprint of the state this HTML was rendered from — so a
    change landing between render and stream-connect is not missed (the
    first frame simply disagrees with data-fp and triggers a refresh)."""
    return HEAD_TPL.format(
        title=title,
        events=html.escape(SESS_BASE + "/sessions/events"),
        fp=session_fingerprint(),
        favicon=html.escape(FAVICON, quote=True),
    )


def render_page(message="", kind="ok"):
    msg_html = render_msg(message, BASE + "/", kind)
    # One pass over the subscription state per render, feeding both
    # panels: it forks the pinned CLI once per session, so the Sessions
    # rows and the standing watches must not each pay for their own.
    unavailable = webhook_unavailable()
    subs, watches = webhook_view() if not unavailable else (None, [])
    # One directory scan per render, feeding the picker in the Sessions row
    # and the panel below it — the same rule the subscription pass above
    # states, for the same reason.
    profiles = read_profiles()
    return (
        render_head("Settings &mdash; " + html.escape(USER))
        + STYLE
        + BODY.format(
            user=html.escape(USER),
            base=html.escape(BASE),
            term_home=html.escape(TERM_HOME),
            mark=POTATO_SVG,
            keys=render_keys(read_keys()),
            # Every user, primary included: the HOME root page is the
            # terminal workspace, so session CRUD lives here.
            sessions_section=render_sessions_section(subs, profiles),
            profiles_section=PROFILES_SECTION_TPL.format(
                base=html.escape(BASE),
                harnesses=render_harness_options(),
                effort=render_effort_options(),
                profiles=render_profiles(profiles),
            ),
            webhooks_section=(
                WEBHOOK_UNAVAILABLE_TPL.format(text=unavailable)
                if unavailable else
                WEBHOOKS_SECTION_TPL.format(
                    endpoint=render_webhook_endpoint(),
                    webhooks=render_webhooks(watches))
            ),
            connect_section=render_connect() if connect_flows() else "",
            message=msg_html,
            password_section=(
                PASSWORD_SECTION.format(base=html.escape(BASE))
                if PASSWORD_CMD else ""
            ),
            update_row=(
                UPDATE_ROW.format(base=html.escape(BASE), update_line=render_update_line())
                if UPDATE_CMD else ""
            ),
            reboot_row=(
                REBOOT_ROW.format(base=html.escape(BASE),
                                  reboot_line=render_reboot_line())
                if REBOOT_CMD else ""
            ),
        )
        + SCRIPT
    )


def render_users():
    """The vhost root on a box with more than one terminal user: which of
    them to open. Every user has their own auth on their own path, so this
    is a list of links and nothing more — it grants no access, and it says
    which user this browser is already authenticated as.

    Only the root daemon renders it, so reaching it means holding THAT
    user's password; everyone else bookmarks their own /<user>/ (which is
    the page this one links to) and never sees this list.
    """
    items = []
    for name in WEB_USERS:
        safe = html.escape(name)
        href = "/" + urllib.parse.quote(name, safe="") + "/"
        mine = " (you)" if name == USER else ""
        items.append(
            f'<li class="conn-row"><div class="conn-head">'
            f'<strong><a href="{href}">{safe}</a></strong>{mine}</div></li>'
        )
    return (
        render_head("Agent Box")
        + STYLE
        + '<main class="wrap"><section class="card"><div class="card-head">'
        + "<h2>Choose a user</h2></div>"
        + '<p class="note">Each user has their own terminal, sessions and '
        + "settings, behind their own sign-in.</p>"
        + '<ul class="tbl">' + "".join(items) + "</ul></section></main>"
    )


def render_home(message="", selected=None, kind="ok"):
    entries = {n: v for n, v in read_sessions().items() if SESSION_RE.match(n)}
    names = list(entries)
    if selected not in entries:
        selected = "main" if "main" in entries else (names[0] if names else None)
    live = live_sessions()
    stopped = {n for n, v in entries.items() if v.get("stopped")}
    died = {n for n, v in entries.items() if crashed_status(v) is not None}
    # Dismissing keeps the selected tab (SESSION_RE names are URL-safe).
    msg_html = render_msg(
        message, TERM_HOME + ("?tab=" + selected if selected else ""), kind)
    return (
        render_head("Agent Box &mdash; " + html.escape(USER))
        + STYLE
        + HOME_BODY.format(
            base=html.escape(BASE),
            action_base=html.escape(SESS_BASE),
            term_base=html.escape(TERM_HOME),
            tabs=render_tabs(names, live, stopped, died, selected),
            pane=render_pane(selected, live, stopped),
            new_session_fields=render_new_session_fields(),
            message=msg_html,
            icon_info=ICON_INFO,
            icon_gear=ICON_GEAR,
        )
        + SCRIPT
    )


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve the authenticated settings UI and its actions."""

    server_version = "agent-box-settings/1"

    def _under_base(self, path):
        """True if request path is BASE or under BASE. Caddy strips nothing,
        so we match the full public path."""
        return path == BASE or path == BASE + "/" or path.startswith(BASE + "/")

    def _send_html(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_transcript(self, name):
        """Stream one session's transcript to the browser as a download
        (issue #248).

        The path is resolved from the session RECORD, never from the
        request: the only client input is a session name, which must match
        SESSION_RE and be listed in sessions.json. So this route reads
        exactly the transcripts the sessions panel lists and cannot be
        pointed anywhere else in the home directory.

        Content-Length is the size at open time and a live session keeps
        appending, so send exactly that many bytes: writing past the
        declared length would desync the connection, and a short read
        (a rotated or truncated file) closes it instead of leaving the
        client waiting for bytes that will not come.
        """
        entries = read_sessions()
        if not SESSION_RE.match(name or "") or name not in entries:
            self._send_html("<h1>404</h1><p>No such session.</p>", status=404)
            return
        found = transcript_of(name, entries[name])
        if not found:
            # The panel offers no button in this case, so a request here is
            # a stale page or a hand-made URL: say which of the two states
            # it is rather than implying the session is unknown.
            self._send_html(
                "<h1>404</h1><p>No transcript found for this session yet.</p>",
                status=404,
            )
            return
        path, size, _mtime = found
        try:
            handle = open(path, "rb")
        except OSError:
            self._send_html("<h1>404</h1><p>Transcript is gone.</p>", status=404)
            return
        with handle:
            self.send_response(200)
            # JSONL (one event per line), which is not application/json. An
            # explicit attachment disposition plus nosniff keeps it a
            # download in every browser rather than a rendered page.
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                'attachment; filename="%s"' % download_name(name, path),
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            left = size
            while left > 0:
                chunk = handle.read(min(65536, left))
                if not chunk:
                    # File shrank under us: the body is short, so the
                    # connection must not be reused for another response.
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # Cancelled download (a long transcript over a phone
                    # link is easy to give up on). Expected, so it ends the
                    # response instead of raising into the server's
                    # handler and logging a traceback per cancel.
                    self.close_connection = True
                    return
                left -= len(chunk)

    def _peer_gone(self):
        """True once the client has closed its end of a stream.

        Server-Sent Events are one-way, so anything readable is EOF (or
        a pipelined request that will never be answered on this
        connection) — either way the stream is over. Checked between
        waits because otherwise a thread sits on the condition variable
        until its next keep-alive write finally fails, holding a slot
        against EVENTS_MAX_STREAMS long after the tab closed.
        """
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not readable:
            return False
        try:
            return not self.connection.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def _send_event(self, fingerprint):
        payload = json.dumps({"fp": fingerprint}).encode("utf-8")
        self.wfile.write(b"event: sessions\ndata: " + payload + b"\n\n")

    def _send_events(self):
        """Stream session-state changes as Server-Sent Events.

        Held open for the life of the page, so it costs one thread —
        capped at EVENTS_MAX_STREAMS, past which the client keeps its
        polling fallback rather than pinning threads here. Frames are
        `event: sessions` with a fingerprint payload; a `:` comment
        every EVENTS_KEEPALIVE seconds keeps idle intermediaries (and
        the write that notices a vanished client) alive.
        """
        start = WATCHER.subscribe()
        if start is None:
            self._send_json({"ok": False, "reason": "busy"}, status=503)
            return
        seq, known = start
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Caddy streams text/event-stream unbuffered on its own; the
            # header is for any other proxy the user puts in front (nginx
            # buffers proxied responses by default, which would stall this).
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # Reconnect delay for the browser, and an immediate byte so it
            # marks the stream open instead of waiting for the first change.
            self.wfile.write(b"retry: 3000\n\n")
            # Replay the current fingerprint straight away: state can have
            # moved between rendering the page and connecting here, and a
            # watcher already running for another tab would have counted
            # that change before this stream existed. The client ignores a
            # fingerprint it is already showing. Empty only while the first
            # sample is still pending, which lands within EVENTS_TICK.
            if known:
                self._send_event(known)
            self.wfile.flush()
            quiet = 0.0
            while True:
                latest, fingerprint = WATCHER.wait(seq, EVENTS_SLICE)
                if self._peer_gone():
                    return
                if latest != seq:
                    seq = latest
                    quiet = 0.0
                    self._send_event(fingerprint)
                else:
                    quiet += EVENTS_SLICE
                    if quiet < EVENTS_KEEPALIVE:
                        continue
                    quiet = 0.0
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            # The page navigated away or the socket died: nothing to
            # report, the client reconnects if it still wants the feed.
            pass
        finally:
            WATCHER.release()

    def _registry_refusal(self, verb, exc, page):
        """Answer a mutation route that could not read the registry
        (issue #279): say so in the journal, where the detail belongs, and
        put a banner in front of the operator, who has no shell here.

        The banner is the visible part of the fix. Silence was the bug: the
        route returned its usual "Session added" while the registry it had
        just republished no longer mentioned any of the other sessions.
        """
        sys.stderr.write("sessions/%s refused: %s\n" % (verb, exc))
        self._redirect("ok=session_registry_unreadable", page)

    def _redirect(self, query="", page=None):
        target = (page or BASE + "/") + (("?" + query) if query else "")
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # Each value is (text, kind). The ok= channel's name describes the
    # MECHANISM (a message queued across a redirect), not that the news
    # is good - a handful of entries are failures carried on it because
    # the banner is the only page-level feedback there is after a
    # redirect, session_registry_unreadable being the original case
    # (issue #279). Carrying the kind right on the entry, rather than in
    # a side-table of the keys that are secretly errors, makes a future
    # failure-shaped entry that forgets its kind default to rendering
    # green (issue #549) structurally impossible rather than easy to miss.
    OK_MESSAGES = {
        "saved": ("Secret saved. Restart sessions to apply it.", "ok"),
        "deleted": ("Secret deleted. Restart sessions to apply the change.", "ok"),
        "restarted": ("Restart of all sessions requested.", "ok"),
        "session_added": ("Session added \u2014 it starts within a few seconds.", "ok"),
        "session_deleted": ("Session deleted.", "ok"),
        "session_restarted": ("Session restart requested.", "ok"),
        "session_started": ("Session started \u2014 it comes up within a few seconds.", "ok"),
        "session_registry_unreadable": (
            "The session list could not be read, so nothing was changed. "
            "The box moves the unreadable file aside and rebuilds the list "
            "within a few seconds \u2014 reload this page and try again.",
            "error",
        ),
        "update": ("Agent-box update started. This page may be briefly unavailable.", "ok"),
        "rebooting": (("Restart requested. Agent-box will be unavailable for "
                       "about a minute; reload this page when it is back."), "ok"),
        "webhook_rotated": (("Secret replaced. Copy the new secret into the "
                             "connected service now; events using the old one "
                             "are no longer accepted."), "ok"),
        "webhook_kept_secret": (("Could not replace that secret from this "
                                 "page. Open a session and run "
                                 "`agent-box-webhook rotate SOURCE`."), "error"),
        "webhook_deleted": ("Event rule deleted. The change applies to the next event.", "ok"),
        "webhook_forgotten": ("Event notification rules cleared for this session.", "ok"),
        "webhook_kept": ("Could not delete that event rule. It may already be gone.", "error"),
        "password_changed": ("Password changed. Sign in with your new password.", "ok"),
        "connect_started": ("Sign-in started \u2014 follow the steps under Connections.", "ok"),
        "connect_cancelled": ("Sign-in cancelled.", "ok"),
        "connect_code": ("Code sent \u2014 waiting for the sign-in to finish.", "ok"),
        "profile_saved": ("Profile saved. Sessions started from now on use it.", "ok"),
        "profile_deleted": (("Profile deleted. Sessions already running keep "
                             "their current settings."), "ok"),
        "profile_key_saved": (("Setting added to the profile. Sessions on it pick "
                               "it up at their next start."), "ok"),
        "profile_key_deleted": (("Setting removed from the profile. Sessions on it "
                                 "drop it at their next start."), "ok"),
    }

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # Working-directory autocomplete (issue #131): the add-session
        # form asks the daemon to list one directory level at a time,
        # so the browser never sees the filesystem — only the confined
        # child names for the level being typed. GET-only, read-only,
        # no state change (so no CSRF concern); auth is Caddy's job for
        # the whole vhost. Handled before the BASE routing below since
        # in HOME mode SESS_BASE is "" and this path is not under BASE.
        if parsed.path.rstrip("/") == SESS_BASE + "/sessions/dirs":
            abs_dir = resolve_browse_dir(params.get("path", [""])[0])
            if abs_dir is None:
                self._send_json({"ok": False, "dirs": []})
            else:
                self._send_json({"ok": True, "dirs": list_subdirs(abs_dir)})
            return
        # Live session feed: routed here for the same reasons as the
        # picker above (SESS_BASE is "" in HOME mode, so this path is not
        # under BASE; GET-only and read-only, so no CSRF concern — and it
        # discloses nothing but a digest). ?poll=1 answers with the same
        # fingerprint as a one-shot JSON reply, for a client that could
        # not establish the stream.
        # Transcript download (issue #248): routed here with the two above
        # for the same reasons — GET-only and read-only, so no CSRF concern,
        # and in HOME mode SESS_BASE is "" so the path is not under BASE.
        if parsed.path.rstrip("/") == SESS_BASE + "/sessions/transcript":
            self._send_transcript((params.get("name", [""])[0]).strip())
            return
        if parsed.path.rstrip("/") == SESS_BASE + "/sessions/events":
            if params.get("poll"):
                self._send_json({"fp": session_fingerprint()})
            else:
                self._send_events()
            return
        message = ""
        kind = "ok"
        if "ok" in params:
            message, kind = self.OK_MESSAGES.get(params["ok"][0], ("", "ok"))
        if HOME and parsed.path == "/":
            # The vhost root picks a USER. With one terminal user — the norm
            # — there is nothing to pick, so it lands in that user's space,
            # carrying the query so an old /?tab=<session> bookmark still
            # opens on its tab.
            if len(WEB_USERS) > 1:
                self._send_html(render_users())
                return
            self._redirect(query=parsed.query, page=TERM_HOME)
            return
        if parsed.path.rstrip("/") == TERM_BASE:
            # This user's own landing page — EVERY user's, not just the one
            # whose daemon also serves the vhost root: /<user>/ is theirs,
            # and the workspace's forms and feed already address SESS_BASE,
            # which for a non-primary user is their own settings base. Before
            # this, a second user's /<user>/ was their raw terminal.
            #
            # A box with no session has nothing to show a tab bar for, and
            # the thing its owner actually needs first is the settings page —
            # sign in, add a session — so that is where it lands until one
            # exists.
            if not [n for n in read_sessions() if SESSION_RE.match(n)]:
                self._redirect(page=BASE + "/")
                return
            # ?tab=<session> selects the rendered tab (also the no-JS
            # switching mechanism); anything invalid falls back to the
            # default selection inside render_home.
            tab = (params.get("tab", [""])[0]).strip()
            self._send_html(render_home(
                message, tab if SESSION_RE.match(tab) else None, kind))
            return
        if not self._under_base(parsed.path):
            self._send_html("<h1>404</h1>", status=404)
            return
        # Progress feed the page long-polls after a restart/update
        # (read-only, same auth block as the page it lives under).
        if parsed.path.rstrip("/") == BASE + "/status":
            self._send_json(status_payload())
            return
        # Guided sign-in state as JSON, for a client that would rather
        # poll one card than re-fetch the page (read-only: it reports
        # what the CLI and the pane say, and carries no secret — the
        # only pane text it can return is a redacted error line).
        # One source's HMAC secret, for the Webhook panel's Show/Copy
        # buttons (see webhook_secret()). GET, because it changes nothing
        # and a cross-site page cannot read this response: no CORS
        # headers, so the browser withholds the body even with the
        # operator's credentials attached. no-store comes from
        # _send_json, so it is never written to the browser's cache.
        if parsed.path.rstrip("/") == BASE + "/webhooks/secret" and WEBHOOKS:
            source = (params.get("source", [""])[0]).strip()
            secret = webhook_secret(source) if SOURCE_RE.match(source or "") else ""
            if not secret:
                self._send_json({"ok": False}, status=404)
            else:
                self._send_json({"ok": True, "source": source, "secret": secret})
            return
        if parsed.path.rstrip("/") == BASE + "/connect":
            flow = connect_flow((params.get("flow", [""])[0]).strip())
            if flow is None:
                self._send_json({"ok": False}, status=404)
            else:
                self._send_json({"ok": True, "flow": connect_state(flow)})
            return
        self._send_html(render_page(message, kind))

    def _read_form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return urllib.parse.parse_qs(raw)

    def _same_origin(self):
        """Reject cross-site state-changing POSTs (issue #117).

        Every POST route here mutates state (secrets, sessions, the
        box update). Auth alone does not stop CSRF: the __Host- cookie
        is SameSite=Strict, but the basic-auth fallback has no SameSite
        equivalent, and browsers reattach cached basic credentials to
        cross-site requests — so a lured, basic-authenticated operator
        could be forced to e.g. inject a GH_TOKEN via /set.

        Browsers always send Sec-Fetch-Site; a genuine form post from
        our own page is "same-origin". Anything a browser labels
        cross-site or same-site (sibling *.sslip.io hosts are
        same-site but different owners) is refused. Older browsers
        that omit Sec-Fetch-Site still send Origin, which we compare
        against the target Host (Caddy forwards both unchanged). A
        request with neither header is not a browser navigation and
        carries no ambient victim credentials (curl, the e2e harness),
        so it is allowed."""
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site == "same-origin"
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            return origin in ("https://" + host, "http://" + host)
        return True

    def _sess_page(self, form):
        """Where a /sessions/* POST redirects back to: the settings
        page when the form carried back=settings (the session manager
        section lives there for every user now), else SESS_PAGE (the
        HOME workspace's own add form)."""
        back = form.get("back", [""])[0]
        if back == "settings":
            return BASE + "/"
        # back=workspace names /<user>/ for EVERY user, which SESS_PAGE only
        # resolves to for the primary one (for a second web user it is that
        # user's settings page). The workspace's own forms carry it, so a
        # Start pressed in a pane comes back to the pane.
        if back == "workspace":
            return TERM_HOME
        return SESS_PAGE

    def do_POST(self):
        """Validate and apply a settings-page form submission."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not self._same_origin():
            # Drain the request body BEFORE answering: replying 403 and
            # closing with unread body bytes in flight races the reverse
            # proxy's write — caddy sees EPIPE on the socket and turns
            # the 403 into a 502 (intermittent; caught by the settings-page
            # VM test under concurrent-CI load, PR #152).
            self._read_form()
            self._send_html(
                "<h1>403</h1><p>Cross-site request blocked.</p>",
                status=403,
            )
            return
        form = self._read_form()
        if path == BASE + "/password" and PASSWORD_CMD:
            previous = form.get("previous_password", [""])[0]
            new = form.get("new_password", [""])[0]
            confirm = form.get("confirm_password", [""])[0]
            if new != confirm:
                self._send_html(
                    render_page("New password and confirmation do not match.",
                                kind="error"),
                    status=400,
                )
                return
            if not valid_password(new):
                self._send_html(
                    render_page("New password must be 16–64 characters and "
                                "cannot contain a line break.", kind="error"),
                    status=400,
                )
                return
            if new == previous:
                self._send_html(
                    render_page("New password must differ from the current password.",
                                kind="error"),
                    status=400,
                )
                return
            result = change_password(previous, new)
            if result == 2:
                self._send_html(
                    render_page("Current password is incorrect.", kind="error"),
                    status=403,
                )
                return
            if result != 0:
                self._send_html(
                    render_page("Could not update the password. Try again or "
                                "check the settings service journal.", kind="error"),
                    status=500,
                )
                return
            self._redirect("ok=password_changed")
        elif path == BASE + "/set":
            key = (form.get("key", [""])[0]).strip()
            # A textarea posts its newlines as CRLF (HTML forms, RFC 1866).
            # Normalize here, so what a session reads back is what was
            # pasted rather than the same text with stray carriage returns.
            value = form.get("value", [""])[0].replace("\r\n", "\n").replace("\r", "\n")
            if not KEY_RE.match(key):
                self._send_html(
                    render_page("Invalid key name. Use letters, digits and "
                                "underscores; do not start with a digit.", kind="error"),
                    status=400,
                )
                return
            # The store refuses a value that holds a NUL (lib/envstore.py: no
            # environment variable can carry one, and NUL frames the argument
            # vectors this file feeds). A form cannot TYPE one, but a POST can
            # carry one, and a 400 saying so beats a traceback in the journal
            # and a 500 on the page.
            try:
                set_key(key, value)
            except EnvStoreError as exc:
                self._send_html(
                    render_page("Could not save that secret — %s." % exc, kind="error"),
                    status=400,
                )
                return
            self._redirect("ok=saved")
        elif path.startswith(BASE + "/connect/"):
            # Guided sign-in (issues #207, #208, #313). All three verbs
            # act on ONE tmux session per flow and store nothing here, so
            # a repeat POST (double click, stale tab) is harmless: start
            # returns the flow already in flight, cancel kills a session
            # that may already be gone.
            action = path[len(BASE + "/connect/"):]
            flow = connect_flow((form.get("flow", [""])[0]).strip())
            if flow is None or action not in ("start", "code", "cancel"):
                self._send_html("<h1>404</h1>", status=404)
                return
            if action == "start":
                result = connect_start(flow)
                # A start that could not begin has something to say, and
                # the card is rebuilt from the pane on the next GET — so
                # say it here rather than redirecting into a page that no
                # longer remembers the attempt.
                if result["state"] == "failed" and result["error"]:
                    self._send_html(
                        render_page(result["error"], kind="error"), status=409)
                    return
                self._redirect("ok=connect_started")
            elif action == "cancel":
                connect_cancel(flow["id"])
                self._redirect("ok=connect_cancelled")
            else:
                code = form.get("code", [""])[0].strip()
                if not CONNECT_CODE_OK.match(code):
                    self._send_html(
                        render_page("That does not look like a sign-in code. "
                                    "Copy the whole value the sign-in page "
                                    "shows, with no spaces.", kind="error"),
                        status=400,
                    )
                    return
                connect_send_code(flow, code)
                self._redirect("ok=connect_code")
        elif path.startswith(BASE + "/profiles/"):
            # Agent profiles (#321, step 5). Four verbs on one runtime
            # directory; none of them touches a running session, which is why
            # every message below says "from now on" rather than "applied".
            action = path[len(BASE + "/profiles/"):]
            name = (form.get("name", [""])[0]).strip()
            if action not in ("set", "delete", "setkey", "delkey"):
                self._send_html("<h1>404</h1>", status=404)
                return
            if not PROFILE_NAME_RE.match(name):
                self._send_html(
                    render_page("Invalid profile name. Use letters, digits, "
                                "'_' and '-', at most 64 characters.", kind="error"),
                    status=400,
                )
                return
            if action == "delete":
                profile_remove(name)
                self._redirect("ok=profile_deleted")
                return
            if action == "delkey":
                key = (form.get("key", [""])[0]).strip()
                # A reserved key is launch config and is cleared by saving the
                # form with that field blank; letting this route drop one too
                # would give the same state two doors and no reason to prefer
                # either.
                # Only on a profile that EXISTS. update() writes the file
                # whether or not the load found one, so deleting a key from a
                # profile that is gone would have created an empty one — with
                # just the header, no harness — and then listed it in the
                # panel and the picker. A stale tab whose profile was deleted
                # in another one reaches exactly this path.
                if KEY_RE.match(key) and key not in PROFILE_RESERVED:
                    # must_exist, checked INSIDE the lock: update() writes
                    # the file whether or not the load found one, so removing
                    # a key from a profile that is gone would create an empty
                    # one — header only, no harness — and the panel and the
                    # picker would then offer it. A stale tab whose profile
                    # was deleted in another one reaches exactly this path.
                    profile_write(name, [], drop=[key], must_exist=True)
                self._redirect("ok=profile_key_deleted")
                return
            if action == "setkey":
                key = (form.get("key", [""])[0]).strip()
                value = form.get("value", [""])[0]
                value = value.replace("\r\n", "\n").replace("\r", "\n")
                if not KEY_RE.match(key) or key in PROFILE_RESERVED:
                    if key in PROFILE_RESERVED:
                        message = (
                            "%s is already a profile option. Set it in the "
                            "form above."
                            % PROFILE_RESERVED_LABELS.get(key, "That name")
                        )
                    else:
                        message = (
                            "Invalid setting name. Use letters, digits and "
                            "underscores; do not start with a digit."
                        )
                    self._send_html(
                        render_page(message, kind="error"),
                        status=400,
                    )
                    return
                # Same must_exist guard as delkey, for the same stale-tab
                # reason: this form only ever appears inside an existing
                # profile's fold, so reaching it for a profile that is gone
                # means the page is out of date — and resurrecting it here,
                # with one env key and no launch config, is not what either
                # the operator or the form asked for. Creating a profile is
                # /profiles/set's job.
                try:
                    written = profile_write(name, [(key, value)],
                                            must_exist=True)
                except EnvStoreError as exc:
                    self._send_html(
                        render_page("Could not save that key \u2014 %s." % exc,
                                    kind="error"),
                        status=400)
                    return
                if not written:
                    self._send_html(
                        render_page("No profile named '%s' — it may have "
                                    "just been deleted." % name, kind="error"),
                        status=404)
                    return
                self._redirect("ok=profile_key_saved")
                return
            # set: the launch config, as a whole. A blank field DROPS its key
            # rather than storing an empty value, so the form shows the whole
            # truth about a profile — an empty MODEL that is present in the
            # file would resolve to `--model ''` and start nothing.
            assignments, drop = [], []
            for key in PROFILE_RESERVED:
                value = form.get(key, [""])[0].replace("\r\n", "\n").replace("\r", "\n")
                if key != "SYSTEM_PROMPT":
                    value = value.strip()
                if value:
                    assignments.append((key, value))
                else:
                    drop.append(key)
            harness = dict(assignments).get("HARNESS", "")
            # Required, not optional (issue #493): there is no box-wide
            # default harness left to fall back to, and the resolver refuses
            # a profile that names none. Caught here so the answer is a
            # sentence on the page rather than a profile that saves fine and
            # then fails to start.
            if not harness:
                self._send_html(
                    render_page("Pick an assistant for that profile \u2014 a "
                                "profile is an assistant plus the settings "
                                "that go with it, so it needs one. "
                                "Available: " + ", ".join(PROFILE_AGENTS),
                                kind="error"),
                    status=400)
                return
            if harness not in PROFILE_AGENTS:
                # "shell" lands here on purpose: it is a session kind, not a
                # worker, so it has no model, effort or prompt to configure.
                self._send_html(
                    render_page("Unknown assistant. Available: "
                                + ", ".join(PROFILE_AGENTS), kind="error"),
                    status=400)
                return
            # An optional starting environment setting (issue #493): the "New
            # profile" pane has no fold to add one to until the profile
            # exists, so this one pair rides in on the same request and the
            # same write as the launch config, rather than needing a second
            # round trip through /profiles/setkey's must_exist gate. Blank
            # key is the ordinary case (most profiles start with none) and is
            # silently skipped; a stray value with no key name is meaningless
            # and dropped with it.
            env_key = (form.get("key", [""])[0]).strip()
            if env_key:
                env_value = form.get("value", [""])[0]
                env_value = env_value.replace("\r\n", "\n").replace("\r", "\n")
                if not KEY_RE.match(env_key) or env_key in PROFILE_RESERVED:
                    if env_key in PROFILE_RESERVED:
                        message = (
                            "%s is already a profile option. Set it in the "
                            "form above."
                            % PROFILE_RESERVED_LABELS.get(env_key, "That name")
                        )
                    else:
                        message = (
                            "Invalid setting name. Use letters, digits and "
                            "underscores; do not start with a digit."
                        )
                    self._send_html(
                        render_page(message, kind="error"),
                        status=400,
                    )
                    return
                assignments.append((env_key, env_value))
            try:
                profile_write(name, assignments, drop=drop)
            except EnvStoreError as exc:
                self._send_html(
                    render_page("Could not save that profile \u2014 %s." % exc,
                                kind="error"),
                    status=400)
                return
            # The resolver has the last word on what this profile actually
            # starts, so a key its harness cannot use is reported HERE and not
            # at the next session start (codex has no --append-system-prompt).
            resolved = profile_launch(name)
            warnings = (resolved or {}).get("warnings") or []
            if warnings:
                # render_msg escapes the banner text itself, so this stays
                # plain: escaping here would show the entities.
                self._send_html(render_page(
                    "Profile saved, with a warning: " + " ".join(warnings),
                    kind="warn"))
                return
            self._redirect("ok=profile_saved")
        elif path == BASE + "/delete":
            key = (form.get("key", [""])[0]).strip()
            if KEY_RE.match(key):
                delete_key(key)
            self._redirect("ok=deleted")
        elif path == SESS_BASE + "/sessions/add":
            back_page = self._sess_page(form)
            # Error pages re-render the page the form came from.
            render = render_home if (HOME and back_page == SESS_PAGE) else render_page
            # The profile is the whole answer now (issue #493): the row has
            # no assistant <select> beside it any more, and there is no
            # box-wide default assistant left to fall back to. So the
            # assistant comes from the profile, and a request naming neither
            # is refused rather than guessed at.
            #
            # The old rule - "the profile's harness WINS over the select on
            # its left" - existed because a <select> always posts a value,
            # so the form could not tell "chose claude" from "left it
            # alone". Deleting the select deletes the ambiguity with it.
            # `agent-box-session add --profile P --harness H` is still the way
            # to say the other thing from a shell.
            profile = (form.get("profile", [""])[0]).strip()
            agent = (form.get("agent", [""])[0]).strip()
            if profile == SHELL_PSEUDO_PROFILE:
                # The one entry that is not a profile file: a bare shell has
                # no model, reasoning level or instructions to carry.
                agent, profile = "shell", ""
            elif profile:
                agent = ""      # resolved from the profile below
            elif not agent:
                # The FORM always sends a profile now, so this is a request
                # made some other way. It used to fall through to the box
                # default; there is none, so it is refused rather than
                # guessed at.
                self._send_html(
                    render("Pick a profile for that session \u2014 it is "
                           "what decides which assistant runs, and how.",
                           kind="error"),
                    status=400,
                )
                return
            # An explicit assistant with no profile is still allowed, which
            # is what `agent-box-session add --harness H` does from a shell.
            # The row no longer offers it (issue #493), but the route is not
            # the row: removing the picker's ambiguity is not a reason to
            # take a capability away from everything else that posts here.
            if agent and agent not in AGENTS:
                self._send_html(
                    render("Unknown assistant. Available: " + ", ".join(AGENTS),
                           kind="error"),
                    status=400,
                )
                return
            pargs = []
            if profile and not PROFILE_BIN:
                # Said plainly, because the two are fixed differently: this
                # one is a restart of the settings daemon, not a hunt for a
                # profile that is sitting right there in the list.
                self._send_html(
                    render("Profiles are temporarily unavailable. Restart "
                           "agent-box and try again.", kind="error"),
                    status=503,
                )
                return
            if profile:
                if not PROFILE_NAME_RE.match(profile):
                    self._send_html(
                        render("Invalid profile name.", kind="error"), status=400)
                    return
                # Resolved by agent-box-profile, the one place the mapping
                # from profile keys to harness arguments lives (#337) — and
                # resolved HERE, at create time, so a later edit to the
                # profile does not change what a running session was started
                # with (the rule webhook.hookSessionArgs already states).
                # No override is passed any more (issue #493). There used
                # to be one: the row's assistant <select> was handed to the
                # resolver when the profile named no harness of its own,
                # because otherwise a profile carrying only MODEL/EFFORT
                # resolved to the box default and neither the profile nor
                # the operator decided which assistant ran. Both halves of
                # that are gone - the select, and the box default - and a
                # profile that names no harness is now refused by the
                # resolver itself rather than quietly resolved.
                resolved = profile_launch(profile)
                if resolved is None:
                    self._send_html(
                        render("Could not load profile '%s'. It may have "
                               "just been deleted." % profile, kind="error"),
                        status=400,
                    )
                    return
                agent = resolved.get("harness") or ""
                if agent not in AGENTS:
                    self._send_html(
                        render("Profile '%s' uses an assistant that is not "
                               "available. Choose one of: %s"
                               % (profile, ", ".join(AGENTS)), kind="error"),
                        status=400,
                    )
                    return
                pargs = [str(a) for a in (resolved.get("args") or [])]
            # Working directory (issue #131): the field defaults to
            # "~" (home); resolve_session_cwd stores that as None (the
            # supervisor's default) and any other path as an absolute
            # directory it has confirmed exists inside HOME.
            try:
                cwd = resolve_session_cwd(form.get("cwd", [""])[0])
            except ValueError as exc:
                self._send_html(render(str(exc), kind="error"), status=400)
                return
            # Optional kickoff prompt (first spawn only; the supervisor
            # clears it and resumes on later respawns). boxSessionId is
            # left null so the supervisor mints a real UUID at spawn — and
            # keeps it in its own state file from then on, these two fields
            # being the migration copy it stops writing next release
            # (issue #282).
            # Browsers submit textarea line endings as CRLF; normalize them
            # before this value reaches the agent as one argv element.
            prompt = form.get("prompt", [""])[0].replace("\r\n", "\n")
            prompt = prompt.replace("\r", "\n").strip()
            # Read, name and write as one step (issue #254): the uniqueness
            # gen_session_name promises comes from the dict it was handed, so
            # a concurrent add — from another browser tab, this daemon's own
            # second thread, or the CLI — could pick the same free name, and
            # the later rename would drop the earlier session outright.
            try:
                with sessions_lock():
                    sessions, version = load_sessions()
                    # The name is always auto-derived: there is no name field
                    # in the form. From the PROFILE when one was given (it is the
                    # more specific worker identity; see gen_session_name), else
                    # the agent. Users rarely care what a session is called
                    # (rename at runtime via /rename), so autogen spares them
                    # inventing one AND guarantees a unique key, so no collision
                    # or accidental-overwrite (issue 100) is possible.
                    name = gen_session_name(profile or agent, sessions, cwd)
                    sessions[name] = {
                        "agent": agent,
                        "skipPermissions": True,
                        "remoteControl": True,
                        "remoteControlName": None,
                        "workingDirectory": cwd,
                        "extraArgs": pargs,
                        # The NAME, not just the resolved arguments: the spawn
                        # wrapper re-reads it at every start to apply the
                        # profile's environment, so a rotated token in a profile
                        # reaches this session on its next restart while the
                        # arguments above stay as they were created.
                        "profile": profile or None,
                        "initialPrompt": prompt or None,
                        "resumePrompt": None,
                        "boxSessionId": None,
                        "hasRun": False,
                    }
                    write_sessions(sessions, version)
            except RegistryUnreadable as exc:
                # Refuse rather than republish (issue #279). Without this the
                # write below carried the empty dict read_sessions used to
                # return for a corrupt file, so adding one session deleted
                # every other one. The supervisor moves the bad file aside
                # within a tick or two, so the answer is "try again", not
                # "open a shell".
                self._registry_refusal("add", exc, back_page)
                return
            # On the workspace, land on the new session's tab (gen_session_name
            # returns a SESSION_RE-shaped name, so it is URL-safe as-is).
            query = "ok=session_added"
            if back_page == TERM_HOME:
                query += "&tab=" + name
            self._redirect(query, back_page)
        elif path == SESS_BASE + "/sessions/delete":
            name = (form.get("name", [""])[0]).strip()
            if SESSION_RE.match(name):
                # Under the lock, so the delete cannot be reverted by another
                # writer that read this file before it (issue #254) — a
                # resurrected entry is a session the supervisor starts and no
                # delete path knows about. The kill stays OUTSIDE: tmux is not
                # this file, and nothing may hold the lock across a subprocess.
                try:
                    with sessions_lock():
                        sessions, version = load_sessions()
                        sessions.pop(name, None)
                        write_sessions(sessions, version)
                except RegistryUnreadable as exc:
                    # A delete against a registry we could not read used to
                    # publish an empty one: the named session went, and so did
                    # every other (issue #279).
                    self._registry_refusal("delete", exc, self._sess_page(form))
                    return
                kill_session(name)
                # Delisted and killed, so its filter file routes nothing —
                # but it would go on claiming its topics against the standing
                # watches until something removes it (#229).
                prune_filter(name)
                # Same for the supervisor's record of what this session was
                # last launched with: an OPTIMISATION only (the supervisor
                # sweeps the directory against the registry every tick,
                # because no delete path is guaranteed to run), but it closes
                # the window in which a re-used name inherits the dead
                # session's launch id — and with it its transcript (#282).
                state_path = session_state_path(name)
                if state_path:
                    try:
                        os.remove(state_path)
                    except OSError:
                        pass
            self._redirect("ok=session_deleted", self._sess_page(form))
        elif path == SESS_BASE + "/sessions/restart":
            name = (form.get("name", [""])[0]).strip()
            # The row calls this route Start on a stopped session, so say
            # back what was actually done rather than "restart" (#241).
            ok = "ok=session_restarted"
            if SESSION_RE.match(name):
                # Restart doubles as the revive verb for a stopped session
                # (clean agent exit or agent-box-session stop, issue #167):
                # the stopped flag is what keeps the supervisor away, so
                # clear it before the kill.
                # One flag on one entry, but the write publishes the WHOLE
                # document, so without the lock this Start reverted every field
                # another writer had changed since this read (issue #254):
                # pressing it while a sibling session was first-spawning put
                # back that session's hasRun=false and its already-consumed
                # initialPrompt, and the supervisor then re-fired the kickoff
                # prompt under a new id the next time that session died.
                try:
                    with sessions_lock():
                        sessions, version = load_sessions()
                        entry = sessions.get(name)
                        # isinstance, not `is not None`: load_sessions keeps an
                        # entry whose value is not an object rather than
                        # dropping it (see its docstring), so the .pop below
                        # has to ask rather than assume.
                        if isinstance(entry, dict) and entry.pop("stopped", None) is not None:
                            write_sessions(sessions, version)
                            ok = "ok=session_started"
                except RegistryUnreadable as exc:
                    # Start is the button an operator presses when the box
                    # looks wrong, which is exactly when the registry might
                    # be the thing that is wrong (issue #279). Do not answer
                    # it by deleting the rest of the list.
                    self._registry_refusal("restart", exc, self._sess_page(form))
                    return
                kill_session(name)
            back_page = self._sess_page(form)
            # On the workspace, land on the tab of the session just started —
            # the pane's own Start button posts here, and dropping the operator
            # back on some other tab hides the very thing they asked for.
            if back_page == TERM_HOME and SESSION_RE.match(name):
                ok += "&tab=" + name
            self._redirect(ok, back_page)
        elif path == BASE + "/webhooks/unsubscribe" and WEBHOOKS:
            topic = (form.get("topic", [""])[0]).strip()
            key = (form.get("key", [""])[0]).strip()
            dispatch = bool(form.get("dispatch", [""])[0])
            # The key names a filter file, so hold it to the shape the
            # supervisor mints: this user, and one of this user's own
            # sessions (or the bare user key, which only reads the shared
            # dispatch list).
            name = key[len(USER) + 1:] if key.startswith(USER + "-") else ""
            known = key == USER or (SESSION_RE.match(name) and name in read_sessions())
            if not (TOPIC_RE.match(topic) and known):
                self._redirect("ok=webhook_kept")
                return
            ok = webhook_unsubscribe(key, topic, dispatch)
            self._redirect("ok=webhook_deleted" if ok else "ok=webhook_kept")
        elif path == BASE + "/webhooks/rotate" and WEBHOOKS:
            # Replace one source's secret. The page's only webhook WRITE:
            # everything else here reads, and topic edits go through
            # webhook.py's own CLI. Bounded by webhook_secret_path() to a
            # secret file inside the state dir belonging to a configured
            # source that setup itself created.
            source = (form.get("source", [""])[0]).strip()
            ok = webhook_rotate(source)
            self._redirect("ok=webhook_rotated" if ok else "ok=webhook_kept_secret")
        elif path == BASE + "/webhooks/forget" and WEBHOOKS:
            # Remove one session's whole filter file. Same bound as the
            # unsubscribe route: one of THIS user's own listed sessions,
            # so no invented name can reach a path outside them.
            name = (form.get("name", [""])[0]).strip()
            if not (SESSION_RE.match(name) and name in read_sessions()):
                self._redirect("ok=webhook_kept")
                return
            ok = prune_filter(name)
            self._redirect("ok=webhook_forgotten" if ok else "ok=webhook_kept")
        elif path == BASE + "/restart":
            # Full unit bounce (see restart_all): re-reads unit-level
            # EnvironmentFiles, which per-session restarts can't.
            restart_all()
            self._redirect("ok=restarted")
        elif path == BASE + "/update" and UPDATE_CMD:
            update_box()
            self._redirect("ok=update")
        elif path == BASE + "/reboot" and REBOOT_CMD:
            # Answer FIRST, then reboot. Every other action here can
            # respond afterwards because the daemon survives it; this one
            # is stopped by the shutdown it just asked for, so a response
            # written after the trigger races its own socket. Flushed
            # explicitly rather than trusting the handler to get another
            # turn — it may not.
            self._redirect("ok=rebooting")
            with contextlib.suppress(OSError):
                self.wfile.flush()
            reboot_box()
        else:
            self._send_html("<h1>404</h1>", status=404)

    def address_string(self):
        # AF_UNIX peers have no (host, port) client_address — the base class
        # would IndexError on the empty string it gets instead.
        if isinstance(self.client_address, tuple) and self.client_address:
            return super().address_string()
        return "unix"

    def log_message(self, fmt, *args):
        # Keep the journal quiet-ish; never log form bodies (would leak
        # secrets). Only method + path + status, which BaseHTTPRequestHandler
        # already restricts to.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


# Per the systemd socket-activation protocol, inherited listening sockets
# start at fd 3 (after stdin/stdout/stderr).
SD_LISTEN_FDS_START = 3


def make_server():
    if int(os.environ.get("LISTEN_FDS", "0") or "0") >= 1:
        # Socket-activated (the module's only mode, issue #49): adopt the
        # unix socket systemd pre-bound with 0660 <user>:caddy permissions.
        # bind_and_activate=False skips bind/listen; the placeholder address
        # is never bound.
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), Handler, bind_and_activate=False
        )
        server.socket = socket.socket(fileno=SD_LISTEN_FDS_START)
        # server_bind() never ran; set the attributes it would have set.
        server.server_name = "agent-box-settings"
        server.server_port = 0
        return server
    # Dev fallback for LAN rigs / e2e runs outside the module.
    return http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)


def main():
    make_server().serve_forever()


if __name__ == "__main__":
    main()
