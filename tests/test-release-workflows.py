"""Exercise release refusal paths from the actual workflow step scripts."""
import ast
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


def condition(expression):
    """Only the booleans and string equality used by the publication guard."""
    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [visit(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if isinstance(node.ops[0], ast.Eq):
                return visit(node.left) == visit(node.comparators[0])
        raise AssertionError(f"unsupported workflow condition: {ast.dump(node)}")
    return visit(ast.parse("(" + expression + ")", mode="eval").body)


class ReleaseWorkflows(unittest.TestCase):
    def run_script(self, script, gh_body, files=None, extra_env=None, stubs=None, setup=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "gh"
            stub.write_text(f"#!{sys.executable}\n" + gh_body)
            stub.chmod(0o755)
            for name, body in (stubs or {}).items():
                tool = root / name
                tool.write_text(f"#!{sys.executable}\n" + body)
                tool.chmod(0o755)
            for name, content in (files or {}).items():
                (root / name).write_text(content)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "GITHUB_REPOSITORY": "dummy/repo", "REV": "a" * 40,
                   "TAG": "release-fixture", "REASON": "fixture",
                   "GITHUB_OUTPUT": str(root / "outputs"),
                   **(extra_env or {})}
            if setup:
                setup(root, env)
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
            self.assertEqual(condition(expression), expected)
        native = PROMOTE["jobs"]["lightsail-test"]
        self.assertEqual(native["uses"], "./.github/workflows/lightsail-test.yml")
        self.assertEqual(native["with"]["ref"], "${{ needs.candidate.outputs.rev }}")

    def test_standalone_publication_cannot_rebuild_a_manifest(self):
        events = PUBLISH.get("on", PUBLISH.get(True))
        self.assertEqual(set(events), {"workflow_call"})
        steps = PUBLISH["jobs"]["publish"]["steps"]
        self.assertFalse(any("release_manifest.py build" in item.get("run", "")
                             for item in steps))

    def test_a_new_candidate_cannot_skip_fresh_boot(self):
        script = step("candidate", "Decide whether this is a fresh candidate or a re-promotion")
        for tag, skip, expected in [("", "true", False), ("", "false", True),
                                    ("release-original", "true", True)]:
            result = self.run_script(script, "", extra_env={"SKIP_DEPLOY_TEST": skip},
                                     stubs={"git": f"print({tag!r})\n"})
            self.assertEqual(result.returncode == 0, expected, result.stderr)

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

    def test_rollback_refusal_and_explicit_pointer_restore_against_local_remote(self):
        preflight = step("candidate", "Refuse an unauthorized rollback before any public write")
        move = step("record", "Move the `release` pointer")

        def setup(root, env):
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, env=env, text=True).strip()
            git("init", "--bare", "-q", "remote.git")
            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root / "marker").write_text("old\n")
            git("add", "marker")
            git("commit", "-qm", "old release")
            env["REV"] = git("rev-parse", "HEAD")
            (root / "marker").write_text("new\n")
            git("commit", "-qam", "new release")
            git("remote", "add", "origin", str(root / "remote.git"))
            git("push", "-q", "origin", "HEAD:refs/heads/release")

        for allow in ["false", "true"]:
            script = preflight + "\n" + move + "\n" + '''
actual=$(git ls-remote --heads origin refs/heads/release | cut -f1)
test "$actual" = "$REV"
'''
            result = self.run_script(script, "", setup=setup,
                                     extra_env={"ALLOW_ROLLBACK": allow,
                                                "GIT_CONFIG_GLOBAL": "/dev/null"})
            self.assertEqual(result.returncode == 0, allow == "true", result.stderr)


if __name__ == "__main__":
    unittest.main()
