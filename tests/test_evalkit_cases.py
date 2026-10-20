import json

import pytest

from argus.evalkit.cases import load_cases


def case_data(case_id="sample"):
    return {
        "case_id": case_id, "repo": "owner/project", "pr": 7,
        "base_sha": "abc", "head_sha": "def", "split": "heldout",
        "expected": [{"id": case_id + "-e1", "file": "a.py", "start_line": 10,
                      "end_line": 12, "severity": "major", "cls": "logic",
                      "summary": "Bad branch", "source": "manual"}],
        "negatives": [],
    }


def write_cases(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_loader_defaults_and_negative_sources(tmp_path):
    data = case_data()
    data["negatives"] = [{**data["expected"][0], "id": "n1", "source": "known_fp"}]
    case = load_cases(write_cases(tmp_path / "cases", [data]))[0]
    assert case.pr_title is None
    assert case.expected[0].weight == 1.0
    assert case.negatives[0].source == "known_fp"


def test_loader_accepts_a_change_without_a_pull_request(tmp_path):
    data = {**case_data(), "pr": 0}
    assert load_cases(write_cases(tmp_path / "cases", [data]))[0].pr == 0


@pytest.mark.parametrize("field,value", [
    ("split", "train"), ("repo", "project"), ("pr", True), ("pr", -1),
    ("base_sha", ""), ("expected", {}), ("pr_body", 42),
])
def test_loader_case_errors_name_case_and_field(tmp_path, field, value):
    data = {**case_data(), field: value}
    with pytest.raises(ValueError, match=f"sample.*{field}"):
        load_cases(write_cases(tmp_path / "cases", [data]))


@pytest.mark.parametrize("field,value", [
    ("start_line", 0), ("end_line", 9), ("severity", "urgent"),
    ("source", "known_fp"), ("weight", -1), ("weight", float("nan")),
    ("weight", True), ("file", ""), ("id", ""), ("cls", None),
])
def test_loader_expectation_errors_name_case_and_field(tmp_path, field, value):
    data = case_data()
    data["expected"][0][field] = value
    with pytest.raises(ValueError, match=f"sample.*expected.*{field}"):
        load_cases(write_cases(tmp_path / "cases", [data]))


def test_loader_missing_duplicate_and_malformed(tmp_path):
    data = case_data()
    del data["head_sha"]
    with pytest.raises(ValueError, match="sample.*head_sha"):
        load_cases(write_cases(tmp_path / "cases", [data]))
    with pytest.raises(ValueError, match="sample.*case_id"):
        load_cases(write_cases(tmp_path / "cases", [case_data(), case_data()]))
    data = case_data()
    data["expected"].append(data["expected"][0])
    with pytest.raises(ValueError, match="sample.*id"):
        load_cases(write_cases(tmp_path / "cases", [data]))
    (tmp_path / "cases").write_text('{"case_id":"broken", invalid}\n')
    with pytest.raises(ValueError, match="broken.*JSON"):
        load_cases(tmp_path / "cases")


def test_loader_oversized_weight_names_field(tmp_path):
    data = case_data()
    data["expected"][0]["weight"] = 10**400
    with pytest.raises(ValueError, match="sample.*expected.*weight"):
        load_cases(write_cases(tmp_path / "cases", [data]))
