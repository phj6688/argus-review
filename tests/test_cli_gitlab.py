"""Command line input normalization and policy refusals."""

import hashlib
import json
import subprocess
import sys

import httpx
import pytest

from argus import cli
from argus.api import db
from argus.forge.errors import ForgeError
from argus.review import ReviewResult


def invoke(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["argus", *args])
    try:
        cli.main()
    except SystemExit as error:
        if isinstance(error.code, str):
            return 1, error.code, capsys.readouterr()
        return error.code, "", capsys.readouterr()
    return 0, "", capsys.readouterr()


@pytest.mark.parametrize("command", ["review", "compress"])
@pytest.mark.parametrize("form", ["key", "path_key", "url"])
def test_review_and_compress_accept_configured_forms(gitlab_event, monkeypatch, capsys, command, form):
    event = gitlab_event
    seen = []
    monkeypatch.setattr(cli, "run_review", lambda repo, pr, model: seen.append((repo, pr)) or ReviewResult(0, [], [], 0))
    args = [command, getattr(event, form)] + ([] if form == "url" else ["7"])
    if command == "compress":
        args += ["--budget", "100"]
    code, message, output = invoke(monkeypatch, capsys, *args)
    assert code == 0, message
    assert output.err == ""
    if command == "review":
        assert seen == [(event.key, 7)]
        assert "model used:" in output.out
    else:
        assert json.loads(output.out)["files_total"] == 1
    paths = [r.url.path for r in event.requests]
    offset = int(form in {"url", "path_key"})
    assert paths[offset:offset + 3] == [event.project, event.mr, event.project]
    assert sum("%2F" in str(r.url.raw_path) for r in event.requests) == offset


@pytest.mark.parametrize("command", ["review", "compress"])
def test_first_use_creates_the_database(gitlab_event, monkeypatch, capsys, command):
    import os

    os.remove(gitlab_event.path)
    monkeypatch.setattr(cli, "run_review", lambda repo, pr, model: ReviewResult(0, [], [], 0))
    args = [command, gitlab_event.key, "7"] + (["--budget", "100"] if command == "compress" else [])
    code, message, _ = invoke(monkeypatch, capsys, *args)
    assert (code, message) == (0, "")
    assert os.path.exists(gitlab_event.path)


@pytest.mark.parametrize("command", ["review", "compress"])
@pytest.mark.parametrize("kind", ["denied", "not_allowlisted", "hard_denied", "fork"])
def test_cli_denies_before_diff(gitlab_event, monkeypatch, capsys, command, kind):
    expected = gitlab_event.deny(kind)
    code, message, output = invoke(monkeypatch, capsys, command, gitlab_event.key, "7")
    assert (code, message) == (1, f"error: repository denied: {gitlab_event.key}")
    assert output.out == output.err == ""
    assert [r.url.path for r in gitlab_event.requests] == expected


@pytest.mark.parametrize("change", [
    lambda url: url.replace("gitlab.example.test", "other.test"),
    lambda url: url.replace("/gitlab/", "/gitlab2/"),
    lambda url: url.replace("allowed/sub", "allowed%2Fsub"),
    lambda url: url.replace("https://", "https://user:password@"),
    lambda url: url.replace("https://", "http://"),
])
def test_url_refused_without_request(gitlab_event, monkeypatch, capsys, change):
    code, message, output = invoke(monkeypatch, capsys, "review", change(gitlab_event.url))
    assert code == 1
    assert message.startswith("error: ") and len(message.splitlines()) == 1
    assert "password" not in message
    assert output.out == output.err == ""
    assert gitlab_event.requests == []


@pytest.mark.parametrize("form", ["key", "path_key", "url"])
def test_gitlab_off_is_neutral(gitlab_event, monkeypatch, capsys, form):
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    args = ["review", getattr(gitlab_event, form)] + ([] if form == "url" else ["7"])
    code, message, output = invoke(monkeypatch, capsys, *args)
    assert (code, message) == (1, "error: GitLab is not configured")
    assert output.out == output.err == ""
    assert gitlab_event.requests == []


@pytest.mark.parametrize("command", ["publish-preview", "publish-placement"])
def test_publish_commands_refuse_gitlab_review(gitlab_event, monkeypatch, capsys, command):
    review_id = db.create_review(gitlab_event.path, gitlab_event.key, 7, "test")
    code, message, output = invoke(monkeypatch, capsys, command, str(review_id), "--db", gitlab_event.path)
    assert (code, message) == (1, f"error: {command} supports GitHub repositories only")
    assert output.out == output.err == ""
    assert gitlab_event.requests == []


def test_lookup_error_is_one_safe_line(gitlab_event, monkeypatch, capsys):
    gitlab_event.responses[gitlab_event.mr] = httpx.Response(401, text=gitlab_event.token)
    code, message, output = invoke(monkeypatch, capsys, "review", gitlab_event.key, "7")
    assert (code, message) == (1, "error: gitlab lookup failed: gitlab_auth")
    assert output.out == output.err == ""


@pytest.mark.parametrize("status,message", [
    (500, "error: gitlab lookup failed: gitlab_http"),
    (404, "error: GitLab project path is unavailable; use the new path or the numeric id"),
])
def test_path_lookup_error_is_one_safe_line(gitlab_event, monkeypatch, capsys, status, message):
    event = gitlab_event
    lookup = event.prefix + "/projects/allowed/sub/app"
    event.responses[lookup] = httpx.Response(status, text=event.token)
    code, actual, output = invoke(monkeypatch, capsys, "review", event.path_key, "7")
    assert (code, actual) == (1, message)
    assert event.token not in actual
    assert output.out == output.err == ""
    assert {r.url.path for r in event.requests} == {lookup}


@pytest.mark.parametrize("command", ["review", "compress"])
def test_head_moved_is_one_retryable_line(gitlab_event, command):
    event = gitlab_event
    event.responses[event.mr + "/versions"][0]["head_commit_sha"] = "d" * 40
    responses = {route: event.responses[route]
                 for route in (event.project, event.mr, event.mr + "/versions")}
    program = """
import json
import sys

import httpx

from argus import cli, review
from argus.gitlab import client, projects

responses = json.loads(sys.argv.pop())

def unexpected(*args, **kwargs):
    raise AssertionError("Unexpected network or model request")

httpx.HTTPTransport.handle_request = unexpected
review.complete_tool = unexpected
shared = client.GitLabClient(transport=httpx.MockTransport(
    lambda request: httpx.Response(200, json=responses[request.url.path])))
client.client = projects.shared_client = lambda: shared
cli.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, command, event.key, "7", json.dumps(responses)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "error: the merge request head moved during the read; run the command again",
    ]
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_forge_failure_is_redacted(gitlab_event, monkeypatch, capsys):
    def fail(*args):
        raise ForgeError("failure " + gitlab_event.token)
    monkeypatch.setattr(cli, "run_review", fail)
    code, message, output = invoke(monkeypatch, capsys, "review", gitlab_event.key, "7")
    assert code == 1 and message.startswith("error: failure ")
    assert gitlab_event.token not in message
    assert output.out == output.err == ""


# Recorded from the parent tree before changing the parser call. The top-level help is not
# pinned by bytes: its usage line wraps differently across Python versions once the
# subcommand list is long. test_top_level_help_keeps_original_commands covers it instead.
PARENT_CLI_OUTPUT = [
    (["review", "--help"], "fd5e13c8306b94d811e09cd6f6e1ff16745452ae4a24314c8bcdf1c6c0e71043"),
    (["compress", "--help"], "44fda7ad6139a4081e1812b5fbb86816c82d32b33e300d39e2a155000bc27171"),
    (["review"], "8c5a98be070fbb8b4cb6007448119e4ddfdd3cae03cf89e9c3267bbb2edefee5"),
    (["review", "o/r", "bad"], "915bc143b796a0edab54a84350fa1a30ab052dfec579e09b269b91b4dcf89f80"),
]


@pytest.mark.parametrize("args,expected", PARENT_CLI_OUTPUT)
def test_github_help_and_usage_bytes_unchanged(monkeypatch, capsys, args, expected):
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    code, message, output = invoke(monkeypatch, capsys, *args)
    assert message == ""
    assert hashlib.sha256(json.dumps([output.out, output.err, code]).encode()).hexdigest() == expected


def test_top_level_help_keeps_original_commands(monkeypatch, capsys):
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    code, message, output = invoke(monkeypatch, capsys, "--help")
    assert code == 0 and message == "" and output.err == ""
    text = " ".join(output.out.split())
    for command in ("compress", "doctor", "review", "publish-preview", "publish-placement",
                    "explain", "why-quiet", "automark"):
        assert command in text
