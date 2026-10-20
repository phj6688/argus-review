import httpx
import pytest

from argus.client import ToolCallMismatch
from argus.errors import classify_error
from argus.github import GitHubAuthError, GitHubDenied, GitHubRateLimited


def http_error(status, host="gateway.invalid"):
    request = httpx.Request("GET", f"https://{host}/x")
    return httpx.HTTPStatusError(
        "failure", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.parametrize(
    "error,category",
    [
        (GitHubRateLimited(100), "github_rate_limited"),
        (GitHubAuthError("bad"), "github_auth"),
        (GitHubDenied("bad"), "github_denied"),
        (http_error(500, "api.github.com"), "github_http"),
        (http_error(503), "gateway_overloaded"),
        (http_error(529), "gateway_overloaded"),
        (RuntimeError("Overloaded"), "gateway_overloaded"),
        (http_error(500), "gateway_error"),
        (http_error(403), "gateway_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (ToolCallMismatch("missed"), "tool_call"),
        (ValueError("shape"), "validation"),
        (InterruptedError("restart"), "interrupted"),
        (RuntimeError("broken"), "internal"),
    ],
)
def test_categories(error, category):
    assert classify_error(error) == category
