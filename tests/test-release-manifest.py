#!/usr/bin/env python3
"""Unit tests for scripts/release_manifest.py (issue #632).

The manifest is what promotion promises: these are the identities the
deployment test ran against and the identities the published templates
carry. So the tests are weighted at the REFUSALS - a manifest that
verifies when it should not is the failure that lets an untested artifact
become the public install default, and it looks exactly like a pass.

No network and no Nix: the candidate is a directory, the channel URL comes
from AGENT_BOX_CHANNEL_URL, and `nix-prefetch-url` is a stub on PATH.
Runnable directly: `python3 tests/test-release-manifest.py`.
"""

import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release_manifest as rm  # noqa: E402

REV = "a" * 40
REPO = "defangdevs/agent-box"
CHANNEL_URL = "https://releases.example/nixos/unstable/nixos-25.11pre1/nixexprs.tar.xz"
CHANNEL_HASH = "0000000000000000000000000000000000000000000000000000"


class Rig(unittest.TestCase):
    """A candidate tree, a stubbed prefetch, and no network at all."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.src = self.tmp / "src"
        (self.src / "modules").mkdir(parents=True)
        (self.src / "deploy" / "aws").mkdir(parents=True)
        (self.src / rm.MODULE).write_text("{ ... }: { }\n", encoding="utf-8")
        (self.src / "flake.lock").write_text('{"nodes":{}}\n',
                                             encoding="utf-8")
        for path in rm.TEMPLATES:
            (self.src / path).write_text(f"# {path}\n", encoding="utf-8")

        bindir = self.tmp / "bin"
        bindir.mkdir()
        stub = bindir / "nix-prefetch-url"
        stub.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            print({CHANNEL_HASH!r})
            """), encoding="utf-8")
        stub.chmod(0o755)
        self._patch_env("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        self._patch_env("AGENT_BOX_CHANNEL_URL", CHANNEL_URL)

    def _patch_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value

        def restore():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.addCleanup(restore)

    def build(self, **kw):
        kw.setdefault("source_dir", str(self.src))
        kw.setdefault("check_remote", False)
        return rm.build(REPO, REV, **kw)

    def verify(self, manifest, **kw):
        kw.setdefault("source_dir", str(self.src))
        kw.setdefault("check_remote", False)
        return rm.verify(manifest, **kw)


class Build(Rig):
    def test_records_every_identity(self):
        m = self.build()
        self.assertEqual(m["repo"], REPO)
        self.assertEqual(m["rev"], REV)
        self.assertEqual(m["flake_ref"], f"github:{REPO}/{REV}")
        self.assertTrue(m["module_sha256"].startswith("sha256-"))
        self.assertEqual(len(m["flake_lock_sha256"]), 64)
        self.assertEqual(m["agent_nixpkgs"]["url"], CHANNEL_URL)
        self.assertEqual(m["agent_nixpkgs"]["sha256"], CHANNEL_HASH)
        self.assertEqual(set(m["templates"]), set(rm.TEMPLATES))

    def test_the_module_hash_is_the_form_the_template_wants(self):
        # template.yaml passes AgentBoxSha256 to Nix's fetchurl, which
        # takes SRI. A hex digest there fails on the box, at first boot,
        # where nobody is watching.
        m = self.build()
        self.assertEqual(
            m["module_sha256"],
            rm.sri((self.src / rm.MODULE).read_bytes()))

    def test_a_short_sha_is_refused(self):
        for bad in ("abc1234", "master", "release-2026-09-10", "A" * 40,
                    "g" * 40, ""):
            with self.subTest(bad):
                with self.assertRaises(rm.ManifestError):
                    rm.build(REPO, bad, source_dir=str(self.src),
                             check_remote=False)

    def test_a_local_tree_that_is_not_the_rev_is_refused(self):
        # The reason --no-remote-check is opt-in: a workflow with an
        # uncommitted edit would otherwise record hashes of bytes no
        # launching box will ever be served.
        original = rm.read_remote
        self.addCleanup(setattr, rm, "read_remote", original)
        rm.read_remote = lambda repo, rev, path, timeout=60: b"something else"
        with self.assertRaises(rm.ManifestError) as cm:
            self.build(check_remote=True)
        self.assertIn("not the candidate it claims to be", str(cm.exception))


class Verify(Rig):
    def setUp(self):
        super().setUp()
        self.manifest = self.build()

    def test_a_fresh_manifest_verifies(self):
        self.assertEqual(self.verify(self.manifest), [])

    def test_provenance_is_not_part_of_the_identity(self):
        # Two builds of the same candidate differ in `created`, and that
        # must not read as a different release.
        m = copy.deepcopy(self.manifest)
        for field in rm.PROVENANCE:
            m[field] = "changed"
        self.assertEqual(self.verify(m), [])

    def test_a_changed_module_is_caught(self):
        (self.src / rm.MODULE).write_text("{ ... }: { evil = true; }\n",
                                          encoding="utf-8")
        problems = self.verify(self.manifest)
        self.assertTrue(any("module_sha256" in p for p in problems), problems)

    def test_a_changed_template_is_caught(self):
        # The case a rev alone cannot see: promotion hashes the templates
        # because the published artifact IS those files.
        (self.src / rm.TEMPLATES[0]).write_text("# edited\n",
                                                encoding="utf-8")
        problems = self.verify(self.manifest)
        self.assertTrue(any(rm.TEMPLATES[0] in p for p in problems), problems)

    def test_a_changed_flake_lock_is_caught(self):
        (self.src / "flake.lock").write_text('{"nodes":{"x":1}}\n',
                                             encoding="utf-8")
        problems = self.verify(self.manifest)
        self.assertTrue(any("flake_lock_sha256" in p for p in problems),
                        problems)

    def test_a_different_rev_is_caught(self):
        problems = self.verify(self.manifest, expect_rev="b" * 40)
        self.assertTrue(any(p.startswith("rev:") for p in problems), problems)

    def test_the_candidate_rev_is_accepted(self):
        self.assertEqual(self.verify(self.manifest, expect_rev=REV), [])

    def test_an_unpinned_channel_is_refused(self):
        for pins in ({}, {"url": CHANNEL_URL}, {"sha256": CHANNEL_HASH},
                     {"url": "", "sha256": ""}):
            with self.subTest(pins=pins):
                m = copy.deepcopy(self.manifest)
                m["agent_nixpkgs"] = pins
                problems = self.verify(m)
                self.assertTrue(
                    any("agent_nixpkgs" in p for p in problems), problems)

    def test_the_channel_hash_is_rechecked_against_the_RECORDED_url(self):
        # The whole point of the manifest: verify must not re-resolve the
        # channel. If it did, it would compare the candidate against
        # whatever unstable moved to since, and every older release would
        # fail to verify - which is how a rollback becomes impossible.
        self._patch_env("AGENT_BOX_CHANNEL_URL",
                        "https://releases.example/nixos/unstable/moved-on/"
                        "nixexprs.tar.xz")
        self.assertEqual(self.verify(self.manifest), [])

    def test_a_wrong_channel_hash_is_caught(self):
        m = copy.deepcopy(self.manifest)
        m["agent_nixpkgs"]["sha256"] = "1" * 52
        problems = self.verify(m)
        self.assertTrue(any("agent_nixpkgs.sha256" in p for p in problems),
                        problems)

    def test_a_flake_ref_that_names_another_rev_is_caught(self):
        m = copy.deepcopy(self.manifest)
        m["flake_ref"] = f"github:{REPO}/master"
        problems = self.verify(m)
        self.assertTrue(any("flake_ref" in p for p in problems), problems)

    def test_a_missing_field_is_caught(self):
        for field in ("module_sha256", "templates", "agent_nixpkgs",
                      "flake_lock_sha256", "flake_ref"):
            with self.subTest(field):
                m = copy.deepcopy(self.manifest)
                del m[field]
                self.assertTrue(self.verify(m))

    def test_a_manifest_missing_a_template_is_caught(self):
        m = copy.deepcopy(self.manifest)
        m["templates"].pop(rm.TEMPLATES[1])
        problems = self.verify(m)
        self.assertTrue(any("templates:" in p for p in problems), problems)

    def test_a_manifest_with_no_rev_cannot_be_verified(self):
        m = copy.deepcopy(self.manifest)
        del m["rev"]
        with self.assertRaises(rm.ManifestError):
            self.verify(m)


class Cli(Rig):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "release_manifest.py"),
             *argv], capture_output=True, text=True)

    def test_build_show_verify_round_trip(self):
        out = self.tmp / "release-manifest.json"
        proc = self.run_cli("build", "--repo", REPO, "--rev", REV,
                            "--source-dir", str(self.src),
                            "--no-remote-check", "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(REV, proc.stdout)
        manifest = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(manifest["rev"], REV)

        proc = self.run_cli("show", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent_nixpkgs url", proc.stdout)

        proc = self.run_cli("verify", str(out), "--source-dir", str(self.src),
                            "--no-remote-check", "--rev", REV)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_verify_exits_non_zero_on_a_difference(self):
        out = self.tmp / "release-manifest.json"
        self.run_cli("build", "--repo", REPO, "--rev", REV, "--source-dir",
                     str(self.src), "--no-remote-check", "--out", str(out))
        (self.src / rm.MODULE).write_text("{ ... }: { evil = true; }\n",
                                          encoding="utf-8")
        proc = self.run_cli("verify", str(out), "--source-dir",
                            str(self.src), "--no-remote-check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
