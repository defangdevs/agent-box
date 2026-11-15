#!/usr/bin/env python3
"""Check NixOS-rendered portal environments and the daemon consuming them."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from unittest import mock
from html.parser import HTMLParser


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.targets.append(dict(attrs).get("href"))


daemon_path = (Path(__file__).resolve().parent / "golden/web/payloads"
               / "agent-box-settings/bin/agent-box-settings")
for index, case in enumerate(json.loads(Path(sys.argv[1]).read_text())):
    lines = case["env"].splitlines()
    issuer_lines = [line for line in lines if line.startswith("AGENT_BOX_PORTAL_ISSUER=")]
    assert issuer_lines == ["AGENT_BOX_PORTAL_ISSUER=https://station.example.com"], issuer_lines
    redirect_lines = [line for line in lines if line.startswith("AGENT_BOX_PORTAL_REDIRECT_URL=")]
    assert len(redirect_lines) == bool(case["target"]), redirect_lines
    assert case["restart"], "the NixOS unit must restart when its environment changes"
    values = {}
    for line in lines:
        if line.startswith("AGENT_BOX_PORTAL_"):
            key, raw = line.split("=", 1)
            words = shlex.split(raw)
            assert len(words) == 1, (key, raw)
            values[key] = words[0]
    if case["target"]:
        assert json.loads(redirect_lines[0].split("=", 1)[1]) == case["target"]
        assert values["AGENT_BOX_PORTAL_REDIRECT_URL"], "invalid explicit URLs must stay nonempty"
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "env"
        env_path.write_text("")
        environment = dict(values, HOME=tmp, AGENT_BOX_SETTINGS_ENV_FILE=str(env_path),
                           AGENT_BOX_TMUX_TMPDIR=str(Path(tmp) / "tmux"))
        with mock.patch.dict(os.environ, environment, clear=True):
            loader = importlib.machinery.SourceFileLoader("redirect_env_" + str(index), str(daemon_path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            daemon = importlib.util.module_from_spec(spec)
            loader.exec_module(daemon)
        links = Links()
        links.feed(daemon.AUTH_REQUIRED_BODY)
        assert links.targets == ([case["expected"]] if case["expected"] else []), links.targets
        assert daemon.PORTAL_ISSUER == "https://station.example.com"
    print("PASS: portal environment and daemon refusal for", repr(case["target"]))
