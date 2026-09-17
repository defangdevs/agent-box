"""Check the workflow's failure gates, lane budget and VM build invocation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


SCRIPT = Path(sys.argv.pop(1)).resolve()
WORKFLOW = yaml.safe_load(Path(sys.argv.pop(1)).read_text())
LANES = json.loads(Path(sys.argv.pop(1)).read_text())


class SchedulingTests(unittest.TestCase):
    def run_schedule(self, checks, jobs, failure=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drivers = root / "drivers"
            drivers.mkdir()
            for check in checks:
                (drivers / check).mkdir()
            stub = root / "nix"
            stub.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys
Path(os.environ["OUTPUT"]).write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ["FAILURE"]))
''')
            stub.chmod(0o755)
            output = root / "output"
            result = subprocess.run(
                ["bash", str(SCRIPT), str(drivers), str(jobs)],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                     "OUTPUT": str(output), "FAILURE": str(int(failure))},
                capture_output=True, text=True, timeout=10,
            )
            call = json.loads(output.read_text()) if output.exists() else None
            return result, call

    def test_each_lane_runs_exact_prepared_inventory_and_propagates_failure(self):
        for lane, spec in LANES.items():
            for failure in [False, True]:
                with self.subTest(lane=lane, failure=failure):
                    result, call = self.run_schedule(spec["checks"], spec["jobs"], failure)
                    self.assertEqual(result.returncode, int(failure), result.stderr)
                    self.assertEqual(
                        sorted(arg.rsplit(".", 1)[-1] for arg in call if arg.startswith(".#")),
                        sorted(spec["checks"]),
                    )
                    self.assertEqual(call[call.index("--max-jobs") + 1], str(spec["jobs"]))
                    self.assertIn("--keep-going", call)
                    self.assertIn("--no-link", call)

    def test_empty_inventory_and_invalid_budget_do_not_execute(self):
        for checks, jobs in [([], 1), (["sessions"], 0), (["sessions"], 4)]:
            result, call = self.run_schedule(checks, jobs)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(call)

    # Whether a native-test or validator edit actually triggers CI is now a
    # question about .github/path-filters/ci.paths, not this workflow's
    # trigger (issue #632 removed the trigger-level `paths:` entirely) -
    # see tests/test-changed-paths.py's CiFilter and Wiring cases instead.

    def test_native_and_vm_are_gated_on_changes(self):
        for job in ("native", "vm"):
            with self.subTest(job=job):
                self.assertEqual(WORKFLOW["jobs"][job]["needs"], "changes")
                self.assertEqual(
                    WORKFLOW["jobs"][job]["if"],
                    "needs.changes.outputs.build == 'true'")

    def test_matrix_matches_nix_lanes_and_keeps_concurrency_budget(self):
        strategy = WORKFLOW["jobs"]["vm"]["strategy"]
        self.assertIs(strategy["fail-fast"], False)
        matrix = strategy["matrix"]["include"]
        self.assertEqual(len(matrix), len(LANES))
        self.assertEqual({row["lane"]: row["jobs"] for row in matrix},
                         {lane: spec["jobs"] for lane, spec in LANES.items()})
        self.assertEqual(sum(row["jobs"] for row in matrix), 4)

    def test_gate_rejects_failure_cancellation_and_skipped_jobs(self):
        # The `changes` job deciding whether CI's build paths changed sits
        # in front of `native`/`vm` (issue #632); the `gate` job at the
        # bottom is what the branch ruleset requires, and it has to report
        # correctly whether or not those two jobs even ran.
        gate = WORKFLOW["jobs"]["gate"]
        self.assertEqual(gate["name"], "CI gate")
        self.assertEqual(sorted(gate["needs"]), ["changes", "native", "vm"])
        self.assertEqual(gate["if"], "always()")
        step, = gate["steps"]
        self.assertEqual(step["env"], {
            "CHANGES": "${{ needs.changes.result }}",
            "NATIVE": "${{ needs.native.result }}",
            "VM": "${{ needs.vm.result }}",
            "BUILD": "${{ needs.changes.outputs.build }}",
        })
        for changes in ["success", "failure"]:
            for build in ["true", "false"]:
                for native in ["success", "failure", "cancelled", "skipped"]:
                    for vm in ["success", "failure", "cancelled", "skipped"]:
                        with self.subTest(changes=changes, build=build,
                                          native=native, vm=vm):
                            result = subprocess.run(
                                ["bash", "-e", "-c", step["run"]],
                                capture_output=True,
                                env={**os.environ, "CHANGES": changes,
                                     "BUILD": build, "NATIVE": native,
                                     "VM": vm},
                                timeout=10,
                            )
                            # `changes` must succeed, and each of native/vm
                            # must either succeed outright, or be skipped
                            # while the build paths did NOT change (a skip
                            # while they DID change means the job's own
                            # guard expression is broken).
                            def ok(job_result):
                                return (job_result == "success"
                                        or (job_result == "skipped"
                                            and build != "true"))
                            expected = (changes == "success"
                                        and ok(native) and ok(vm))
                            self.assertEqual(
                                result.returncode == 0, expected,
                                result.stderr)


if __name__ == "__main__":
    unittest.main()
