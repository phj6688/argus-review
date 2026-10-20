"""Findings from running the public README from a fresh clone."""

import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from argus import cli, doctor, github, verdict
from argus.api import db, main
from argus.diff import FilePatch, change_stats
from argus.errors import HeadMoved
from argus.redact import REDACTED, redact
from argus.review import ReviewResult, fallback_eligible
from argus.schema import Finding, Severity, Verdict, VerifiedFinding

NO_PROVIDER = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
               "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ARGUS_PROVIDER")


@pytest.fixture
def no_provider(monkeypatch):
    for name in NO_PROVIDER:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    db.init_db(path)
    return path


def _bug() -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(file="a.py", line=1, severity=Severity.major, confidence=0.8,
                        title="t", description="d", proposed_fix="f", rule_id="R1"),
        verdict=Verdict(is_real=True, confidence=0.9, reasoning="r"),
    )


# --- redaction -----------------------------------------------------------------

def test_redact_removes_credentials_in_urls_and_assignments():
    # Built at run time so the file holds no literal that looks like a credential.
    secrets = ("hunter" + "2", "abc" + "123", "abcdefgh" + "12345678", "sk-" + "live-12345678")
    text = (f"failed https://user:{secrets[0]}@host.example/x?token={secrets[1]} "
            f"Authorization: Bearer {secrets[2]} api_key={secrets[3]}")
    out = redact(text)
    for secret in secrets:
        assert secret not in out
    assert REDACTED in out
    assert "host.example" in out


def test_redact_removes_values_of_secret_environment_variables(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zzz-opaque-gateway-value")
    assert "zzz-opaque-gateway-value" not in redact("boom zzz-opaque-gateway-value end")


def test_redact_passes_none_and_plain_text_through():
    assert redact(None) is None
    assert redact("") == ""
    assert redact("RuntimeError: boom") == "RuntimeError: boom"


# --- diff stats ----------------------------------------------------------------

def test_change_stats_prefers_counts_reported_by_github():
    files = [FilePatch("a.py", "modified", "@@ -1 +1 @@\n-x\n+y", additions=5, deletions=2)]
    assert change_stats(files) == (1, 5, 2)


def test_change_stats_counts_hunk_lines_without_reported_counts():
    patch = "@@ -1,2 +1,3 @@\n keep\n-old\n+new\n+more"
    files = [FilePatch("a.py", "modified", patch), FilePatch("b.bin", "added", None)]
    assert change_stats(files) == (2, 2, 1)


# --- database ------------------------------------------------------------------

def test_save_result_stores_header_facts(db_path):
    review_id = db.create_review(db_path, "o/r", 3, "m")
    result = ReviewResult(raw_count=1, bugs=[_bug()], nits=[], dropped=0, branch="feat/x",
                          author="octo", files_changed=4, lines_added=30, lines_removed=7)
    db.save_result(db_path, review_id, result)
    row = db.get_review(db_path, review_id)
    assert (row["branch"], row["author"]) == ("feat/x", "octo")
    assert (row["files_changed"], row["lines_added"], row["lines_removed"]) == (4, 30, 7)


def test_old_database_gains_the_header_columns(tmp_path):
    path = str(tmp_path / "old.db")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for column in ("branch", "author", "files_changed", "lines_added", "lines_removed"):
            try:
                conn.execute(f"ALTER TABLE reviews DROP COLUMN {column}")
            except sqlite3.OperationalError:
                pytest.skip("SQLite without DROP COLUMN")
    db.init_db(path)
    review_id = db.create_review(path, "o/r", 1, "m")
    assert db.get_review(path, review_id)["branch"] is None


def test_set_error_and_fallback_text_are_stored_without_credentials(db_path):
    review_id = db.create_review(db_path, "o/r", 1, "m")
    db.set_fallback(db_path, review_id, "fb", "m", "401 https://u:pw123456@gw/x?key=abcdef")
    db.set_error(db_path, review_id, "Err: Bearer abcdefgh12345678", "Err")
    row = db.get_review(db_path, review_id)
    assert "pw123456" not in row["fallback_reason"] and "abcdef" not in row["fallback_reason"]
    assert "abcdefgh12345678" not in row["error"]


def test_default_db_path_is_outside_the_tracked_data_folder(monkeypatch):
    monkeypatch.delenv("ARGUS_DB_PATH", raising=False)
    monkeypatch.setenv("ARGUS_DB_PATH", "")
    path = Path(db.db_path())
    assert path.parts[-2:] in {(".argus", "argus.db"), ("data", "argus.db")}
    root = Path(__file__).resolve().parents[1]
    assert ".argus/" in (root / ".gitignore").read_text().splitlines()
    if path.parts[-2] == "data":
        assert path.exists()  # the legacy location is used only when it already exists


# --- /health, POST /reviews, GET /reviews --------------------------------------

@pytest.fixture
def client(db_path, monkeypatch):
    from argus.jarvis import live

    monkeypatch.setattr(live, "_pr_head_sha", lambda *a: None)
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_reports_provider_state_without_a_model_call(client, no_provider, monkeypatch):
    monkeypatch.setattr(main, "ping", Mock(side_effect=AssertionError("shallow")))
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provider"] is None
    assert body["provider_configured"] is False


def test_health_with_a_gateway_provider(client, no_provider, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t" * 12)
    body = client.get("/health").json()
    assert body["provider"] == "gateway"
    assert body["provider_configured"] is True


def test_post_review_without_provider_fails_fast_with_the_doctor_message(client, no_provider):
    response = client.post("/reviews", json={"repo": "example/app", "pr": 7})
    assert response.status_code == 503
    assert response.json()["detail"] == doctor.configuration_problem(main.DEFAULT_MODEL)
    assert client.get("/reviews").json()["reviews"] == []


def test_review_diff_full_mode_without_provider_is_refused(client, no_provider):
    response = client.post("/review-diff", json={"diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                                                 "mode": "full"})
    assert response.status_code == 503


def test_post_review_with_a_provider_still_starts(client, monkeypatch):
    monkeypatch.setattr(main, "run_review", lambda *a, **k: ReviewResult(
        raw_count=1, bugs=[_bug()], nits=[], dropped=0, branch="feat/x", author="octo",
        files_changed=2, lines_added=9, lines_removed=1))
    assert client.post("/reviews", json={"repo": "example/app", "pr": 7}).status_code == 201


def test_detail_shows_real_header_and_model_truth(client, db_path):
    review_id = db.create_review(db_path, "o/r", 3, "configured-model")
    db.save_result(db_path, review_id, ReviewResult(
        raw_count=1, bugs=[_bug()], nits=[], dropped=0, branch="feat/x", author="octo",
        files_changed=4, lines_added=30, lines_removed=7))
    db.set_fallback(db_path, review_id, "fallback-model", "configured-model", "overloaded")
    detail = client.get(f"/reviews/{review_id}").json()
    assert (detail["branch"], detail["author"]) == ("feat/x", "octo")
    assert (detail["filesChanged"], detail["added"], detail["removed"]) == (4, 30, 7)
    assert detail["model"] == "configured-model"
    assert detail["model_used"] == "fallback-model"
    assert detail["fallback_from"] == "configured-model"
    assert detail["fallback_reason"] == "overloaded"
    assert detail["error"] is None
    summary = client.get("/reviews").json()["reviews"][0]
    assert summary["model_used"] == "fallback-model"
    assert summary["fallback_from"] == "configured-model"


def test_detail_omits_header_values_the_run_never_recorded(client, db_path):
    review_id = db.create_review(db_path, "o/r", 3, "m")
    detail = client.get(f"/reviews/{review_id}").json()
    for key in ("branch", "author", "filesChanged", "added", "removed"):
        assert key not in detail


def test_errored_review_exposes_sanitised_error_and_no_model_used(client, db_path):
    review_id = db.create_review(db_path, "o/r", 3, "configured-model")
    db.set_error(db_path, review_id, "ConnectError: https://u:pw123456@gw.test/v1?key=abcdef", "x")
    detail = client.get(f"/reviews/{review_id}").json()
    assert detail["status"] == "error"
    assert "ConnectError" in detail["error"]
    assert "pw123456" not in detail["error"] and "abcdef" not in detail["error"]
    assert detail["model_used"] is None
    assert detail["model"] == "configured-model"
    summary = client.get("/reviews").json()["reviews"][0]
    assert summary["status"] == "error" and summary["model_used"] is None


def test_verdict_document_carries_run_facts(db_path):
    review_id = db.create_review(db_path, "o/r", 3, "m")
    db.set_error(db_path, review_id, "RuntimeError: boom", "RuntimeError")
    engine = verdict.document(db.get_review(db_path, review_id), [])["engine"]
    assert engine["error"] == "RuntimeError: boom"
    assert engine["model_used"] is None
    assert {"fallback_from", "fallback_reason"} <= set(engine)


def test_fallback_failure_message_names_both_models(db_path, monkeypatch):
    review_id = db.create_review(db_path, "o/r", 3, "primary")

    def boom():
        raise RuntimeError("primary down")

    def worse():
        raise RuntimeError("fallback down")

    main.run_one_review(db_path, review_id, "o/r", 3, boom, fallback_runner=worse,
                        fallback_model="fb", primary_model="primary")
    error = db.get_review(db_path, review_id)["error"]
    assert "fallback down" in error and "primary down" in error and "fb" in error


# --- doctor --------------------------------------------------------------------

def test_configuration_problem_matches_doctor_output(no_provider):
    problem = doctor.configuration_problem("claude-x")
    assert problem
    failing = [detail for _, ok, detail in doctor.run_checks("claude-x") if not ok]
    assert failing == [problem]


def test_configuration_problem_is_none_with_credentials(no_provider, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t" * 12)
    assert doctor.configuration_problem("claude-x") is None


# --- CLI -----------------------------------------------------------------------

def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["argus", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_cli_doctor_prints_each_failure_once_and_exits_nonzero(no_provider, monkeypatch, capsys):
    code = _run_cli(monkeypatch, "doctor")
    out = capsys.readouterr()
    lines = [line for line in (out.out + out.err).splitlines() if line.strip()]
    assert code == 1
    assert len(lines) == len(set(lines))
    assert any(line.startswith("FAIL") for line in lines)


def test_cli_review_without_github_token_is_one_line_and_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli, "run_review", Mock(side_effect=github.GitHubAuthError("401")))
    code = _run_cli(monkeypatch, "review", "o/r", "1")
    assert isinstance(code, str) and code.startswith("error: GITHUB_TOKEN is not set")
    assert "\n" not in code
    assert "Traceback" not in capsys.readouterr().err


def test_cli_rejects_a_bad_repo_with_one_line(monkeypatch):
    code = _run_cli(monkeypatch, "review", "not a repo", "1")
    assert isinstance(code, str) and code.startswith("error:")


def test_cli_review_prints_the_model_that_ran(monkeypatch, capsys):
    result = ReviewResult(raw_count=0, bugs=[], nits=[], dropped=0)
    monkeypatch.setattr(cli, "run_review", Mock(return_value=result))
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr("sys.argv", ["argus", "review", "o/r", "1"])
    cli.main()
    assert f"model used: {cli.DEFAULT_MODEL}" in capsys.readouterr().out


def test_cli_review_honours_the_fallback_model(monkeypatch, capsys):
    result = ReviewResult(raw_count=0, bugs=[], nits=[], dropped=0)

    def fake(repo, pr, model=None):
        if model == "fallback-x":
            return result
        raise RuntimeError("primary failed")

    monkeypatch.setattr(cli, "run_review", fake)
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback-x")
    monkeypatch.setattr("sys.argv", ["argus", "review", "o/r", "1"])
    cli.main()
    captured = capsys.readouterr()
    assert "model used: fallback-x" in captured.out
    assert "trying fallback fallback-x" in captured.err


def test_cli_review_does_not_fall_back_on_a_github_failure(monkeypatch):
    run = Mock(side_effect=github.GitHubDenied("denied"))
    monkeypatch.setattr(cli, "run_review", run)
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback-x")
    code = _run_cli(monkeypatch, "review", "o/r", "1")
    assert run.call_count == 1 and str(code).startswith("error:")


def test_fallback_eligibility():
    assert fallback_eligible(RuntimeError("x"))
    assert not fallback_eligible(github.GitHubAuthError("x"))
    assert not fallback_eligible(HeadMoved("moved"))


def test_cli_maps_http_errors_to_one_line(monkeypatch):
    request = httpx.Request("GET", "https://api.github.com/repos/o/r/pulls/1")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request))
    monkeypatch.setattr(cli, "run_review", Mock(side_effect=error))
    code = _run_cli(monkeypatch, "review", "o/r", "1")
    assert "404" in code and "\n" not in code


# --- compose, MCP snippet, docs ------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_no_fixed_names_and_configurable_loopback_ports():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "name" not in compose
    for service in compose["services"].values():
        assert "container_name" not in service
        assert "image" not in service
    assert compose["services"]["argus-api"]["ports"] == ["127.0.0.1:${ARGUS_API_PORT:-8350}:8000"]
    assert compose["services"]["argus-web"]["ports"] == ["127.0.0.1:${ARGUS_WEB_PORT:-3350}:3000"]
    env = compose["services"]["argus-api"]["environment"]
    assert env["ARGUS_PUBLIC_API_URL"].startswith("${ARGUS_PUBLIC_API_URL:-http://127.0.0.1:${ARGUS_API_PORT:-8350}")


def test_mcp_snippet_uses_compose_exec_without_a_tty(client):
    config = client.get("/integrations").json()["mcp"]["config"]
    assert '"compose"' in config and '"exec", "-T"' in config
    assert "argus-api" in config
    assert '"exec", "-i"' not in config


def test_docs_do_not_address_containers_by_a_fixed_name():
    for name in ("README.md", "DEPLOY.md", "docs/publishing.md"):
        text = (ROOT / name).read_text()
        assert "docker exec" not in text, name
        assert "docker cp argus-api" not in text, name


# --- review round: redaction of mapping entries, fallback rules, cron lookup ---------

def test_redact_removes_quoted_mapping_entries():
    value = "opaque-" + "credential-value"
    for text in (f'{{"password": "{value}"}}', f"{{'token': '{value}'}}",
                 f'{{"api_key":"{value}", "detail": "x"}}', f'secret = "{value}"'):
        out = redact(text)
        assert value not in out, text
        assert REDACTED in out
    assert "detail" in redact(f'{{"api_key":"{value}", "detail": "x"}}')


def test_redact_keeps_prose_that_only_mentions_a_token():
    text = '{"error": "invalid token supplied"}'
    assert redact(text) == text


def _http_status_error(url: str, status: int = 404) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))


def test_github_http_and_transport_errors_never_trigger_the_fallback():
    assert not fallback_eligible(_http_status_error("https://api.github.com/repos/o/r/pulls/1/files"))
    github_down = httpx.ConnectError("down", request=httpx.Request("GET", "https://api.github.com/x"))
    assert not fallback_eligible(github_down)


def test_provider_http_errors_still_trigger_the_fallback():
    assert fallback_eligible(_http_status_error("https://router.example/v1/messages", 529))
    assert fallback_eligible(httpx.ConnectError("no request attached"))


def test_cli_review_does_not_fall_back_on_a_github_404(monkeypatch):
    run = Mock(side_effect=_http_status_error("https://api.github.com/repos/o/r/pulls/9/files"))
    monkeypatch.setattr(cli, "run_review", run)
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback-x")
    code = _run_cli(monkeypatch, "review", "o/r", "9")
    assert run.call_count == 1 and "404" in code


def test_post_review_is_accepted_when_only_the_fallback_can_run(client, no_provider, monkeypatch):
    monkeypatch.setattr(main, "run_review", lambda *a, **k: ReviewResult(
        raw_count=1, bugs=[_bug()], nits=[], dropped=0))
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-" + "x" * 12)
    assert doctor.configuration_problem(main.DEFAULT_MODEL)  # the primary cannot start
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "openai/gpt-6.1-sol")
    assert client.post("/reviews", json={"repo": "example/app", "pr": 7}).status_code == 201


def test_post_review_is_refused_when_neither_model_can_run(client, no_provider, monkeypatch):
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "openai/gpt-6.1-sol")
    assert client.post("/reviews", json={"repo": "example/app", "pr": 7}).status_code == 503


def test_cron_stops_when_the_given_compose_file_has_no_running_api(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "docker").chmod(0o755)
    log = tmp_path / "log" / "run.log"
    result = subprocess.run(
        ["bash", "-c", "source deploy/cron/common.sh"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "LOG": str(log),
             "ARGUS_COMPOSE_FILE": "/srv/other/compose.yml"},
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 1
    assert "No running argus-api service" in log.read_text()


def test_cron_replay_stops_on_the_same_condition():
    text = (ROOT / "deploy/cron/argus_replay.sh").read_text()
    assert "No running argus-api service" in text and "exit 1" in text


def test_cli_names_the_provider_not_github_for_a_provider_http_error(monkeypatch):
    error = _http_status_error("https://router.example/v1/messages", 529)
    monkeypatch.setattr(cli, "run_review", Mock(side_effect=error))
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    code = _run_cli(monkeypatch, "review", "o/r", "1")
    assert code.startswith("error: router.example answered 529")
    assert "GitHub" not in code
