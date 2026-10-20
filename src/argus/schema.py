"""Structured-output schema for findings.

The model emits findings against this schema (validated, not free text). A
finding may cite a rule by id, but the rule index is the only place a rule is
defined, so an invented rule_id is rejected downstream.
"""

import math
import re
from enum import Enum
from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    ValidationError,
    field_validator,
    model_validator,
)


class Severity(str, Enum):
    critical = "critical"
    major = "major"
    minor = "minor"
    nit = "nit"


CATEGORIES = (
    "contracts", "error_paths", "input_handling", "boundaries", "resources",
    "concurrency", "security", "tests", "data_integrity", "other",
)


class Finding(BaseModel):
    file: str
    line: int = Field(ge=1)
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    title: str
    description: str
    proposed_fix: str
    rule_id: str | None = None
    end_line: int | None = None
    category: str | None = None
    evidence: str = ""


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[Finding] = Field(default_factory=list)
    parsing_stats: dict[str, int | list[int]] = Field(default_factory=dict, exclude=True)


def parse_findings(data: object) -> tuple[list[Finding], dict[str, int]]:
    """Repair recoverable fields without letting one item discard its siblings."""
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise ValueError("review output must contain a findings list")
    items = data["findings"]
    stats = {"findings_received": len(items), "findings_repaired": 0, "findings_dropped": 0}
    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            stats["findings_dropped"] += 1
            continue
        file = item.get("file")
        if not isinstance(file, str) or not file.strip():
            stats["findings_dropped"] += 1
            continue
        title = item.get("title")
        description = item.get("description")
        title = title.strip() if isinstance(title, str) else ""
        description = description.strip() if isinstance(description, str) else ""
        if not title:
            if not description:
                stats["findings_dropped"] += 1
                continue
            title = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][:120].strip()
        line = item.get("line")
        if isinstance(line, str) and line.isdecimal():
            line = int(line)
        if isinstance(line, float) and line.is_integer():
            line = int(line)
        if type(line) is not int or not 1 <= line <= 2**31 - 1:
            line = 1
        severity = item.get("severity")
        if isinstance(severity, str):
            severity = severity.strip().lower()
            severity = {"blocker": "critical", "high": "major", "medium": "minor",
                        "moderate": "minor", "low": "nit", "info": "nit", "trivial": "nit"}.get(
                            severity, severity)
        try:
            severity = Severity(severity) if isinstance(severity, str) else Severity.minor
        except ValueError:
            severity = Severity.minor
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError, OverflowError):
            confidence = 0.5
        if math.isnan(confidence):
            confidence = 0.5
        if 1 < confidence <= 100:
            confidence /= 100
        confidence = max(0.0, min(1.0, confidence))
        rule_id = item.get("rule_id")
        if isinstance(rule_id, str):
            rule_id = rule_id.strip()
            if not rule_id or rule_id == "null":
                rule_id = None
        end_line = item.get("end_line")
        if type(end_line) is not int or not line <= end_line <= 2**31 - 1:
            end_line = None
        category = item.get("category")
        if category not in CATEGORIES:
            category = "other"
        evidence = item.get("evidence")
        if not isinstance(evidence, str):
            evidence = ""
        proposed_fix = item.get("proposed_fix")
        if not isinstance(proposed_fix, str):
            proposed_fix = ""
        normalized = {
            "file": file.strip(), "line": line, "severity": severity, "confidence": confidence,
            "title": title, "description": description or title,
            "proposed_fix": proposed_fix, "rule_id": rule_id,
            "end_line": end_line, "category": category, "evidence": evidence,
        }
        try:
            finding = Finding.model_validate(normalized)
        except ValidationError:
            stats["findings_dropped"] += 1
            continue
        repaired = any(item.get(key) != value for key, value in normalized.items())
        repaired = repaired or any(key not in item for key in ("end_line", "category", "evidence"))
        repaired = repaired or (item.get("end_line") is not None
                                and type(item["end_line"]) is not int)
        repaired = repaired or type(item.get("line")) is not int
        stats["findings_repaired"] += int(repaired)
        findings.append(finding)
    return findings, stats


class Verdict(BaseModel):
    is_real: bool
    confidence: float = Field(ge=0.0, le=1.0)
    # Judges sometimes omit reasoning despite the tool schema requiring it; an
    # empty default keeps the review alive instead of dying on ValidationError.
    reasoning: str = ""


class CounterEvidence(BaseModel):
    """The code a refutation quotes against a finding."""

    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(pattern=r"\S")

    @model_validator(mode="after")
    def _ordered_lines(self) -> "CounterEvidence":
        if self.end_line < self.start_line:
            raise ValueError("end_line is before start_line")
        return self


class CounterEvidenceVerdict(Verdict):
    """A verdict from a variant whose schema asks for quoted counter-evidence.

    Unusable counter-evidence parses to None instead of failing the verdict: a
    retry would not make the model quote better, and a refutation without a
    quote is exactly what the quote check exists to void.
    """

    counter_evidence: CounterEvidence | None = None
    # Set by the quote check, never by the model: why a refutation was voided and the
    # finding kept, and whether the cause was the model's quote or an unreadable file.
    # The strict tool schema has no such fields and _verify drops them from a reply.
    refutation_voided: str | None = None
    refutation_void_kind: str | None = None

    SERVER_FIELDS: ClassVar[tuple[str, ...]] = ("refutation_voided", "refutation_void_kind")

    @field_validator("counter_evidence", mode="wrap")
    @classmethod
    def _drop_unusable(cls, value, handler):
        try:
            return handler(value)
        except ValidationError:
            return None


class JudgeOpinion(BaseModel):
    """Second opinion from the decorrelated (non-Anthropic) judge on one finding."""

    verdict: str  # real | fp | unsure
    confidence: float = 0.0
    model: str = ""


class VerifiedFinding(BaseModel):
    finding: Finding
    # SerializeAsAny: a CounterEvidenceVerdict would otherwise serialize as a plain
    # Verdict and drop its quote from every dump and replay row.
    verdict: SerializeAsAny[Verdict]
    # llm | probe: which lane produced the finding. Evidence tiers key on this
    # (probe = deterministic-grade), so only the probe path overrides the default.
    lane: str = "llm"
    judge: JudgeOpinion | None = None
    fingerprint: str = ""
