#!/usr/bin/env python3
r"""Unit tests for modules/src/lib/registry.sh — the session registry's write
protocol (#254, #289).

Why this exists
---------------
sessions.json decides which sessions this box runs, and five programs write it
— four shell ones that splice the library under test in, and the settings
daemon, which is python and takes the same flock(2) on the same sidecar file
through fcntl. Until now nothing in the suite referenced #254 at all (issue
#285): the VM tests deliberately avoid concurrency (they stop the supervisor,
or write once "so the supervisor's first spawn already sees the final
config"), so a lock that stopped working would cost 300 seconds a run and
still go unnoticed.

So the protocol gets a unit test, natively, on every architecture, in about a
second. It asserts what the five writers have to agree on:

  * concurrent read-modify-writes all survive — the lost update itself;
  * a HELD lock blocks a writer, and blocks it ACROSS LANGUAGES: python holds
    it the way the settings daemon does and the shell writer waits, and the
    other way round. That agreement is the whole guarantee and this is the
    only thing that checks it;
  * nesting does not deadlock (flock(2) conflicts between two descriptions of
    the same process, so a writer can block against itself);
  * an INHERITED lock survives the exec agent-box-webhook-spawn does, and the
    heir does not close it;
  * creation is inside the protocol too (#289), and never clobbers;
  * every way the lock can be unavailable — no flock binary, a holder that
    times us out — still writes, because a missing lock must never be the
    reason a session cannot be added.

Writers are composed the way the generated module composes them (a header of
REGISTRY_* pins, then the library text, then the caller's own body), so the
library and the wrappers cannot drift apart unnoticed.
"""
import fcntl
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = (REPO / "modules" / "src" / "lib" / "registry.sh").read_text()
FLOCK = shutil.which("flock") or ""
JQ = shutil.which("jq") or "jq"

# Long enough that a slow machine does not fail a test, short enough that a
# genuinely wedged case does not hang CI: every wait here is on a condition,
# never a fixed sleep.
TIMEOUT = 20.0
# What "it did not happen" is worth: the window in which a blocked writer must
# NOT have written. A false pass costs a test that proves less, never a test
# that fails on a fast machine.
BLOCKED_FOR = 1.0


def wait_until(predicate, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.file = self.home / ".config" / "agent-box" / "sessions.json"
        self.file.parent.mkdir(parents=True)
        self.addCleanup(self.dir.cleanup)

    # --- composing a writer ------------------------------------------------
    def writer(self, body, flock=None, wait=None, prog="test-writer", file=None):
        """Write out a shell writer composed as the generated module composes
        one: the wrapper's REGISTRY_* pins, the shared library, then the body.
        """
        path = self.home / ("writer-%d.sh" % len(list(self.home.glob("writer-*.sh"))))
        header = [
            "set -u",
            "REGISTRY_FILE=%s" % (file or self.file),
            "REGISTRY_JQ=%s" % JQ,
            "REGISTRY_FLOCK=%s" % (FLOCK if flock is None else flock),
            "REGISTRY_PROG=%s" % prog,
        ]
        if wait is not None:
            header.append("REGISTRY_LOCK_WAIT=%s" % wait)
        path.write_text("\n".join(header) + "\n" + LIB + "\n" + body + "\n")
        return path

    def env(self):
        # Hermetic: this box runs the very units under test, so an inherited
        # AGENT_BOX_FLOCK_BIN would answer a writer that says it has no flock.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BOX_")}
        clean["HOME"] = str(self.home)
        return clean

    def run_writer(self, body, args=(), check=True, **kw):
        path = self.writer(body, **kw)
        done = subprocess.run(
            ["bash", str(path)] + list(args),
            capture_output=True, text=True, timeout=TIMEOUT, env=self.env(),
        )
        if check:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def start_writer(self, body, args=(), **kw):
        path = self.writer(body, **kw)
        proc = subprocess.Popen(
            ["bash", str(path)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env(),
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc):
        # A writer left running would hold the lock into the next test, and an
        # unclosed pipe is a warning in the log the next reader has to discount.
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    # --- the other language ------------------------------------------------
    def hold_lock_as_the_daemon_does(self):
        """Take the sidecar lock exactly as settings-daemon.py's sessions_lock
        does — same path, same primitive — and return the open file."""
        handle = open(str(self.file) + ".lock", "a", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def lock_is_free(self):
        """True when nobody holds the sidecar lock, asked the way the daemon
        would ask it."""
        with open(str(self.file) + ".lock", "a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    # --- helpers -----------------------------------------------------------
    def seed(self, sessions=None):
        self.file.write_text(json.dumps({"version": 1, "sessions": sessions or {}}))

    def sessions(self):
        return json.loads(self.file.read_text())["sessions"]

    ADD_ONE = (
        'registry_edit --arg n "$1" '
        "'.sessions[$n] = {agent: \"claude\"}'"
    )


class LostUpdate(RegistryCase):
    """The race the lock exists for (#254)."""

    WRITERS = 24

    def concurrent_add(self, flock=None):
        # A start gate, because spawning 24 processes costs more than the
        # read-modify-write each of them then does: without it they queue up
        # behind their own startup and never overlap, which would make an
        # unlocked run look safe.
        #
        # Each writer reports READY before it starts spinning, and the gate
        # opens only once all of them have. Waiting for "no child has exited
        # yet" would prove nothing — Popen has just returned — and on a loaded
        # runner the gate would open while some writers were still in bash
        # startup, degrading the run toward serial.
        self.seed()
        ready = self.home / "ready"
        ready.mkdir()
        gate = self.home / "go"
        body = 'touch %s/"$1"\nwhile [ ! -e %s ]; do :; done\n%s' % (ready, gate, self.ADD_ONE)
        procs = [
            self.start_writer(body, args=["s%02d" % i], flock=flock)
            for i in range(self.WRITERS)
        ]
        self.assertTrue(wait_until(lambda: len(list(ready.iterdir())) == self.WRITERS),
                        "only %d of %d writers reached the gate"
                        % (len(list(ready.iterdir())), self.WRITERS))
        gate.touch()
        for proc in procs:
            # communicate() drains the pipes while it waits: a writer that
            # filled its stderr buffer would otherwise block on write and turn
            # a failure into a 20s hang.
            _, err = proc.communicate(timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, err)
        return self.sessions()

    def test_every_concurrent_write_survives(self):
        got = self.concurrent_add()
        missing = [i for i in range(self.WRITERS) if "s%02d" % i not in got]
        self.assertEqual(missing, [], "lost %d of %d updates" % (len(missing), self.WRITERS))

    def test_unlocked_is_the_behaviour_being_fixed(self):
        # Not an assertion — a measurement, printed so a reader can see the
        # lock is load-bearing rather than take it on trust. The live-box
        # number in the library's comment is 96 of 300 (32%). Asserting a loss
        # here would be asserting a race, which is the one thing a test may
        # not do.
        lost = self.WRITERS - len(self.concurrent_add(flock=""))
        print("\n  unlocked: lost %d of %d concurrent updates" % (lost, self.WRITERS))


class HeldLock(RegistryCase):
    """A held lock blocks the other writers — in both languages."""

    def test_python_holder_blocks_a_shell_writer(self):
        # The settings daemon holds the lock while it reads, edits and
        # publishes; a shell writer must wait for it and then write. This is
        # the cross-language agreement, and nothing else checks it.
        self.seed()
        holder = self.hold_lock_as_the_daemon_does()
        proc = self.start_writer(self.ADD_ONE, args=["waited"])
        time.sleep(BLOCKED_FOR)
        self.assertNotIn("waited", self.sessions(), "wrote while the daemon held the lock")
        self.assertIsNone(proc.poll(), "the writer did not wait")
        fcntl.flock(holder, fcntl.LOCK_UN)
        _, err = proc.communicate(timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("waited", self.sessions())

    def test_shell_holder_blocks_the_daemon(self):
        # The other direction: while a shell writer holds the lock, the
        # daemon's own fcntl attempt must fail. Same sidecar path, spelled
        # from python as SESSIONS_FILE + ".lock".
        self.seed()
        proc = self.start_writer(
            "registry_lock\n"
            'echo held > "$1"\n'
            "sleep 3\n"
            "registry_unlock\n",
            args=[str(self.home / "held")],
        )
        self.assertTrue(wait_until(lambda: (self.home / "held").exists()))
        self.assertFalse(self.lock_is_free(), "the daemon could take a held lock")
        self.assertTrue(wait_until(lambda: proc.poll() is not None))
        self.assertTrue(self.lock_is_free(), "the lock outlived the writer")

    def test_the_lock_is_a_sidecar_and_the_registry_is_replaced(self):
        # Why the lock cannot live on the registry itself: a writer replaces
        # that inode, so a lock taken on the inode it read is not the lock the
        # next writer takes. Both halves are asserted, because the sidecar
        # path is also the cross-language contract.
        self.seed()
        before = self.file.stat().st_ino
        self.run_writer(self.ADD_ONE, args=["one"])
        self.assertNotEqual(before, self.file.stat().st_ino, "the registry was edited in place")
        self.assertTrue((self.home / ".config/agent-box/sessions.json.lock").exists())


class StderrSurvives(RegistryCase):
    """Unlocking must close fd 9 and nothing else."""

    MARKER = "still-talking"

    def test_the_program_can_still_talk_after_unlocking(self):
        # `exec` with no command applies its redirections to the CURRENT SHELL
        # and keeps them, so the obvious `exec 9>&- 2>/dev/null` closes the lock
        # fd and then throws this program's stderr away for good. The supervisor
        # logs to stderr — that IS the journal — so it lost every diagnostic
        # after its first unlock, including the plugin-refresh line a VM test
        # waits 60s for. Cheap to break, invisible without this test.
        self.seed()
        done = self.run_writer(
            "registry_lock\nregistry_unlock\necho %s >&2\n" % self.MARKER)
        self.assertIn(self.MARKER, done.stderr)

    def test_the_program_can_still_talk_after_a_timeout(self):
        # Same trap on the degraded path, where the fd is closed straight after
        # the warning — and where losing stderr would also lose the reason.
        self.seed()
        self.hold_lock_as_the_daemon_does()
        done = self.run_writer(
            "registry_lock\necho %s >&2\n" % self.MARKER, wait=1)
        self.assertIn("lock timed out", done.stderr)
        self.assertIn(self.MARKER, done.stderr)

    def test_an_edit_leaves_stderr_alone(self):
        # registry_edit unlocks on both its paths; neither may cost the caller
        # its stderr.
        self.seed()
        done = self.run_writer(
            self.ADD_ONE + "\nregistry_edit '.sessions | error(\"no\")' 2>/dev/null\n"
            "echo %s >&2\n" % self.MARKER, args=["one"], check=False)
        self.assertIn(self.MARKER, done.stderr)


class Nesting(RegistryCase):
    """flock(2) conflicts between two descriptions of the same process."""

    def test_edit_inside_a_held_section_does_not_deadlock(self):
        # The shape both the supervisor (start_session around mark_started)
        # and the CLI (a check-then-write around registry_edit) use. Without
        # the depth counter this blocks against itself forever, so the timeout
        # in run_writer IS the assertion.
        self.seed()
        self.run_writer(
            "registry_lock\n"
            + self.ADD_ONE + "\n"
            + self.ADD_ONE.replace('"$1"', '"$1-two"') + "\n"
            "registry_unlock\n",
            args=["nested"],
        )
        self.assertIn("nested", self.sessions())
        self.assertIn("nested-two", self.sessions())

    def test_the_lock_is_released_once_the_outermost_section_ends(self):
        self.seed()
        self.run_writer("registry_lock\n" + self.ADD_ONE + "\nregistry_unlock\n", args=["x"])
        self.assertTrue(self.lock_is_free())


class InheritedLock(RegistryCase):
    """agent-box-webhook-spawn holds the lock across its exec into the CLI."""

    def test_the_heir_neither_reopens_nor_closes_the_inherited_fd(self):
        # The spawn wrapper's cap check and the add it execs into have to be
        # one critical section. The heir must not re-open fd 9 (that would
        # close the description and drop the lock) and must not close it in
        # registry_unlock (same). So: while the heir runs, the lock is still
        # held — asked from python, which shares nothing with either process.
        self.seed()
        heir = self.writer(
            "registry_lock\n"
            + self.ADD_ONE + "\n"
            "registry_unlock\n"
            'echo edited > "$2"\n'
            "sleep 3\n",
        )
        proc = self.start_writer(
            "registry_lock\n"
            'export AGENT_BOX_REGISTRY_LOCK_FD=9\n'
            'exec bash %s "$@"\n' % heir,
            args=["heir", str(self.home / "edited")],
        )
        self.assertTrue(wait_until(lambda: (self.home / "edited").exists()),
                        "the heir never wrote")
        self.assertIn("heir", self.sessions())
        self.assertFalse(self.lock_is_free(), "the heir dropped the inherited lock")
        self.assertTrue(wait_until(lambda: proc.poll() is not None))
        self.assertTrue(self.lock_is_free())

    def test_a_writer_that_could_not_lock_advertises_nothing(self):
        # The wrapper exports the fd only when REGISTRY_HELD says it really
        # holds one: an heir told about a lock that was never taken would skip
        # its own.
        self.seed()
        done = self.run_writer('registry_lock\nprintf "%s" "$REGISTRY_HELD"\n', flock="")
        self.assertEqual(done.stdout, "0")
        done = self.run_writer('registry_lock\nprintf "%s" "$REGISTRY_HELD"\n')
        self.assertEqual(done.stdout, "1")


class Degrading(RegistryCase):
    """Every way the lock can be unavailable still writes."""

    def test_no_flock_binary_still_writes(self):
        # A box whose module predates the pin has no AGENT_BOX_FLOCK_BIN, and
        # a missing lock must never be the reason a session cannot be added.
        self.seed()
        self.run_writer(self.ADD_ONE, args=["unlocked"], flock="")
        self.assertIn("unlocked", self.sessions())

    def test_a_wedged_holder_times_out_and_says_so(self):
        # The bound is what keeps a wedged holder from parking the supervisor's
        # reconcile loop, which every session on the box waits behind.
        self.seed()
        self.hold_lock_as_the_daemon_does()
        started = time.monotonic()
        done = self.run_writer(self.ADD_ONE, args=["degraded"], wait=1)
        self.assertLess(time.monotonic() - started, TIMEOUT / 2)
        self.assertIn("degraded", self.sessions())
        self.assertIn("lock timed out", done.stderr)
        self.assertIn("#254", done.stderr)
        self.assertIn("test-writer", done.stderr)

    def test_a_write_that_cannot_land_is_reported_as_a_failure(self):
        # The two writers that do not run under `set -e` — the supervisor and
        # the pane epilogue — act on registry_edit's status, so a publish that
        # never happened must not come back as success. A read-only directory
        # is the shape a full disk or a remounted $HOME takes here.
        if os.geteuid() == 0:
            self.skipTest("root ignores the directory mode this test relies on")
        self.seed({"keep": {"agent": "claude"}})
        before = self.file.read_text()
        os.chmod(self.file.parent, 0o500)
        self.addCleanup(os.chmod, self.file.parent, 0o700)
        done = self.run_writer(self.ADD_ONE, args=["doomed"], check=False)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(self.file.read_text(), before)

    def test_a_failing_filter_leaves_the_registry_alone(self):
        # registry_edit publishes jq's output or nothing at all: a filter that
        # fails must not truncate the file every writer reads.
        self.seed({"keep": {"agent": "claude"}})
        before = self.file.read_text()
        done = self.run_writer("registry_edit '.sessions | error(\"no\")' 2>/dev/null\n",
                               check=False)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(list(self.home.glob(".config/agent-box/sessions.json.*")),
                         [self.home / ".config/agent-box/sessions.json.lock"])


class Creation(RegistryCase):
    """Creating the registry is part of the protocol too (#289)."""

    def setUp(self):
        super().setUp()
        self.seed_file = self.home / "seed.json"
        self.seed_file.write_text(json.dumps({"version": 1, "sessions": {"main": {}}}))

    def test_the_seed_waits_for_a_holder(self):
        # The first-boot race: the supervisor seeds the registry while a
        # webhook spawn adds a session to it, both having found no file. The
        # seed must not decide "there is nothing here" while another writer
        # holds the lock.
        holder = self.hold_lock_as_the_daemon_does()
        proc = self.start_writer('registry_ensure "$1"\n', args=[str(self.seed_file)])
        time.sleep(BLOCKED_FOR)
        self.assertFalse(self.file.exists(), "seeded while another writer held the lock")
        fcntl.flock(holder, fcntl.LOCK_UN)
        _, err = proc.communicate(timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(self.sessions(), {"main": {}})

    def test_an_existing_registry_is_never_clobbered(self):
        # Sessions are runtime data (#59): a rebuild reseeds nothing.
        self.seed({"added-at-runtime": {"agent": "codex"}})
        self.run_writer('registry_ensure "$1"\n', args=[str(self.seed_file)])
        self.assertEqual(self.sessions(), {"added-at-runtime": {"agent": "codex"}})

    def test_a_seedless_creation_is_an_empty_registry(self):
        self.run_writer("registry_ensure\n")
        self.assertEqual(json.loads(self.file.read_text()), {"version": 1, "sessions": {}})

    def test_the_registry_is_private(self):
        # It carries kickoff prompts and working directories; only this user
        # and root have any business reading them. Both creation paths, and
        # the file an edit republishes.
        self.run_writer("registry_ensure\n")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.run_writer(self.ADD_ONE, args=["one"])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        os.remove(self.file)
        self.run_writer('registry_ensure "$1"\n', args=[str(self.seed_file)])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_a_malformed_seed_is_rejected_not_installed(self):
        # issue #356: a producer that disagrees with the schema (.sessions as
        # a list, not an object — seen live from the native backend) must not
        # be installed verbatim. The reconcile loop reads a session name as
        # an array index against a list and jq errors every tick forever,
        # with nothing pointing at the seed as the cause. A rejected seed
        # yields the same empty, valid registry a missing seed would.
        bad = self.home / "bad-seed.json"
        bad.write_text(json.dumps({"version": 1, "sessions": [{"name": "main"}]}))
        done = self.run_writer('registry_ensure "$1"\n', args=[str(bad)])
        self.assertEqual(json.loads(self.file.read_text()), {"version": 1, "sessions": {}})
        self.assertIn(str(bad), done.stderr)

    def test_a_non_object_top_level_seed_is_rejected(self):
        bad = self.home / "bad-seed.json"
        bad.write_text(json.dumps([{"name": "main", "agent": "claude"}]))
        self.run_writer('registry_ensure "$1"\n', args=[str(bad)])
        self.assertEqual(json.loads(self.file.read_text()), {"version": 1, "sessions": {}})

    def test_a_missing_directory_is_made_and_locked(self):
        # A first boot can find neither the file nor its directory, and that
        # is exactly when creation races.
        elsewhere = self.home / "fresh" / "agent-box" / "sessions.json"
        self.run_writer("registry_ensure\n", file=elsewhere)
        self.assertTrue(elsewhere.exists())
        self.assertTrue(elsewhere.with_suffix(".json.lock").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
