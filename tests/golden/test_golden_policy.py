"""Repository policy, failure categories and coverage decisions."""

from itertools import product
from types import SimpleNamespace

import httpx

from argus import errors, github, repo_policy, verdict
from argus.client import CliError, ToolCallMismatch

EXTRA_DENY = ("extra-owner", "example/private", "acme-org/internal")


def repo_inputs():
    seeds = [
        "example/app", "Example/App", "EXAMPLE/APP", "acme-org/app", "Acme-Org/App",
        "example/app.name", "example/app-name", "example/app_name", "example/APP",
        "example/a", "a/app", "0/app", "123/app", "example/0", "example/-app",
        "example/_app", "example/.hidden", "example/..hidden", "example/...", "example/app.",
        "example/app-", "example/app_", "example/APP.NAME", "example-/app", "ex--ample/app",
        "e" * 39 + "/app", "e" * 40 + "/app", "e" * 100 + "/app",
        "example/" + "r" * 100, "example/" + "r" * 101, "example/" + "r" * 255,
        "", "/", "example", "example/", "/app", "a/b/c", "example/app/", "/example/app",
        "example//app", "//", " example/app", "example/app ", "example /app", "example/ app",
        "example/app name", "\texample/app", "example/app\n", "example/\tapp",
        "./app", "../app", "example/.", "example/..", "-example/app", "_example/app", ".example/app",
        "example/app~", "example/app+", "example/app*", "example/app:tag", "example/app#1",
        "example/app?query", "example/app%2fpart", "example/app\\part", "example/app\x00",
        "éxample/app", "example/café", "例/app", "example/应用", "example/🧪", "example/\u200bapp",
        "gitlab:h#1", "bitbucket:x#1", "a:b/c", "github:example/app",
        "denied-org", "denied-org/app", "Denied-Org/App", "DENIED-ORG/APP",
        "denied-org/app.name", "denied-org-other/app", "example/denied-org", " denied-org/app",
        "denied-org/", "denied-org/a/b", "extra-owner", "extra-owner/app", "Extra-Owner/App",
        "extra-owner/other", "example/private", "Example/Private", "example/private-more",
        "acme-org/internal", "Acme-Org/Internal", "acme-org/internal-more",
    ]
    inputs = list(dict.fromkeys(seeds))
    owners = ("example", "Example", "acme-org", "Acme-Org", "example-2", "example0",
              "EXAMPLE", "denied-org", "Denied-Org", "extra-owner", "Extra-Owner", "acme-team")
    names = ("app", "App", "app.name", "app-name", "app_name", "app-1", "app_2", "release.v1",
             ".hidden", "-app", "_app", "private", "internal")
    for owner, name in product(owners, names):
        candidate = f"{owner}/{name}"
        if candidate not in inputs:
            inputs.append(candidate)
        if len(inputs) == 200:
            break
    assert len(inputs) == len(set(inputs)) == 200
    return inputs


def test_repo_policy_table(golden):
    result = []
    for name in repo_inputs():
        try:
            repo_policy.validate_repo(name, EXTRA_DENY)
        except ValueError as error:
            validated = {"raised": type(error).__name__, "message": str(error)}
        else:
            validated = "ok"
        result.append({
            "input": name, "validate": validated,
            "is_denied": repo_policy.is_denied(name),
            "is_denied_with_extra": repo_policy.is_denied(name, EXTRA_DENY),
        })

    golden.check("policy", "repo-policy-table", result)


class StatusFailure(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"Request returned {status_code}.")
        self.status_code = status_code


class APIError(RuntimeError):
    pass


class GatewayTimeout(RuntimeError):
    pass


def test_classify_error(golden, frozen_now):
    request = httpx.Request("GET", "https://api.github.com/repos/example/app/pulls/42")
    response = httpx.Response(503, request=request)
    response_error = RuntimeError("Response was rejected.")
    response_error.response = SimpleNamespace(status_code=500)
    cases = [
        ("head-moved", errors.HeadMoved("The reviewed head changed.")),
        ("github-rate-limited", github.GitHubRateLimited(frozen_now.timestamp() + 60)),
        ("github-auth", github.GitHubAuthError("Credentials were rejected.")),
        ("github-denied", github.GitHubDenied("Repository access was refused.")),
        ("timeout", TimeoutError("Request timed out.")),
        ("read-timeout", httpx.ReadTimeout("Response timed out.")),
        ("named-timeout", GatewayTimeout("Request expired.")),
        ("interrupted", InterruptedError("The process stopped.")),
        ("tool-call", ToolCallMismatch("The requested call is missing.")),
        ("github-http", httpx.HTTPStatusError("Request returned 503.", request=request, response=response)),
        ("status-503", StatusFailure(503)),
        ("status-529", StatusFailure(529)),
        ("status-500", StatusFailure(500)),
        ("response-status-500", response_error),
        ("overloaded-message", RuntimeError("The gateway is overloaded.")),
        ("api-named", APIError("Request was rejected.")),
        ("cli-error", CliError("Command failed.")),
        ("unbound-request", httpx.RequestError("The request failed before sending.")),
        ("validation", ValueError("Invalid value.")),
        ("runtime", RuntimeError("Unexpected failure.")),
        ("key-error", KeyError("missing")),
    ]
    result = []
    for name, error in cases:
        row = {"case": name, "error": error, "classification": errors.classify_error(error)}
        if isinstance(error, httpx.HTTPStatusError):
            row.update(request_url=str(error.request.url), status_code=error.response.status_code)
        elif getattr(error, "status_code", None) is not None:
            row["status_code"] = error.status_code
        elif getattr(error, "response", None) is not None:
            row["status_code"] = error.response.status_code
        result.append(row)

    golden.check("policy", "classify-error", result)


def test_coverage_level_grid(golden):
    lane_names = ("probe", "ruff", "config", "llm", "judge")
    statuses = ("ran", "skipped", "error")
    result = []
    for combination in product(statuses, repeat=len(lane_names)):
        for counts in ("missing-counts", "complete", "omitted"):
            lanes = [{"lane": name, "status": status}
                     for name, status in zip(lane_names, combination, strict=True)]
            if counts != "missing-counts":
                for lane in lanes:
                    if lane["lane"] == "llm":
                        lane.update(files_reviewed=2, files_omitted=int(counts == "omitted"))
                    else:
                        lane["checks"] = int(counts == "complete" and lane["lane"] != "judge")
            result.append({
                "case": "/".join(combination) + "/" + counts,
                "lanes": lanes, "level": verdict.coverage_level(lanes),
            })
    extras = [
        ("no-lanes", []),
        ("zero-files", [{"lane": "llm", "status": "ran", "files_reviewed": 0, "files_omitted": 0}]),
        ("null-counts", [{"lane": "llm", "status": "ran", "files_reviewed": None, "files_omitted": None},
                         {"lane": "probe", "status": "ran", "checks": None}]),
        ("multiple-reviewers", [{"lane": "llm", "status": "ran", "files_omitted": 0},
                                {"lane": "llm", "status": "ran", "files_omitted": 0}]),
        ("one-reviewer-omits", [{"lane": "llm", "status": "ran", "files_omitted": 0},
                                {"lane": "llm", "status": "ran", "files_omitted": 1}]),
        ("skipped-omissions", [{"lane": "llm", "status": "ran", "files_omitted": 0},
                               {"lane": "llm", "status": "skipped", "files_omitted": 3}]),
        ("one-reviewer-errors", [{"lane": "llm", "status": "ran"},
                                 {"lane": "llm", "status": "error"}]),
    ]
    result.extend({"case": name, "lanes": lanes, "level": verdict.coverage_level(lanes)}
                  for name, lanes in extras)
    assert len(result) == 3 ** 5 * 3 + len(extras)
    assert {row["level"] for row in result} == {"full", "partial", "none"}

    golden.check("policy", "coverage-level-grid", result)
