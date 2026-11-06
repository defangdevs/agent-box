#!/usr/bin/env python3
r"""Stale-key bound and refresh serialization for the portal JWKS cache (#634).

Subject is the assembled golden daemon (see test-downloads.py for why).
No network: portal_jwks_fetch is stubbed. Run: python3 tests/test-portal-jwks.py
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")
KEYS = [{"kid": "k1", "kty": "OKP"}]


def load(work):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["HOME"] = work
    os.environ["AGENT_BOX_SETTINGS_USER"] = "alice"
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = os.path.join(work, "env")
    os.environ["AGENT_BOX_SESSIONS_FILE"] = os.path.join(work, "s.json")
    os.environ["AGENT_BOX_SETTINGS_BASE"] = "/alice/settings"
    try:
        loader = importlib.machinery.SourceFileLoader("jwks_under_test",
                                                      str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


class Jwks(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.d = load(self.work)
        self.calls = 0
        self.result = None

        def fetch():
            self.calls += 1
            time.sleep(0.05)
            return self.result
        self.d.portal_jwks_fetch = fetch

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def seed(self, age, attempted_age):
        now = int(time.time())
        self.d.portal_jwks_write_cache(
            {"fetched": now - age, "attempted": now - attempted_age,
             "keys": KEYS})

    def test_expired_keys_rejected_on_failed_fetch(self):
        self.seed(365 * 86400, 365 * 86400)
        self.assertEqual(self.d.portal_jwks_keys("k1"), [])

    def test_expired_keys_rejected_during_cooldown(self):
        self.seed(365 * 86400, 0)
        self.assertEqual(self.d.portal_jwks_keys("k1"), [])
        self.assertEqual(self.calls, 0)

    def test_stale_within_grace_still_served_on_outage(self):
        self.seed(self.d.JWKS_TTL + 60, self.d.JWKS_TTL + 60)
        self.assertEqual(self.d.portal_jwks_keys("k1"), KEYS)

    def test_expiry_survives_repeated_failures(self):
        self.seed(self.d.JWKS_MAX_STALE - 1, 1000)
        self.assertEqual(self.d.portal_jwks_keys("k1"), KEYS)
        cache = self.d.portal_jwks_read_cache()
        cache["fetched"] -= 10
        cache["attempted"] -= 1000
        self.d.portal_jwks_write_cache(cache)
        self.assertEqual(self.d.portal_jwks_keys("k1"), [])

    def test_concurrent_requests_fetch_once(self):
        self.seed(self.d.JWKS_TTL + 60, self.d.JWKS_TTL + 60)
        self.result = {"keys": KEYS}
        out = []
        threads = [threading.Thread(
            target=lambda: out.append(self.d.portal_jwks_keys("k1")))
            for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(out), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
