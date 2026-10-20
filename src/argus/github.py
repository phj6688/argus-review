"""Authenticated, quota-aware GitHub REST access."""

import json
import logging
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

from .repo_policy import env_int, hard_deny_owners, is_denied

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
log = logging.getLogger("argus.github")
_lock = threading.RLock()
_quota: dict = {}
_cache: OrderedDict = OrderedDict()
_refusals = 0
_not_modified = 0
_last_quota_log = 0.0
_was_low = False
_pause_until = 0.0
_graphql_pause_until = 0.0


class GitHubAuthError(RuntimeError):
    pass


class GitHubDenied(RuntimeError):
    pass


class GitHubRateLimited(RuntimeError):
    def __init__(self, reset_at: float):
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limited until {reset_at}")


class GraphQLRateLimited(GitHubRateLimited):
    pass


def _refuse(owner=None):
    global _refusals
    with _lock:
        _refusals += 1
    log.warning("github: refused request to denied owner %s", owner or "unknown")
    raise GitHubDenied(f"GitHub path refused: {owner or 'unverifiable owner'}")


def _checked_path(path: str) -> str:
    # Check before URL construction, which silently removes dot segments.
    while True:
        if ".." in path.split("/") or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
            raise ValueError("path contains traversal, a backslash, or a control character")
        if re.search(r"%(?:2f|5c|00)", path, re.I):
            raise ValueError("path contains an encoded separator or NUL")
        decoded = unquote(path)
        if decoded == path:
            return path
        path = decoded


def quote_file_path(path: str) -> str:
    decoded = _checked_path(path)
    if not path or decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        raise ValueError("file path must be relative to the repository")
    return "/".join(quote(part, safe="") for part in path.split("/"))


def guard_url(url) -> httpx.URL:
    try:
        original_path = urlsplit(str(url)).path
        decoded = _checked_path(original_path)
        root = re.match(r"/repos/[^/]+/[^/]+/", decoded, re.I)
        if root and not posixpath.normpath(decoded).startswith(root[0]):
            raise ValueError("path escapes the repository")
        if "//" in decoded:
            raise ValueError("path contains an empty segment")
    except ValueError:
        _refuse()
    url = httpx.URL(url)
    path = url.path
    contents = re.search(r"(/repos/[^/]+/[^/]+)/contents(?=/|$)", path, re.I)
    route = path[:contents.end(1)] if contents else path
    owner = None
    for match in re.finditer(r"/(repos|orgs|users)/([^/]+)(?=/|$)", route, re.I):
        target = match[2]
        if match[1].lower() == "repos":
            target += "/" + route[match.end():].lstrip("/").split("/", 1)[0]
        if is_denied(target):
            owner = target.casefold()
            break
    encoded_path = url.raw_path.split(b"?", 1)[0]
    obscured = re.search(rb"%(?:2e|2f|5c)", encoded_path, re.I)
    if (
        owner
        or "%" in route
        or obscured
        or "//" in path
        or "\\" in path
        or ":" in route
        or "/repositories/" in route.lower()
        or url.host != "api.github.com"
        or url.scheme != "https"
    ):
        _refuse(owner)
    return url


def refusal_count() -> int:
    with _lock:
        return _refusals


def not_modified_count() -> int:
    with _lock:
        return _not_modified


def quota_snapshot() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _quota.items()}


def pause_until() -> float:
    with _lock:
        core = _quota.get("core", {})
        reset = core.get("reset", 0)
        low = core.get("remaining", 10**9) < env_int("ARGUS_GITHUB_MIN_REMAINING", 500, 0)
        return max(_pause_until, reset if low else 0)


def observe_response(response: httpx.Response, record_quota: bool = True) -> None:
    global _last_quota_log, _was_low, _pause_until, _graphql_pause_until
    guard_url(response.url)
    headers = response.headers
    now = time.time()
    is_graphql = response.url.path == "/graphql"
    resource = "graphql" if is_graphql else headers.get("x-ratelimit-resource", "core")
    values = {}
    for key in ("limit", "remaining", "reset", "used"):
        try:
            values[key] = int(headers[f"x-ratelimit-{key}"])
        except (KeyError, ValueError):
            pass
    with _lock:
        if record_quota and is_graphql and values.get("remaining") == 0:
            _graphql_pause_until = max(_graphql_pause_until, values.get("reset", now + 60))
        if record_quota and values:
            _quota[resource] = {
                **_quota.get(resource, {}),
                **values,
                "resource": resource,
                "read_at": datetime.now(timezone.utc).isoformat(),
            }
        if record_quota and resource == "core" and "remaining" in values:
            low = values["remaining"] < env_int("ARGUS_GITHUB_MIN_REMAINING", 500, 0)
            if now - _last_quota_log >= 600 or low != _was_low:
                log.info(
                    "github quota: remaining=%s/%s resets_in=%ss",
                    values["remaining"],
                    values.get("limit", "?"),
                    max(0, int(values.get("reset", now) - now)),
                )
                _last_quota_log = now
            _was_low = low
    if response.status_code in (403, 429):
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = response.text
        retry = headers.get("retry-after")
        if values.get("remaining") == 0 or retry is not None or "rate limit" in message.lower():
            reset = float(values.get("reset", now + 60)) if values.get("remaining") == 0 else now + 60
            if retry:
                try:
                    reset = now + float(retry)
                except ValueError:
                    try:
                        reset = parsedate_to_datetime(retry).timestamp()
                    except (ValueError, TypeError):
                        reset = now + 60
            if record_quota:
                with _lock:
                    if is_graphql:
                        _graphql_pause_until = max(_graphql_pause_until, reset)
                    else:
                        _pause_until = max(_pause_until, reset)
            raise (GraphQLRateLimited if is_graphql else GitHubRateLimited)(reset)
    if is_graphql and response.status_code == 200:
        body = response.json()
        if any(error.get("type") == "RATE_LIMITED" for error in body.get("errors", [])):
            reset = float(values.get("reset", now + 60))
            if record_quota:
                with _lock:
                    _graphql_pause_until = max(_graphql_pause_until, reset)
            raise GraphQLRateLimited(reset)
    if is_graphql and values.get("remaining") == 0:
        raise GraphQLRateLimited(float(values.get("reset", now + 60)))
    if response.status_code in (401, 403):
        raise GitHubAuthError(
            f"GitHub rejected the token ({response.status_code}); check scopes and expiry"
        )


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubAuthError("GITHUB_TOKEN is not set")
    return token


def get(path: str, params: dict | None = None, *, headers: dict | None = None) -> httpx.Response:
    url = guard_url(path if path.startswith("https://") else f"{GITHUB_API}{path}")
    auth = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        **(headers or {}),
    }
    retried = False
    server_error = None
    # Validate each redirect before following it, including owner-obscuring numeric URLs.
    for _ in range(10):
        while True:
            url = guard_url(url)
            reset = pause_until()
            if url.path != "/rate_limit" and reset > time.time():
                raise GitHubRateLimited(reset)
            try:
                response = httpx.get(
                    str(url), params=params, headers=auth, timeout=_TIMEOUT, follow_redirects=False
                )
                observe_response(response)
            except httpx.TransportError as error:
                if retried:
                    if server_error is not None:
                        return server_error
                    raise
                cause = type(error).__name__
            else:
                # Callers need the final status to decide whether to degrade softly.
                if retried or response.status_code not in (500, 502, 503, 504):
                    break
                server_error = response
                cause = response.status_code
            log.info("github: retrying GET (%s)", cause)
            retried = True
            time.sleep(1)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        try:
            _checked_path(urlsplit(response.headers["location"]).path)
        except ValueError:
            _refuse()
        url = guard_url(response.url.join(response.headers["location"]))
        params = None
    raise GitHubDenied("GitHub redirect limit exceeded")


def get_json_cached(path: str, params: dict | None = None) -> tuple[object, str]:
    global _not_modified
    url = guard_url(path if path.startswith("https://") else f"{GITHUB_API}{path}")
    if params:
        url = url.copy_merge_params(params)
    key = str(url)
    with _lock:
        cached = _cache.get(key)
        if cached:
            _cache.move_to_end(key)
    response = get(path, params, headers={"If-None-Match": cached[0]} if cached else {})
    if response.status_code == 304 and cached:
        with _lock:
            _not_modified += 1
        return cached[1], cached[2]
    response.raise_for_status()
    body, link = response.json(), response.headers.get("link", "")
    if response.headers.get("etag"):
        with _lock:
            _cache[key] = (response.headers["etag"], body, link)
            _cache.move_to_end(key)
            while len(_cache) > 2000:
                _cache.popitem(last=False)
    return body, link


def list_cached(path: str, params: dict | None = None) -> list[dict]:
    records = []
    seen = set()
    original = guard_url(path if path.startswith("https://") else f"{GITHUB_API}{path}")
    original_params = dict(original.params)
    original_params.update(params or {})
    params = original_params
    while True:
        key = tuple(sorted(original.copy_merge_params(params).params.multi_items()))
        if key in seen:
            raise RuntimeError("GitHub pagination loop")
        seen.add(key)
        batch, link = get_json_cached(path, params)
        records.extend(batch)
        next_link = next(
            (
                part
                for part in httpx.Headers({"link": link})["link"].split(",")
                if re.search(r'rel=["\']?next', part)
            ),
            None,
        )
        if not next_link:
            break
        next_url = httpx.URL(next_link.split("<", 1)[1].split(">", 1)[0])
        # GitHub may return a numeric repository path. Keep the validated path
        # and original filters; only the pagination query comes from the link.
        params = {**dict(next_url.params), **original_params}
        if "page" in next_url.params:
            params["page"] = next_url.params["page"]
    return records


def owned_repo_records() -> list[dict]:
    return list_cached(
        "/user/repos", {"affiliation": "owner", "per_page": 100, "sort": "full_name"}
    )


def list_owned_repos(include_archived: bool = False) -> list[str]:
    return [
        r["full_name"]
        for r in owned_repo_records()
        if r.get("full_name") and (include_archived or not r.get("archived"))
    ]


def _query_strings(value):
    if isinstance(value, str):
        yield value.casefold()
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _query_strings(key)
            yield from _query_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _query_strings(item)


def _guard_query_values(query: str, variables: dict):
    text = " ".join(_query_strings([query, variables]))
    for entry in hard_deny_owners():
        # Preserve the conservative owner-only boundary for existing callers.
        if "/" not in entry and entry in text:
            _refuse(entry)

    # Tokenize strings and comments together so their contents cannot masquerade
    # as selections or split one repository's identity across unrelated fields.
    tokens = [
        match[0] for match in re.finditer(
            r'"""[\s\S]*?"""|"[^"\\\r\n]*"|#[^\r\n]*|\$?[A-Za-z_][A-Za-z_0-9]*|[^\s,\ufeff]',
            query,
        ) if not match[0].startswith("#")
    ]
    depth = 0
    # (brace depth inside the selection, login) for organization/user selections,
    # so a nested repository(name:) is resolved against its parent login.
    parents = []
    pending = None
    for index, token in enumerate(tokens):
        if token == "{":
            depth += 1
            if pending:
                parents.append((depth, pending))
                pending = None
            continue
        if token == "}":
            while parents and parents[-1][0] >= depth:
                parents.pop()
            depth -= 1
            continue
        if token not in {"repository", "organization", "user"} or index + 1 >= len(tokens) \
                or tokens[index + 1] != "(":
            continue
        if index and tokens[index - 1] == "query":
            continue
        arguments = {}
        cursor = index + 2
        while cursor < len(tokens) and tokens[cursor] != ")":
            if cursor + 2 >= len(tokens) or tokens[cursor + 1] != ":":
                _refuse()
            key, value = tokens[cursor], tokens[cursor + 2]
            if key in arguments:
                _refuse()
            if value.startswith("$"):
                resolved = variables.get(value[1:])
            elif value.startswith('"'):
                try:
                    resolved = json.loads(value)
                except ValueError:
                    _refuse()
            else:
                resolved = None
            arguments[key] = resolved
            cursor += 3
        if cursor == len(tokens):
            _refuse()
        if token == "repository" and "owner" not in arguments and parents:
            arguments["owner"] = parents[-1][1]
        required = ("owner", "name") if token == "repository" else ("login",)
        if any(not isinstance(arguments.get(key), str) or not arguments[key] for key in required):
            _refuse()
        identity = "/".join(arguments[key] for key in required).casefold()
        if is_denied(identity):
            _refuse(identity)
        # Only a selection that opens right after the arguments belongs to this login.
        if token != "repository" and tokens[cursor + 1:cursor + 2] == ["{"]:
            pending = arguments["login"]


def graphql_read(query: str, variables: dict) -> dict:
    """Read a named query through the same authentication and quota boundary."""
    if "\\" in query:
        _refuse()
    _guard_query_values(query, variables)
    if not re.match(r"^query\b", query.lstrip()) or re.search(r"mutation", query, re.I):
        raise ValueError("Only explicit read queries are allowed")
    url = guard_url(f"{GITHUB_API}/graphql")
    reset = pause_until()
    if reset > time.time():
        raise GitHubRateLimited(reset)
    with _lock:
        reset = _graphql_pause_until
    if reset > time.time():
        raise GraphQLRateLimited(reset)
    response = httpx.post(
        str(url), json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": API_VERSION},
        timeout=_TIMEOUT, follow_redirects=False,
    )
    observe_response(response)
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError("Read query returned errors")
    return body["data"]
