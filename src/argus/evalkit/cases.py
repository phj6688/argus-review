"""Labelled historical changes, with errors scoped to a case and field."""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Expectation:
    id: str
    file: str
    start_line: int
    end_line: int
    severity: str
    cls: str
    summary: str
    source: str
    weight: float = 1.0


@dataclass(frozen=True)
class Case:
    case_id: str
    repo: str
    pr: int
    base_sha: str
    head_sha: str
    split: str
    expected: list[Expectation] = field(default_factory=list)
    negatives: list[Expectation] = field(default_factory=list)
    pr_title: str | None = None
    pr_body: str | None = None


def normalize_path(path: str) -> str:
    path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:].lstrip("/")
    return path


def _fail(case_id: str, field: str, reason: str):
    raise ValueError(f"case {case_id}: {field}: {reason}")


def _text(data: dict, key: str, case_id: str, prefix: str = "") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(case_id, prefix + key, "must be a nonempty string")
    return value


def _expectations(data: dict, key: str, case_id: str, ids: set[str]) -> list[Expectation]:
    values = data.get(key)
    if not isinstance(values, list):
        _fail(case_id, key, "must be an array")
    sources = {"cr_accepted", "incident", "szz", "manual"} if key == "expected" else {
        "cr_rejected", "known_fp"}
    result = []
    for index, value in enumerate(values):
        prefix = f"{key}[{index}]."
        if not isinstance(value, dict):
            _fail(case_id, prefix, "must be an object")
        strings = {k: _text(value, k, case_id, prefix)
                   for k in ("id", "file", "severity", "cls", "summary", "source")}
        if strings["id"] in ids:
            _fail(case_id, prefix + "id", "must be unique across the case file")
        ids.add(strings["id"])
        if strings["severity"] not in {"critical", "major", "minor", "nit"}:
            _fail(case_id, prefix + "severity", "invalid severity")
        if strings["source"] not in sources:
            _fail(case_id, prefix + "source", "invalid source")
        for k in ("start_line", "end_line"):
            if type(value.get(k)) is not int or value[k] < 1:
                _fail(case_id, prefix + k, "must be a positive integer")
        if value["end_line"] < value["start_line"]:
            _fail(case_id, prefix + "end_line", "must be at least start_line")
        weight = value.get("weight", 1.0)
        if type(weight) not in (int, float):
            _fail(case_id, prefix + "weight", "must be finite and nonnegative")
        try:
            weight = float(weight)
        except OverflowError:
            _fail(case_id, prefix + "weight", "must be finite and nonnegative")
        if not math.isfinite(weight) or weight < 0:
            _fail(case_id, prefix + "weight", "must be finite and nonnegative")
        unknown = value.keys() - Expectation.__dataclass_fields__.keys()
        if unknown:
            _fail(case_id, prefix + sorted(unknown)[0], "unknown field")
        result.append(Expectation(**strings, start_line=value["start_line"],
                                  end_line=value["end_line"], weight=float(weight)))
    return result


def load_cases(path: str | Path) -> list[Case]:
    cases = []
    seen: set[str] = set()
    expectation_ids: set[str] = set()
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError as error:
                match = re.search(r'"case_id"\s*:\s*"([^"\\]+)"', line)
                _fail(match[1] if match else f"<line {number}>", "JSON", str(error))
            if not isinstance(data, dict):
                _fail(f"<line {number}>", "JSON", "must be an object")
            case_id = _text(data, "case_id", f"<line {number}>")
            if case_id in seen:
                _fail(case_id, "case_id", "duplicate case")
            seen.add(case_id)
            for key in ("repo", "base_sha", "head_sha", "split"):
                _text(data, key, case_id)
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", data["repo"], flags=re.ASCII) or (
                data["repo"].split("/")[1] in {".", ".."}
            ):
                _fail(case_id, "repo", "must be owner/name")
            if data["split"] not in {"dev", "heldout"}:
                _fail(case_id, "split", "must be dev or heldout")
            for key in ("base_sha", "head_sha"):
                if not re.fullmatch(r"[0-9a-fA-F]{1,64}", data[key]):
                    _fail(case_id, key, "must be a hexadecimal commit id")
            # 0 marks a change that never went through a pull request (a direct commit).
            if type(data.get("pr")) is not int or data["pr"] < 0:
                _fail(case_id, "pr", "must be a non-negative integer")
            for key in ("pr_title", "pr_body"):
                if data.get(key) is not None and not isinstance(data[key], str):
                    _fail(case_id, key, "must be a string or null")
            unknown = data.keys() - Case.__dataclass_fields__.keys()
            if unknown:
                _fail(case_id, sorted(unknown)[0], "unknown field")
            expected = _expectations(data, "expected", case_id, expectation_ids)
            negatives = _expectations(data, "negatives", case_id, expectation_ids)
            cases.append(Case(**{k: v for k, v in data.items() if k not in {
                "expected", "negatives"}}, expected=expected, negatives=negatives))
    return cases
