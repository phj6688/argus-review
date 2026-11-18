"""Keep GitLab URL construction and validation inside the configured API boundary."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, unquote, urlencode

import httpx

from argus.github import _checked_path
from argus.gitlab.config import base_url


class GitLabGuardError(ValueError):
    """A URL or route input is unsafe. Messages never contain input values."""


@dataclass(frozen=True)
class Route:
    name: str
    template: str
    query: dict[str, str] = field(default_factory=dict)
    required: frozenset[str] = frozenset()
    multi: frozenset[str] = frozenset()


_P = "/projects/{project_id}"
_M = _P + "/merge_requests/{iid}"
_PAGING = {"page": "page", "per_page": "per_page"}
_ORDER = {"order_by": "order", "sort": "sort"}
ROUTES: dict[str, Route] = {route.name: route for route in (
    Route("project", _P),
    Route("project_by_path", "/projects/{project_path}"),
    Route("mr_list", _P + "/merge_requests", _PAGING | _ORDER | {"state": "state", "updated_after": "datetime", "updated_before": "datetime", "target_branch": "ref", "scope": "scope"}),
    Route("mr", _M),
    Route("mr_versions", _M + "/versions", _PAGING),
    Route("mr_diffs", _M + "/diffs", _PAGING | {"unidiff": "bool"}),
    Route("mr_raw_diffs", _M + "/raw_diffs"),
    Route("mr_discussions", _M + "/discussions", _PAGING),
    Route("mr_discussion", _M + "/discussions/{discussion_id}"),
    Route("mr_discussion_notes", _M + "/discussions/{discussion_id}/notes"),
    Route("mr_notes", _M + "/notes", _PAGING | _ORDER),
    Route("file_raw", _P + "/repository/files/{file_path}/raw", {"ref": "ref"}, frozenset({"ref"})),
    Route("compare", _P + "/repository/compare", {"from": "ref", "to": "ref", "straight": "bool", "unidiff": "bool"}, frozenset({"from", "to"})),
    Route("merge_base", _P + "/repository/merge_base", {"refs[]": "ref"}, frozenset({"refs[]"}), frozenset({"refs[]"})),
    Route("members_all", _P + "/members/all/{user_id}"),
    Route("user", "/user"),
    Route("version", "/version"),
    Route("group_projects", "/groups/{group}/projects", _PAGING | {"include_subgroups": "bool", "archived": "bool", "simple": "bool", "with_shared": "bool", "order_by": "group_order", "sort": "sort", "min_access_level": "access"}),
)}
_LIMITS = {"page": 1_000_000, "per_page": 100, "int": 10**18 - 1, "access": 10**18 - 1}
_ENUMS = {
    "state": {"opened", "closed", "merged", "locked", "all"},
    "scope": {"created_by_me", "assigned_to_me", "all"},
    "order": {"created_at", "updated_at"},
    "group_order": {"id", "name", "path", "created_at", "updated_at", "last_activity_at"},
    "sort": {"asc", "desc"}, "bool": {"true", "false"},
}
_PATTERNS = {
    "sha": r"[0-9a-f]{40}", "ref": r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}",
    "datetime": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?(Z|[+-]\d\d:\d\d)",
}
_IDS = {"project_id", "iid", "user_id"}
_UNSAFE = re.compile(r"[\\\x00-\x1f\x7f-\x9f]")


def _require(valid: bool) -> None:
    if not valid:
        raise GitLabGuardError("Invalid GitLab URL or route input")


def _base():
    try:
        base = base_url()
    except ValueError:
        raise GitLabGuardError("Invalid GitLab configuration") from None
    if base is None:
        raise GitLabGuardError("GitLab is not configured")
    return base


def _value(kind: str, text: str) -> str:
    if kind in _LIMITS:
        _require(bool(re.fullmatch(r"[0-9]{1,18}", text)) and 1 <= int(text) <= _LIMITS[kind])
        _require(kind != "access" or int(text) in {5, 10, 15, 20, 30, 40, 50})
    elif kind in _ENUMS:
        _require(text in _ENUMS[kind])
    else:
        _require(bool(re.fullmatch(_PATTERNS[kind], text)))
        if kind == "ref":
            _require(".." not in text and not text.endswith((".", "/")) and all(
                segment and not segment.startswith(".") and not segment.endswith(".lock") for segment in text.split("/")))
        if kind == "datetime":
            # The datetime parser normalizes overflowing offset minutes.
            _require(text.endswith("Z") or (int(text[-5:-3]) <= 23 and int(text[-2:]) <= 59))
            try:
                _require(datetime.fromisoformat(text).utcoffset() is not None)
            except ValueError:
                raise GitLabGuardError("Invalid GitLab URL or route input") from None
    return text


def _text(kind: str, value: object) -> str:
    if kind in _LIMITS:
        _require(type(value) is int and 1 <= value <= _LIMITS[kind])
    elif kind == "bool":
        _require(type(value) is bool)
        value = "true" if value else "false"
    elif kind == "datetime" and isinstance(value, datetime):
        _require(value.utcoffset() is not None)
        value = value.isoformat()
    else:
        _require(isinstance(value, str))
    return _value(kind, str(value))


def _part(name: str, value: object) -> str:
    if name in _IDS or (name == "group" and type(value) is int):
        return _text("int", value)
    _require(isinstance(value, str))
    if name == "discussion_id":
        return _value("sha", value)
    if name in {"project_path", "group"}:
        _require(len(value) <= 1024)
        segments = value.split("/")
        _require(1 <= len(segments) <= 22 and all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", segment) for segment in segments))
    else:
        # Bound work before the shared decoder follows nested percent escapes.
        _require(len(value) <= 4096)
        decoded = _checked_path(value)
        _require(not _UNSAFE.search(decoded) and all(
            path and not path.startswith("/") and not re.match(r"[A-Za-z]:", path) and "://" not in path
            and all(segment not in {"", ".", ".."} for segment in path.split("/")) for path in (value, decoded)))
    return quote(value, safe="")


def _query(route: Route, pairs: list[tuple[str, str]]) -> None:
    counts = Counter(key for key, _ in pairs)
    _require(route.required <= counts.keys())
    for key, value in pairs:
        _require(key in route.query and counts[key] == (2 if key in route.multi else 1))
        _value(route.query[key], value)


def build(route: str, /, *, query: Mapping[str, object] | None = None, **parts) -> httpx.URL:
    base = _base()
    try:
        _require(isinstance(route, str) and route in ROUTES)
        spec = ROUTES[route]
        _require(set(parts) == set(re.findall(r"\{(\w+)\}", spec.template)))
        path = spec.template.format(**{name: _part(name, value) for name, value in parts.items()})
        _require(query is None or isinstance(query, Mapping))
        pairs = []
        for key, value in (query or {}).items():
            _require(key in spec.query)
            if key in spec.multi:
                _require(isinstance(value, (list, tuple)) and len(value) == 2)
            else:
                value = [value]
            pairs.extend((key, _text(spec.query[key], item)) for item in value)
        _query(spec, pairs)
        suffix = "?" + urlencode(pairs, quote_via=quote) if pairs else ""
        return check(httpx.URL(base.url + "/api/v4" + path + suffix))
    except GitLabGuardError:
        raise
    except (ValueError, httpx.InvalidURL):
        raise GitLabGuardError("Invalid GitLab URL or route input") from None


def check(url: str | httpx.URL) -> httpx.URL:
    base = _base()
    try:
        _require(isinstance(url, (str, httpx.URL)))
        original = str(url)
        _require(len(original) <= 8192)
        # Reject syntax that URL parsing can erase, including traversal and empty userinfo.
        _require(not _UNSAFE.search(original) and "#" not in original and not re.search(r"://[^/?#]*@", original))
        _require(not re.search(r"(?:^|/)\.{1,2}(?:/|$)", original.split("?", 1)[0]))
        parsed = httpx.URL(url)
        # Configuration stores ASCII hosts; httpx.host decodes internationalized names.
        _require(parsed.scheme == "https" and parsed.raw_host.decode("ascii") == base.host and parsed.port == base.port
                 and not parsed.userinfo and not parsed.fragment)
        raw = parsed.raw_path.split(b"?", 1)[0].decode("ascii")
        segments = raw.split("/")[1:]
        prefix = base.path_prefix.split("/")[1:] + ["api", "v4"]
        _require(segments[:len(prefix)] == prefix and len(segments) > len(prefix))
        for segment in segments:
            _require(len(segment) <= 4096 * 3)
            decoded = unquote(segment, errors="strict")
            _require(bool(segment) and decoded not in {".", ".."} and not _UNSAFE.search(decoded))
        path = "/" + "/".join(segments[len(prefix):])
        for route in ROUTES.values():
            pattern = re.sub(r"\{(\w+)\}", lambda m: "(?P<" + m[1] + ">" + (
                r"[1-9][0-9]{0,17}" if m[1] in _IDS else _PATTERNS["sha"] if m[1] == "discussion_id" else r"[^/]+") + ")", route.template)
            match = re.fullmatch(pattern, path)
            if not match or (route.name == "project_by_path" and "%2F" not in match["project_path"]):
                continue
            for name, encoded in match.groupdict().items():
                value = int(encoded) if name in _IDS else unquote(encoded, errors="strict")
                _require(_part(name, value) == encoded)
            _query(route, parsed.params.multi_items())
            return parsed
        _require("%" not in raw and "?" not in str(parsed))
        return parsed
    except GitLabGuardError:
        raise
    except (ValueError, httpx.InvalidURL):
        raise GitLabGuardError("Invalid GitLab URL or route input") from None
