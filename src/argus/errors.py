"""Stable categories for watch attempts."""

import httpx

from .client import CliError, ToolCallMismatch
from .github import GitHubAuthError, GitHubDenied, GitHubRateLimited


class HeadMoved(Exception):
    """The listed head was replaced before review."""


def classify_error(exc: Exception) -> str:
    if isinstance(exc, HeadMoved):
        return "head_moved"
    if isinstance(exc, GitHubRateLimited):
        return "github_rate_limited"
    if isinstance(exc, GitHubAuthError):
        return "github_auth"
    if isinstance(exc, GitHubDenied):
        return "github_denied"
    if (
        isinstance(exc, (TimeoutError, httpx.TimeoutException))
        or "timeout" in type(exc).__name__.lower()
    ):
        return "timeout"
    if isinstance(exc, InterruptedError):
        return "interrupted"
    if isinstance(exc, ToolCallMismatch):
        return "tool_call"
    response = getattr(exc, "response", None)
    try:
        request = getattr(exc, "request", None)
    except RuntimeError:
        request = None
    if request is not None and request.url.host == "api.github.com":
        return "github_http"
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if status in (503, 529) or "overloaded" in str(exc).lower():
        return "gateway_overloaded"
    if status or "api" in type(exc).__name__.lower() or isinstance(exc, CliError):
        return "gateway_error"
    if isinstance(exc, ValueError):
        return "validation"
    return "internal"
