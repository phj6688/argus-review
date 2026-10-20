import pytest

from argus import schema
from argus.schema import Severity


GOOD = {"file": "a.py", "line": 2, "severity": "major", "confidence": 0.9,
        "title": "Wrong result", "description": "Returns the wrong result. More detail.",
        "proposed_fix": "Correct the return value", "end_line": None,
        "category": "contracts", "evidence": "return wrong"}


def test_parse_findings_preserves_six_findings_without_titles():
    payload = [{k: v for k, v in GOOD.items() if k != "title"} for _ in range(6)]
    findings, stats = schema.parse_findings({"findings": payload})
    assert len(findings) == 6
    assert {f.title for f in findings} == {"Returns the wrong result."}
    assert stats == {"findings_received": 6, "findings_repaired": 6, "findings_dropped": 0}


def test_parse_findings_repairs_each_item_once_and_drops_unlocated_items():
    findings, stats = schema.parse_findings({"findings": [
        GOOD, {k: v for k, v in GOOD.items() if k != "file"},
        {**GOOD, "line": 0, "severity": "MAJOR"}, {**GOOD, "confidence": 85},
    ]})
    assert len(findings) == 3
    assert findings[1].line == 1
    assert findings[1].severity is Severity.major
    assert findings[2].confidence == 0.85
    assert stats == {"findings_received": 4, "findings_repaired": 2, "findings_dropped": 1}


@pytest.mark.parametrize("payload", [None, [], {}, {"findings": None}, {"findings": {}}])
def test_parse_findings_rejects_missing_or_invalid_review_output(payload):
    with pytest.raises(ValueError):
        schema.parse_findings(payload)


@pytest.mark.parametrize("item", [None, "bad", {}, {**GOOD, "file": "  "},
                                   {**GOOD, "title": " ", "description": ""}])
def test_parse_findings_drops_unusable_items_without_losing_good_ones(item):
    findings, stats = schema.parse_findings({"findings": [item, GOOD]})
    assert [f.title for f in findings] == ["Wrong result"]
    assert stats == {"findings_received": 2, "findings_repaired": 0, "findings_dropped": 1}


@pytest.mark.parametrize("fields, expected", [
    ({"line": "12"}, {"line": 12}),
    ({"line": None}, {"line": 1}),
    ({"line": "bad"}, {"line": 1}),
    ({"line": -3}, {"line": 1}),
    ({"line": 2.5}, {"line": 1}),
    ({"severity": None}, {"severity": Severity.minor}),
    ({"severity": "unknown"}, {"severity": Severity.minor}),
    ({"confidence": -5}, {"confidence": 0.0}),
    ({"confidence": 101}, {"confidence": 1.0}),
    ({"confidence": 100}, {"confidence": 1.0}),
    ({"rule_id": "null"}, {"rule_id": None}),
    ({"rule_id": "  "}, {"rule_id": None}),
    ({"description": "  "}, {"description": "Wrong result"}),
    ({"title": " ", "description": "x" * 150}, {"title": "x" * 120}),
])
def test_parse_findings_normalizes_fields(fields, expected):
    findings, stats = schema.parse_findings({"findings": [{**GOOD, **fields}]})
    for key, value in expected.items():
        assert getattr(findings[0], key) == value
    assert stats["findings_repaired"] == 1


def test_parse_findings_defaults_missing_fields_and_ignores_unknown_keys():
    findings, stats = schema.parse_findings({"findings": [
        {"file": "a.py", "title": "Wrong result", "extra": "ignored"},
    ]})
    assert findings[0].model_dump() == {
        "file": "a.py", "line": 1, "severity": Severity.minor, "confidence": 0.5,
        "title": "Wrong result", "description": "Wrong result", "proposed_fix": "",
        "rule_id": None, "end_line": None, "category": "other", "evidence": "",
    }
    assert stats == {"findings_received": 1, "findings_repaired": 1, "findings_dropped": 0}


def test_parse_findings_accepts_an_explicit_empty_list():
    findings, stats = schema.parse_findings({"findings": []})
    assert findings == []
    assert stats == {"findings_received": 0, "findings_repaired": 0, "findings_dropped": 0}


def test_parse_findings_ignores_unknown_keys_without_counting_a_repair():
    findings, stats = schema.parse_findings({"findings": [{**GOOD, "unknown": "ignored"}]})
    assert [f.title for f in findings] == ["Wrong result"]
    assert stats == {"findings_received": 1, "findings_repaired": 0, "findings_dropped": 0}


@pytest.mark.parametrize("fields", [{"rule_id": {}}])
def test_parse_findings_isolates_remaining_validation_errors(fields):
    findings, stats = schema.parse_findings({"findings": [{**GOOD, **fields}, GOOD]})
    assert [f.title for f in findings] == ["Wrong result"]
    assert stats == {"findings_received": 2, "findings_repaired": 0, "findings_dropped": 1}


@pytest.mark.parametrize("severity, expected", [
    ("High", "major"), ("critical ", "critical"), ("weird", "minor"),
    ("blocker", "critical"), ("medium", "minor"), ("moderate", "minor"),
    ("low", "nit"), ("info", "nit"), ("trivial", "nit"),
])
def test_severity_aliases_are_repaired(severity, expected):
    findings, stats = schema.parse_findings({"findings": [{**GOOD, "severity": severity}]})
    assert findings[0].severity == expected
    assert stats["findings_repaired"] == 1


@pytest.mark.parametrize("line", [2**31, 2**63])
def test_oversized_lines_are_repaired(line):
    findings, stats = schema.parse_findings({"findings": [{**GOOD, "line": line, "end_line": line}]})
    assert findings[0].line == 1
    assert findings[0].end_line is None
    assert stats["findings_repaired"] == 1
