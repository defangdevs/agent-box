#!/usr/bin/env python3
"""Unit tests for scripts/changed_paths.py and the committed filter files.

The gate this feeds is a REQUIRED status check (issue #632), so both of its
failure directions are expensive and neither is visible in review:

  * a pattern that matches too little skips the checks on a change that
    needed them, and the gate reports green over it;
  * a pattern that matches too much runs the whole VM suite on a
    docs-only pull request.

So the dialect gets unit tests, and the real committed .paths files get
assertions against the concrete paths whose bug histories put them in the
list. Runnable directly: `python3 tests/test-changed-paths.py`.
"""

import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import changed_paths  # noqa: E402

FILTERS = ROOT / ".github" / "path-filters"
WORKFLOWS = ROOT / ".github" / "workflows"

# Every gated workflow, and the filter its `changes` job must read.
GATED = {
    "ci.yml": "ci.paths",
    "aws-ci.yml": "aws-ci.paths",
    "azure-ci.yml": "azure-ci.paths",
}


def decide(filter_name, files):
    """What the gate would answer for this set of changed paths."""
    patterns = changed_paths.load_patterns(str(FILTERS / filter_name))
    return bool(changed_paths.matches(patterns, files))


class Dialect(unittest.TestCase):
    def test_double_star_crosses_slashes(self):
        rx = changed_paths.to_regex("**.nix")
        self.assertTrue(rx.match("flake.nix"))
        self.assertTrue(rx.match("modules/agent-box.nix"))
        self.assertTrue(rx.match("a/b/c/d.nix"))
        self.assertFalse(rx.match("modules/agent-box.nix.in"))

    def test_single_star_stops_at_a_slash(self):
        rx = changed_paths.to_regex("tests/*.nix")
        self.assertTrue(rx.match("tests/webhook.nix"))
        self.assertFalse(rx.match("tests/e2e/a.nix"))

    def test_trailing_double_star_is_a_prefix(self):
        rx = changed_paths.to_regex("modules/src/**")
        self.assertTrue(rx.match("modules/src/settings.js"))
        self.assertTrue(rx.match("modules/src/vendor/idiomorph.js"))
        self.assertFalse(rx.match("modules/agent-box.nix"))

    def test_question_mark_is_one_non_slash_character(self):
        rx = changed_paths.to_regex("a?c")
        self.assertTrue(rx.match("abc"))
        self.assertFalse(rx.match("a/c"))
        self.assertFalse(rx.match("abbc"))

    def test_patterns_are_anchored_at_both_ends(self):
        rx = changed_paths.to_regex("bin/agentbox")
        self.assertTrue(rx.match("bin/agentbox"))
        self.assertFalse(rx.match("bin/agentbox.bak"))
        self.assertFalse(rx.match("x/bin/agentbox"))

    def test_dots_are_literal(self):
        # The bug this forbids: an unescaped `.` in `flake.lock` also
        # matching `flakeXlock`, which is harmless, and in `**.nix`
        # matching `anix`, which is not.
        rx = changed_paths.to_regex("flake.lock")
        self.assertTrue(rx.match("flake.lock"))
        self.assertFalse(rx.match("flakeXlock"))

    def test_an_empty_filter_is_refused(self):
        # An empty list matches nothing, so it would skip every build and
        # the gate would report green. Louder than that: a hard failure.
        empty = ROOT / "tests" / ".empty-filter-fixture"
        empty.write_text("# nothing but a comment\n", encoding="utf-8")
        try:
            with self.assertRaises(SystemExit):
                changed_paths.load_patterns(str(empty))
        finally:
            empty.unlink()


class CiFilter(unittest.TestCase):
    def test_module_sources_run_ci(self):
        for name in ("flake.nix",
                     "modules/agent-box.nix",
                     "modules/agent-box.nix.in",
                     "modules/src/settings.js",
                     "modules/src/vendor/idiomorph.js",
                     "bin/assemble-module.py",
                     "bin/agentbox",
                     "bin/golden-snapshot.py",
                     "tests/golden/web/etc/agent-box/x",
                     "tests/native/config.json",
                     "docs/potato.svg",
                     "flake.lock",
                     "scripts/check_vendor.py",
                     ".github/workflows/vendor-updates.yml"):
            with self.subTest(name):
                self.assertTrue(decide("ci.paths", [name]))

    def test_the_gate_machinery_runs_ci(self):
        # A change to what decides which jobs run has to run them.
        for name in (".github/path-filters/ci.paths",
                     ".github/path-filters/azure-ci.paths",
                     "scripts/changed_paths.py",
                     "tests/test-changed-paths.py",
                     "scripts/release_manifest.py",
                     "tests/test-release-manifest.py"):
            with self.subTest(name):
                self.assertTrue(decide("ci.paths", [name]))

    def test_documentation_only_changes_skip_ci(self):
        # The case the whole restructure exists for: this must answer
        # false AND still get a reported gate.
        self.assertFalse(decide("ci.paths", ["README.md",
                                             "AGENTS.md",
                                             "docs/index.html",
                                             "deploy/aws/README.md"]))

    def test_one_matching_path_in_a_large_change_is_enough(self):
        self.assertTrue(decide("ci.paths", ["README.md"] * 50 + ["flake.nix"]))


class DeployFilters(unittest.TestCase):
    def test_aws(self):
        self.assertTrue(decide("aws-ci.paths", ["deploy/aws/template.yaml"]))
        self.assertTrue(decide("aws-ci.paths", ["docs/index.html"]))
        self.assertTrue(decide("aws-ci.paths", ["tests/native/config.json"]))
        self.assertFalse(decide("aws-ci.paths", ["deploy/azure/agent-box.bicep"]))
        self.assertFalse(decide("aws-ci.paths", ["README.md"]))

    def test_azure(self):
        self.assertTrue(decide("azure-ci.paths", ["deploy/azure/agent-box.bicep"]))
        self.assertTrue(decide("azure-ci.paths", ["deploy/azure/README.md"]))
        self.assertFalse(decide("azure-ci.paths", ["deploy/aws/template.yaml"]))
        self.assertFalse(decide("azure-ci.paths", ["README.md"]))


class Wiring(unittest.TestCase):
    """The workflow side, which no other check looks at.

    A filter file nothing reads, or a gated workflow that quietly grew a
    trigger-level `paths:` again, both put the repo back where issue #632
    found it - with a required check that is never reported.
    """

    def test_every_filter_is_read_by_its_workflow(self):
        for workflow, filt in GATED.items():
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            with self.subTest(workflow):
                self.assertIn(f".github/path-filters/{filt}", text)
                self.assertIn("scripts/changed_paths.py", text)

    def test_no_gated_workflow_filters_on_its_trigger(self):
        # `paths:`/`paths-ignore:` inside `on:` is exactly the shape that
        # makes a workflow report nothing. The gated ones must not have it.
        for workflow in GATED:
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            body = text.split("\njobs:", 1)[0]
            with self.subTest(workflow):
                self.assertIsNone(
                    re.search(r"^\s*paths(-ignore)?:", body, re.M),
                    f"{workflow}: the trigger filters on paths again; the "
                    "filter belongs in .github/path-filters (issue #632)")

    def test_every_gated_workflow_has_an_always_reporting_gate(self):
        for workflow in GATED:
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            with self.subTest(workflow):
                self.assertIn("if: always()", text)
                self.assertIn("gate:", text)

    def test_no_orphan_filter_files(self):
        on_disk = {p.name for p in FILTERS.glob("*.paths")}
        self.assertEqual(on_disk, set(GATED.values()))


class Cli(unittest.TestCase):
    def test_files_from_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "changed_paths.py"),
             str(FILTERS / "ci.paths"), "--files-from", "-"],
            input="README.md\nflake.nix\n", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "true")

    def test_no_usable_range_runs_the_checks(self):
        # A force-push, a new branch, a manual dispatch. Fail SAFE: run.
        for base in ("", "0" * 40):
            with self.subTest(base=base):
                self.assertEqual(
                    self._run(["--base", base, "--head", "HEAD"]), "true")

    @unittest.skipUnless(
        (ROOT / ".git").exists() and shutil.which("git"),
        "no git checkout here (the flake check copies files, not a repo)")
    def test_a_real_commit_range_is_read_from_git(self):
        # An empty range (a rev against itself) touches nothing.
        self.assertEqual(
            self._run(["--base", "HEAD", "--head", "HEAD"]), "false")

    def _run(self, extra):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "changed_paths.py"),
             str(FILTERS / "ci.paths")] + extra,
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()


if __name__ == "__main__":
    unittest.main(verbosity=2)
