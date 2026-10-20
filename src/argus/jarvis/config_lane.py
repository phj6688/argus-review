"""The config lane: deterministic checks for configuration failure classes.

YAML structure distinguishes an empty runtime environment override from the same
value passed as a build argument. A line regex cannot tell those cases apart.
RULE_INCIDENTS describes the failure class behind each rule.

Rules fire only on evidence: environment shadowing needs the Dockerfile that
bakes the value. Unreadable files raise because a failed scan is not a clean scan.
"""

import os
import posixpath
import re

import yaml

from ..schema import Finding, Severity, Verdict, VerifiedFinding

LANE = "config"

ENV_IGNORE = "ARGUS_CONFIG_IGNORE"

YAML_SUFFIXES = (".yml", ".yaml")
_REMOVED = "removed"

RULE_ENV_SHADOW = "compose-env-shadows-image"
RULE_PUBLIC_BIND = "compose-datastore-public-bind"
RULE_PLACEHOLDER = "config-placeholder-secret"
RULE_AUTH_SKIP = "auth-skip-unanchored"
RULE_PORT_CLASH = "compose-duplicate-host-port"
RULE_LOCK_DRIFT = "image-ignores-lockfile"

# Lockfiles, and the installs that do or do not honour one. The rule needs both
# halves: a repository with no lockfile has made no promise for the image to
# break, and flagging it would be an opinion about dependency hygiene rather
# than a statement about this repository disagreeing with itself.
LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock",
             "requirements.lock", "package-lock.json", "pnpm-lock.yaml",
             "yarn.lock", "bun.lockb")
_UNLOCKED_INSTALL = re.compile(
    r"(?i)\b(pip\s+install(?!.*(?:-r\s+\S*lock|--require-hashes))|"
    r"poetry\s+install|npm\s+install|npm\s+i\b|yarn\s+add|pnpm\s+add)")
_LOCKED_INSTALL = re.compile(
    r"(?i)(uv\s+sync|uv\s+pip\s+sync|npm\s+ci\b|--frozen-lockfile|--frozen|"
    r"--locked|--require-hashes|poetry\s+install\s+--sync|pnpm\s+install\s+--frozen)")

DOCKERFILE_PREFIX = "Dockerfile"

# Container ports that identify a datastore. The rule is scoped to these because
# the unrestricted version was measured at 25.6 findings per 100 real YAML files,
# almost all of them web apps published on purpose behind a proxy. Exposure of a
# web port is a routing choice; a public database port can expose stored
# data to anyone who can reach the host.
DATASTORE_PORTS = frozenset({
    "5432", "3306", "6379", "27017", "7687", "7474", "9200", "9300", "8086",
    "5984", "11211", "2379", "5672", "15672", "1433", "26257", "6333", "19530",
})

# Keys whose value is a credential. The placeholder rule is restricted to these
# after a measured false positive: a CI `run:` step containing the word
# "placeholder" is prose, not a published secret.
_SECRET_KEY = re.compile(
    r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|_key$|^key$|auth|credential|"
    r"private|salt|dsn|conn(ection)?[_-]?string)")

# Keep the failure class beside each rule so its purpose remains clear.
RULE_INCIDENTS = {
    RULE_ENV_SHADOW: "An empty runtime override erases a value baked into the image",
    RULE_PUBLIC_BIND: "A datastore published on every interface exposes stored data",
    RULE_PLACEHOLDER: "A placeholder credential reaches production as a real password",
    RULE_AUTH_SKIP: "An unanchored auth skip pattern lets longer paths bypass authentication",
    RULE_PORT_CLASH: "Two services claim the same host port and the second cannot start",
    RULE_LOCK_DRIFT: "An image resolves dependencies outside the lockfile and cannot start "
                     "while CI stays green with locked versions",
}

_SEVERITY = {
    RULE_ENV_SHADOW: Severity.major,
    RULE_PUBLIC_BIND: Severity.major,
    RULE_PLACEHOLDER: Severity.critical,
    RULE_AUTH_SKIP: Severity.critical,
    RULE_PORT_CLASH: Severity.major,
    RULE_LOCK_DRIFT: Severity.major,
}

# An interpolation whose default is empty. Compose resolves this to "" when the
# variable is unset, and "" is a value: it overrides whatever the image baked in.
_EMPTY_DEFAULT = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-|-)\}$")

# Keys whose value is a list of paths that skip authentication. Getting one of
# these wrong opens the gate rather than closing it, which is why they are the
# only place this lane reads a regex as data.
_AUTH_SKIP_KEY = re.compile(
    r"(?i)(skip[_-]?auth|auth[_-]?skip|allow[_-]?list|allowlist|whitelist|"
    r"bypass|no[_-]?auth|unauthenticated|public[_-]?paths?)")

# Placeholders that ship as the compose default and are meant to be replaced.
# Deliberately literal: these strings are near-absent from working config, which
# is what keeps the rule quiet enough to be worth having.
_PLACEHOLDER = re.compile(
    r"(?i)(change[_-]?me|replace[_-]?me|your[_-][a-z0-9_-]*[_-]here|"
    r"placeholder|secret123|password123|admin:admin|changethis)")

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# The address this lane exists to FIND in someone else's config, never one this
# process binds. bandit's B104 cannot tell a detector from a listener, so the
# literal is named once and suppressed once rather than five times inline.
_ANY_ADDRESS = "0.0.0.0"  # nosec B104 - read from config under review, never bound
_OPEN_ADDRESSES = (_ANY_ADDRESS, "::", "[::]")
# A trailing wildcard or slash makes a prefix match visibly deliberate. Its
# absence is what made `^/mcp` read as "the /mcp route" and behave as "anything
# starting with /mcp".
_DELIBERATE_PREFIX = (".*", ".+", "/", "[", "\\w", "\\d")


class ConfigUnreadable(RuntimeError):
    """The lane could not parse the file, so it has no opinion to report.

    Raised rather than returning no findings. A file that failed to parse and a
    file that came back clean are different claims, and only one of them is
    evidence that the config is sound.
    """


class Hit:
    __slots__ = ("rule_id", "file", "line", "severity", "message")

    def __init__(self, rule_id: str, file: str, line: int, message: str):
        self.rule_id, self.file, self.line, self.message = rule_id, file, line, message
        self.severity = _SEVERITY[rule_id].value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Hit({self.rule_id} {self.file}:{self.line})"


def ignored() -> tuple[str, ...]:
    raw = os.environ.get(ENV_IGNORE, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def rule_ids() -> tuple[str, ...]:
    return tuple(r for r in RULE_INCIDENTS if r not in ignored())


def tool_identity() -> dict:
    """What a re-run would need. The deterministic tier claims re-executability,
    so the active rule set travels with every finding the lane produces."""
    return {"name": LANE, "rules": ",".join(rule_ids()),
            "ignored": ",".join(ignored()), "parser": f"pyyaml {yaml.__version__}"}


def _is_dockerfile(path: str) -> bool:
    """`Dockerfile`, `Dockerfile.prod`, never `Dockerfile.dockerignore`."""
    name = posixpath.basename(path)
    if name == DOCKERFILE_PREFIX:
        return True
    return name.startswith(f"{DOCKERFILE_PREFIX}.") and \
        not name.endswith(".dockerignore")


def eligible(files: list) -> list[str]:
    """Changed config the lane can read. A deletion has no post-image to parse."""
    return [f.filename for f in files
            if f.status != _REMOVED
            and (f.filename.endswith(YAML_SUFFIXES) or _is_dockerfile(f.filename))]


def scan(path: str, content: str, read=None) -> list[Hit]:
    """Run the active rules over one file, dispatched by what kind of file it is.

    `read(path) -> str | None` fetches a sibling at the same revision, resolved
    from the repository root. Two rules need it: the shadowing rule reads the
    Dockerfile that bakes a value, and the lock-drift rule reads whether a
    lockfile exists at all. Both decline rather than guess when it returns None.
    """
    read = read or (lambda _path: None)
    if _is_dockerfile(path):
        return _dockerfile_rules(path, content, read)
    return _yaml_rules(path, content, read)


def _dockerfile_rules(path: str, content: str, read) -> list[Hit]:
    if RULE_LOCK_DRIFT not in set(rule_ids()):
        return []
    return _lock_drift(path, content, read)


def _lock_drift(path: str, content: str, read) -> list[Hit]:
    """An image that resolves dependencies a lockfile in the repo already pinned.

    Any locked install anywhere in the file clears it: a multi-stage build that
    installs loosely in a throwaway builder and strictly in the final stage has
    not shipped drift, and flagging it would train the reader to ignore the rule.
    """
    if _LOCKED_INSTALL.search(content):
        return []
    present = next((name for name in LOCKFILES if read(name) is not None), None)
    if not present:
        return []
    return [Hit(RULE_LOCK_DRIFT, path, number,
                f"{present} pins this repository's dependencies, and this image "
                f"installs without honouring it, so CI and production can resolve "
                f"different versions of the same requirement")
            for number, line in enumerate(content.splitlines(), start=1)
            if _UNLOCKED_INSTALL.search(line)]


def _yaml_rules(path: str, content: str, read) -> list[Hit]:
    try:
        # compose_all, not compose: 93 of this operator's 226 real YAML files are
        # multi-document, and a single-document parser would have called every
        # one of them unreadable.
        documents = list(yaml.compose_all(content, Loader=yaml.SafeLoader))
    except yaml.YAMLError as error:
        raise ConfigUnreadable(f"{path} did not parse: {error}") from error

    active = set(rule_ids())
    hits: list[Hit] = []
    for root in documents:
        if not isinstance(root, yaml.MappingNode):
            continue
        if RULE_PLACEHOLDER in active:
            hits += _placeholders(path, root)
        if RULE_AUTH_SKIP in active:
            hits += _auth_skips(path, root)
        services = _child(root, "services")
        if isinstance(services, yaml.MappingNode):
            hits += _compose_rules(path, services, read, active)
    return sorted(hits, key=lambda h: (h.line, h.rule_id))


def to_finding(hit: Hit) -> VerifiedFinding:
    """A hit as a verified finding, skipping the LLM verifier by design. The rule
    already executed against the file's own bytes; asking a model to confirm it
    would put an opinion in front of the one lane that does not need one."""
    incident = RULE_INCIDENTS[hit.rule_id]
    finding = Finding(
        file=hit.file, line=hit.line, severity=Severity(hit.severity),
        confidence=1.0, title=f"{hit.rule_id}: {hit.message}"[:120],
        description=f"{hit.message}\n\nMined from: {incident}",
        proposed_fix=_FIXES[hit.rule_id],
        rule_id=hit.rule_id,
    )
    return VerifiedFinding(
        finding=finding,
        verdict=Verdict(is_real=True, confidence=1.0,
                        reasoning=f"{hit.rule_id} fired at {hit.file}:{hit.line}"),
        lane=LANE,
    )


_FIXES = {
    RULE_ENV_SHADOW: "Move the variable to build.args so the value lives in the "
                     "image, or drop the empty default so an unset variable stays unset.",
    RULE_PUBLIC_BIND: "Bind to 127.0.0.1 when only a host-side proxy needs the "
                      "port; every other address publishes on the tailnet and LAN too.",
    RULE_PLACEHOLDER: "Replace the placeholder with a real secret reference. A "
                      "compose default that reaches production is a published credential.",
    RULE_AUTH_SKIP: "Anchor the entry at both ends, for example ^/mcp($|/). "
                    "Anchored only at the start, it matches every longer path.",
    RULE_PORT_CLASH: "Give each service its own host port. The second container "
                     "fails at creation and gets no network, which reads as a health-check timeout.",
    RULE_LOCK_DRIFT: "Install from the lockfile in the image too (uv sync "
                     "--frozen, npm ci, poetry install --sync). Otherwise a green "
                     "suite says nothing about what production will resolve.",
}


# --- node helpers ------------------------------------------------------------

def _pairs(node):
    """(key, key_node, value_node) for a mapping, ignoring non-scalar keys."""
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(str(k.value), k, v) for k, v in node.value
            if isinstance(k, yaml.ScalarNode)]


def _child(node, name: str):
    for key, _key_node, value in _pairs(node):
        if key == name:
            return value
    return None


def _line(node) -> int:
    return int(node.start_mark.line) + 1


def _walk(node):
    """Every (key, value_node) pair anywhere in the document."""
    if isinstance(node, yaml.MappingNode):
        for key, key_node, value in _pairs(node):
            yield key, key_node, value
            yield from _walk(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk(item)


# --- rules that apply to any YAML document -----------------------------------

def _placeholders(path: str, root) -> list[Hit]:
    hits = []
    for key, key_node, value in _walk(root):
        if not (isinstance(value, yaml.ScalarNode) and _SECRET_KEY.search(key)):
            continue
        found = _PLACEHOLDER.search(str(value.value))
        if found:
            hits.append(Hit(RULE_PLACEHOLDER, path, _line(key_node),
                            f"{key} carries the placeholder "
                            f"{found.group(0)!r}, which reaches production as a real value"))
    return hits


def _auth_skips(path: str, root) -> list[Hit]:
    """Both spellings of a skip list: one comma-separated scalar, or a sequence.

    Proxy middleware and Kubernetes manifests use the sequence form, so reading
    only the scalar would miss those authentication bypasses.
    """
    hits = []
    for key, key_node, value in _walk(root):
        if not _AUTH_SKIP_KEY.search(key):
            continue
        for node in _scalars(value):
            for entry in _unanchored(str(node.value)):
                hits.append(Hit(RULE_AUTH_SKIP, path, _line(node),
                                f"{key} entry {entry!r} is anchored at the start "
                                f"but not at the end, so it also matches every "
                                f"longer path"))
    return hits


def _scalars(node):
    """The scalar leaves of a value, whether it is one scalar or a sequence."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            if isinstance(item, yaml.ScalarNode):
                yield item


def _unanchored(raw: str) -> list[str]:
    """Entries that open the gate wider than they read.

    `^/mcp` matches /mcp-extra. `^/health$` and `^/mcp($|/)` do not. An entry
    ending in a wildcard is a prefix match somebody chose on purpose.
    """
    loose = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry.startswith("^") or "$" in entry:
            continue
        if entry.endswith(_DELIBERATE_PREFIX):
            continue
        loose.append(entry)
    return loose


# --- rules that apply to a compose document ----------------------------------

def _compose_rules(path: str, services, read, active: set) -> list[Hit]:
    hits: list[Hit] = []
    claimed: dict[tuple[str, str], str] = {}
    for name, _key_node, service in _pairs(services):
        if not isinstance(service, yaml.MappingNode):
            continue
        if RULE_ENV_SHADOW in active:
            hits += _shadowed_env(path, name, service, read)
        for host_ip, host_port, container_port, node in _published(service):
            if RULE_PUBLIC_BIND in active and host_ip in _OPEN_ADDRESSES \
                    and container_port in DATASTORE_PORTS:
                where = f" as {host_port}" if host_port else ""
                hits.append(Hit(RULE_PUBLIC_BIND, path, _line(node),
                                f"{name} publishes datastore port {container_port}"
                                f"{where} on {host_ip}, which is every host "
                                f"interface including the tailnet and LAN"))
            if not host_port:
                continue
            slot = (host_ip, host_port)
            if RULE_PORT_CLASH in active and slot in claimed:
                hits.append(Hit(RULE_PORT_CLASH, path, _line(node),
                                f"{name} and {claimed[slot]} both publish host port "
                                f"{host_port} on {host_ip}; the second container fails at creation"))
            claimed.setdefault(slot, name)
    return hits


def _published(service):
    """(host_ip, host_port, container_port, node) for every published port.

    The container port is carried because it is what identifies the service:
    neo4j's bolt was published as `7697:7687`, where the host side is arbitrary
    and 7687 is the fact that a database is listening.
    """
    ports = _child(service, "ports")
    if not isinstance(ports, yaml.SequenceNode):
        return
    for item in ports.value:
        if isinstance(item, yaml.ScalarNode):
            parsed = _short_port(str(item.value))
            if parsed:
                yield (*parsed, item)
        elif isinstance(item, yaml.MappingNode):
            fields = {k: v for k, _kn, v in _pairs(item)}
            published, target = fields.get("published"), fields.get("target")
            if published is None or target is None:
                continue
            host_ip = fields.get("host_ip")
            address = str(host_ip.value) if isinstance(host_ip, yaml.ScalarNode) \
                else _ANY_ADDRESS
            yield address, str(published.value), str(target.value), item


def _short_port(raw: str) -> tuple[str, str, str] | None:
    """`8080`, `8080:80`, `127.0.0.1:8080:80` and `[::1]:8080:80`.

    An entry holding a `${...}` substitution is declined rather than split. The
    braces contain their own colons, so positional splitting once reported that a
    port named `-127.0.0.1}` was published on every interface: a confident
    statement about a value the lane cannot resolve without the environment.
    """
    text = raw.strip().split("/")[0]
    if not text or "${" in text:
        return None
    if text.startswith("["):
        address, _, rest = text.partition("]:")
        parts = rest.split(":")
        if len(parts) < 2:
            return None
        return f"{address}]", parts[0], parts[1]
    parts = text.split(":")
    if len(parts) == 1:
        # Only the container port is declared; Docker assigns the host port. The
        # lane cannot name it, so it must not claim a slot for it either: two
        # services each declaring `- "8080"` do not collide.
        return _ANY_ADDRESS, "", parts[0]
    if len(parts) == 2:
        return _ANY_ADDRESS, parts[0], parts[1]
    address = parts[0] if _IPV4.match(parts[0]) or parts[0] == "::" else _ANY_ADDRESS
    return address, parts[1], parts[2]


def _shadowed_env(path: str, name: str, service, read) -> list[Hit]:
    """An empty default that overrides a value the image already baked in.

    Fires only when the Dockerfile is readable AND declares the same variable.
    Both halves are required: the empty default alone is a normal way to make a
    variable optional, and it only becomes a defect when something underneath it
    had a value to lose.
    """
    candidates = list(_env_empty_defaults(service))
    if not candidates:
        return []
    dockerfile = _dockerfile_for(path, service)
    if not dockerfile:
        return []
    content = read(dockerfile)
    if content is None:
        return []
    baked = _image_variables(content)
    return [Hit(RULE_ENV_SHADOW, path, line,
                f"{name} sets {var} to an empty default, and {dockerfile} bakes "
                f"{var} into the image; compose resolves the unset variable to an "
                f"empty string and that empty value wins")
            for var, line in candidates if var in baked]


def _env_empty_defaults(service):
    """(variable, line) for each environment entry whose default is empty."""
    environment = _child(service, "environment")
    if isinstance(environment, yaml.MappingNode):
        for key, key_node, value in _pairs(environment):
            if isinstance(value, yaml.ScalarNode) and \
                    _EMPTY_DEFAULT.match(str(value.value).strip()):
                yield key, _line(key_node)
    elif isinstance(environment, yaml.SequenceNode):
        for item in environment.value:
            if not isinstance(item, yaml.ScalarNode):
                continue
            key, _, raw = str(item.value).partition("=")
            if raw and _EMPTY_DEFAULT.match(raw.strip()):
                yield key, _line(item)


def _dockerfile_for(compose_path: str, service) -> str | None:
    """Where this service's image is built from, relative to the compose file."""
    build = _child(service, "build")
    if build is None:
        return None
    root = posixpath.dirname(compose_path)
    if isinstance(build, yaml.ScalarNode):
        return posixpath.normpath(posixpath.join(root, str(build.value), "Dockerfile"))
    context = _child(build, "context")
    named = _child(build, "dockerfile")
    directory = str(context.value) if isinstance(context, yaml.ScalarNode) else "."
    filename = str(named.value) if isinstance(named, yaml.ScalarNode) else "Dockerfile"
    return posixpath.normpath(posixpath.join(root, directory, filename))


_IMAGE_VAR = re.compile(r"^\s*(?:ENV|ARG)\s+(.*)$", re.IGNORECASE)


def _image_variables(dockerfile: str) -> set[str]:
    """Variables the image itself defines, via ENV or a defaulted ARG."""
    names: set[str] = set()
    for line in dockerfile.splitlines():
        found = _IMAGE_VAR.match(line)
        if not found:
            continue
        rest = found.group(1).strip()
        directive = line.strip().split(None, 1)[0].upper()
        if "=" in rest:
            names.update(part.split("=", 1)[0].strip()
                         for part in rest.split() if "=" in part)
        elif directive == "ENV":
            names.add(rest.split(None, 1)[0])
    return names
