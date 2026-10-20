"""The config lane inside a review: what it contributes, and what it admits.

The lane's findings must reach the result, its coverage record must appear in the
document even when it had nothing to read, and fast mode must run it. Fast mode
is the reason this whole line of work started: a caller reading `verdict` could
not tell "a reviewer read this and passed it" from "nothing was run".
"""

import pytest

from argus import review as review_mod
from argus import verdict
from argus.diff import FilePatch
from argus.review import run_diff_review
from argus.schema import Review, Verdict

VULNERABLE = """\
services:
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy
    environment:
      OAUTH2_PROXY_SKIP_AUTH_ROUTES: "^/health$,^/mcp"
  neo4j:
    image: neo4j:5.26
    ports:
      - "7697:7687"
"""

PATCH = "@@ -1,2 +1,3 @@\n services:\n+  neo4j:\n"


def _files(*names):
    return [FilePatch(filename=n, status="modified", patch=PATCH) for n in names]


def _lane(result, name):
    return next((r for r in result.coverage if r["lane"] == name), None)


@pytest.fixture()
def quiet_llm(monkeypatch):
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda f, d, m, **_: Verdict(is_real=True, confidence=0.9,
                                                reasoning="r"))
    monkeypatch.setattr(review_mod, "fetch_house_rules", lambda repo: None)


@pytest.fixture()
def whole_files(monkeypatch):
    from argus.jarvis import live
    monkeypatch.setattr(live, "_fetch_file_at",
                        lambda repo, path, sha: VULNERABLE)


@pytest.fixture()
def no_probes(monkeypatch):
    from argus.jarvis import live
    monkeypatch.setattr(live, "probe_scan_for_files",
                        lambda repo, sha, files: _EmptyScan())


class _EmptyScan:
    findings: list = []
    coverage = {"lane": "probe", "status": verdict.LANE_RAN, "checks": 0}


def test_the_lane_reports_its_findings_into_the_review(
        quiet_llm, whole_files, no_probes):
    result = run_diff_review(_files("docker-compose.yml"), repo="o/r",
                             head_sha="abc123")
    rules = {f.finding.rule_id for f in result.bugs + result.nits}
    assert "auth-skip-unanchored" in rules
    assert "compose-datastore-public-bind" in rules


def test_a_config_finding_carries_the_deterministic_tier(
        quiet_llm, whole_files, no_probes):
    result = run_diff_review(_files("docker-compose.yml"), repo="o/r",
                             head_sha="abc123")
    hit = next(f for f in result.bugs + result.nits
               if f.finding.rule_id == "auth-skip-unanchored")
    assert verdict.evidence_tier(hit.lane, None) == verdict.TIER_DETERMINISTIC


def test_the_lane_appears_in_coverage_even_with_no_yaml_to_read(
        quiet_llm, whole_files, no_probes):
    """A lane missing from the document is a lane a reader has to already know
    about in order to notice is missing."""
    result = run_diff_review(_files("src/app.py"), repo="o/r", head_sha="abc123")
    record = _lane(result, "config")
    assert record is not None
    assert record["status"] == verdict.LANE_SKIPPED
    assert record["reason"]


def test_fast_mode_runs_every_deterministic_lane(whole_files, no_probes):
    """Fast mode's contract is the deterministic lanes, plural. Running one of
    three while the docstring promised all of them is the same overstatement
    this project spent a day removing from the verdict document."""
    result = run_diff_review(_files("docker-compose.yml"), repo="o/r",
                             head_sha="abc123", fast=True)
    lanes = {r["lane"] for r in result.coverage}
    assert {"probe", "ruff", "config"} <= lanes
    assert "auth-skip-unanchored" in {f.finding.rule_id
                                      for f in result.bugs + result.nits}


def test_fast_mode_no_longer_approves_a_diff_it_found_a_bypass_in(
        whole_files, no_probes):
    result = run_diff_review(_files("docker-compose.yml"), repo="o/r",
                             head_sha="abc123", fast=True)
    coverage = verdict.coverage_level(result.coverage)
    findings = [{"verdict_is_real": True, "is_bug": True, "lane": f.lane,
                 "severity": f.finding.severity.value} for f in result.bugs]
    assert verdict.decision(findings, {"level": coverage}) == "changes_requested"


def test_one_unparseable_file_does_not_discard_what_the_others_found(
        quiet_llm, no_probes, monkeypatch):
    """A Helm template or a Jinja-rendered workflow does not parse as YAML, and
    both use .yml. Letting the first of them abandon the loop threw away the
    critical findings already collected from the compose file beside it: a
    partial failure destroying valid evidence, which is the defect class this
    whole lane was built from."""
    from argus.jarvis import live
    bodies = {"compose.yml": VULNERABLE,
              "chart/templates/deploy.yaml": "a:\n  b:\n \t- broken: ["}
    monkeypatch.setattr(live, "_fetch_file_at",
                        lambda repo, path, sha: bodies[path])
    result = run_diff_review(_files("compose.yml", "chart/templates/deploy.yaml"),
                             repo="o/r", head_sha="abc123")
    rules = {f.finding.rule_id for f in result.bugs + result.nits}
    assert "auth-skip-unanchored" in rules

    record = _lane(result, "config")
    assert record["status"] == verdict.LANE_ERROR
    assert record["files_analyzed"] == 1
    assert "chart/templates/deploy.yaml" in record["reason"]
    assert verdict.coverage_level(result.coverage) != verdict.COVERAGE_FULL


def test_the_skip_reason_describes_this_lane_not_another_one(
        quiet_llm, no_probes):
    """The coverage document is read by someone who does not already know which
    lane does what, so a config skip explained by ruff's parser is not an
    explanation."""
    result = run_diff_review(_files("compose.yml"), repo=None, head_sha=None)
    record = _lane(result, "config")
    assert record["status"] == verdict.LANE_SKIPPED
    assert "ruff" not in record["reason"]


def test_an_unparseable_file_is_an_error_lane_not_a_clean_one(
        quiet_llm, no_probes, monkeypatch):
    """The law this project learned the hard way: absence of evidence is not a
    pass. A file the lane could not parse must degrade coverage, not vanish."""
    from argus.jarvis import live
    monkeypatch.setattr(live, "_fetch_file_at",
                        lambda repo, path, sha: "a:\n  b:\n \t- broken: [")
    result = run_diff_review(_files("compose.yml"), repo="o/r", head_sha="abc123")
    record = _lane(result, "config")
    assert record["status"] == verdict.LANE_ERROR
    assert verdict.coverage_level(result.coverage) != verdict.COVERAGE_FULL
