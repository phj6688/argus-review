"""Keep repository identities and policy entries independent of provider I/O."""

import ipaddress
import re
from dataclasses import dataclass

from .types import RepoRef

GITHUB_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
GITHUB_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
GITLAB_MAX_DEPTH = 20


def canonical_host(host: str, port: int | None = None, default_port: int = 443) -> str:
    """Normalize a host without validation, always bracketing IPv6 literals."""
    host = host.strip().lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None and port != default_port else host


def _canonical_authority(text: str) -> str:
    match = re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([1-9][0-9]{0,4}))?", text)
    if not match:
        raise ValueError("invalid gitlab host")
    host, port_text = match.groups()
    port = int(port_text) if port_text else None
    if port is not None and port > 65535:
        raise ValueError("invalid gitlab port")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError("invalid gitlab host") from None
    elif len(host) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.lower().split(".")
    ):
        raise ValueError("invalid gitlab host")
    return canonical_host(host, port)


def gitlab_key(host: str, project_id: int) -> str:
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("invalid gitlab project id")
    if not isinstance(host, str) or _canonical_authority(host) != host:
        raise ValueError("gitlab host must be canonical")
    return f"gitlab:{host}#{project_id}"


def canonicalize_key(text: str) -> str:
    if ":" not in text:
        return text
    if text[:7].casefold() != "gitlab:":
        raise ValueError("unknown forge")
    parts = text[7:].split("#")
    if len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,17}", parts[1]):
        raise ValueError("invalid gitlab project id")
    return f"gitlab:{_canonical_authority(parts[0])}#{parts[1]}"


def is_gitlab_key(key: str) -> bool:
    return key.startswith("gitlab:")


def is_github_key(key: str) -> bool:
    return ":" not in key


def parse_repo_key(key: str) -> RepoRef:
    if is_gitlab_key(key):
        if canonicalize_key(key) != key:
            raise ValueError("gitlab key must be canonical")
        host, project_id = key[7:].split("#")
        return RepoRef("gitlab", host, None, int(project_id))
    if not is_github_key(key):
        raise ValueError("unknown forge")
    parts = key.split("/")
    if (len(parts) != 2 or not GITHUB_OWNER.fullmatch(parts[0])
            or not GITHUB_NAME.fullmatch(parts[1]) or parts[1] in {".", ".."}):
        raise ValueError("invalid github repository")
    return RepoRef("github", "github.com", key, None)


@dataclass(frozen=True)
class PolicyEntry:
    kind: str
    host: str | None = None
    segments: tuple[str, ...] = ()
    project_id: int | None = None
    owner: str | None = None


def parse_entry(text: str) -> PolicyEntry:
    if ":" not in text:
        if "/" in text:
            parse_repo_key(text)
        elif not GITHUB_OWNER.fullmatch(text):
            raise ValueError("invalid policy owner")
        return PolicyEntry("owner", owner=text.casefold())
    if text[:7].casefold() != "gitlab:":
        raise ValueError("unknown forge")
    if "#" in text:
        ref = parse_repo_key(canonicalize_key(text))
        return PolicyEntry("id", ref.host, project_id=ref.project_id)
    host, separator, path = text[7:].partition("/")
    segments = path.split("/")
    if not separator or not 1 <= len(segments) <= GITLAB_MAX_DEPTH or not all(
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", segment) for segment in segments
    ):
        raise ValueError("invalid gitlab policy path")
    return PolicyEntry("path", _canonical_authority(host), tuple(s.casefold() for s in segments))
