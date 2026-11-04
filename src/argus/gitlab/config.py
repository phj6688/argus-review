"""Read GitLab settings without opening files or connections."""

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from argus.forge.keys import canonical_host


class GitLabConfigError(ValueError):
    """A setting violates its constraints. Messages never include input values."""


@dataclass(frozen=True)
class GitLabBase:
    scheme: str
    host: str
    port: int | None
    path_prefix: str

    @property
    def origin(self) -> str:
        return f"https://{canonical_host(self.host, self.port)}"

    @property
    def url(self) -> str:
        return self.origin + self.path_prefix

_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def base_url() -> GitLabBase | None:
    raw = os.environ.get("ARGUS_GITLAB_URL", "")
    if not raw.strip():
        return None
    # urlsplit removes some control characters, so reject them before parsing.
    if re.search(r"[\s\x00-\x1f\x7f-\x9f\\%?#]", raw):
        raise GitLabConfigError("ARGUS_GITLAB_URL: whitespace, controls, backslashes, encoding, queries and fragments are forbidden")
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "https" or not parsed.netloc or "@" in parsed.netloc:
            raise ValueError
        host = parsed.hostname or ""
        port = parsed.port
        if parsed.netloc.startswith("["):
            ipaddress.IPv6Address(host)
            authority_valid = re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?", parsed.netloc)
        else:
            authority_valid = re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
            labels = host.split(".")
            if not host or len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
                raise ValueError
        if not authority_valid or port == 0:
            raise ValueError
    except ValueError:
        raise GitLabConfigError("ARGUS_GITLAB_URL: requires an https host without userinfo and an optional port from 1 to 65535") from None
    prefix = parsed.path.removesuffix("/")
    if prefix and any(segment in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._~-]+", segment)
                      for segment in prefix[1:].split("/")):
        raise GitLabConfigError("ARGUS_GITLAB_URL: path segments must be nonempty, use letters, digits or ._~-, and cannot be . or ..")
    return GitLabBase("https", host.lower(), None if port == 443 else port, prefix)


def _read(name: str) -> str:
    return os.environ.get(name, "").strip()


def token() -> str:
    return _read("ARGUS_GITLAB_TOKEN")


def ca_bundle() -> str | None:
    value = os.environ.get("ARGUS_GITLAB_CA_BUNDLE", "")
    if not value.strip():
        return None
    if any(char in value for char in "\x00\r\n"):
        raise GitLabConfigError("ARGUS_GITLAB_CA_BUNDLE: NUL and newline characters are forbidden")
    return value.strip()


def _list(name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(entry.strip() for entry in _read(name).split(",") if entry.strip()))


def allow() -> tuple[str, ...]:
    return _list("ARGUS_GITLAB_ALLOW")


def publish_mode() -> str:
    mode = _read("ARGUS_GITLAB_PUBLISH").lower() or "0"
    if mode not in {"0", "dry-run", "1"}:
        raise GitLabConfigError("ARGUS_GITLAB_PUBLISH: must be 0, dry-run or 1")
    return mode


def effective_publish_mode() -> str:
    modes = {"0": 0, "dry-run": 1, "1": 2}
    general = os.environ.get("ARGUS_PUBLISH", "0")
    return min(general if general in modes else "0", publish_mode(), key=modes.__getitem__)


def publish_projects() -> tuple[str, ...]:
    projects = _list("ARGUS_GITLAB_PUBLISH_PROJECTS")
    if any(char in "*?[" or char.isspace() for project in projects for char in project):
        raise GitLabConfigError("ARGUS_GITLAB_PUBLISH_PROJECTS: exact keys only, without wildcards or whitespace")
    return projects


def _integer(name: str, default: int, minimum: int = 1) -> int:
    value = _read(name) or str(default)
    try:
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise ValueError
        result = int(value)
        if result < minimum:
            raise ValueError
    except ValueError:
        raise GitLabConfigError(f"{name}: must be an ASCII integer >= {minimum}") from None
    return result


def notes_per_minute() -> int:
    return _integer("ARGUS_GITLAB_NOTES_PER_MINUTE", 30)


def trusted_level() -> int:
    level = _integer("ARGUS_GITLAB_TRUSTED_LEVEL", 30, minimum=0)
    if level not in {0, 5, 10, 15, 20, 30, 40, 50}:
        raise GitLabConfigError("ARGUS_GITLAB_TRUSTED_LEVEL: must be 0, 5, 10, 15, 20, 30, 40 or 50")
    return level


def _boolean(name: str) -> bool:
    value = _read(name).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise GitLabConfigError(f"{name}: must be 1/true/yes/on or 0/false/no/off")


def watch_enabled() -> bool:
    return _boolean("ARGUS_GITLAB_WATCH")


def reconcile_ticks() -> int:
    return _integer("ARGUS_GITLAB_RECONCILE_TICKS", 10)


def reply_loop() -> bool:
    return _boolean("ARGUS_GITLAB_REPLY_LOOP")


def timeout_s() -> float:
    try:
        value = float(_read("ARGUS_GITLAB_TIMEOUT_S") or "30")
        if not math.isfinite(value) or value <= 0:
            raise ValueError
    except ValueError:
        raise GitLabConfigError("ARGUS_GITLAB_TIMEOUT_S: must be finite and greater than zero") from None
    return value


def max_response_bytes() -> int:
    return _integer("ARGUS_GITLAB_MAX_RESPONSE_BYTES", 10485760, minimum=1024)
