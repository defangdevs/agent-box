#!/usr/bin/env python3
"""Focused tests for the Buzz -> Agentbox ACP endpoint (issue #300)."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "modules" / "src" / "acp-cli.py"
SPEC = importlib.util.spec_from_file_location("agent_box_acp", SOURCE)
acp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acp)


def write_executable(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)
    return path


class MessageRewriteTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path("/home/test/project")
        self.values = {
            "HARNESS": "claude",
            "MODEL": "haiku",
            "EFFORT": "low",
            "SYSTEM_PROMPT": "profile rules",
        }

    def test_claude_profile_and_remote_cwd_are_composed(self):
        sources = set()
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {
                "cwd": "/Users/local/project",
                "mcpServers": [],
                "_meta": {
                    "systemPrompt": {
                        "append": "work in /Users/local/project"
                    },
                    "kept": {"callback": 7},
                },
            },
        }
        got = acp.rewrite_message(
            message, "claude", self.values, self.cwd, False, {}, sources
        )
        self.assertEqual(got["params"]["cwd"], str(self.cwd))
        self.assertEqual(
            got["params"]["_meta"]["systemPrompt"]["append"],
            "profile rules\n\nwork in /home/test/project",
        )
        self.assertEqual(
            got["params"]["_meta"]["claudeCode"]["options"],
            {"model": "haiku", "effort": "low"},
        )
        self.assertEqual(got["params"]["_meta"]["kept"], {"callback": 7})
        self.assertEqual(sources, {"/Users/local/project"})
        self.assertEqual(message["params"]["cwd"], "/Users/local/project")

    def test_followup_prompt_maps_only_known_source_path(self):
        sources = {"/Users/local/project"}
        message = {
            "method": "session/prompt",
            "params": {
                "prompt": [
                    {"type": "text", "text": "open /Users/local/project/a"},
                    {"type": "image", "data": "unchanged"},
                ]
            },
        }
        got = acp.rewrite_message(
            message, "codex", {}, self.cwd, False, {}, sources
        )
        self.assertEqual(
            got["params"]["prompt"][0]["text"],
            "open /home/test/project/a",
        )
        self.assertEqual(
            acp.replace_path(
                "leave /Users/local/project-two alone",
                "/Users/local/project",
                "/home/test/project",
            ),
            "leave /Users/local/project-two alone",
        )
        self.assertEqual(
            acp.replace_path("keep /usr/bin and https://example.test", "/", "/remote"),
            "keep /usr/bin and https://example.test",
        )

    def test_unmapped_mcp_server_fails_closed(self):
        message = {
            "method": "session/new",
            "params": {"cwd": "/local", "mcpServers": [{"command": "/x"}]},
        }
        with self.assertRaisesRegex(acp.AcpLaunchError, "unmapped remote paths"):
            acp.rewrite_message(
                message, "claude", self.values, self.cwd, False, {}, set()
            )
        got = acp.rewrite_message(
            message, "claude", self.values, self.cwd, True, {}, set()
        )
        self.assertEqual(got["params"]["mcpServers"][0]["command"], "/x")

    def test_mapped_mcp_command_preserves_arguments_and_environment(self):
        message = {
            "method": "session/new",
            "params": {
                "cwd": "/local",
                "mcpServers": [
                    {
                        "name": "buzz-dev",
                        "command": (
                            "/Applications/Buzz.app/Contents/MacOS/"
                            "buzz-dev-mcp"
                        ),
                        "args": ["--literal", "$HOME"],
                        "env": {"BUZZ_RELAY_URL": "wss://relay.example"},
                    }
                ],
            },
        }
        remote = Path("/nix/store/example/bin/buzz-dev-mcp")
        got = acp.rewrite_message(
            message,
            "codex",
            {},
            self.cwd,
            False,
            {"buzz-dev-mcp": remote},
            set(),
        )
        server = got["params"]["mcpServers"][0]
        self.assertEqual(server["command"], str(remote))
        self.assertEqual(server["args"], ["--literal", "$HOME"])
        self.assertEqual(
            server["env"], {"BUZZ_RELAY_URL": "wss://relay.example"}
        )
        self.assertIn(
            "/Applications/Buzz.app",
            message["params"]["mcpServers"][0]["command"],
        )

    def test_malformed_mcp_server_fails_closed(self):
        message = {
            "method": "session/new",
            "params": {"cwd": "/local", "mcpServers": [{"args": []}]},
        }
        with self.assertRaisesRegex(acp.AcpLaunchError, "command must be text"):
            acp.rewrite_message(
                message, "codex", {}, self.cwd, False, {}, set()
            )

    def test_replacement_system_prompt_is_refused(self):
        message = {
            "method": "session/new",
            "params": {
                "cwd": "/local",
                "_meta": {"systemPrompt": {"replace": "discard the profile"}},
            },
        }
        with self.assertRaisesRegex(acp.AcpLaunchError, "append-form"):
            acp.rewrite_message(
                message, "claude", self.values, self.cwd, False, {}, set()
            )

    def test_working_directory_must_stay_inside_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(Path(tmp) / "home")
            Path(os.environ["HOME"]).mkdir()
            try:
                with self.assertRaisesRegex(acp.AcpLaunchError, "outside HOME"):
                    acp.remote_cwd(tmp)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.cwd = self.home / "project"
        self.cwd.mkdir(parents=True)
        self.adapter = write_executable(
            self.root / "adapter",
            "import sys\n"
            "for line in sys.stdin.buffer:\n"
            "    sys.stdout.buffer.write(line)\n"
            "    sys.stdout.buffer.flush()\n",
        )
        self.native = write_executable(self.root / "native", "pass\n")
        self.remote_mcp = write_executable(self.root / "buzz-dev-mcp", "pass\n")
        self.profile = write_executable(
            self.root / "profile",
            "import json\n"
            "print(json.dumps({'harness':'claude','args':['--model','haiku'],"
            "'envKeys':['ROLE'],'warnings':[]}))\n",
        )
        self.envstore = write_executable(
            self.root / "envstore",
            "import json\n"
            "print(json.dumps({'HARNESS':'claude','MODEL':'haiku',"
            "'EFFORT':'low','SYSTEM_PROMPT':'profile rules','ROLE':'remote',"
            "'SECRET':'do-not-print'}))\n",
        )
        self.base_env = {
            **os.environ,
            "HOME": str(self.home),
            "AGENT_BOX_PROFILE_BIN": str(self.profile),
            "AGENT_BOX_ENVSTORE_BIN": str(self.envstore),
            "AGENT_BOX_ACP_ENV_READY": "1",
            "AGENT_BOX_ACP_SELF": str(SOURCE),
        }

    def tearDown(self):
        self.temp.cleanup()

    def command(self, verb, *extra):
        return [
            sys.executable,
            str(SOURCE),
            verb,
            "--profile",
            "pm",
            "--adapter",
            str(self.adapter),
            "--native",
            str(self.native),
            "--cwd",
            str(self.cwd),
            *extra,
        ]

    def test_check_is_secret_free_and_reports_binding(self):
        result = subprocess.run(
            self.command("check", "--local"),
            env=self.base_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(answer["profile"], "pm")
        self.assertEqual(answer["harness"], "claude")
        self.assertEqual(answer["mcpPolicy"], "refuse")
        self.assertEqual(answer["mcpCommandMap"], {})
        self.assertNotIn("do-not-print", result.stdout + result.stderr)

    def test_check_validates_and_reports_mcp_command_map(self):
        result = subprocess.run(
            self.command(
                "check",
                "--local",
                "--map-mcp-command",
                f"buzz-dev-mcp={self.remote_mcp}",
            ),
            env=self.base_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(answer["mcpPolicy"], "mapped")
        self.assertEqual(
            answer["mcpCommandMap"],
            {"buzz-dev-mcp": str(self.remote_mcp.resolve())},
        )

    def test_forced_command_must_match_adapter_identity(self):
        env = {**self.base_env, "SSH_ORIGINAL_COMMAND": "ssh not-an-adapter"}
        result = subprocess.run(
            self.command("check"),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be exactly 'claude-agent-acp'", result.stderr)

    def test_stdio_round_trip_rewrites_remote_context(self):
        new = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {
                "cwd": "/Users/buzz/project",
                "mcpServers": [{
                    "name": "buzz-dev",
                    "command": (
                        "/Applications/Buzz.app/Contents/MacOS/buzz-dev-mcp"
                    ),
                    "args": [],
                    "env": {"BUZZ_AUTH_TAG": "public-test-value"},
                }],
                "_meta": {"systemPrompt": {"append": "at /Users/buzz/project"}},
            },
        }
        prompt = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "prompt": [{"type": "text", "text": "use /Users/buzz/project"}]
            },
        }
        payload = "".join(json.dumps(item) + "\n" for item in (new, prompt))
        result = subprocess.run(
            self.command(
                "serve",
                "--local",
                "--map-mcp-command",
                f"buzz-dev-mcp={self.remote_mcp}",
            ),
            env=self.base_env,
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(lines[0]["params"]["cwd"], str(self.cwd))
        self.assertEqual(
            lines[0]["params"]["_meta"]["systemPrompt"]["append"],
            f"profile rules\n\nat {self.cwd}",
        )
        self.assertEqual(
            lines[1]["params"]["prompt"][0]["text"], f"use {self.cwd}"
        )
        server = lines[0]["params"]["mcpServers"][0]
        self.assertEqual(server["command"], str(self.remote_mcp.resolve()))
        self.assertEqual(server["env"], {"BUZZ_AUTH_TAG": "public-test-value"})

    def test_disconnect_kills_stubborn_process_group(self):
        pid_file = self.root / "descendant.pid"
        self.adapter = write_executable(
            self.root / "stubborn-adapter",
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            f"with open({str(pid_file)!r}, 'w') as handle:\n"
            "    handle.write(str(child.pid))\n"
            "sys.stdin.buffer.read()\n"
            "time.sleep(60)\n",
        )
        env = {**self.base_env, "AGENT_BOX_ACP_EOF_GRACE": "0.05"}
        process = subprocess.Popen(
            self.command("serve", "--local"),
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(250):
                if pid_file.exists() and pid_file.stat().st_size:
                    break
                self.assertIsNone(
                    process.poll(), process.stderr.read() if process.poll() else ""
                )
                time.sleep(0.02)
            else:
                self.fail("adapter did not report its descendant within 5 seconds")
            process.stdin.close()
            returncode = process.wait(timeout=10)
            stderr = process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.assertEqual(returncode, 129, stderr)
        descendant = int(pid_file.read_text())
        for _ in range(50):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail(f"stubborn descendant {descendant} survived disconnect")

    def test_codex_shim_keeps_profile_arguments_literal(self):
        captured = self.root / "argv.json"
        native = write_executable(
            self.root / "codex-native",
            "import json, sys\n"
            f"open({str(captured)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        )
        env = {
            **os.environ,
            "AGENT_BOX_ACP_CODEX_SHIM": "1",
            "AGENT_BOX_ACP_NATIVE": str(native),
            "AGENT_BOX_ACP_NATIVE_ARGS": json.dumps(
                ["-m", "model with spaces", "-c", "x=$HOME;literal"]
            ),
        }
        result = subprocess.run(
            [sys.executable, str(SOURCE), "app-server", "--flag"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(captured.read_text()),
            ["-m", "model with spaces", "-c", "x=$HOME;literal", "app-server", "--flag"],
        )


if __name__ == "__main__":
    unittest.main()
