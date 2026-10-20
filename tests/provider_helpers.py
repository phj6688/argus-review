"""Subprocess doubles shared by provider boundary tests."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import cli_providers

PROVIDER_ENV = (
    "ARGUS_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "ARGUS_OPENAI_TOKEN_PARAM",
    "ARGUS_CLI_TIMEOUT", "ARGUS_CLAUDE_BIN", "ARGUS_CODEX_BIN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_API_KEY", "ARGUS_CLI_MAX_CONCURRENCY",
    "ARGUS_CODEX_HOME", "ARGUS_CLAUDE_CONFIG_DIR", "ARGUS_FALLBACK_MODEL",
)


class FakeProcesses:
    def __init__(self):
        self.calls = []
        self.kills = []
        self.stdout = json.dumps({"structured_output": {"reply": "ok"}})
        self.stderr = ""
        self.returncode = 0
        self.last = '{"reply": "ok"}'
        self.on_communicate = None
        self.on_start = None

    def __call__(self, argv, **kwargs):
        files = {}
        for flag in ("--system-prompt-file", "--output-schema"):
            if flag in argv:
                path = Path(argv[argv.index(flag) + 1])
                files[flag] = (path, path.read_text())
        call = SimpleNamespace(
            argv=argv, kwargs=kwargs, files=files, cwd_exists=Path(kwargs["cwd"]).is_dir(),
            cwd_contents=os.listdir(kwargs["cwd"]), communications=[],
            pid=900000 + len(self.calls), returncode=None,
        )
        self.calls.append(call)
        if self.on_start:
            self.on_start(call)

        def communicate(input=None, timeout=None):
            call.communications.append((input, timeout))
            if self.on_communicate:
                self.on_communicate(call)
            if "-o" in argv and self.last is not None:
                Path(argv[argv.index("-o") + 1]).write_text(self.last)
            call.returncode = self.returncode
            return self.stdout, self.stderr

        call.communicate = communicate
        call.poll = lambda: call.returncode
        return call


@pytest.fixture
def fake_popen(monkeypatch):
    fake = FakeProcesses()
    monkeypatch.setattr(cli_providers.subprocess, "Popen", fake)
    monkeypatch.setattr(cli_providers.os, "killpg", lambda pid, sig: fake.kills.append((pid, sig)))
    return fake
