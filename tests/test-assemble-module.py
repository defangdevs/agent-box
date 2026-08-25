#!/usr/bin/env python3
r"""Unit tests for bin/assemble-module.py's Nix escaping (issue #244).

Why this exists
---------------
`module-generated-up-to-date` cannot catch an escaping bug: it regenerates
`modules/agent-box.nix` with the same assembler the commit used, so the check
and the bug agree on the wrong bytes. Only a test that knows what Nix does with
the escaped text can — hence a decoder for Nix's indented-string syntax here,
and a round-trip over every short string built from the characters that matter.

`nix_unescape_indented` below models the IND_STRING rules of Nix's lexer. It is
a model, so it was checked against the real thing: every corpus string in this
file round-trips identically through `nix-instantiate --eval --json`. Run
`python3 tests/test-assemble-module.py --nix` to redo that (needs nix on PATH);
plain `python3 tests/test-assemble-module.py` uses the model and needs nothing.
"""
import importlib.util
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_assembler():
    path = REPO / "bin" / "assemble-module.py"
    spec = importlib.util.spec_from_file_location("assemble_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


am = _load_assembler()


class NixStringError(Exception):
    """The text is not a well-formed, fully literal Nix indented string."""


def nix_unescape_indented(escaped: str) -> str:
    r"""Decode `''<escaped>''` the way Nix's lexer does, or raise.

    Models the IND_STRING rules: `'''` is a literal `''`, `''$` a literal `$`,
    `''\<c>` the escaped character, a lone `'` itself, and a bare `''` closes
    the string. Anything the escaper must never emit raises instead of being
    quietly accepted — a live `${` antiquotation, an unterminated string, or
    text after the close.

    Single-line input only: Nix also strips the common indentation of a
    multi-line indented string, which this deliberately does not model (the
    assembler's re-indentation is covered by the golden snapshot instead).
    """
    if "\n" in escaped:
        raise ValueError("single-line input only")
    src = "''" + escaped + "''"
    out = []
    i = 2
    while True:
        if i >= len(src):
            raise NixStringError("unterminated string")
        if src.startswith("'''", i):
            out.append("''")
            i += 3
        elif src.startswith("''$", i):
            out.append("$")
            i += 3
        elif src.startswith("''\\", i):
            if i + 3 >= len(src):
                raise NixStringError("dangling ''\\ escape")
            out.append({"n": "\n", "r": "\r", "t": "\t"}.get(src[i + 3], src[i + 3]))
            i += 4
        elif src.startswith("''", i):
            i += 2
            break
        elif src.startswith("${", i):
            raise NixStringError("live antiquotation")
        else:
            out.append(src[i])
            i += 1
    if i != len(src):
        raise NixStringError(f"trailing text after the closing '': {src[i:]!r}")
    return "".join(out)


def corpus() -> list:
    """Every string up to 5 characters over the alphabet that can go wrong.

    `'`, `$`, `{` and `}` are the characters the escaping reasons about, `\\` is
    there because it is literal in an indented string but not in the `''\\<c>`
    escape the escaper emits, and `a` stands in for an ordinary character. 9331
    strings, which covers every apostrophe-run length and adjacency the two
    rules can compose into.
    """
    alphabet = ["'", "$", "{", "}", "\\", "a"]
    out = [""]
    for n in range(1, 6):
        out += ["".join(t) for t in itertools.product(alphabet, repeat=n)]
    return out


class TestNixEscape(unittest.TestCase):
    def test_leaves_ordinary_text_alone(self):
        for text in ("", "no specials", "a}b{c", "$ alone", "back\\slash", "x'y"):
            self.assertEqual(am.nix_escape(text), text)

    def test_known_escapes(self):
        # The two base rules, and the compositions issue #244 is about.
        for text, want in [
            ("a${B}c", "a''${B}c"),
            ("${", "''${"),
            ("it''s", "it'''s"),
            # A lone apostrophe before a `${`: the reported bug. `'` + `''${`
            # would lex as an escaped `''` plus a LIVE antiquotation.
            ("echo \"key '${FOO:-bar}'\"", "echo \"key ''\\'''${FOO:-bar}'\""),
            # Odd-length runs in general, not just a single apostrophe.
            ("'''${X}", "'''''\\'''${X}"),
            # Even-length runs already composed correctly; keep them that way.
            ("''${X}", "'''''${X}"),
            ("''''${X}", "''''''''${X}"),
            # A trailing apostrophe has the host's closing `''` behind it.
            ("trailing'", "trailing''\\'"),
            ("'", "''\\'"),
            ("'''", "'''''\\'"),
            ("''", "'''"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(am.nix_escape(text), want)

    def test_the_243_diagnostic_survives_a_round_trip(self):
        # The literal line that made `nix build` fail with `undefined variable
        # 'event'` and forced the workaround in modules/src/webhook-spawn.sh.
        line = """echo "key '${LOCAL_WEBHOOK_SPAWN_KEY:-event}' is unusable" >&2"""
        self.assertEqual(nix_unescape_indented(am.nix_escape(line)), line)

    def test_round_trips_the_whole_corpus(self):
        for text in corpus():
            with self.subTest(text=text):
                self.assertEqual(nix_unescape_indented(am.nix_escape(text)), text)

    def test_the_old_two_pass_escaping_would_fail_this(self):
        # Guard the guard: the pre-#244 implementation, so a regression in the
        # decoder that made everything pass would show up here.
        def old(text):
            return text.replace("''", "'''").replace("${", "''${")

        broken = [t for t in corpus() if self._decode_or_none(old(t)) != t]
        self.assertTrue(broken, "the decoder accepts the buggy escaping")

    @staticmethod
    def _decode_or_none(escaped):
        try:
            return nix_unescape_indented(escaped)
        except NixStringError:
            return None


class TestIncludeMarkers(unittest.TestCase):
    """The escaping as the assembler actually reaches it, via `resolve()`."""

    def _repo(self, tmp, marker, payload):
        (tmp / "modules" / "src").mkdir(parents=True)
        (tmp / "modules" / "src" / "payload.sh").write_text(payload)
        (tmp / "modules" / "agent-box.nix.in").write_text(
            "{\n  script = ''\n    " + marker + "\n  '';\n}\n"
        )
        return am.resolve(tmp / "modules" / "agent-box.nix.in")

    def test_include_escapes_a_quoted_expansion(self):
        payload = """echo "key '${K}'"\n"""
        with tempfile.TemporaryDirectory() as td:
            out = self._repo(Path(td), "@@include:src/payload.sh@@", payload)
        line = next(ln for ln in out.split("\n") if "echo" in ln)
        self.assertEqual(line, "    " + am.nix_escape(payload.rstrip("\n")))
        self.assertEqual(nix_unescape_indented(line.strip()), payload.strip())

    def test_verbatim_include_is_not_escaped(self):
        payload = """echo "key '${K}'"\n"""
        with tempfile.TemporaryDirectory() as td:
            out = self._repo(Path(td), "@@include-verbatim:src/payload.sh@@", payload)
        self.assertIn("""    echo "key '${K}'\"""", out)


def check_against_real_nix() -> int:
    """Round-trip the corpus through `nix-instantiate` instead of the model."""
    cases = corpus()
    expr = "[\n" + "\n".join("''" + am.nix_escape(c) + "''" for c in cases) + "\n]"
    with tempfile.NamedTemporaryFile("w", suffix=".nix") as f:
        f.write(expr)
        f.flush()
        proc = subprocess.run(
            ["nix-instantiate", "--eval", "--json", "--strict", f.name],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print(f"FAIL: nix could not evaluate the {len(cases)} escaped strings")
        return 1
    bad = [(c, g) for c, g in zip(cases, json.loads(proc.stdout)) if c != g]
    for want, got in bad[:10]:
        print(f"FAIL: {want!r} escaped to {am.nix_escape(want)!r}, nix read {got!r}")
    if bad:
        print(f"{len(bad)}/{len(cases)} strings do not survive real Nix")
        return 1
    print(f"all {len(cases)} escaped strings round-trip through real Nix")
    return 0


if __name__ == "__main__":
    if "--nix" in sys.argv:
        sys.exit(check_against_real_nix())
    unittest.main()
