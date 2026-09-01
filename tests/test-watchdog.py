#!/usr/bin/env python3
r"""Unit tests for modules/src/watchdog.py — stalled-assignment detection.

Why this exists
---------------
The watchdog's whole job is a judgement call: given an open issue assigned to
this box, is somebody already on it? Both ways of getting that wrong are
expensive and neither is visible from the outside.

  * a false "stalled" starts a SECOND agent on work already in flight — the
    duplicate-session failure this box has hit repeatedly (#251, #319, #419),
    and the reason the classifier reads a failed API call as "cannot tell"
    rather than as "no";
  * a false "in flight" is the bug the watchdog exists to fix, silently. The
    assignment just stays open and nothing ever says so.

The VM tests cannot pin this down: the inputs are a GitHub timeline and other
sessions' live subscriptions, and reproducing those in a VM costs minutes a
run to assert one case. So the classifier performs no I/O of its own — it is
handed what was read — and every rule is asserted here instead.

Two cases below are regressions from the first run against this box's real
subscriptions, and both are the dangerous direction:

  * claims are per REPO. A flat set of numbers let a session holding agent-box
    PR #476 mark pulumi-defang#476 as attended;
  * `gh api user --jq .login` prints a bare word, not JSON, so parsing it as
    JSON made a box with a valid token report that it had no identity — and a
    watchdog with no identity never runs at all.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "modules" / "src" / "watchdog.py"


def load():
    spec = importlib.util.spec_from_file_location("watchdog", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wd = load()

ISSUE = {"repo": "o/r", "number": 7, "title": "t", "url": "u",
         "updatedAt": ""}
HOUR = 3600


class ClaimScoping(unittest.TestCase):
    """A claim belongs to the repo its topic names, never to a bare number."""

    def test_number_does_not_cross_repos(self):
        claimed = {"defangdevs/agent-box": {"476"}}
        self.assertTrue(wd.claims_cover(claimed, "defangdevs/agent-box", 476))
        self.assertFalse(
            wd.claims_cover(claimed, "DefangLabs/pulumi-defang", 476))

    def test_prefix_topic_covers_its_owner(self):
        claimed = {"defangdevs/*": {"12"}}
        self.assertTrue(wd.claims_cover(claimed, "defangdevs/agent-box", 12))
        self.assertFalse(wd.claims_cover(claimed, "DefangLabs/defang", 12))

    def test_unreadable_claims_are_not_an_answer(self):
        # None means "cannot tell". It must never read as "nobody claims it".
        self.assertIsNone(wd.claims_cover(None, "o/r", 1))

    def test_numbers_come_from_claims_branches_and_note(self):
        topic = {
            "topic": "github:o/r",
            "note": "PR 477 (closes #471): waiting on CI",
            "include": {"any": [
                {"path": "pull_request.number", "in": [477]},
                {"path": "workflow_run.head_branch",
                 "in": ["fix/471-hook-args-source-label"]},
            ]},
        }
        found = wd._numbers_in_topic(topic)
        # The claim itself, the issue named only in the note, and the number
        # carried by the branch ref: a session that claimed the PR is working
        # the ISSUE, and leaving it alone depends on seeing all three.
        self.assertIn("477", found)
        self.assertIn("471", found)

    def test_topic_without_a_repo_claims_nothing(self):
        self.assertIsNone(wd._topic_repo({"topic": ""}))
        self.assertIsNone(wd._topic_repo({}))
        self.assertEqual(wd._topic_repo({"topic": "github:o/r"}), "o/r")
        self.assertEqual(wd._topic_repo({"topic": "o/r"}), "o/r")


class SessionNaming(unittest.TestCase):
    """The name is the idempotency: one assignment, one session."""

    def test_stable_and_legal(self):
        name = wd.session_name("defangdevs/agent-box", 242)
        self.assertEqual(name, "wd-defangdevs-agent-box-242")
        self.assertEqual(name, wd.session_name("defangdevs/agent-box", 242))
        self.assertRegex(name, r"^[A-Za-z0-9_-]+$")

    def test_long_repo_names_stay_within_the_limit(self):
        name = wd.session_name("o/" + "x" * 400, 9)
        self.assertLessEqual(len(name), 150)
        # The number identifies the work, so it must survive truncation.
        self.assertTrue(name.endswith("-9"))

    def test_prefix_cannot_collide_with_hook_or_reserved(self):
        name = wd.session_name("o/r", 1)
        self.assertTrue(name.startswith("wd-"))
        self.assertFalse(name.startswith("hook-"))
        for reserved in ("settings", "downloads", "webhook", "sessions",
                         "token", "ws"):
            self.assertNotEqual(name, reserved)


class FilterPrefix(unittest.TestCase):
    """The filter files carry the agent user's login, not the word "agent"."""

    def setUp(self):
        self.old = os.environ.get("USER")
        os.environ["USER"] = "robot"
        os.environ["LOGNAME"] = "robot"

    def tearDown(self):
        for key in ("USER", "LOGNAME"):
            if self.old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.old

    def test_prefix_follows_the_user(self):
        # The supervisor names these from LOCAL_WEBHOOK_SESSION=$USER-$sname.
        # Hardcoding "filter.agent-" matched NOTHING on a box whose agent user
        # is called anything else — and this repo's own fixtures ship a
        # `robot` user. Every issue then looked unclaimed and the sweep would
        # have started a session beside the live one already holding it.
        self.assertEqual(wd._filter_prefix(), "filter.robot-")

    def test_claims_are_found_under_that_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "filter.robot-main.json").write_text(json.dumps({
                "topics": [{"topic": "github:o/r",
                            "include": {"any": [
                                {"path": "issue.number", "in": [7]}]}}]}),
                encoding="utf-8")
            os.environ["LOCAL_WEBHOOK_STATE_DIR"] = d
            try:
                claimed = wd.claimed_by_live_session({"main"})
            finally:
                os.environ.pop("LOCAL_WEBHOOK_STATE_DIR", None)
        self.assertTrue(wd.claims_cover(claimed, "o/r", 7))


class Capacity(unittest.TestCase):
    """Both spawned families count against the one ceiling."""

    def test_counts_hook_and_wd_only(self):
        live = {"hook-a", "wd-b", "main", "claude", "shell"}
        self.assertEqual(wd.capacity_used(live), 2)

    def test_unreadable_registry_is_not_zero(self):
        # Reading "cannot tell" as "no slots in use" would uncap spawning.
        self.assertIsNone(wd.capacity_used(None))


class Classify(unittest.TestCase):
    """Every verdict, with the I/O answers handed in rather than performed."""

    def setUp(self):
        self.state = wd._empty_state()
        self.pr = False
        self.orig = wd.open_pr_exists
        wd.open_pr_exists = lambda repo, number: self.pr

    def tearDown(self):
        wd.open_pr_exists = self.orig

    def run_one(self, claimed=None, cooldown=6 * HOUR, attempts=3, now=1000.0):
        return wd.classify(ISSUE, self.state, {"s"},
                           {} if claimed is None else claimed,
                           cooldown, attempts, now)

    def test_open_pr_is_in_flight(self):
        self.pr = True
        self.assertEqual(self.run_one()[0], "in-flight")

    def test_no_pr_and_no_claim_is_stalled(self):
        self.assertEqual(self.run_one()[0], "stalled")

    def test_live_claim_is_in_flight(self):
        self.assertEqual(self.run_one({"o/r": {"7"}})[0], "in-flight")

    def test_unreadable_timeline_is_unknown(self):
        # The dangerous direction: a GitHub outage must not read as "no PR"
        # and start an agent on top of one that is already open.
        wd.open_pr_exists = lambda repo, number: None
        self.assertEqual(self.run_one()[0], "unknown")

    def test_cooldown_suppresses_a_repeat(self):
        self.state["issues"]["o/r#7"] = {"attempts": 1, "lastPickupAt": 900.0}
        self.assertEqual(self.run_one()[0], "cooling-down")

    def test_cooldown_expires(self):
        self.state["issues"]["o/r#7"] = {"attempts": 1, "lastPickupAt": 1.0}
        self.assertEqual(self.run_one(now=1.0 + 7 * HOUR)[0], "stalled")

    def test_gives_up_after_max_attempts(self):
        # An issue that outlives this many pickups is waiting on a person, not
        # on an agent. Starting a fourth is how a watchdog becomes a nuisance.
        self.state["issues"]["o/r#7"] = {"attempts": 3, "lastPickupAt": 1.0}
        self.assertEqual(self.run_one(now=1.0 + 7 * HOUR)[0], "given-up")

    def test_zero_max_attempts_never_gives_up(self):
        self.state["issues"]["o/r#7"] = {"attempts": 99, "lastPickupAt": 1.0}
        self.assertEqual(
            self.run_one(attempts=0, now=1.0 + 7 * HOUR)[0], "stalled")

    def test_progress_clears_the_record(self):
        # Attempts must not accumulate across unrelated stalls: an issue that
        # got its PR starts from zero if it ever stalls again.
        self.state["issues"]["o/r#7"] = {"attempts": 2, "lastPickupAt": 1.0}
        self.pr = True
        self.assertEqual(self.run_one()[0], "in-flight")
        self.assertNotIn("o/r#7", self.state["issues"])


class StateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old
        self.tmp.cleanup()

    def test_round_trip(self):
        state = wd._empty_state()
        state["issues"]["o/r#1"] = {"attempts": 1, "lastPickupAt": 5.0}
        wd.save_state(state)
        self.assertEqual(wd.load_state()["issues"]["o/r#1"]["attempts"], 1)

    def test_corrupt_state_does_not_stop_the_sweep(self):
        # Losing the cooldown costs one repeated pickup, which the session
        # name already makes a no-op. Refusing to run because the bookkeeping
        # is unreadable is the worse failure: that is exactly when
        # assignments go unnoticed (#279, same lesson).
        path = Path(wd.state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(wd.load_state()["issues"], {})

    def test_missing_state_is_the_first_run(self):
        self.assertEqual(wd.load_state()["issues"], {})


class EnvConfig(unittest.TestCase):
    """The config surface is `agent-box-session env set` — user input."""

    def test_bad_values_fall_back(self):
        for raw in ("", "six", "-1", "1.5"):
            os.environ["AGENT_BOX_TEST_INT"] = raw
            self.assertEqual(wd._env_int("AGENT_BOX_TEST_INT", 6), 6, raw)
        os.environ["AGENT_BOX_TEST_INT"] = "12"
        self.assertEqual(wd._env_int("AGENT_BOX_TEST_INT", 6), 12)
        os.environ.pop("AGENT_BOX_TEST_INT", None)

    def test_repo_allowlist_splits_on_whitespace(self):
        os.environ["AGENT_BOX_WATCHDOG_REPOS"] = "  a/b   c/d \n"
        self.assertEqual(wd._repo_allowlist(), ["a/b", "c/d"])
        os.environ["AGENT_BOX_WATCHDOG_REPOS"] = ""
        self.assertEqual(wd._repo_allowlist(), [])
        os.environ.pop("AGENT_BOX_WATCHDOG_REPOS", None)


class GhOutput(unittest.TestCase):
    def test_jq_string_results_are_not_json(self):
        # `gh api user --jq .login` prints `defangdevs`, with no quotes. Asking
        # gh() to parse that as JSON returned None, and box_login() read None
        # as "this box has no GitHub identity" — so the watchdog exited 0 and
        # did nothing, on a box whose token was fine. The fix is to read the
        # object; this pins the reason.
        self.assertRaises(ValueError, json.loads, "defangdevs")
        # ...while a jq BOOLEAN result is valid JSON, which is why can_push()
        # may keep using --jq.
        self.assertEqual(json.loads("true"), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
