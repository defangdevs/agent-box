#!/usr/bin/env python3
"""Unit tests for modules/src/webhook-backfill.py (issue #605).

Every assertion here is a pure function of a canned GitHub delivery log, so
this runs natively in about a second instead of costing a VM boot - the move
`webhook-spawn-claim` and `webhook-defer` already made out of
`tests/webhook.nix`, which has no room left in its 128 KiB testScript.

`gh` is a stub that serves fixtures and records what it was asked to POST.
That makes the two assertions that matter testable at all:

  - the 19-digit delivery id must reach the API BYTE-EXACT. It is past the
    2**53 a double holds, so any JSON reader that rounds it (jq does, the
    moment it builds an object around one) produces an id that 404s and a
    sweep that silently recovers nothing. The stub compares the id it was
    given against the fixture's own string.
  - only a hook pointing at THIS box may be swept. This repo carries two
    hooks; re-requesting the other box's failed deliveries would push events
    at a machine that never asked for them.

Given the CLI and a webhook.py as well, it also covers the two halves that
live in `agent-box-webhook` itself: the `backfill` verb's delegation, and the
`ingress` object `status` reports from the last sweep's record. Those belong
here rather than in tests/webhook.nix for the same reason as the rest -
tests/webhook.nix has no room left in its 128 KiB testScript, and none of
this needs a VM.

Run directly:
    python3 tests/test-webhook-backfill.py [payload [cli.sh [webhook.py]]]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PAYLOAD = os.path.join(
    HERE, "..", "modules", "src", "webhook-backfill.py")

BASE = "https://box.example.com/agent/webhook"
# The payload's own page ceiling, so the truncation case can be built.
MAX_PAGES = 12
OURS = 111
THEIRS = 222

# Ids are strings in the fixture and must arrive as those exact digits.
IDS = {
    "old-ok": "3841517353944752001",
    "lost-a": "3841517359602860032",
    "lost-b": "3841517363555991552",
    "lost-c": "3841517366764634112",
    "healed": "3841517378150072320",
    "healed-again": "3841517883010056192",
    "page-two": "3841517000000000064",
}

FAILURES = 0


def check(condition, label, detail=""):
    global FAILURES
    if condition:
        print(f"ok   {label}")
        return True
    FAILURES += 1
    print(f"FAIL {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"       {line}")
    return False


# Ages, not timestamps: a fixture pinned to a wall-clock date passes today
# and silently ages out of every window later.
AGE_M = {
    "healed-again": 2,
    "lost-c": 5,
    "lost-b": 7,
    "healed": 8,
    "lost-a": 9,
    "page-two": 40,
    "old-ok": 60 * 24 * 4,
}


# Frozen at import, so a fixture built at :19 and an assertion made at :20
# still agree on the timestamp - the whole run is relative to one instant.
STARTED = time.time()


def when(name):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.gmtime(STARTED - AGE_M[name] * 60)) + ".000Z"


def delivery(name, event, code, action="completed", guid=None):
    return {
        "id": IDS[name],
        "guid": guid or name,
        "delivered_at": when(name),
        "event": event,
        "action": action,
        "status": "OK" if 200 <= code < 300 else "failed to connect to host",
        "status_code": code,
        "redelivery": name.endswith("-again"),
    }


def fixture():
    """Two hooks, one of them ours, and a log with every case in it."""
    return {
        "hooks": [
            {"id": OURS, "active": True,
             "config": {"url": f"{BASE}/github"}},
            {"id": THEIRS, "active": True,
             "config": {"url": "https://other-box.example.com/agent/webhook"}},
        ],
        # Newest first, the order GitHub returns.
        "deliveries": {
            str(OURS): [
                [
                    # A failure that was already recovered: same guid, 200.
                    delivery("healed-again", "workflow_run", 200,
                             guid="healed"),
                    delivery("lost-c", "check_run", 502),
                    delivery("lost-b", "workflow_run", 502),
                    delivery("healed", "workflow_run", 502,
                             action="in_progress", guid="healed"),
                    delivery("lost-a", "workflow_run", 502),
                ],
                # A second page, reachable only by following the Link header.
                [
                    delivery("page-two", "push", 502),
                    # Older than any window used below: paging must stop here.
                    delivery("old-ok", "push", 200),
                ],
            ],
            str(THEIRS): [
                [delivery("lost-a", "workflow_run", 502)],
            ],
        },
    }


STUB = '''#!{python}
"""A `gh` that answers from a fixture and records every POST."""
import json
import os
import sys

FIXTURE = json.load(open(os.environ["FIXTURE"], encoding="utf-8"))
LOG = os.environ["GH_LOG"]


def note(kind, value):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{{kind}} {{value}}\\n")


def fail(message):
    sys.stderr.write(f"gh: {{message}}\\n")
    sys.exit(1)


argv = sys.argv[1:]
if not argv or argv[0] != "api":
    fail("only `gh api` is stubbed")
argv = argv[1:]
method = "GET"
include = False
while argv and argv[0].startswith("-"):
    if argv[0] == "--include":
        include = True
        argv = argv[1:]
    elif argv[0] == "--method":
        method = argv[1]
        argv = argv[2:]
    else:
        fail(f"unexpected flag {{argv[0]}}")
if not argv:
    fail("no path")
path = argv[0]
note(method, path)

if os.environ.get("GH_BROKEN"):
    fail("HTTP 401: Bad credentials")

if method == "POST":
    # .../hooks/<hook>/deliveries/<id>/attempts - the id has to be one of
    # the fixture's own, spelled exactly.
    parts = path.strip("/").split("/")
    if parts[-1] != "attempts":
        fail(f"unexpected POST {{path}}")
    hook, given = parts[-4], parts[-2]
    known = {{d["id"]
              for page in FIXTURE["deliveries"].get(hook, [])
              for d in page}}
    if given not in known:
        fail(f"no delivery {{given}} on hook {{hook}} "
             f"(known: {{sorted(known)}})")
    print("{{}}")
    sys.exit(0)

if path.endswith("/hooks"):
    print(json.dumps(FIXTURE["hooks"]))
    sys.exit(0)

if "/deliveries" in path:
    hook = path.strip("/").split("/hooks/")[1].split("/")[0]
    pages = FIXTURE["deliveries"].get(hook, [])
    index = 0
    if "cursor=" in path:
        index = int(path.split("cursor=")[1].split("&")[0])
    if index >= len(pages):
        fail(f"no page {{index}} for hook {{hook}}")
    body = json.dumps(pages[index])
    if include:
        headers = ["HTTP/2.0 200 OK", "Content-Type: application/json"]
        if index + 1 < len(pages):
            following = (f"https://api.github.com/repos/x/y/hooks/{{hook}}"
                         f"/deliveries?per_page=100&cursor={{index + 1}}")
            headers.append(f'Link: <{{following}}>; rel="next"')
        print("\\n".join(headers) + "\\n\\n" + body)
    else:
        print(body)
    sys.exit(0)

fail(f"unhandled path {{path}}")
'''


class Box:
    """One throwaway box: its own HOME, state dir, gh stub and POST log."""

    def __init__(self, root, topics=("github:defangdevs/agent-box",),
                 sources=("github",)):
        self.root = root
        self.home = os.path.join(root, "home")
        self.state = os.path.join(self.home, ".local", "state",
                                  "local-webhook")
        self.bin = os.path.join(root, "bin")
        self.log = os.path.join(root, "gh.log")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        with open(os.path.join(self.state, "sources.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"sources": {name: {} for name in sources}}, handle)
        self.subscribe(topics)
        self.fixture = os.path.join(root, "fixture.json")
        with open(self.fixture, "w", encoding="utf-8") as handle:
            json.dump(fixture(), handle)
        stub = os.path.join(self.bin, "gh")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB.format(python=sys.executable))
        os.chmod(stub, 0o755)

    def subscribe(self, topics, name="filter.agent-main.json"):
        with open(os.path.join(self.state, name), "w",
                  encoding="utf-8") as handle:
            json.dump({"topics": [{"topic": topic} for topic in topics]},
                      handle)

    def env(self, **extra):
        environment = {
            "HOME": self.home,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "LOCAL_WEBHOOK_STATE_DIR": self.state,
            "AGENT_BOX_WEBHOOK_URL": BASE,
            "FIXTURE": self.fixture,
            "GH_LOG": self.log,
        }
        environment.update({k: v for k, v in extra.items() if v is not None})
        return environment

    def run(self, *args, **extra):
        return subprocess.run(
            [sys.executable, PAYLOAD, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env(**extra), timeout=60)

    def calls(self, method=None):
        try:
            with open(self.log, encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
        except OSError:
            return []
        if method is None:
            return lines
        return [line.split(" ", 1)[1] for line in lines
                if line.split(" ", 1)[0] == method]

    def posted(self):
        """The delivery ids the sweep asked GitHub to send again, in order."""
        return [path.strip("/").split("/")[-2] for path in self.calls("POST")]

    def state_file(self):
        path = os.path.join(self.home, ".local", "state", "agent-box",
                            "webhook-backfill.json")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)


def box(**kwargs):
    root = tempfile.mkdtemp(prefix="backfill-test-")
    ROOTS.append(root)
    return Box(root, **kwargs)


ROOTS = []


def test_recovers_only_what_is_owed():
    live = box()
    done = live.run("--hours", "24")
    check(done.returncode == 0, "a sweep exits 0", done.stderr)
    # lost-a, lost-b, lost-c and page-two failed and were never recovered.
    # `healed` failed too, but its guid also carries a 200, so it is done.
    check(live.posted() == [IDS["page-two"], IDS["lost-a"],
                            IDS["lost-b"], IDS["lost-c"]],
          "re-requests every unrecovered failure, oldest first, and skips "
          "the guid a redelivery already healed",
          live.posted())
    check(IDS["healed"] not in live.posted(),
          "never re-requests a delivery a redelivery already recovered")
    hook = live.state_file()["repos"][0]["hooks"][0]
    check(hook["lastDelivery"]["accepted"] is True
          and hook["lastDelivery"]["at"] == when("healed-again"),
          "records the newest delivery and that it was accepted",
          hook.get("lastDelivery"))


def test_ids_are_byte_exact():
    """The stub fails any id that is not one of the fixture's own strings, so
    a sweep that rounded a 19-digit id would exit non-zero here."""
    live = box()
    done = live.run("--hours", "24")
    check(done.returncode == 0 and "no delivery" not in done.stderr,
          "every 19-digit delivery id survives the round trip",
          done.stderr)
    check(all(len(given) == 19 for given in live.posted()),
          "and none of them was truncated", live.posted())


def test_other_boxes_are_left_alone():
    live = box()
    live.run("--hours", "24")
    asked = " ".join(live.calls())
    check(f"/hooks/{THEIRS}/" not in asked,
          "never reads the delivery log of a hook pointing elsewhere", asked)
    check(f"/hooks/{OURS}/" in asked, "and does read ours", asked)


def test_bare_endpoint_counts_as_ours():
    """A hook may be registered at the bare endpoint (which means the default
    source) as well as at the per-source path, and both are this box."""
    live = box()
    with open(live.fixture, encoding="utf-8") as handle:
        data = json.load(handle)
    data["hooks"][0]["config"]["url"] = BASE
    with open(live.fixture, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    live.run("--hours", "24")
    check(len(live.posted()) == 4,
          "sweeps a hook registered at the bare endpoint too", live.posted())


def test_window_and_paging():
    live = box()
    live.run("--hours", "24")
    pages = [path for path in live.calls("GET") if "/deliveries" in path]
    check(len(pages) == 2 and "cursor=1" in pages[1],
          "follows the Link header's cursor to the next page", pages)
    check(not any("/deliveries" in path and "cursor=2" in path
                  for path in live.calls("GET")),
          "and stops paging at the first record older than the window",
          pages)
    narrow = box()
    narrow.run("--hours", "0.5")
    check(len(narrow.posted()) == 3 and IDS["page-two"] not in narrow.posted(),
          "--hours narrows what is considered lost", narrow.posted())


def test_a_truncated_walk_is_never_reported_as_clean():
    """The page cap can end a walk before the window does. If what it read
    happened to be all-accepted, the report must still say the walk was cut
    short - "nothing owed" over an unfinished window is the silent-cap
    failure this whole command exists to avoid."""
    live = box()
    with open(live.fixture, encoding="utf-8") as handle:
        data = json.load(handle)
    # One page, all accepted, and a Link header promising another: exactly
    # what a walk stopped by the cap rather than by the window looks like.
    page = [record for record in data["deliveries"][str(OURS)][0]]
    for record in page:
        record["status_code"] = 200
        record["status"] = "OK"
    data["deliveries"][str(OURS)] = [page] * (MAX_PAGES + 1)
    with open(live.fixture, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    done = live.run("--hours", "24")
    check("nothing owed" in done.stdout and "longer than one sweep" in
          done.stdout,
          "a page-capped sweep says so even when it found nothing owed",
          done.stdout)
    check(live.state_file()["repos"][0]["hooks"][0]["truncated"] is True,
          "and records it, so status can read it back too")


def test_missing_webhook_scope_names_the_scope():
    """GitHub answers 404, not 403, for a token without webhook access, so
    the bare gh error reads as "no such repo"."""
    live = box()
    done = live.run("--hours", "24", GH_BROKEN="1")
    check("repository_hooks=write" in done.stderr
          and "admin:repo_hook" in done.stderr,
          "a failed hooks listing names the scope to look at", done.stderr)


def test_dry_run_changes_nothing():
    live = box()
    done = live.run("--hours", "24", "--dry-run")
    check(live.posted() == [], "--dry-run re-requests nothing", live.posted())
    check("would re-request 4" in done.stdout,
          "and still reports what is owed", done.stdout)
    record = os.path.join(live.home, ".local", "state", "agent-box",
                          "webhook-backfill.json")
    check(not os.path.exists(record),
          "and does not overwrite the last real sweep's record")


def test_limit_is_reported_never_silent():
    live = box()
    done = live.run("--hours", "24", "--limit", "2")
    check(len(live.posted()) == 2, "--limit caps one sweep", live.posted())
    check(live.posted() == [IDS["lost-b"], IDS["lost-c"]],
          "keeping the newest, which are the ones still worth having",
          live.posted())
    check("2 older one(s) left alone" in done.stdout,
          "and says what it left behind", done.stdout)
    check("re-requested 2" in done.stdout,
          "and counts what it actually asked for, not the pre-cap total",
          done.stdout)
    dry = box()
    done = dry.run("--hours", "24", "--limit", "2", "--dry-run")
    check("would re-request 2" in done.stdout
          and "2 older one(s) left alone" in done.stdout,
          "a capped DRY run says 2 too - printing the pre-cap 4 directly "
          "above '2 left alone' is the one thing it must not do",
          done.stdout)
    check(live.state_file()["repos"][0]["hooks"][0]["dropped"] == 2,
          "which status can read back too")
    none = box()
    done = none.run("--hours", "24", "--limit", "0")
    check(none.posted() == [] and "4 older one(s) left alone" in done.stdout,
          "--limit 0 reports everything and re-requests nothing "
          "(owed[-0:] would be the whole list)",
          (none.posted(), done.stdout))


def test_wildcard_topics_are_reported():
    live = box(topics=("github:defangdevs/*",))
    done = live.run("--hours", "24")
    check(live.calls() == [],
          "a wildcard topic names no repo, so nothing is swept", live.calls())
    check("wildcard topic names no repo" in done.stdout,
          "and the report says so rather than reading as 'all clear'",
          done.stdout)
    named = box(topics=("github:defangdevs/*",))
    named.run("defangdevs/agent-box", "--hours", "24")
    check(len(named.posted()) == 4,
          "naming the repo sweeps it anyway", named.posted())


def test_topics_come_from_every_filter_file():
    live = box(topics=("github:defangdevs/agent-box",))
    live.subscribe(("github:defangdevs/other",), name="filter.dispatch.json")
    live.run("--hours", "24", "--dry-run")
    swept = {path.split("/repos/")[1].split("/hooks")[0]
             for path in live.calls("GET") if path.endswith("/hooks")}
    check(swept == {"defangdevs/agent-box", "defangdevs/other"},
          "a standing watch's repo is swept as well as a session's", swept)


def test_non_github_topics_are_ignored():
    live = box(topics=("linear:TEAMID", "github:defangdevs/agent-box"))
    live.run("--hours", "24", "--dry-run")
    swept = [path for path in live.calls("GET") if "TEAMID" in path]
    check(swept == [], "a non-GitHub topic has no delivery log to read",
          swept)


def test_throttle_and_lock():
    live = box()
    live.run("--hours", "24")
    before = len(live.calls())
    done = live.run("--throttled")
    check(done.returncode == 0 and len(live.calls()) == before,
          "--throttled does nothing when a sweep just ran", done.stdout)
    quiet = box()
    lock = os.path.join(quiet.home, ".local", "state", "agent-box",
                        ".webhook-backfill.lock")
    os.makedirs(os.path.dirname(lock))
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,sys,time\n"
         "h=open(sys.argv[1],'a+')\n"
         "fcntl.flock(h,fcntl.LOCK_EX)\n"
         "print('held',flush=True)\n"
         "time.sleep(30)\n", lock],
        stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        done = quiet.run("--hours", "24")
        check(done.returncode == 0 and quiet.calls() == [],
              "a second sweep declines while one is running", done.stderr)
    finally:
        holder.kill()
        holder.wait()


def test_a_broken_token_is_loud_but_never_at_session_start():
    live = box()
    done = live.run("--hours", "24", GH_BROKEN="1")
    check(done.returncode == 1 and "Bad credentials" in done.stderr,
          "a sweep a person asked for reports why it failed", done.stderr)
    quiet = box()
    done = quiet.run("--throttled", GH_BROKEN="1")
    check(done.returncode == 0 and done.stdout == "" and done.stderr == "",
          "the automatic one at session start stays silent",
          (done.returncode, done.stdout, done.stderr))


def test_no_endpoint_is_not_a_crash():
    live = box()
    done = live.run("--hours", "24", AGENT_BOX_WEBHOOK_URL="")
    check(done.returncode == 1 and "AGENT_BOX_WEBHOOK_URL" in done.stderr,
          "a user with no browser terminal serves no endpoint, and is told",
          done.stderr)


def test_no_hook_of_ours_says_so():
    live = box()
    with open(live.fixture, encoding="utf-8") as handle:
        data = json.load(handle)
    data["hooks"] = [data["hooks"][1]]
    with open(live.fixture, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    done = live.run("--hours", "24")
    check(live.posted() == [], "nothing is swept", live.posted())
    check("point at this box" in done.stdout,
          "and a repo whose hooks all belong elsewhere is named as such",
          done.stdout)


def cli(box_, *args, delegate=True, **extra):
    """Run the real agent-box-webhook against a throwaway box.

    A stub stands in for the backfill payload, so what is asserted is the
    CLI's own behaviour: which command it runs and with what.
    """
    if delegate:
        stub = os.path.join(box_.bin, "agent-box-webhook-backfill")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         'printf "%s\\n" "$@" >> "$GH_LOG.delegate"\n')
        os.chmod(stub, 0o755)
    return subprocess.run(
        ["sh", CLI, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=box_.env(AGENT_BOX_WEBHOOK_SCRIPT=WEBHOOK_PY, **extra),
        timeout=60)


def delegated(box_):
    try:
        with open(box_.log + ".delegate", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    except OSError:
        return []


def write_sweep(box_, accepted=True, outstanding=0, requested=0):
    home = os.path.join(box_.home, ".local", "state", "agent-box")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "webhook-backfill.json"), "w",
              encoding="utf-8") as handle:
        json.dump({
            "//": "prose the CLI must strip",
            "at": "2026-09-08T05:52:09Z",
            "atEpoch": 1788846729,
            "repos": [{"repo": "defangdevs/agent-box", "hooks": [{
                "hookId": OURS,
                "outstanding": outstanding,
                "requested": requested,
                "lastDelivery": {"at": when("healed-again"),
                                 "event": "workflow_run.completed",
                                 "statusCode": 200 if accepted else 502,
                                 "accepted": accepted},
            }]}],
            "skippedTopics": [],
        }, handle)


def cli_test_the_backfill_verb_delegates():
    live = box()
    done = cli(live, "backfill", "--dry-run", "--hours", "2")
    check(done.returncode == 0, "the backfill verb runs", done.stderr)
    check(delegated(live) == ["--dry-run", "--hours", "2"],
          "and hands its arguments to the payload untouched",
          delegated(live))
    bare = box()
    done = cli(bare, "backfill", delegate=False)
    check(done.returncode == 1 and "not on PATH" in done.stderr,
          "a missing payload is a named failure, not a silent no-op",
          (done.returncode, done.stderr))


def cli_test_status_reports_ingress():
    live = box()
    write_sweep(live)
    done = cli(live, "status")
    try:
        report = json.loads(done.stdout)
    except ValueError:
        check(False, "status prints JSON", done.stdout + done.stderr)
        return
    check(report.get("ingress", {}).get("at") == "2026-09-08T05:52:09Z",
          "status carries the last sweep's record", report.get("ingress"))
    check("//" not in (report.get("ingress") or {}),
          "with the file's own prose stripped")
    check("ingress" not in done.stderr,
          "and says nothing on stderr when ingress is healthy", done.stderr)


def cli_test_status_warns_when_ingress_is_deaf():
    missing = box()
    done = cli(missing, "status")
    check(json.loads(done.stdout).get("ingress") is None
          and "no ingress sweep has run" in done.stderr,
          "no sweep yet: status says the question is unanswered",
          done.stderr)
    refused = box()
    write_sweep(refused, accepted=False)
    done = cli(refused, "status")
    check("REFUSED" in done.stderr,
          "a refused last delivery is called out", done.stderr)
    owed = box()
    write_sweep(owed, outstanding=6, requested=2)
    done = cli(owed, "status")
    check("4 delivery(s)" in done.stderr,
          "and so is what is still owed after a capped sweep", done.stderr)


def main(argv):
    global PAYLOAD, CLI, WEBHOOK_PY
    PAYLOAD = os.path.abspath(argv[0]) if argv else os.path.abspath(
        DEFAULT_PAYLOAD)
    CLI = os.path.abspath(argv[1]) if len(argv) > 1 else ""
    WEBHOOK_PY = os.path.abspath(argv[2]) if len(argv) > 2 else ""
    if not os.path.exists(PAYLOAD):
        print(f"no payload at {PAYLOAD}")
        return 2
    print(f"testing {PAYLOAD}")
    prefixes = ["test_"]
    if CLI and WEBHOOK_PY:
        print(f"...and {CLI} against {WEBHOOK_PY}")
        prefixes.append("cli_test_")
    else:
        print("skipping the CLI assertions: no webhook-cli.sh + webhook.py "
              "given")
    for name, function in sorted(globals().items()):
        if callable(function) and any(name.startswith(prefix)
                                      for prefix in prefixes):
            print(f"-- {name}")
            function()
    for root in ROOTS:
        shutil.rmtree(root, ignore_errors=True)
    if FAILURES:
        print(f"\n{FAILURES} assertion(s) failed")
        return 1
    print("\nall assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
