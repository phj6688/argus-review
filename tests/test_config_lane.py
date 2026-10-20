"""Configuration checks against vulnerable and fixed config shapes.

A file the lane cannot parse raises rather than returning a clean result.
"""

import pytest

from argus.diff import FilePatch
from argus.jarvis import config_lane

# An empty runtime value erases image build metadata. The identical string
# `${BUILD_SHA:-}` is the DEFECT under `environment:` and the FIX under
# `build.args`, because compose resolves an unset variable to an empty string and
# that empty runtime value overrides what the image baked in. A line-level regex
# cannot tell the two apart, which is why this lane parses structure.
COMPOSE_SHADOWED = """\
name: example-app
services:
  example-app:
    build: .
    restart: unless-stopped
    environment:
      APP_ENV: prod
      BUILD_SHA: ${BUILD_SHA:-}
      APP_MESSAGE: ${APP_MESSAGE:-}
"""

# The shipped fix. Same variable, same empty default, moved under build.args.
COMPOSE_BAKED = """\
name: example-app
services:
  example-app:
    build:
      context: .
      args:
        BUILD_SHA: ${BUILD_SHA:-}
    restart: unless-stopped
    environment:
      APP_ENV: prod
      APP_MESSAGE: ${APP_MESSAGE:-}
"""

DOCKERFILE_WITH_ENV = """\
FROM node:22-slim
ENV HOME=/tmp
ARG BUILD_SHA=""
ENV BUILD_SHA=$BUILD_SHA
EXPOSE 8090
"""

DOCKERFILE_PLAIN = """\
FROM node:22-slim
ENV HOME=/tmp
EXPOSE 8090
"""

# Public datastore ports and a placeholder password let anyone on the network
# read and write data. These are independent defects on adjacent lines.
COMPOSE_DATASTORE = """\
services:
  example-db:
    image: neo4j:5.26
    restart: unless-stopped
    ports:
      - "7697:7687"
      - "7484:7474"
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-changeme-in-prod}
  api:
    image: example-app
    restart: unless-stopped
    ports:
      - "127.0.0.1:8210:8210"
"""

# oauth2-proxy reads each entry as a regex. An unanchored prefix lets
# longer paths such as /mcp-extra bypass authentication.
COMPOSE_SKIP_AUTH = """\
services:
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy
    environment:
      OAUTH2_PROXY_SKIP_AUTH_ROUTES: "^/health$,^/mcp"
"""

COMPOSE_SKIP_AUTH_FIXED = """\
services:
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy
    environment:
      OAUTH2_PROXY_SKIP_AUTH_ROUTES: "^/health$,^/mcp($|/)"
"""

# Two services publishing the same host port prevent the second container
# from starting.
COMPOSE_PORT_CLASH = """\
services:
  example-api:
    image: example-api
    ports:
      - "127.0.0.1:8002:8002"
  example-worker:
    image: example-worker
    ports:
      - "127.0.0.1:8002:8080"
"""

CLEAN_COMPOSE = """\
services:
  api:
    image: argus-api
    restart: unless-stopped
    ports:
      - "127.0.0.1:8350:8350"
    environment:
      ARGUS_ENV: prod
      ARGUS_MODEL: ${ARGUS_MODEL}
"""


def _files(*names):
    return [FilePatch(filename=n, status="modified", patch="@@ -1 +1 @@\n+x\n")
            for n in names]


def _reader(mapping):
    return lambda path: mapping.get(path)


def _ids(hits):
    return sorted(h.rule_id for h in hits)


# --- eligibility -------------------------------------------------------------

def test_eligible_takes_yaml_and_skips_everything_else():
    files = _files("docker-compose.yml", "proxy/dynamic/apps.yml", "src/app.py",
                   "README.md", "compose.yaml", "web/app.tsx")
    assert config_lane.eligible(files) == [
        "docker-compose.yml", "proxy/dynamic/apps.yml", "compose.yaml"]


def test_a_deleted_file_has_no_post_image_to_read():
    files = [FilePatch(filename="compose.yml", status="removed", patch="")]
    assert config_lane.eligible(files) == []


# --- the honesty rule --------------------------------------------------------

def test_unparseable_yaml_raises_instead_of_reporting_clean():
    """A file the lane could not read is not a file the lane found clean.

    This is the same law the verdict contract learned: absence of evidence must
    never render as a pass.
    """
    with pytest.raises(config_lane.ConfigUnreadable):
        config_lane.scan("compose.yml", "services:\n  a:\n   - b\n  \t bad: [")


def test_a_yaml_file_that_is_not_a_mapping_is_simply_not_compose():
    assert config_lane.scan("list.yml", "- one\n- two\n") == []


# --- rule: an empty default that shadows what the image baked in -------------

def test_empty_default_is_flagged_only_when_the_image_sets_the_same_variable():
    hits = config_lane.scan("docker-compose.yml", COMPOSE_SHADOWED,
                            read=_reader({"Dockerfile": DOCKERFILE_WITH_ENV}))
    assert _ids(hits) == ["compose-env-shadows-image"]
    assert "BUILD_SHA" in hits[0].message


def test_the_sibling_empty_default_is_not_flagged():
    """APP_MESSAGE sits on the next line with the identical shape and
    was never a bug, because no image layer sets it. Firing on it would make the
    lane noise within one review."""
    hits = config_lane.scan("docker-compose.yml", COMPOSE_SHADOWED,
                            read=_reader({"Dockerfile": DOCKERFILE_WITH_ENV}))
    assert all("APP_MESSAGE" not in h.message for h in hits)


def test_the_shipped_fix_is_silent():
    """Same variable, same empty default, moved under build.args. The lane must
    go quiet on the known-good shape or it cannot be used as a gate."""
    hits = config_lane.scan("docker-compose.yml", COMPOSE_BAKED,
                            read=_reader({"Dockerfile": DOCKERFILE_WITH_ENV}))
    assert _ids(hits) == []


def test_no_finding_without_the_dockerfile_evidence():
    """Fire on evidence or not at all. Without the image layer the lane cannot
    know the value is shadowing anything, and a guess belongs in another lane."""
    hits = config_lane.scan("docker-compose.yml", COMPOSE_SHADOWED,
                            read=_reader({}))
    assert _ids(hits) == []


def test_a_dockerfile_without_that_env_does_not_trigger_it():
    hits = config_lane.scan("docker-compose.yml", COMPOSE_SHADOWED,
                            read=_reader({"Dockerfile": DOCKERFILE_PLAIN}))
    assert _ids(hits) == []


def test_build_context_directs_which_dockerfile_is_read():
    compose = COMPOSE_SHADOWED.replace("build: .", "build: ./services/web")
    hits = config_lane.scan(
        "docker-compose.yml", compose,
        read=_reader({"services/web/Dockerfile": DOCKERFILE_WITH_ENV}))
    assert _ids(hits) == ["compose-env-shadows-image"]


# --- rule: a published port with no host address -----------------------------

def test_a_datastore_port_published_on_every_interface_is_flagged():
    hits = config_lane.scan("compose.yml", COMPOSE_DATASTORE, read=_reader({}))
    bind = [h for h in hits if h.rule_id == "compose-datastore-public-bind"]
    assert len(bind) == 2
    assert {h.line for h in bind} == {6, 7}


def test_a_loopback_bound_port_is_not_flagged():
    hits = config_lane.scan("compose.yml", CLEAN_COMPOSE, read=_reader({}))
    assert _ids(hits) == []


def test_a_web_port_on_every_interface_is_left_alone():
    """Measured, not assumed. Over 133 real YAML files the unrestricted rule fired
    25.6 times per 100, mostly on web apps deliberately published behind a proxy.
    A rule at that rate teaches the operator to skip the lane, so it is scoped to
    the ports whose exposure is the incident: a datastore anyone can reach."""
    compose = CLEAN_COMPOSE.replace('"127.0.0.1:8350:8350"', '"0.0.0.0:8350:8350"')
    assert config_lane.scan("compose.yml", compose, read=_reader({})) == []


def test_an_explicit_zero_address_on_a_datastore_is_the_same_defect():
    compose = CLEAN_COMPOSE.replace('"127.0.0.1:8350:8350"', '"0.0.0.0:5432:5432"')
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert _ids(hits) == ["compose-datastore-public-bind"]


def test_long_form_port_syntax_is_read_too():
    compose = """\
services:
  db:
    image: postgres
    ports:
      - target: 5432
        published: 5432
        protocol: tcp
"""
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert _ids(hits) == ["compose-datastore-public-bind"]


def test_the_container_port_decides_what_a_service_is_not_the_host_port():
    """neo4j's bolt port was published as 7697:7687. The host side is arbitrary;
    7687 is what makes it a database."""
    compose = COMPOSE_DATASTORE.replace('"7697:7687"', '"19999:7687"')
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert "compose-datastore-public-bind" in _ids(hits)


# --- the lane must not guess where it cannot read -----------------------------

def test_an_interpolated_bind_address_is_declined_not_guessed():
    """`${BIND_IP:-127.0.0.1}:5432:5432` contains a colon inside the substitution.
    Splitting it positionally produced a finding that a port named `-127.0.0.1}`
    was published on 0.0.0.0, which is nonsense stated with full confidence. The
    lane cannot resolve the variable, so it says nothing about that entry."""
    compose = """\
services:
  db:
    image: postgres
    ports:
      - "${BIND_IP:-127.0.0.1}:5432:5432"
"""
    assert config_lane.scan("compose.yml", compose, read=_reader({})) == []


def test_every_document_in_a_multi_document_file_is_scanned():
    """41% of this operator's real YAML is multi-document. Refusing it would have
    made the lane report `unreadable` on 93 of 226 files, which is a lane that
    fails loudly rather than one that works."""
    compose = 'services:\n  db:\n    image: postgres\n    ports:\n      - "0.0.0.0:5432:5432"\n'
    document = "---\napiVersion: v1\nkind: Pod\n---\n" + compose
    hits = config_lane.scan("bundle.yml", document, read=_reader({}))
    assert _ids(hits) == ["compose-datastore-public-bind"]


def test_a_document_stream_that_is_genuinely_broken_still_raises():
    with pytest.raises(config_lane.ConfigUnreadable):
        config_lane.scan("bundle.yml", "---\nok: 1\n---\nbad: [\n  \t x\n")


# --- rule: a placeholder left where a secret belongs -------------------------

def test_a_placeholder_secret_is_flagged():
    hits = config_lane.scan("compose.yml", COMPOSE_DATASTORE, read=_reader({}))
    secret = [h for h in hits if h.rule_id == "config-placeholder-secret"]
    assert len(secret) == 1
    assert secret[0].severity == "critical"


def test_a_real_variable_reference_is_not_a_placeholder():
    hits = config_lane.scan("compose.yml", CLEAN_COMPOSE, read=_reader({}))
    assert not [h for h in hits if h.rule_id == "config-placeholder-secret"]


def test_the_word_placeholder_in_a_build_step_is_not_a_leaked_credential():
    """Measured false positive: a CI `run:` block that merely contains the word.
    The rule only speaks about keys that carry a credential, because that is the
    only place a placeholder is a published secret rather than prose."""
    workflow = """\
jobs:
  build:
    steps:
      - run: echo "writing a placeholder config" > out.txt
"""
    assert config_lane.scan(".github/workflows/ci.yml", workflow,
                            read=_reader({})) == []


# --- rule: an unanchored regex in an auth skip list --------------------------

def test_an_unanchored_auth_skip_entry_is_flagged_critical():
    hits = config_lane.scan("compose.yml", COMPOSE_SKIP_AUTH, read=_reader({}))
    skip = [h for h in hits if h.rule_id == "auth-skip-unanchored"]
    assert len(skip) == 1
    assert skip[0].severity == "critical"
    assert "/mcp" in skip[0].message


def test_the_anchored_entry_beside_it_is_not_flagged():
    """`^/health$` is anchored and correct. Flagging it would bury the real one."""
    hits = config_lane.scan("compose.yml", COMPOSE_SKIP_AUTH, read=_reader({}))
    assert all("health" not in h.message for h in hits)


def test_the_shipped_auth_fix_is_silent():
    hits = config_lane.scan("compose.yml", COMPOSE_SKIP_AUTH_FIXED, read=_reader({}))
    assert _ids(hits) == []


def test_a_skip_list_written_as_a_yaml_sequence_is_checked_too():
    """Proxy middleware and Kubernetes manifests also use sequences. Checking
    only comma-separated strings would miss authentication bypasses here."""
    document = """\
http:
  middlewares:
    gate:
      forwardAuth:
        skip_auth_routes:
          - ^/health$
          - ^/mcp
"""
    hits = config_lane.scan("proxy/dynamic/apps.yml", document, read=_reader({}))
    assert _ids(hits) == ["auth-skip-unanchored"]
    assert "/mcp" in hits[0].message


def test_a_sequence_skip_list_that_is_fully_anchored_is_silent():
    document = ("http:\n  middlewares:\n    gate:\n      skip_auth_routes:\n"
                "        - ^/health$\n        - ^/mcp($|/)\n")
    assert config_lane.scan("apps.yml", document, read=_reader({})) == []


# --- rule: two services claiming one host port -------------------------------

def test_two_services_on_one_host_port_are_flagged_once():
    hits = config_lane.scan("compose.yml", COMPOSE_PORT_CLASH, read=_reader({}))
    clash = [h for h in hits if h.rule_id == "compose-duplicate-host-port"]
    assert len(clash) == 1
    assert "8002" in clash[0].message


def test_a_bare_container_port_claims_no_host_slot():
    """`- "8080"` declares only the container port; Docker picks the host port.
    Treating the container port as a claimed host port invented a collision that
    cannot happen, and a blocking finding for a collision that cannot happen is
    worse than no rule."""
    compose = """\
services:
  a:
    image: x
    ports:
      - "8080"
  b:
    image: y
    ports:
      - "8080"
"""
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert not [h for h in hits if h.rule_id == "compose-duplicate-host-port"]


def test_a_bare_datastore_port_is_still_published_on_every_interface():
    compose = 'services:\n  db:\n    image: postgres\n    ports:\n      - "5432"\n'
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert _ids(hits) == ["compose-datastore-public-bind"]
    assert "as 5432" not in hits[0].message


def test_the_same_container_port_on_two_hosts_ports_is_fine():
    compose = COMPOSE_PORT_CLASH.replace('"127.0.0.1:8002:8080"',
                                         '"127.0.0.1:8003:8080"')
    hits = config_lane.scan("compose.yml", compose, read=_reader({}))
    assert not [h for h in hits if h.rule_id == "compose-duplicate-host-port"]


# --- the finding contract ----------------------------------------------------

def test_a_hit_becomes_a_verified_finding_that_skips_the_llm_verifier():
    hits = config_lane.scan("compose.yml", COMPOSE_SKIP_AUTH, read=_reader({}))
    finding = config_lane.to_finding(hits[0])
    assert finding.lane == config_lane.LANE
    assert finding.verdict.is_real is True
    assert finding.verdict.confidence == 1.0
    assert finding.finding.rule_id == "auth-skip-unanchored"
    assert finding.finding.line >= 1


def test_every_rule_describes_its_failure_class():
    """Keep the reason for each rule available alongside its identifier."""
    for rule_id, incident in config_lane.RULE_INCIDENTS.items():
        assert incident, rule_id
        assert rule_id in config_lane.rule_ids()


def test_tool_identity_records_the_active_rule_set():
    identity = config_lane.tool_identity()
    assert identity["name"] == config_lane.LANE
    assert set(config_lane.rule_ids()) <= set(identity["rules"].split(","))
