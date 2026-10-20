"""The lockfile-drift rule: CI and production must use the same dependencies.

An image that resolves fresh dependencies can fail to import while CI stays
healthy with locked versions. Both a lockfile and an unlocked install are needed
to detect this mismatch.
"""

from argus.diff import FilePatch
from argus.jarvis import config_lane

UNLOCKED_PY = """\
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["uvicorn", "app:api"]
"""

LOCKED_PY = """\
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev
CMD ["uvicorn", "app:api"]
"""

UNLOCKED_NODE = """\
FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN npm install
"""

LOCKED_NODE = """\
FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci
"""


def _reader(mapping):
    return lambda path: mapping.get(path)


def _ids(hits):
    return sorted(h.rule_id for h in hits)


def test_a_dockerfile_is_eligible_alongside_yaml():
    files = [FilePatch(filename=n, status="modified", patch="@@ -1 +1 @@\n+x\n")
             for n in ("Dockerfile", "deploy/Dockerfile.prod", "compose.yml",
                       "src/app.py", "Dockerfile.dockerignore")]
    assert config_lane.eligible(files) == [
        "Dockerfile", "deploy/Dockerfile.prod", "compose.yml"]


def test_an_unlocked_install_beside_a_lockfile_is_flagged():
    hits = config_lane.scan("Dockerfile", UNLOCKED_PY,
                            read=_reader({"uv.lock": "version = 1\n"}))
    assert _ids(hits) == ["image-ignores-lockfile"]
    assert "uv.lock" in hits[0].message
    assert hits[0].line == 4


def test_a_locked_install_is_silent():
    hits = config_lane.scan("Dockerfile", LOCKED_PY,
                            read=_reader({"uv.lock": "version = 1\n"}))
    assert _ids(hits) == []


def test_no_lockfile_means_no_promise_to_break():
    """A repo that never pinned anything has not made CI and production
    disagree. The rule needs both halves or it is just an opinion about
    dependency hygiene, which belongs in a model lane."""
    assert config_lane.scan("Dockerfile", UNLOCKED_PY, read=_reader({})) == []


def test_the_node_form_is_read_too():
    hits = config_lane.scan("Dockerfile", UNLOCKED_NODE,
                            read=_reader({"package-lock.json": "{}"}))
    assert _ids(hits) == ["image-ignores-lockfile"]


def test_npm_ci_honours_the_lockfile():
    hits = config_lane.scan("Dockerfile", LOCKED_NODE,
                            read=_reader({"package-lock.json": "{}"}))
    assert _ids(hits) == []


def test_one_locked_install_clears_the_file():
    """A multi-stage build that installs unlocked in a throwaway builder stage
    and locked in the final one has not shipped drift."""
    mixed = UNLOCKED_PY + "\nFROM python:3.13-slim AS final\nRUN uv sync --frozen\n"
    assert config_lane.scan("Dockerfile", mixed,
                            read=_reader({"uv.lock": "x"})) == []


def test_a_dockerfile_is_not_parsed_as_yaml():
    """Dispatch by file kind. A Dockerfile is not a YAML document, and running it
    through the YAML parser would have reported every image as unreadable."""
    hits = config_lane.scan("Dockerfile", "FROM scratch\nRUN echo a: [\n",
                            read=_reader({}))
    assert hits == []


def test_the_rule_names_its_incident():
    assert config_lane.RULE_INCIDENTS["image-ignores-lockfile"]
    assert "image-ignores-lockfile" in config_lane.rule_ids()


def test_a_disabled_rule_does_not_fire(monkeypatch):
    monkeypatch.setenv(config_lane.ENV_IGNORE, "image-ignores-lockfile")
    assert config_lane.scan("Dockerfile", UNLOCKED_PY,
                            read=_reader({"uv.lock": "x"})) == []
