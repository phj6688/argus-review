import json
import re

import pytest

from argus import publish, risk_tier, verdict
from argus.markdown import code_span, escape_inline

HEAD = "a" * 40


def document():
    return {"verdict": "comment", "findings": []}


def risk(paths=(), lines=0, **kwargs):
    facts = risk_tier.classify(paths, lines, **kwargs).to_dict()
    return risk_tier.from_row({"risk_tier": facts["tier"], "risk_json": json.dumps(facts)})


@pytest.mark.parametrize("extra", [{}, {"risk": None}])
def test_absent_risk_preserves_exact_body(extra):
    body = publish.render_review(HEAD, 7, document() | extra, [])["body"]
    assert body == (
        "**Argus review** for aaaaaaa\n\n"
        "Verdict: comment\n\nCoverage: unknown\n\nFiles reviewed: 0; omitted: 0\n\n"
        "Severity counts: Critical: 0; Major: 0; Minor: 0; Nit: 0\n\n"
        "Tier counts: deterministic: 0; consensus: 0; single_critic: 0; disputed: 0\n\n"
        "Below posting threshold: 0\n\nAlready posted: 0\n\n"
        f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->"
    )


@pytest.mark.parametrize("paths,lines,tier", [
    (["src/main.py"], 300, "safe"),
    (["deploy/docker-compose.prod.yml"], 412, "risky"),
])
def test_tier_line_immediately_follows_tier_counts(paths, lines, tier):
    facts = risk(paths, lines)
    body = publish.render_review(HEAD, 7, document() | {"risk": facts}, [])["body"]
    paragraphs = body.split("\n\n")
    index = next(i for i, text in enumerate(paragraphs) if text.startswith("Tier counts:"))
    line = paragraphs[index + 1]
    assert line.startswith(risk_tier.label(tier))
    assert not re.search(r"no defect|clean|approved", line, re.I)
    assert "\n" not in line
    if tier == "risky":
        assert (
            " Reasons: tripwire path `deploy/docker-compose.prod.yml` (docker\\-compose\\*); "
            "412 changed lines exceed the 300 line limit"
        ) in line
    else:
        assert line == risk_tier.LABEL_SAFE


def test_risky_reason_paths_are_literal_code_spans():
    path = "deploy/auth`[x]<img>.yml"
    body = publish.render_review(HEAD, 7, document() | {"risk": risk([path])}, [])["body"]
    assert code_span(path) in body
    assert "(*auth*)" not in body


def test_incomplete_risk_reason_and_size_only_reason():
    facts = risk([], 301, incomplete=True)
    body = publish.render_review(HEAD, 7, document() | {"risk": facts}, [])["body"]
    assert "Reasons: 301 changed lines exceed the 300 line limit; " in body
    assert escape_inline("file list incomplete, an unseen path might match") in body


def test_risk_line_is_included_in_unverified_body_budget(monkeypatch):
    item = {
        "file": "a.py", "line": 1, "end_line": None, "category": "contracts",
        "severity": "major", "evidence_tier": "single_critic", "title": "Check the input",
        "description": "x" * 200, "proposed_fix": "Validate.", "fingerprint": "fp-one",
    }
    doc = document() | {"unverified": [item]}
    baseline = publish.render_review(HEAD, 7, doc, [])["body"]
    monkeypatch.setattr(publish, "_MAX_BODY", len(baseline))
    body = publish.render_review(HEAD, 7, doc | {"risk": risk(["nginx.conf"])}, [])["body"]
    assert risk_tier.LABEL_RISKY in body
    assert "1 more unverified findings are not listed." in body
    assert "Check the input" not in body
    assert len(body) <= publish._MAX_BODY
    assert body.endswith(f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->")


def test_verdict_risk_is_full_and_does_not_change_decision():
    facts = risk(["nginx.conf"])
    row = {"id": 7, "risk_tier": "risky", "risk_json": json.dumps(facts)}
    result = verdict.document(row, [])
    assert result["risk"] == facts
    assert result["verdict"] == verdict.document({"id": 7}, [])["verdict"]


def test_legacy_verdict_risk_is_null():
    assert verdict.document({"id": 7}, [])["risk"] is None
