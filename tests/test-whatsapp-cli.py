#!/usr/bin/env python3
"""Optional WhatsApp runtime upgrades preserve the working installation."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parent.parent / "modules/src/whatsapp-cli.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("whatsapp_cli_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhatsAppInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = load_cli()
        self.cli.HOME = self.root
        self.cli.RUNTIME = self.root / ".local/share/local-whatsapp"
        self.cli.STATE = self.root / ".local/state/local-whatsapp"
        self.cli.READY = self.cli.STATE / "ready"
        self.cli.NODE = self.root / ".nix-profile/bin/node"
        self.cli.NPM = self.root / ".nix-profile/bin/npm"
        self.cli.NODE.parent.mkdir(parents=True)
        self.cli.NODE.touch()
        self.cli.NPM.touch()
        self.files = {
            name: ("updated " + name).encode()
            for name in ("bridge.mjs", "state.mjs", "package.json", "package-lock.json")
        }
        self.cli.FILES = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in self.files.items()
        }
        self.files["router.py"] = b"updated router"
        self.cli.ROUTER_FILE = ("local-router/router.py",
                                hashlib.sha256(self.files["router.py"]).hexdigest())
        self.cli.RUNTIME.mkdir(parents=True)
        (self.cli.RUNTIME / "bridge.mjs").write_text("old bridge")
        (self.cli.RUNTIME / "node_modules").mkdir()
        self.cli.STATE.mkdir(parents=True)
        (self.cli.STATE / "auth-marker").write_text("linked")

    def run_install(self, corrupt=False):
        calls = []

        def urlopen(url, timeout):
            name = url.rsplit("/", 1)[-1]
            self.assertTrue(url.endswith(
                f"/{self.cli.REV}/local-router/{name}" if name == "router.py"
                else f"/{self.cli.REV}/local-whatsapp/{name}"))
            content = b"wrong" if corrupt else self.files[name]
            calls.append(name)
            return io.BytesIO(content)

        def npm(argv, **kwargs):
            self.assertEqual(str(self.cli.NPM), argv[0])
            (kwargs["cwd"] / "node_modules").mkdir()

        with mock.patch.object(self.cli, "urlopen", side_effect=urlopen), \
                mock.patch.object(self.cli.subprocess, "run", side_effect=npm):
            if corrupt:
                with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                    self.cli.install()
            else:
                self.cli.install()
        return calls

    def test_upgrade_replaces_runtime_and_keeps_device_link(self):
        self.assertEqual(set(self.files), set(self.run_install()))
        self.assertEqual(self.files["bridge.mjs"],
                         (self.cli.RUNTIME / "bridge.mjs").read_bytes())
        self.assertEqual(self.files["router.py"],
                         (self.cli.RUNTIME / "router.py").read_bytes())
        self.assertEqual("linked", (self.cli.STATE / "auth-marker").read_text())
        self.assertFalse(self.cli.RUNTIME.with_name("local-whatsapp.previous").exists())
        self.assertEqual([], self.run_install())

    def test_bad_download_leaves_old_runtime_in_place(self):
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertEqual("linked", (self.cli.STATE / "auth-marker").read_text())

    def test_interrupted_upgrade_restores_legacy_backup_before_download(self):
        backup = self.cli.RUNTIME.with_name("local-whatsapp.previous")
        self.cli.RUNTIME.rename(backup)
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertFalse(backup.exists())

    def test_interrupted_upgrade_restores_pending_runtime(self):
        pending = self.cli.RUNTIME.with_name("local-whatsapp.pending")
        self.cli.RUNTIME.rename(pending)
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertFalse(pending.exists())

    def test_profile_is_private_and_defaults_when_cleared(self):
        self.assertIsNone(self.cli.profile())
        self.assertEqual("phone-agent", self.cli.profile("phone-agent"))
        config = self.cli.STATE / "config.json"
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        self.assertEqual("phone-agent", self.cli.profile())
        self.assertIsNone(self.cli.profile("default"))
        with self.assertRaisesRegex(RuntimeError, "profile"):
            self.cli.profile("not a profile")
        with self.assertRaisesRegex(RuntimeError, "profile"):
            self.cli.profile("dotted.name")

    def test_pair_rejects_non_ascii_digits(self):
        for phone in ("+1 415 555 1234\u0663", "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668", "12345abc678"):
            with mock.patch.dict("os.environ", {"LOCAL_WHATSAPP_PHONE": phone}):
                with self.assertRaisesRegex(RuntimeError, "LOCAL_WHATSAPP_PHONE"):
                    self.cli.pair()

    def test_serve_refreshes_runtime_before_execing_bridge(self):
        auth = self.cli.STATE / "auth"
        auth.mkdir()
        (auth / "creds.json").write_text(json.dumps({"me": {"id": "linked"}}))
        calls = []

        def install():
            calls.append("install")

        def execve(path, argv, env):
            calls.append("exec")
            self.assertEqual(str(self.cli.NODE), path)
            self.assertEqual([
                str(self.cli.NODE), str(self.cli.RUNTIME / "bridge.mjs"), "serve",
            ], argv)
            self.assertEqual("session-helper", env["LOCAL_WHATSAPP_SESSION_BIN"])

        with mock.patch.object(self.cli, "install", side_effect=install), \
                mock.patch.object(self.cli.os, "execve", side_effect=execve), \
                mock.patch.dict("os.environ", {"LOCAL_WHATSAPP_SESSION_BIN": "session-helper"}):
            self.cli.serve()
        self.assertEqual(["install", "exec"], calls)

    def test_serve_refuses_unpaired_state_before_install(self):
        with mock.patch.object(self.cli, "install") as install:
            with self.assertRaisesRegex(RuntimeError, "not linked"):
                self.cli.serve()
        install.assert_not_called()

    def test_unlink_stops_bridge_and_removes_all_state_but_keeps_runtime(self):
        auth = self.cli.STATE / "auth"
        auth.mkdir()
        (auth / "creds.json").write_text(json.dumps({"me": {"id": "linked"}}))
        (self.cli.STATE / "ready").touch()
        (self.cli.STATE / "messages.json").write_text("queued message")

        with mock.patch.object(self.cli.time, "sleep"), \
                mock.patch.object(self.cli, "bridge_connected", return_value=False):
            self.cli.unlink()

        self.assertFalse(self.cli.STATE.exists())
        self.assertTrue(self.cli.RUNTIME.exists())

    def test_unlink_refuses_to_delete_live_bridge_state(self):
        auth = self.cli.STATE / "auth"
        auth.mkdir()
        (auth / "creds.json").write_text(json.dumps({"me": {"id": "linked"}}))
        (self.cli.STATE / "ready").touch()
        self.cli.UNLINK_TIMEOUT = 0

        with mock.patch.object(self.cli.time, "sleep"), \
                mock.patch.object(self.cli, "bridge_connected", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                self.cli.unlink()

        self.assertFalse((self.cli.STATE / "ready").exists())
        self.assertTrue((auth / "creds.json").exists())

    def test_unlink_is_idempotent_without_state(self):
        shutil.rmtree(self.cli.STATE)
        with mock.patch.object(self.cli.time, "sleep") as sleep:
            self.cli.unlink()
        sleep.assert_not_called()


# The interpreter by absolute path: the Nix build sandbox has no /usr/bin/env.
STUB = "#!" + sys.executable + r"""
import json, os, sys
log = os.environ["STUB_LOG"]
with open(log, "a") as f:
    stdin = sys.stdin.read() if "-" in sys.argv[1:] else ""
    f.write(json.dumps({"bin": os.path.basename(sys.argv[0]), "argv": sys.argv[1:], "stdin": stdin,
                        "session": os.environ.get("LOCAL_WEBHOOK_SESSION"),
                        "thread": os.environ.get("LOCAL_WEBHOOK_CODEX_THREAD")}) + "\n")
if sys.argv[1:3] == ["setup", "inbox"] and os.environ.get("STUB_SOURCES"):
    # Like the real setup: the template fills in, existing fields win.
    path = os.environ["STUB_SOURCES"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = json.load(open(path)) if os.path.exists(path) else {"sources": {}}
    entry = {"format": "generic", "keyPath": "session", "trust": "owner"}
    entry.update(cfg["sources"].get("inbox", {}))
    cfg["sources"]["inbox"] = entry
    json.dump(cfg, open(path, "w"))
key = " ".join([os.path.basename(sys.argv[0])] + sys.argv[1:3])
answers = json.load(open(os.environ["STUB_ANSWERS"]))
code, out = answers.get(key, [0, ""])
if code == "once75":
    marker = log + ".once"
    if not os.path.exists(marker):
        open(marker, "w").close()
        sys.stderr.write("Session limit reached\n"); sys.exit(75)
    code = 0
sys.stdout.write(out)
sys.exit(code)
"""


class FakeRouter(object):
    """Stands in for local-router's router.py: records what it was shown."""

    class Backend(object):
        name = "fake"

        def __init__(self, model="haiku", timeout=None):
            self.model, self.timeout = model, timeout

    DEFAULT_ORDER = ("fake",)

    def __init__(self, decision, answers=True):
        self.decision, self.calls = decision, []
        self.BACKENDS = {"fake": self.Backend, "codex": self.Backend}
        self.probes, self.answers = 0, answers

    def probe(self, backend):
        self.probes += 1
        return self.answers

    def route(self, message, sessions, current, backend=None):
        if callable(backend) and not hasattr(backend, "complete"):
            backend = backend()
        self.calls.append({"message": message, "sessions": sessions, "current": current,
                           "backend": backend})
        return dict(self.decision)


class WhatsAppRouteTest(unittest.TestCase):
    REQUEST = {"channel": "whatsapp",
               "message": {"id": "M2", "text": "and in Greece?", "ts": 1794765433, "attachments": ""},
               "reply": "node bridge.mjs reply M2 <reply text>", "ack": "node bridge.mjs ack M2",
               "recent": [{"id": "M1", "session": "wa-vat", "reply": "Cyprus: 19%."}]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = load_cli()
        self.cli.HOME = self.root
        self.cli.STATE = self.root / ".local/state/local-whatsapp"
        self.cli.ROUTE_STATE = self.root / ".local/state/agent-box/router/state.json"
        self.cli.ROUTE_BACKEND_CACHE = self.root / ".local/state/agent-box/router/backend.json"
        self.log = self.root / "calls.jsonl"
        self.answers = self.root / "answers.json"
        bins = self.root / "bin"
        bins.mkdir()
        for name in ("agent-box-session", "agent-box-webhook"):
            (bins / name).write_text(STUB)
            (bins / name).chmod(0o700)
        self.cli.SESSION_BIN = str(bins / "agent-box-session")
        self.cli.WEBHOOK_BIN = str(bins / "agent-box-webhook")
        self.env = mock.patch.dict(self.cli.os.environ, {
            "STUB_LOG": str(self.log), "STUB_ANSWERS": str(self.answers), "USER": "agent",
            "STUB_SOURCES": str(self.root / ".local/state/local-webhook/sources.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cli.os.environ.pop("AGENT_BOX_ROUTER_BACKEND", None)
        self.cli.READY = self.cli.STATE / "ready"
        self.cli.os.environ["AGENT_BOX_WHATSAPP_PAIRING"] = ""
        self.cli.os.environ.pop("AGENT_BOX_ROUTER_MODEL", None)
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": True, "selected": False},
                   {"name": "manual", "harness": "claude", "stopped": False, "selected": False}])
        self.cli.write_private(self.cli.ROUTE_STATE, {"current": "wa-vat", "sessions": {"wa-vat": {"messages": [
            {"id": "M1", "role": "user", "text": "VAT rate in Cyprus?", "ts": 1794765000, "topic": "VAT rate Cyprus"}]}}})

    def live(self, sessions, **extra):
        answers = {"agent-box-session whatsapp candidates": [0, json.dumps(sessions)],
                   "agent-box-session whatsapp wake": [0, '{"activated": true}']}
        answers.update(extra)
        self.answers.write_text(json.dumps(answers))

    def calls(self, binary=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if binary is None or r["bin"] == binary]

    def state(self):
        return json.loads(self.cli.ROUTE_STATE.read_text())

    def test_continue_wakes_subscribes_and_emits_an_owner_message(self):
        router = FakeRouter({"action": "CONTINUE", "session": "wa-vat", "links": [],
                             "topic": "VAT rate Greece", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, router)
        self.assertEqual(out, {"session": "wa-vat", "action": "CONTINUE", "links": [], "reason": "llm"})
        shown = router.calls[0]
        # Only the router's own sessions are candidates; the reply the channel
        # sent became the assistant turn.
        self.assertEqual([s["id"] for s in shown["sessions"]], ["wa-vat"])
        self.assertEqual([m["role"] for m in shown["sessions"][0]["messages"]], ["user", "assistant"])
        self.assertEqual(shown["sessions"][0]["messages"][1]["text"], "Cyprus: 19%.")
        self.assertEqual(shown["current"], "wa-vat")
        self.assertEqual(shown["message"], {"text": "and in Greece?", "ts": 1794765433})
        session_calls = [c["argv"][:3] for c in self.calls("agent-box-session")]
        self.assertIn(["whatsapp", "wake", "wa-vat"], session_calls)
        hooks = self.calls("agent-box-webhook")
        self.assertEqual(hooks[0]["argv"], ["setup", "inbox"])
        sub = next(c for c in hooks if c["argv"][0] == "subscribe")
        self.assertEqual(sub["argv"][:4], ["subscribe", "inbox:wa-vat", "--ttl", "8"])
        self.assertEqual(sub["session"], "agent-wa-vat")
        emit = next(c for c in hooks if c["argv"][0] == "emit")
        self.assertEqual(emit["argv"][1:5], ["inbox", "-", "--event", "message"])
        payload = json.loads(emit["stdin"])
        self.assertEqual((payload["session"], payload["text"], payload["id"], payload["channel"]),
                         ("wa-vat", "and in Greece?", "M2", "whatsapp"))
        self.assertIn("node bridge.mjs reply M2 <reply text>", payload["reply"])
        self.assertIn("node bridge.mjs ack M2", payload["reply"])
        msgs = self.state()["sessions"]["wa-vat"]["messages"]
        self.assertEqual([m["id"] for m in msgs if m["role"] == "user"], ["M1", "M2"])
        self.assertEqual(msgs[-1]["topic"], "VAT rate Greece")
        self.assertEqual(self.state()["current"], "wa-vat")

    def test_quote_resolves_only_a_matching_live_session_identity(self):
        for quote, saved_id, live_id, expected in [
            ("M1", "v1", "v1", "wa-vat"),
            ("missing", "v1", "v1", None),
            ("M1", "v1", "v2", None),
            ("M1", None, "v1", None),
            (["bad"], "v1", "v1", None),
        ]:
            with self.subTest(quote=quote, saved_id=saved_id, live_id=live_id):
                self.live([{"name": "wa-vat", "id": live_id, "harness": "claude", "stopped": False}])
                self.cli.write_private(self.cli.ROUTE_STATE, {"current": "wa-vat", "sessions": {
                    "wa-vat": {"id": live_id, "messages": []}}, "decisions": {
                    "M1": {"session": "wa-vat", "id": saved_id}}})
                router = FakeRouter({"action": "CONTINUE", "session": "wa-vat", "links": [], "reason": "llm"})
                request = dict(self.REQUEST, message=dict(self.REQUEST["message"], reply_to_message=quote))
                self.cli.route_request(request, router)
                self.assertEqual(router.calls[0]["message"].get("reply_to"), expected)

    def test_existing_owner_source_is_not_set_up_again(self):
        sources = self.root / ".local/state/local-webhook/sources.json"
        sources.parent.mkdir(parents=True)
        sources.write_text(json.dumps({"sources": {"inbox": {"keyPath": "session", "trust": "owner"}}}))
        self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"}))
        self.assertNotIn(["setup", "inbox"], [c["argv"] for c in self.calls("agent-box-webhook")])

    def test_new_link_starts_a_hook_session_whose_kickoff_is_the_message(self):
        router = FakeRouter({"action": "NEW_LINKS", "session": None, "links": ["wa-vat"],
                             "topic": "VAT in Greece!", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, router)
        self.assertEqual(out["session"], "wa-vat-in-greece")
        add = next(c["argv"] for c in self.calls("agent-box-session") if c["argv"][0] == "add")
        self.assertEqual(add[:4], ["add", "wa-vat-in-greece", "--origin", "webhook"])
        prompt = add[add.index("--prompt") + 1]
        self.assertTrue(prompt.startswith("[OWNER message via whatsapp id=M2"))
        self.assertIn("\nand in Greece?\n", prompt)
        self.assertIn("node bridge.mjs ack M2", prompt)
        self.assertIn("- wa-vat: VAT rate Cyprus", prompt)
        self.assertEqual([c for c in self.calls("agent-box-webhook") if c["argv"][0] == "emit"], [])
        self.assertEqual(self.state()["current"], "wa-vat-in-greece")
        self.assertEqual(self.state()["sessions"]["wa-vat-in-greece"]["messages"][0]["id"], "M2")

    def test_new_session_uses_the_whatsapp_profile_and_a_free_name(self):
        self.cli.profile = lambda value=None: "phone-agent"
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False},
                   {"name": "wa-and-in-greece", "harness": "codex", "stopped": False, "selected": False}])
        self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "NEW", "session": None, "links": [], "topic": "", "reason": "fallback"}))
        add = next(c["argv"] for c in self.calls("agent-box-session") if c["argv"][0] == "add")
        # No topic from the router: the name and topic come from the text.
        self.assertEqual(add[:5], ["add", "wa-and-in-greece-2", "--profile", "phone-agent", "--origin"])
        self.assertEqual(self.state()["sessions"]["wa-and-in-greece-2"]["messages"][0]["topic"],
                         "and in Greece?")

    def test_text_topic_takes_the_first_words(self):
        self.assertEqual(self.cli.text_topic("  Which Node.js\nversion does the T3 spike use? Just tell me"),
                         "Which Node.js version does the T3")
        self.assertEqual(self.cli.text_topic(""), "")
        self.assertEqual(self.cli.new_session_name(self.cli.text_topic("?!"), set()), "wa-chat")

    def test_full_box_hibernates_one_session_then_retries_the_start(self):
        self.live([], **{"agent-box-session add wa-greece": ["once75", ""]})
        self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))
        argv = [c["argv"][0] for c in self.calls("agent-box-session")]
        self.assertEqual(argv, ["whatsapp", "add", "hibernate", "add", "whatsapp"])

    def test_failed_start_records_no_session(self):
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False}],
                  **{"agent-box-session add wa-greece": [3, ""]})
        with self.assertRaises(self.cli.RouteError):
            self.cli.route_request(self.REQUEST, FakeRouter(
                {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))
        state = self.state()
        self.assertEqual(list(state["sessions"]), ["wa-vat"])
        self.assertEqual(state["current"], "wa-vat")
        # The pending decision names a session that does not exist, so the
        # retry decides again instead of replaying.
        retry = FakeRouter({"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"})
        self.assertEqual(self.cli.route_request(self.REQUEST, retry)["reason"], "llm")

    def test_codex_delivery_needs_its_registered_thread(self):
        self.live([{"name": "wa-vat", "harness": "codex", "stopped": False, "selected": False}])
        decision = {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"}
        with self.assertRaisesRegex(RuntimeError, "no registered Codex thread"):
            self.cli.route_request(self.REQUEST, FakeRouter(decision))
        self.cli.STATE.mkdir(parents=True)
        thread = "019a0000-0000-7000-8000-000000000001"
        (self.cli.STATE / "codex-threads.json").write_text(json.dumps({"wa-vat": {"thread": thread}}))
        self.cli.route_request(self.REQUEST, FakeRouter(decision))
        sub = next(c for c in self.calls("agent-box-webhook") if c["argv"][0] == "subscribe")
        self.assertEqual(sub["thread"], thread)

    def test_removed_sessions_are_forgotten(self):
        self.live([{"name": "manual", "harness": "claude", "stopped": False, "selected": False}])
        router = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "x", "reason": "llm"})
        self.cli.route_request(self.REQUEST, router)
        self.assertEqual(router.calls[0]["sessions"], [])
        self.assertIsNone(router.calls[0]["current"])
        self.assertNotIn("wa-vat", self.state()["sessions"])

    def test_backend_policy(self):
        router = FakeRouter({})
        self.cli.os.environ["AGENT_BOX_ROUTER_BACKEND"] = "none"
        self.assertIsNone(self.cli.router_backend(router))
        self.cli.os.environ["AGENT_BOX_ROUTER_BACKEND"] = "codex"
        self.cli.os.environ["AGENT_BOX_ROUTER_MODEL"] = "gpt-6-luna"
        self.assertEqual(self.cli.router_backend(router).model, "gpt-6-luna")
        self.cli.os.environ["AGENT_BOX_ROUTER_BACKEND"] = "nonsense"
        with self.assertRaisesRegex(RuntimeError, "AGENT_BOX_ROUTER_BACKEND"):
            self.cli.router_backend(router)
        # auto probes once, then reuses the cached answer, with the model
        # override applied to the discovered backend too.
        self.cli.os.environ["AGENT_BOX_ROUTER_BACKEND"] = "auto"
        factory = self.cli.router_backend(router)
        first, second = factory(), factory()
        self.assertEqual(router.probes, 1)
        self.assertEqual((first.model, second.model), ("gpt-6-luna", "gpt-6-luna"))
        self.assertEqual(json.loads(self.cli.ROUTE_BACKEND_CACHE.read_text())["name"], "fake")

    def test_discovery_with_no_answer_is_remembered_briefly(self):
        router = FakeRouter({}, answers=False)
        factory = self.cli.router_backend(router)
        self.assertIsNone(factory())
        self.assertIsNone(factory())
        self.assertEqual(router.probes, 1)       # no probe storm on every retry
        self.assertIsNone(json.loads(self.cli.ROUTE_BACKEND_CACHE.read_text())["name"])

    def test_probe_limits_fit_the_bridge_deadline(self):
        self.assertLess(sum(v["timeout"] for v in self.cli.PROBE_LIMITS.values()), 120)

    def test_backend_error_drops_the_cached_choice(self):
        self.cli.write_private(self.cli.ROUTE_BACKEND_CACHE, {"name": "claude", "at": 1e12})
        self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "", "reason": "fallback",
             "error": "claude exited 1"}))
        self.assertFalse(self.cli.ROUTE_BACKEND_CACHE.exists())

    def test_names_that_could_read_as_options_never_reach_a_command(self):
        self.live([{"name": "-rf", "harness": "claude", "stopped": False, "selected": False}])
        self.cli.write_private(self.cli.ROUTE_STATE, {"current": "-rf", "sessions": {"-rf": {"messages": []}}})
        router = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "--help", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, router)
        self.assertEqual(router.calls[0]["sessions"], [])
        self.assertEqual(out["session"], "wa-help")
        with self.assertRaisesRegex(RuntimeError, "invalid session name"):
            self.cli.deliver("--x", "claude", self.REQUEST, "a", "r")

    def test_state_writes_stay_in_the_router_directory(self):
        with self.assertRaisesRegex(RuntimeError, "refusing to write outside"):
            self.cli.write_private(self.cli.ROUTE_STATE.parent / ".." / "elsewhere.json", {})
        with self.assertRaisesRegex(RuntimeError, "refusing to write outside"):
            self.cli.write_private(self.root / "x.json", {})

    def test_replayed_continue_redelivers_without_deciding_again(self):
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": True, "selected": False, "id": "v1"}])
        decision = {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"}
        self.cli.route_request(self.REQUEST, FakeRouter(decision))
        again = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "x", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, again)
        self.assertEqual((out["session"], out["reason"]), ("wa-vat", "replay"))
        self.assertEqual(again.calls, [])
        emits = [c["argv"] for c in self.calls("agent-box-webhook") if c["argv"][0] == "emit"]
        self.assertEqual(len(emits), 2)
        ids = [a[a.index("--delivery-id") + 1] for a in emits]
        self.assertEqual(ids[0], ids[1])
        self.assertRegex(ids[0], r"^route-[0-9a-f]{40}$")

    def started_live(self, sid):
        """Candidates without wa-greece until `add` ran, then with it (id sid)."""
        before = [{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "v1"}]
        after = before + [{"name": "wa-greece", "harness": "claude", "stopped": False, "selected": False, "id": sid}]
        real = self.cli.run_json

        def fake(argv, env=None, timeout=60):
            if argv[1:3] == ["whatsapp", "candidates"]:
                added = any(c["argv"][0] == "add" for c in self.calls("agent-box-session"))
                return after if added else before
            return real(argv, env, timeout)
        return mock.patch.object(self.cli, "run_json", side_effect=fake)

    def test_replayed_new_does_not_start_a_second_session(self):
        router = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"})
        with self.started_live("g1"):
            self.cli.route_request(self.REQUEST, router)
            out = self.cli.route_request(self.REQUEST, FakeRouter({}))
        self.assertEqual((out["session"], out["reason"]), ("wa-greece", "replay"))
        adds = [c for c in self.calls("agent-box-session") if c["argv"][0] == "add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(self.state()["decisions"]["M2"]["id"], "g1")

    def interrupted_start(self, collide):
        """Route a NEW message, but die right after `add` succeeded (before the
        id is read back and the history written)."""
        stub = Path(self.cli.SESSION_BIN)
        stub.write_text(stub.read_text().replace(
            'sys.stdout.write(out)', 'sys.stderr.write("session already exists\\n" if code == 2 else ""); sys.stdout.write(out)'))
        extra = {"agent-box-session add wa-greece": [2, ""]} if collide else {}
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "v1"}], **extra)
        real = self.cli.run_json

        def dies_after_add(argv, env=None, timeout=60):
            if argv[1:3] == ["whatsapp", "candidates"] and any(
                    c["argv"][0] == "add" and c["argv"][1] != "wa-greece" * collide
                    for c in self.calls("agent-box-session")):
                raise KeyboardInterrupt("bridge killed")
            return real(argv, env, timeout)
        with mock.patch.object(self.cli, "run_json", side_effect=dies_after_add):
            with self.assertRaises(KeyboardInterrupt):
                self.cli.route_request(self.REQUEST, FakeRouter(
                    {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))

    def test_replay_restores_a_session_started_just_before_an_interruption(self):
        for collide, started in ((False, "wa-greece"), (True, "wa-greece-2")):
            with self.subTest(collide=collide):
                self.setUp()
                self.interrupted_start(collide)
                pending = self.state()["decisions"]["M2"]
                self.assertEqual(pending["session"], started)
                self.assertRegex(pending["nonce"], r"^[0-9a-f]{32}$")
                add = [c["argv"] for c in self.calls("agent-box-session") if c["argv"][0] == "add"][-1]
                self.assertEqual(add[add.index("--route-start") + 1], pending["nonce"])
                self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "v1"},
                           {"name": started, "harness": "claude", "stopped": False, "selected": False, "id": "g9",
                            "start": pending["nonce"]}])
                again = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "x", "reason": "llm"})
                out = self.cli.route_request(self.REQUEST, again)
                self.assertEqual((out["session"], out["reason"]), (started, "replay"))
                self.assertEqual(again.calls, [])
                adds = [c["argv"][1] for c in self.calls("agent-box-session") if c["argv"][0] == "add"]
                self.assertEqual(adds.count(started), 1, adds)
                state = self.state()
                self.assertEqual(state["decisions"]["M2"]["id"], "g9")
                self.assertEqual(state["current"], started)
                self.assertEqual([m["id"] for m in state["sessions"][started]["messages"]], ["M2"])
                self.assertEqual(state["sessions"][started]["id"], "g9")

    def test_interrupted_start_never_adopts_a_hand_made_session(self):
        # Interrupted after the start (decision saved, no id), then the
        # session is replaced by hand before the retry.
        self.interrupted_start(False)
        self.live([{"name": "wa-greece", "harness": "claude", "stopped": False, "selected": False,
                    "id": "manual", "start": None}])
        again = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, again)
        self.assertEqual(out["reason"], "llm")              # decided again
        self.assertEqual(out["session"], "wa-greece-2")     # the hand-made one is left alone
        self.assertNotIn("wa-greece", self.state()["sessions"])

    def test_interrupted_before_add_never_adopts_a_hand_made_session(self):
        # Decision saved, then the call died BEFORE `add`; a session of that
        # name appears from elsewhere.
        real_start = self.cli.start_session

        def dies_before_add(name, prompt, before_attempt=None, route_start=None):
            before_attempt(name)
            raise KeyboardInterrupt("bridge killed")
        with mock.patch.object(self.cli, "start_session", side_effect=dies_before_add):
            with self.assertRaises(KeyboardInterrupt):
                self.cli.route_request(self.REQUEST, FakeRouter(
                    {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))
        self.assertTrue(real_start)
        self.live([{"name": "wa-greece", "harness": "codex", "stopped": False, "selected": False, "id": "x"}])
        out = self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))
        self.assertEqual((out["session"], out["reason"]), ("wa-greece-2", "llm"))

    def test_replay_never_goes_to_a_replacement_session(self):
        # CONTINUE recorded for wa-vat (id v1), then wa-vat is re-created (id v2).
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "v1"}])
        self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"}))
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "v2"}])
        again = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "fresh", "reason": "llm"})
        out = self.cli.route_request(self.REQUEST, again)
        self.assertEqual(out["reason"], "llm")          # decided again, not replayed
        self.assertEqual(again.calls[0]["sessions"], [])  # the replacement is not a candidate either
        self.assertEqual(out["session"], "wa-fresh")
        # NEW recorded for wa-greece (g1); a replacement wa-greece (g2) is not "already started".
        with self.started_live("g1"):
            self.cli.route_request(dict(self.REQUEST, message=dict(self.REQUEST["message"], id="M5")),
                                   FakeRouter({"action": "NEW", "session": None, "links": [],
                                               "topic": "Greece", "reason": "llm"}))
        self.live([{"name": "wa-greece", "harness": "claude", "stopped": False, "selected": False, "id": "g2"}])
        out = self.cli.route_request(dict(self.REQUEST, message=dict(self.REQUEST["message"], id="M5")),
                                     FakeRouter({"action": "NEW", "session": None, "links": [],
                                                 "topic": "other", "reason": "llm"}))
        self.assertEqual((out["session"], out["reason"]), ("wa-other", "llm"))

    def test_a_replaced_session_is_forgotten(self):
        self.cli.write_private(self.cli.ROUTE_STATE, {"current": "wa-vat", "sessions": {
            "wa-vat": {"id": "old", "messages": [{"id": "M1", "role": "user", "text": "x", "ts": 1}]}}})
        self.live([{"name": "wa-vat", "harness": "claude", "stopped": False, "selected": False, "id": "new"}])
        router = FakeRouter({"action": "NEW", "session": None, "links": [], "topic": "y", "reason": "llm"})
        self.cli.route_request(self.REQUEST, router)
        self.assertEqual(router.calls[0]["sessions"], [])

    def test_a_name_taken_by_another_session_kind_moves_on(self):
        self.live([], **{"agent-box-session add wa-greece": [2, ""]})
        answers = json.loads(self.answers.read_text())
        answers["agent-box-session add wa-greece"] = [2, ""]
        self.answers.write_text(json.dumps(answers))
        # The stub prints nothing on stderr; make it say "already exists".
        stub = Path(self.cli.SESSION_BIN)
        stub.write_text(stub.read_text().replace(
            'sys.stdout.write(out)', 'sys.stderr.write("session already exists\\n" if code == 2 else ""); sys.stdout.write(out)'))
        out = self.cli.route_request(self.REQUEST, FakeRouter(
            {"action": "NEW", "session": None, "links": [], "topic": "Greece", "reason": "llm"}))
        self.assertEqual(out["session"], "wa-greece-2")

    def test_an_incompatible_inbox_source_fails_closed(self):
        sources = self.root / ".local/state/local-webhook/sources.json"
        sources.parent.mkdir(parents=True)
        sources.write_text(json.dumps({"sources": {"inbox": {"keyPath": "recipient", "trust": "owner"}}}))
        with self.assertRaisesRegex(RuntimeError, 'keyPath "session"'):
            self.cli.route_request(self.REQUEST, FakeRouter(
                {"action": "CONTINUE", "session": "wa-vat", "links": [], "topic": "t", "reason": "llm"}))
        self.assertEqual([c for c in self.calls("agent-box-webhook") if c["argv"][0] == "emit"], [])

    def test_unlink_forgets_the_routing_history(self):
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        self.cli.READY = self.cli.STATE / "ready"
        with mock.patch.object(self.cli, "bridge_connected", return_value=False), \
                mock.patch.object(self.cli, "UNLINK_SETTLE", 0):
            self.cli.unlink()
        self.assertFalse(self.cli.ROUTE_STATE.parent.exists())
        self.assertFalse(self.cli.STATE.exists())

    def test_unlink_waits_for_a_running_route_and_a_late_route_writes_nothing(self):
        import fcntl
        import threading
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        self.cli.READY = self.cli.STATE / "ready"
        self.cli.READY.touch()
        lock = open(self.cli.ROUTE_STATE.with_name("lock"), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)          # a route call in progress
        done = threading.Event()

        def run_unlink():
            with mock.patch.object(self.cli, "bridge_connected", return_value=False), \
                    mock.patch.object(self.cli, "UNLINK_SETTLE", 0):
                self.cli.unlink()
            done.set()
        worker = threading.Thread(target=run_unlink)
        worker.start()
        self.assertFalse(done.wait(1.0), "unlink must wait for the running route")
        self.assertTrue(self.cli.ROUTE_STATE.parent.exists())
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        self.assertTrue(done.wait(5))
        worker.join()
        self.assertFalse(self.cli.ROUTE_STATE.parent.exists())
        # A route that only now gets to run finds WhatsApp unlinked.
        with mock.patch.object(self.cli.sys, "stdin", io.StringIO(json.dumps(self.REQUEST))):
            with self.assertRaisesRegex(RuntimeError, "unlinked"):
                self.cli.route()
        self.assertFalse(self.cli.ROUTE_STATE.exists())

    def test_a_route_from_before_unlink_cannot_write_into_a_new_pairing(self):
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        self.cli.READY = self.cli.STATE / "ready"
        self.cli.READY.touch()
        # A route opened the lock file, then paused before taking it.
        paused = open(self.cli.ROUTE_STATE.with_name("lock"), "a")
        self.addCleanup(paused.close)
        with mock.patch.object(self.cli, "bridge_connected", return_value=False), \
                mock.patch.object(self.cli, "UNLINK_SETTLE", 0):
            self.cli.unlink()
        # Pair again: a new ready marker and fresh router state.
        self.cli.STATE.mkdir(parents=True)
        self.cli.READY.touch()
        self.cli.ROUTE_STATE.parent.mkdir(parents=True)
        self.cli.ROUTE_STATE.with_name("lock").touch()
        with self.assertRaisesRegex(RuntimeError, "unlinked while this route waited"):
            self.cli.take_route_lock(paused)
        self.assertFalse(self.cli.ROUTE_STATE.exists())
        # A route that opens the CURRENT lock file proceeds.
        with open(self.cli.ROUTE_STATE.with_name("lock"), "a") as fresh:
            self.cli.take_route_lock(fresh)

    def test_a_route_from_an_earlier_pairing_refuses_even_if_it_opens_the_new_lock(self):
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(self.cli, "paired", return_value=True):
            self.cli.activate()
        old_token = self.cli.pairing_token()
        self.assertRegex(old_token, r"^[0-9a-f]{32}$")
        # The old bridge's route call pauses before it even opens the lock.
        with mock.patch.object(self.cli, "bridge_connected", return_value=False), \
                mock.patch.object(self.cli, "UNLINK_SETTLE", 0):
            self.cli.unlink()
        with mock.patch.object(self.cli, "paired", return_value=True):
            self.cli.activate()                     # pair again: a new token
        self.assertNotEqual(self.cli.pairing_token(), old_token)
        self.cli.os.environ["AGENT_BOX_WHATSAPP_PAIRING"] = old_token
        with mock.patch.object(self.cli.sys, "stdin", io.StringIO(json.dumps(self.REQUEST))):
            with self.assertRaisesRegex(RuntimeError, "earlier WhatsApp pairing"):
                self.cli.route()
        self.assertFalse(self.cli.ROUTE_STATE.exists())
        # The new bridge's calls carry the new token and proceed to routing.
        self.cli.os.environ["AGENT_BOX_WHATSAPP_PAIRING"] = self.cli.pairing_token()
        with open(self.cli.ROUTE_STATE.with_name("lock"), "a") as lock:
            self.cli.take_route_lock(lock)

    def test_only_a_well_formed_token_counts(self):
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        for raw, want in (("ab" * 16, "ab" * 16), ("x=1\nPATH=/evil", ""), ("AB" * 16, ""), ("", "")):
            self.cli.READY.write_text(raw)
            self.assertEqual(self.cli.pairing_token(), want, raw)

    def test_activate_keeps_an_existing_token(self):
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(self.cli, "paired", return_value=True):
            self.cli.activate()
            first = self.cli.pairing_token()
            self.cli.activate()
        self.assertEqual(self.cli.pairing_token(), first)
        self.assertEqual(self.cli.READY.stat().st_mode & 0o777, 0o600)

    def test_unlink_gives_up_if_a_route_never_finishes(self):
        import fcntl
        self.cli.STATE.mkdir(parents=True, exist_ok=True)
        self.cli.READY = self.cli.STATE / "ready"
        lock = open(self.cli.ROUTE_STATE.with_name("lock"), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(lock.close)
        with mock.patch.object(self.cli, "bridge_connected", return_value=False), \
                mock.patch.object(self.cli, "UNLINK_SETTLE", 0), \
                mock.patch.object(self.cli, "ROUTE_UNLINK_WAIT", 0.3):
            with self.assertRaisesRegex(RuntimeError, "still running"):
                self.cli.unlink()
        self.assertTrue(self.cli.STATE.exists())   # nothing half-removed

    def test_bad_request_is_refused(self):
        for bad in ({}, {"message": {"id": "../x"}}, {"message": "hi"}):
            with self.assertRaises(RuntimeError):
                self.cli.route_request(bad, FakeRouter({}))


if __name__ == "__main__":
    unittest.main()
