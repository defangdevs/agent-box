"""Exercise release refusal paths from the actual workflow step scripts."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


PROMOTE = workflow("promote.yml")
PUBLISH = workflow("publish-template.yml")


def step(job, name):
    return next(item["run"] for item in PROMOTE["jobs"][job]["steps"]
                if item.get("name") == name)


class ReleaseWorkflows(unittest.TestCase):
    def run_script(self, script, gh_body, files=None, extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "gh"
            stub.write_text(f"#!{sys.executable}\n" + gh_body)
            stub.chmod(0o755)
            for name, content in (files or {}).items():
                (root / name).write_text(content)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "GITHUB_REPOSITORY": "dummy/repo", "REV": "a" * 40,
                   "TAG": "release-fixture", "REASON": "fixture",
                   **(extra_env or {})}
            return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                  cwd=root, env=env, capture_output=True, text=True,
                                  timeout=10)

    def test_absent_failed_pending_and_other_app_checks_block_promotion(self):
        script = step("candidate", "Require every aggregate CI gate to be green for that sha")
        good = [{"name": name, "status": "completed", "conclusion": "success",
                 "started_at": "2026-01-01", "app": {"slug": "github-actions"}}
                for name in ["CI gate", "AWS template gate", "Azure template gate"]]
        variants = [(good, True), (good[:-1], False)]
        for field, value in [("conclusion", "failure"), ("conclusion", "cancelled"),
                             ("status", "in_progress"), ("conclusion", "skipped"),
                             ("app", {"slug": "other-app"})]:
            rows = json.loads(json.dumps(good))
            rows[0][field] = value
            variants.append((rows, False))
        for rows, expected in variants:
            result = self.run_script(script, f"print({json.dumps([{'check_runs': rows}])!r})\n")
            self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_both_fresh_boot_results_are_required_for_publication(self):
        publish = PROMOTE["jobs"]["publish"]
        self.assertEqual(set(publish["needs"]), {"candidate", "deploy-test", "lightsail-test"})
        states = ["success", "failure", "cancelled", "skipped"]
        for candidate, ec2, native, skip in itertools.product(states, states, states, [True, False]):
            expression = publish["if"].replace("always()", "True")
            for name, value in [("candidate", candidate), ("deploy-test", ec2),
                                ("lightsail-test", native)]:
                expression = expression.replace(f"needs.{name}.result", repr(value))
            expression = expression.replace("inputs.skip_deploy_test", repr(skip))
            expression = expression.replace("&&", "and").replace("||", "or")
            expected = candidate == "success" and (
                ec2 == native == "success" or skip and ec2 == native == "skipped")
            self.assertEqual(eval(expression, {"__builtins__": {}}), expected)
        native = PROMOTE["jobs"]["lightsail-test"]
        self.assertEqual(native["uses"], "./.github/workflows/lightsail-test.yml")
        self.assertEqual(native["with"]["ref"], "${{ needs.candidate.outputs.rev }}")

    def test_standalone_publication_cannot_rebuild_a_manifest(self):
        events = PUBLISH.get("on", PUBLISH.get(True))
        self.assertEqual(set(events), {"workflow_call"})
        steps = PUBLISH["jobs"]["publish"]["steps"]
        self.assertFalse(any("release_manifest.py build" in item.get("run", "")
                             for item in steps))

    def test_repromotion_preserves_original_release_and_rejects_changed_manifest(self):
        script = step("record", "Publish the Release object carrying the manifest")
        for original, expected in [("original\n", True), ("changed\n", False)]:
            stub = f"""
from pathlib import Path
import sys
args = sys.argv[1:]
if args[:2] == ['release', 'view']:
    pass
elif args[:2] == ['release', 'download']:
    Path('original/release-manifest.json').write_text({original!r})
else:
    sys.exit('attempt to mutate the original release: ' + repr(args))
"""
            result = self.run_script(script, stub, {"release-manifest.json": "original\n"})
            self.assertEqual(result.returncode == 0, expected, result.stderr)
        self.assertIn("always()", PROMOTE["jobs"]["record"]["if"])

    def test_release_lookup_errors_cannot_be_treated_as_first_promotion(self):
        script = step("candidate", "Refuse an unauthorized rollback before any public write")
        result = self.run_script(script, "", extra_env={"GIT_CONFIG_GLOBAL": "/dev/null"})
        # The fixture has no git remote. A failed lookup must exit before
        # printing the first-promotion exception to the ancestry guard.
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("No release branch yet", result.stdout)


if __name__ == "__main__":
    unittest.main()
