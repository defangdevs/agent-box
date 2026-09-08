"""Ask the sender what it could not hand this box, and hand it over (#605).

A webhook delivery is fire-and-forget. GitHub does not retry, so every event
that arrives while the box's front door is down is lost for good - and the
session waiting for it cannot tell "quiet repo" from "deaf box". On
2026-09-03 that cost about 44 deliveries across 47 minutes, including the
review verdict and the CI failure a live, correctly-subscribed session was
waiting for. PR #608 closed the footgun that caused THAT outage; this closes
the two halves it left: nothing detected the outage, and nothing recovered
from it.

Both halves come from the same place - GitHub's own delivery log, which
records the status code of every attempt:

  liveness  the newest delivery it recorded, and whether it was accepted.
            An agent an hour into a silent wait can see a two-minute-old 200
            (the repo is quiet) or a 502 (the box is deaf).
  recovery  every delivery it could not hand over, re-requested through
            POST .../deliveries/{id}/attempts, so GitHub sends it again over
            the real signed path. Nothing is replayed locally and nothing is
            forged: the receiver sees a delivery indistinguishable from the
            first attempt, because it is one.

An outage almost always ends in a restart, a reboot, or a `systemctl start`,
and every one of those restarts the box's sessions - so the automatic sweep
hangs off session start (env-exec.py), throttled, in the background. That
also puts it where the GitHub token is: the token lives in the user's env
store and reaches processes through env-exec, never through a unit's
environment, so the receiver daemon could not do this even though it is the
thing that noticed the silence.

Two facts about the delivery log this depends on, both measured against the
live API rather than read off the docs:

  - a redelivery keeps the ORIGINAL delivery's `guid` and gets a new `id`.
    So "already recovered" is a question the log answers by itself: a failed
    delivery whose guid also appears with a 2xx status needs nothing. This
    keeps no local high-water mark, which means it cannot go stale, cannot
    disagree with the sender, and self-corrects after any restart.
  - delivery ids are 19 digits, past the 2**53 a double can hold exactly.
    jq preserves an unmutated number literal, but building any object around
    it silently rounds it (`{id: .id}` turns ...634112 into ...634000, and
    that id 404s), which is why this is Python and not four lines of jq.
"""

import argparse
import calendar
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

# GitHub keeps a limited window of deliveries (empirically about three days,
# or 750 records, on a busy repo). Sweeping further back than the outage
# gains nothing and risks re-requesting stale CI noise into a live session,
# so the default window is "an outage that ended recently".
DEFAULT_HOURS = 6
# A cap on how many events one sweep may push at the box at once. A burst of
# stale deliveries can spawn hook sessions and interrupt live ones, and the
# oldest are the least useful; what the cap drops is REPORTED, never silent.
DEFAULT_LIMIT = 50
# Pages of 100. Bounded so a misconfiguration cannot walk a hook's whole log.
MAX_PAGES = 12
# --throttled runs at every session start, and a reboot starts several at
# once. One sweep per this interval is enough to catch an outage that just
# ended, and the lock below keeps the concurrent ones from all calling out.
THROTTLE_S = 900
GH_TIMEOUT_S = 30

STATE_HOME = os.path.join(
    os.environ.get("HOME", ""), ".local", "state", "agent-box")
STATE_FILE = os.path.join(STATE_HOME, "webhook-backfill.json")
LOCK_FILE = os.path.join(STATE_HOME, ".webhook-backfill.lock")

WHY = (
    "Written by agent-box-webhook-backfill (issue #605): what GitHub's own "
    "delivery log said about this box's ingress, and which deliveries it "
    "could not hand over were re-requested. agent-box-webhook status reads "
    "it, so a session waiting on events can tell a quiet repo from a deaf "
    "box without a network call of its own."
)


class Failure(Exception):
    """Something the operator has to fix - no token, no gh, no endpoint."""


def now():
    return time.time()


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def parse_iso(text):
    """GitHub's delivered_at, as a unix timestamp. 0 when unparseable, which
    sorts oldest and so is never mistaken for a fresh delivery."""
    stamp = r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)"
    match = re.match(stamp, text or "")
    if not match:
        return 0.0
    parts = [int(group) for group in match.groups()]
    try:
        return float(calendar.timegm(tuple(parts) + (0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


def gh(args, capture_headers=False):
    """One `gh api` call. gh resolves the token the way every other tool on
    this box does ($GH_TOKEN, then $GITHUB_TOKEN, then its stored
    credentials), so the sweep acts as the identity the agent pushes with."""
    binary = shutil.which("gh")
    if not binary:
        raise Failure("no gh on PATH")
    command = [binary, "api"]
    if capture_headers:
        command.append("--include")
    command += args
    try:
        done = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=GH_TIMEOUT_S)
    except OSError as error:
        raise Failure(f"cannot run gh: {error}")
    except subprocess.TimeoutExpired:
        raise Failure(f"gh api {' '.join(args)} timed out")
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise Failure(
            f"gh api {' '.join(args)} failed: "
            + (detail[-1] if detail else f"exit {done.returncode}"))
    return done.stdout


def gh_json(path):
    return json.loads(gh([path]) or "null")


def gh_page(path):
    """One page of a cursor-paginated list, plus the path of the next.

    The deliveries endpoint pages by opaque cursor, not by page number
    (`?page=2` is accepted and ignored), so the only way forward is the Link
    header - which means reading the headers, which means --include.
    """
    raw = gh([path], capture_headers=True)
    head, _, body = raw.partition("\n\n")
    if not _:
        head, _, body = raw.partition("\r\n\r\n")
    link = ""
    for line in head.splitlines():
        if line.lower().startswith("link:"):
            link = line.split(":", 1)[1]
            break
    following = ""
    for piece in link.split(","):
        if 'rel="next"' in piece:
            match = re.search(r"<([^>]+)>", piece)
            if match:
                following = match.group(1)
            break
    return json.loads(body or "null"), following


def endpoints():
    """Every URL a hook of OURS could be registered at.

    The base is what the agent unit exports beside AGENT_BOX_URL; a source
    gets its own path under it, and a bare base still means the default
    source. Matching on this is what keeps the sweep off hooks belonging to
    somebody else's box - this repo carries two, and re-requesting another
    box's failed deliveries would push events at a machine that never asked.
    """
    base = (os.environ.get("AGENT_BOX_WEBHOOK_URL") or "").strip()
    if not base:
        raise Failure(
            "no AGENT_BOX_WEBHOOK_URL in the environment - this user has no "
            "browser terminal, so nothing serves a webhook endpoint")
    base = base.rstrip("/")
    return {base} | {f"{base}/{source}" for source in sources()}


def sources():
    path = os.path.join(
        os.environ.get(
            "LOCAL_WEBHOOK_STATE_DIR",
            os.path.join(os.environ.get("HOME", ""),
                         ".local", "state", "local-webhook")),
        "sources.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        return set()
    known = data.get("sources")
    return set(known) if isinstance(known, dict) else set()


def subscribed_repos():
    """owner/repo for every GitHub topic any filter file in this state dir
    names - session subscriptions and standing watches alike.

    A wildcard topic (github:owner/*) names no repo, so it cannot be swept:
    hooks are per-repo and enumerating an org's repos needs a scope this
    box's token does not have. Those are RETURNED as skips rather than
    dropped, so the report says which watches the sweep could not cover
    instead of implying it covered everything.
    """
    directory = os.environ.get(
        "LOCAL_WEBHOOK_STATE_DIR",
        os.path.join(os.environ.get("HOME", ""),
                     ".local", "state", "local-webhook"))
    repos, skipped = [], []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return repos, skipped
    for name in names:
        if not (name.startswith("filter.") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        for entry in data.get("topics") or []:
            topic = (entry or {}).get("topic") or ""
            source, _, key = topic.partition(":")
            if not key:
                source, key = "github", topic
            if source != "github" or "/" not in key:
                continue
            if "*" in key:
                if key not in skipped:
                    skipped.append(key)
            elif key not in repos:
                repos.append(key)
    return repos, skipped


def window(repo, hook_id, cutoff):
    """Deliveries this hook recorded since `cutoff`, newest first.

    Stops at the first record older than the window, so a quiet sweep is one
    request. `truncated` says the page cap stopped it early, which the report
    prints - a window that was not fully walked must not read as an empty one.
    """
    path = f"/repos/{repo}/hooks/{hook_id}/deliveries?per_page=100"
    found, pages = [], 0
    while path and pages < MAX_PAGES:
        page, path = gh_page(path)
        pages += 1
        if not isinstance(page, list):
            return found, False
        for record in page:
            if parse_iso(record.get("delivered_at")) < cutoff:
                return found, False
            found.append(record)
    # A `path` still in hand means the page cap, not the window, ended this.
    return found, bool(path)


def stamp_of(record):
    """Sort key. GitHub's delivered_at is fixed-width ISO-8601 in UTC, so
    comparing the strings IS comparing the instants - and it keeps the
    milliseconds, which parse_iso drops and two events inside one second
    need."""
    return record.get("delivered_at") or ""


def accepted(record):
    code = record.get("status_code")
    return isinstance(code, int) and 200 <= code < 300


def outstanding(records):
    """The deliveries still owed to this box, oldest first.

    Oldest first because that is the order the events happened in, and a
    session reading a redelivered burst reads a story rather than a shuffle.
    """
    recovered = {r.get("guid") for r in records if accepted(r)}
    best = {}
    for record in records:
        guid = record.get("guid")
        if accepted(record) or guid in recovered:
            continue
        # Several failed attempts share one guid; re-requesting any of them
        # sends the same event, so keep one - the newest, whose id is
        # certainly still in the log.
        seen = best.get(guid)
        if seen is None or stamp_of(record) > stamp_of(seen):
            best[guid] = record
    return sorted(best.values(), key=stamp_of)


def redeliver(repo, hook_id, record):
    gh(["--method", "POST",
        f"/repos/{repo}/hooks/{hook_id}/deliveries/{record['id']}/attempts"])


def describe(record):
    event = record.get("event") or "?"
    action = record.get("action")
    return f"{event}{'.' + action if action else ''}"


def sweep_repo(repo, mine, cutoff, limit, dry_run):
    report = {"repo": repo, "hooks": []}
    hooks = gh_json(f"/repos/{repo}/hooks")
    if not isinstance(hooks, list):
        hooks = []
    ours = [h for h in hooks
            if ((h.get("config") or {}).get("url") or "").rstrip("/") in mine]
    if not ours:
        report["warning"] = (
            f"none of the {len(hooks)} hook(s) on {repo} point at this box"
            " - nothing here delivers to it")
        return report
    for hook in ours:
        hook_id = hook["id"]
        records, truncated = window(repo, hook_id, cutoff)
        owed = outstanding(records)
        entry = {
            "hookId": hook_id,
            "active": bool(hook.get("active")),
            "deliveries": len(records),
            "truncated": truncated,
            "outstanding": len(owed),
            "requested": 0,
            "dropped": 0,
            "failures": [],
        }
        if records:
            newest = records[0]
            entry["lastDelivery"] = {
                "at": newest.get("delivered_at"),
                "event": describe(newest),
                "statusCode": newest.get("status_code"),
                "status": newest.get("status"),
                "accepted": accepted(newest),
            }
        if len(owed) > limit:
            entry["dropped"] = len(owed) - limit
            # Newest first to survive the cap: the oldest CI event is the
            # least worth waking a session for. `owed[-0:]` is the whole
            # list, so --limit 0 ("report, request nothing") needs its own
            # arm rather than a slice.
            owed = owed[-limit:] if limit > 0 else []
        for record in owed:
            entry["failures"].append({
                "at": record.get("delivered_at"),
                "event": describe(record),
                "statusCode": record.get("status_code"),
            })
            if dry_run:
                continue
            try:
                redeliver(repo, hook_id, record)
            except Failure as error:
                entry.setdefault("errors", []).append(str(error))
                break
            entry["requested"] += 1
        report["hooks"].append(entry)
    return report


def render(state, stream):
    dry = state.get("dryRun")
    for repo in state.get("repos") or []:
        if repo.get("warning"):
            print(f"{repo['repo']}: {repo['warning']}", file=stream)
            continue
        for hook in repo.get("hooks") or []:
            last = hook.get("lastDelivery") or {}
            if last:
                verdict = "accepted" if last.get("accepted") else (
                    f"REFUSED ({last.get('statusCode')} "
                    f"{last.get('status')})")
                print(f"{repo['repo']} hook {hook['hookId']}: last delivery "
                      f"{last.get('at')} {last.get('event')} - {verdict}",
                      file=stream)
            else:
                print(f"{repo['repo']} hook {hook['hookId']}: no delivery in "
                      "the window", file=stream)
            owed = hook.get("outstanding") or 0
            if not owed:
                print("  nothing owed - every delivery in the window was "
                      "accepted", file=stream)
                continue
            verb = "would re-request" if dry else "re-requested"
            print(f"  {owed} delivery(s) never reached this box; {verb} "
                  f"{hook.get('requested') if not dry else owed}",
                  file=stream)
            for failure in hook.get("failures") or []:
                print(f"    {failure['at']} {failure['event']} "
                      f"({failure['statusCode']})", file=stream)
            if hook.get("dropped"):
                print(f"  {hook['dropped']} older one(s) left alone (--limit "
                      f"{state.get('limit')}); raise --limit or --hours to "
                      "reach them", file=stream)
            if hook.get("truncated"):
                print("  the delivery log was longer than this sweep walks; "
                      "there may be more before the oldest shown",
                      file=stream)
            for error in hook.get("errors") or []:
                print(f"  stopped: {error}", file=stream)
    for pattern in state.get("skippedTopics") or []:
        print(f"{pattern}: a wildcard topic names no repo - pass the repos to "
              "sweep as arguments", file=stream)
    if not state.get("repos"):
        print("no GitHub topic is subscribed and no repo was named - nothing "
              "to sweep", file=stream)


def save(state):
    try:
        os.makedirs(STATE_HOME, exist_ok=True)
        temporary = f"{STATE_FILE}.{os.getpid()}"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(dict({"//": WHY}, **state), handle, indent=2)
            handle.write("\n")
        os.replace(temporary, STATE_FILE)
    except OSError as error:
        print(f"agent-box-webhook-backfill: cannot write {STATE_FILE}: "
              f"{error}", file=sys.stderr)


def last_sweep():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def claim():
    """Hold the sweep lock, or return None.

    A reboot starts every session at once and each one fires a throttled
    sweep; without this they would all call GitHub and all re-request the
    same deliveries, turning one recovered burst into five.
    """
    try:
        os.makedirs(STATE_HOME, exist_ok=True)
        handle = open(LOCK_FILE, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # EAGAIN/EACCES is another sweep holding it; anything else is a
        # filesystem this cannot lock on. Neither is worth failing over -
        # a skipped sweep costs nothing, a doubled one costs deliveries.
        handle.close()
        return None
    return handle


def main(argv):
    parser = argparse.ArgumentParser(
        prog="agent-box-webhook-backfill",
        description="Report what GitHub's delivery log says about this box's "
                    "webhook ingress, and re-request every delivery it could "
                    "not hand over (issue #605).")
    parser.add_argument("repos", nargs="*", metavar="OWNER/REPO",
                        help="repositories to sweep; default is every "
                             "non-wildcard GitHub topic this box subscribes")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                        help=f"how far back to look (default {DEFAULT_HOURS})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="most deliveries to re-request per hook "
                             f"(default {DEFAULT_LIMIT}); the newest win")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what is owed and re-request nothing")
    parser.add_argument("--throttled", action="store_true",
                        help="do nothing when a sweep ran recently or one is "
                             "running; for the automatic session-start path")
    parser.add_argument("--json", action="store_true",
                        help="print the sweep record instead of prose")
    args = parser.parse_args(argv)

    if args.throttled:
        previous = last_sweep()
        stamp = previous.get("atEpoch")
        if isinstance(stamp, (int, float)) and now() - stamp < THROTTLE_S:
            return 0
    lock = claim()
    if lock is None:
        if args.throttled:
            return 0
        print("agent-box-webhook-backfill: another sweep is running",
              file=sys.stderr)
        return 0

    try:
        mine = endpoints()
        named = list(args.repos)
        if named:
            repos, skipped = named, []
        else:
            repos, skipped = subscribed_repos()
        stamp = now()
        state = {
            "at": iso(stamp),
            "atEpoch": int(stamp),
            "windowHours": args.hours,
            "limit": args.limit,
            "dryRun": bool(args.dry_run),
            "endpoints": sorted(mine),
            "skippedTopics": skipped,
            "repos": [],
        }
        cutoff = stamp - args.hours * 3600
        for repo in repos:
            state["repos"].append(
                sweep_repo(repo, mine, cutoff, max(args.limit, 0),
                           args.dry_run))
    except Failure as error:
        # A throttled sweep is a background convenience: a box with no token,
        # no network or no endpoint must not print at every session start.
        if args.throttled:
            return 0
        print(f"agent-box-webhook-backfill: {error}", file=sys.stderr)
        return 1
    finally:
        lock.close()

    if not args.dry_run:
        save(state)
    if args.json:
        json.dump(state, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif not args.throttled:
        render(state, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
