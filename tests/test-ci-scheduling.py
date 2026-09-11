"""Exercise CI lane failures and inventory drift without booting a VM."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(sys.argv.pop(1)).resolve()
CHECKS = [
    "connect", "containers", "memory-protection", "sessions", "sessions-web",
    "settings-page", "ttyd-isolation", "web-surface", "webhook",
]


class SchedulingTests(unittest.TestCase):
    def run_schedule(self, inventory=CHECKS, failure=""):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drivers = root / "drivers"
            drivers.mkdir()
            for check in inventory:
                (drivers / check).mkdir()
            stub = root / "nix"
            stub.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys
import time
checks = [arg.rsplit(".", 1)[-1] for arg in sys.argv if arg.startswith(".#")]
failing = os.environ["FAILURE"] in checks
# Failed lanes finish first; all successful siblings must still be waited on.
if not failing:
    time.sleep(0.2)
Path(os.environ["OUTPUT"], checks[0]).write_text(json.dumps(sys.argv[1:]))
sys.exit(1 if failing else 0)
''')
            stub.chmod(0o755)
            output = root / "output"
            output.mkdir()
            result = subprocess.run(
                ["bash", str(SCRIPT), str(drivers)],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                     "OUTPUT": str(output), "FAILURE": failure},
                capture_output=True, text=True, timeout=10,
            )
            calls = [json.loads(p.read_text()) for p in output.iterdir()]
            return result, calls

    def assert_all_lanes(self, calls):
        self.assertEqual(len(calls), 3)
        actual = [arg.rsplit(".", 1)[-1] for call in calls for arg in call
                  if arg.startswith(".#")]
        self.assertEqual(sorted(actual), sorted(CHECKS))
        self.assertEqual(sorted(call[call.index("--max-jobs") + 1] for call in calls),
                         ["1", "1", "2"])
        for call in calls:
            self.assertIn("--keep-going", call)
            self.assertIn("--no-link", call)

    def test_success(self):
        result, calls = self.run_schedule()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_all_lanes(calls)

    def test_each_lane_failure_still_waits_for_siblings(self):
        for check in ["sessions", "webhook", "connect"]:
            with self.subTest(check=check):
                result, calls = self.run_schedule(failure=check)
                self.assertNotEqual(result.returncode, 0)
                self.assert_all_lanes(calls)

    def test_inventory_drift_prevents_any_execution(self):
        for inventory in [CHECKS + ["new-test"], CHECKS[1:]]:
            with self.subTest(inventory=inventory):
                result, calls = self.run_schedule(inventory=inventory)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
