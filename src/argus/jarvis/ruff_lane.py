"""Ruff as a review lane: findings whose truth needs nobody's opinion.

Every other lane in this system rests on a model's judgement. Measured on this
operator's own corpus, that judgement does not converge: two decorrelated judges
were confident together on 29 of 77 bug-class findings and split on 9 of those,
and the estimated false-positive rate ranged from 44% to 92% depending purely on
which model was asked. No number of extra judges fixes that, because the
disagreement is the method, not the sample.

A ruff rule is different in kind. Given a pinned binary, a pinned rule selection
and the same bytes, it fires or it does not, and anyone can re-run it and get the
same answer. That reproducibility is what earns the `deterministic` tier — not
the fact that a tool rather than a model produced it.

Three deliberate choices, each closing a way the lane could lie about itself:

`--isolated` so a repository's own config cannot switch a gate rule off.
`--ignore-noqa` so code under review cannot suppress the reviewer; a `# noqa`
added by the diff is the change asking not to be looked at, which is exactly
when looking matters. `--no-respect-gitignore` so an ignored path is still read.

Content goes over stdin rather than a temp tree. There is no directory to escape
from, which retires the path-traversal class that the probe lane had to be fixed
for, and `files_analyzed` is then whatever the caller handed over rather than
whatever the tool decided to look at.
"""

import json
import os
import subprocess
import sys

from ..schema import Finding, Severity, Verdict, VerifiedFinding

LANE = "ruff"

ENV_RULES = "ARGUS_RUFF_RULES"
ENV_BIN = "ARGUS_RUFF_BIN"

# E9 is syntax and unparseable-file errors, F821 an undefined name (a NameError
# waiting to happen), S the flake8-bandit security set.
DEFAULT_RULES = "E9,F821,S"

# Measured on this codebase before shipping, not guessed. Over src+tests the full
# selection produced 834 hits: 827 were S101 and every one of them was an `assert`
# in a test file, where asserting is the point. Of the remaining 7, three were
# S105 firing on constants that merely NAME an environment variable
# (`ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"`), a pattern used throughout these
# repos, and two were S603 flagging this module's own subprocess call. That left
# two genuine leads, both S608.
#
# Shipping the untuned set would have buried a real finding under 400 false ones
# per review and taught the operator to skip the lane inside a week. These three
# rules are excluded on evidence; the rest of S, including the injection rules
# this lane exists for, stays. The exclusion travels in the lane's coverage
# record, so a narrowed gate is never invisible.
DEFAULT_IGNORE = "S101,S105,S603"
ENV_IGNORE = "ARGUS_RUFF_IGNORE"

# Blocking from day one. All are mechanical facts about the file rather than
# judgements about intent: the code does not parse, or it references a name that
# does not exist. The security rules ship as warnings until their yield on this
# corpus is measured; shipping an unmeasured rule as a blocker is how a reviewer
# earns the habit of being ignored.
#
# `invalid-syntax` is how ruff 0.15 reports an unparseable file; earlier versions
# used E902/E999. Both spellings are listed so a version move in either direction
# cannot quietly turn a file that does not compile into a clean result.
BLOCKING_PREFIXES = ("E9", "F821", "invalid-syntax")

PYTHON_SUFFIXES = (".py", ".pyi")
_TIMEOUT_SECONDS = 60
_REMOVED = "removed"


class RuffUnavailable(RuntimeError):
    """The lane could not produce a trustworthy answer.

    Raised instead of returning no findings, because "the binary is missing" and
    "the file is clean" are different claims and only one of them is evidence.
    """


class Hit:
    __slots__ = ("code", "message", "file", "line", "url", "end_line")

    def __init__(self, code: str, message: str, file: str, line: int, url: str = "",
                 end_line: int | None = None):
        self.code, self.message, self.file, self.line, self.url = (
            code, message, file, line, url)
        self.end_line = line if end_line is None else end_line

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Hit({self.code} {self.file}:{self.line})"


def rules() -> str:
    return os.environ.get(ENV_RULES) or DEFAULT_RULES


def ignored() -> str:
    return os.environ.get(ENV_IGNORE, DEFAULT_IGNORE)


RULES = DEFAULT_RULES


def command() -> list[str]:
    """How to invoke the ruff this project pinned.

    Via the running interpreter, not PATH. ruff is a pinned dependency of this
    environment, and PATH may hold an entirely different one: on the box this was
    written, `which ruff` resolved 0.15.10 while the venv held the pinned 0.15.17.
    The deterministic tier claims a finding is re-executable against a known
    binary, so running whichever ruff happened to be first on PATH would make the
    recorded version a fiction. ARGUS_RUFF_BIN overrides for odd deployments.
    """
    override = os.environ.get(ENV_BIN)
    if override:
        return [override]
    return [sys.executable, "-m", "ruff"]


def eligible(files: list) -> list[str]:
    """Changed Python files the lane can read. A deletion has no post-image."""
    return [f.filename for f in files
            if f.status != _REMOVED and f.filename.endswith(PYTHON_SUFFIXES)]


def is_blocking(code: str) -> bool:
    return code.startswith(BLOCKING_PREFIXES)


def _args() -> list[str]:
    args = ["check", "--isolated", "--no-respect-gitignore", "--ignore-noqa",
            "--select", rules()]
    if ignored():
        args += ["--ignore", ignored()]
    return [*args, "--output-format", "json"]


def tool_identity() -> dict:
    """What a re-run would need. The tier claim is re-executability, so the
    version and the exact invocation are part of the record."""
    try:
        version = subprocess.run([*command(), "--version"], capture_output=True,
                                 text=True, timeout=_TIMEOUT_SECONDS).stdout.strip()
    except Exception:  # noqa: BLE001 - reported, never raised into the review
        version = ""
    return {"name": LANE, "version": version, "rules": rules(),
            "ignored": ignored(), "args": " ".join(_args())}


def _run(_unused, content: str, path: str) -> str:
    try:
        result = subprocess.run(
            [*command(), *_args(), "--stdin-filename", path, "-"],
            input=content, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except FileNotFoundError as error:
        raise RuffUnavailable(f"{command()} not runnable: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise RuffUnavailable(f"ruff timed out on {path}") from error
    return result.stdout


def scan_content(path: str, content: str) -> list[Hit]:
    """Run the pinned rule set over one file's whole content.

    Whole content, never a patch fragment: ruff parses the file, and a fragment
    would report a syntax error on every truncated function. That is the same
    trap the file probes fall into on raw patches, so the lane declines rather
    than guessing.
    """
    raw = _run(None, content, path)
    if not raw.strip():
        return []
    try:
        entries = json.loads(raw)
    except ValueError as error:
        raise RuffUnavailable(
            f"ruff output was not JSON for {path}: {raw[:200]!r}") from error
    return [
        Hit(code=e.get("code") or "", message=e.get("message") or "",
            file=path, line=int((e.get("location") or {}).get("row") or 1),
            url=e.get("url") or "",
            end_line=int((e.get("end_location") or {}).get("row")
                         or (e.get("location") or {}).get("row") or 1))
        for e in entries if e.get("code")
    ]


def to_finding(hit: Hit) -> VerifiedFinding:
    """A hit as a verified finding. It skips the LLM verifier on purpose: the
    check already executed, and asking a model whether ruff was right would put
    an opinion back in front of the one lane that does not need one."""
    severity = Severity.critical if is_blocking(hit.code) else Severity.nit
    detail = f"{hit.message} ({hit.code})"
    finding = Finding(
        file=hit.file, line=hit.line, severity=severity, confidence=1.0,
        title=f"{hit.code}: {hit.message}"[:120],
        description=f"{detail}\n{hit.url}".strip(),
        proposed_fix="Address the rule, or justify it in review; the lane runs "
                     "--ignore-noqa so an inline suppression will not clear it.",
        rule_id=hit.code,
    )
    return VerifiedFinding(
        finding=finding,
        verdict=Verdict(is_real=True, confidence=1.0,
                        reasoning=f"ruff {hit.code} fired at {hit.file}:{hit.line}"),
        lane=LANE,
    )
