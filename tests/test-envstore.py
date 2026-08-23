#!/usr/bin/env python3
r"""Unit tests for modules/src/lib/envstore.py — the env-store format (#212).

Why this exists
---------------
The env store is read on the session-spawn path and written by the settings
page, and it now holds values that span lines. Nothing else in the test suite
can pin the format down cheaply: the VM tests prove one PEM survives one round
trip through one caller, at 300+ seconds a run, while the property that
actually matters is that EVERY value survives EVERY caller — including the
ones a hand-edited file invents.

So the format's owner gets a unit test, and it asserts three things:

  * round trip — parse(render(v)) is v, for a corpus built from the characters
    that break naive parsers (quote, backslash, newline, `=`, `#`, blanks);
  * no injection — a value can never introduce a SECOND key, however it is
    spelled. This is the whole reason multi-line support has one owner;
  * the legacy readings the six pre-#212 parsers had, so an env file already
    on a deployed box keeps meaning what it meant.

The CLI is exercised too, by composing it the way the generated module does
(library text, then CLI text) and running it — which also proves the two
halves still fit together.
"""
import importlib.util
import itertools
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "modules" / "src" / "lib" / "envstore.py"
CLI = REPO / "modules" / "src" / "envstore-cli.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


es = _load(LIB, "envstore")

PEM = """-----BEGIN PRIVATE KEY-----
MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEA1x
c3+quoted"line+and\\backslash
-----END PRIVATE KEY-----"""


class RoundTrip(unittest.TestCase):
    def assert_round_trips(self, value):
        text = es.render([("K", value)], es.ENV_HEADER)
        self.assertEqual(es.parse(text), [("K", value)], f"for {value!r}: {text!r}")

    def test_plain_values_stay_plain(self):
        # The VM tests grep for exact lines (`^MY_TOKEN=sekret$`), and so do
        # humans. A value that needs no quoting must never acquire any.
        self.assertEqual(es.quote("sekret"), "sekret")
        self.assertEqual(es.quote("ghp_abc-123.XY/z+="), "ghp_abc-123.XY/z+=")
        self.assertEqual(es.quote(""), "")
        self.assertIn("MY_TOKEN=sekret\n", es.render([("MY_TOKEN", "sekret")]))

    def test_pem(self):
        self.assert_round_trips(PEM)

    def test_corpus(self):
        alphabet = ['"', "\\", "\n", "=", "#", " ", "'", "a"]
        for size in (1, 2, 3):
            for combo in itertools.product(alphabet, repeat=size):
                self.assert_round_trips("".join(combo))

    def test_multiline_survives_a_second_round_trip(self):
        once = es.render([("K", PEM)], es.ENV_HEADER)
        twice = es.render(es.parse(once), es.ENV_HEADER)
        self.assertEqual(once, twice)


class NoInjection(unittest.TestCase):
    """A value must never introduce a second key — or a second argument."""

    def setUp(self):
        self.nul_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.nul_dir.cleanup()

    def test_value_holding_an_assignment(self):
        for value in (
            "a\nEVIL=1",
            'a"\nEVIL=1',
            'a\\"\nEVIL=1',
            "a\\\nEVIL=1",
            '"\nEVIL=1',
            "a\n# comment\nEVIL=1\n",
        ):
            text = es.render([("K", value)], es.ENV_HEADER)
            pairs = es.parse(text)
            self.assertEqual([k for k, _ in pairs], ["K"], f"for {value!r}: {text!r}")
            self.assertEqual(pairs[0][1], value)

    def test_nul_is_refused_on_write_and_skipped_on_read(self):
        # NUL frames the argument vectors this store feeds (session-cli's
        # profile decode, webhook-spawn's hook args), and execve cannot carry
        # one anyway — a variable would truncate at it. So a value holding one
        # must never reach the file, and one that somehow did must not be
        # handed on as if it were ordinary text.
        self.assertFalse(es.valid_value("a\0b"))
        with self.assertRaises(es.EnvStoreError):
            es.save(os.path.join(self.nul_dir.name, "env"), [("K", "a\0b")])
        self.assertEqual(es.parse("K=a\0b\nJ=fine\n"), [("J", "fine")])
        self.assertEqual(es.parse('K="a\0b"\nJ=fine\n'), [("J", "fine")])

    def test_key_charset_is_enforced_on_read(self):
        text = "1BAD=x\nBAD-KEY=x\n=x\n BAD KEY=x\nGOOD=y\n"
        self.assertEqual(es.parse(text), [("GOOD", "y")])


class LegacyReadings(unittest.TestCase):
    """What the six pre-#212 parsers did, still done."""

    def test_one_pair_of_quotes_is_stripped(self):
        self.assertEqual(es.parse('K="v"\n'), [("K", "v")])
        self.assertEqual(es.parse("K='v'\n"), [("K", "v")])
        self.assertEqual(es.parse("K=v\n"), [("K", "v")])
        self.assertEqual(es.parse('K=""\n'), [("K", "")])
        self.assertEqual(es.parse("K=\n"), [("K", "")])

    def test_comments_blanks_and_non_assignments(self):
        # `J = w` is not an assignment in this format: the key is `J `.
        self.assertEqual(
            es.parse("# c\n\n   \nnot an assignment\n  K=v\nJ = w\n"),
            [("K", "v")],
        )

    def test_trailing_whitespace_on_an_unquoted_value(self):
        self.assertEqual(es.parse("K=v  \n"), [("K", "v")])
        self.assertEqual(es.parse('K="v  "\n'), [("K", "v  ")])

    def test_crlf(self):
        self.assertEqual(es.parse("K=v\r\nJ=w\r\n"), [("K", "v"), ("J", "w")])
        self.assertEqual(es.parse('K="a\r\nb"\r\n'), [("K", "a\nb")])

    def test_unterminated_quote_costs_one_entry(self):
        # The lines below an unterminated quote must still be parsed: one
        # corrupt hand edit may not swallow the rest of the secrets.
        self.assertEqual(
            es.parse('K="oops\nJ=w\n'), [("K", '"oops'), ("J", "w")]
        )

    def test_duplicate_keys(self):
        pairs = es.parse("K=1\nK=2\n")
        self.assertEqual(pairs, [("K", "1"), ("K", "2")])
        self.assertEqual(es.as_dict(pairs), {"K": "2"})


class FileOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "env")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_modes_and_header(self):
        es.save(self.path, [("K", "v")], es.ENV_HEADER)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700
        )
        with open(self.path, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("# Managed by agent-box"))

    def test_save_leaves_no_temp_file_behind(self):
        es.save(self.path, [("K", "v")], es.ENV_HEADER)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["env"])

    def test_save_tightens_a_pre_existing_loose_directory(self):
        # makedirs applies its mode only when it creates the directory, so a
        # directory an older `mkdir -p` left at 0755 under umask 022 would
        # otherwise keep the secrets file listable by every user on the box.
        os.makedirs(os.path.dirname(self.path), mode=0o755)
        os.chmod(os.path.dirname(self.path), 0o755)
        es.save(self.path, [("K", "v")], es.ENV_HEADER)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700
        )

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(es.load(self.path), [])
        self.assertEqual(es.keys(self.path), [])

    def test_update_replaces_in_place_and_drops(self):
        es.save(self.path, [("A", "1"), ("B", "2"), ("C", "3")], es.ENV_HEADER)
        es.update(self.path, [("B", "9")], es.ENV_HEADER)
        self.assertEqual(
            es.load(self.path), [("A", "1"), ("C", "3"), ("B", "9")]
        )
        es.update(self.path, [], es.ENV_HEADER, drop=["A", "missing"])
        self.assertEqual(es.load(self.path), [("C", "3"), ("B", "9")])

    def test_keys_are_sorted_and_deduplicated(self):
        es.save(self.path, [("B", "1"), ("A", "2"), ("B", "3")], es.ENV_HEADER)
        self.assertEqual(es.keys(self.path), ["A", "B"])


class Cli(unittest.TestCase):
    """Run the CLI as the generated module composes it: library, then CLI."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.program = os.path.join(cls.tmp.name, "agent-box-envstore")
        with open(cls.program, "w", encoding="utf-8") as handle:
            handle.write(LIB.read_text() + "\n" + CLI.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, AGENT_BOX_CONFIG_DIR=self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, self.program, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_set_get_keys_unset(self):
        self.assertEqual(self.run_cli("set", "A=1", "B=2").returncode, 0)
        self.assertEqual(self.run_cli("keys").stdout, "A\nB\n")
        self.assertEqual(self.run_cli("get", "A").stdout, "1")
        self.assertEqual(self.run_cli("get", "NOPE").returncode, 1)
        self.assertEqual(self.run_cli("unset", "A").returncode, 0)
        self.assertEqual(self.run_cli("keys").stdout, "B\n")

    def test_pem_through_stdin_and_back(self):
        self.assertEqual(
            self.run_cli("set", "--stdin", "PEM", stdin=PEM + "\n").returncode, 0
        )
        self.assertEqual(self.run_cli("get", "PEM").stdout, PEM)
        # And the file the session-spawn loader reads holds it as one key.
        env_file = os.path.join(self.dir.name, "env")
        self.assertEqual([k for k, _ in es.load(env_file)], ["PEM"])

    def test_json_is_jq_readable(self):
        self.run_cli("set", "A=1", "B=two lines\nhere")
        self.assertEqual(
            self.run_cli("json").stdout.strip(),
            '{"A": "1", "B": "two lines\\nhere"}',
        )

    def test_profile_target_and_header(self):
        self.assertEqual(self.run_cli("--profile", "triage", "set", "MODEL=sonnet").returncode, 0)
        path = os.path.join(self.dir.name, "profiles", "triage.env")
        with open(path, encoding="utf-8") as handle:
            self.assertIn('profile "triage"', handle.read())
        self.assertEqual(self.run_cli("--profile", "triage", "get", "MODEL").stdout, "sonnet")

    def test_bad_usage_exits_2(self):
        for args in (
            ("set", "notanassignment"),
            ("set", "1BAD=x"),
            ("get",),
            ("get", "A", "B"),
            ("unset",),
            ("--profile", "../escape", "keys"),
            ("--file", "/tmp/x", "--profile", "p", "keys"),
        ):
            self.assertEqual(self.run_cli(*args).returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
