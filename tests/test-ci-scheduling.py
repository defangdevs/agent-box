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

    def test_matrix_matches_nix_lanes_and_keeps_concurrency_budget(self):
        strategy = WORKFLOW["jobs"]["vm"]["strategy"]
        self.assertIs(strategy["fail-fast"], False)
        matrix = strategy["matrix"]["include"]
        self.assertEqual(len(matrix), len(LANES))
        self.assertEqual({row["lane"]: row["jobs"] for row in matrix},
                         {lane: spec["jobs"] for lane, spec in LANES.items()})
        self.assertEqual(sum(row["jobs"] for row in matrix), 4)

    def test_gate_rejects_failure_cancellation_and_skipped_jobs(self):
        gate = WORKFLOW["jobs"]["validate"]
        self.assertEqual(gate["name"], "Validate module & VM")
        self.assertEqual(sorted(gate["needs"]), ["native", "vm"])
        self.assertEqual(gate["if"], "${{ always() }}")
        step, = gate["steps"]
        self.assertEqual(step["env"], {
            "NATIVE_RESULT": "${{ needs.native.result }}",
            "VM_RESULT": "${{ needs.vm.result }}",
        })
        for native in ["success", "failure", "cancelled", "skipped"]:
            for vm in ["success", "failure", "cancelled", "skipped"]:
                result = subprocess.run(
                    ["bash", "-e", "-c", step["run"]], capture_output=True,
                    env={**os.environ, "NATIVE_RESULT": native, "VM_RESULT": vm},
                    timeout=10,
                )
                self.assertEqual(result.returncode == 0, native == vm == "success")


if __name__ == "__main__":
    unittest.main()
