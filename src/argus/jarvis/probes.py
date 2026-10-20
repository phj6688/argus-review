"""The probe lane: deterministic checks mined from real incidents.

Two probe kinds, both deterministic and near-zero-FP by design:
  - Probe: a smoking-gun regex flagged per line (presence is the bug).
  - FileProbe: a 'present AND fix-marker absent' check over a whole file. The
    fix marker is what a corrected file contains, so a probe fires on the
    known-bad shape and goes silent on the known-good shape automatically.

Every run logs, per probe, whether its glob still matched any file
(path_matched) and whether it fired, so a probe that silently decays after a
refactor is visible. The incident catalog is at the bottom; the engine is
generic and unit-tested with synthetic fixtures, then the catalog is validated
against the real before/after code of each incident.
"""

import glob as globlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from . import corpus


@dataclass(frozen=True)
class Hit:
    probe_id: str
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class Probe:
    """Fires on every line matching a smoking-gun regex within the glob."""

    probe_id: str
    path_glob: str
    pattern: str
    severity: str
    note: str = ""

    def find(self, root: str) -> tuple[int, list[Hit]]:
        regex = re.compile(self.pattern)
        files = _glob_files(root, self.path_glob)
        hits = [Hit(self.probe_id, os.path.relpath(path, root), number, line.rstrip("\n"))
                for path in files
                for number, line in enumerate(_read_lines(path), start=1)
                if regex.search(line)]
        return len(files), hits


@dataclass(frozen=True)
class FileProbe:
    """Fires once per file where `present` matches but the fix marker `absent` does not."""

    probe_id: str
    path_glob: str
    present: str
    absent: str
    severity: str
    note: str = ""

    def find(self, root: str) -> tuple[int, list[Hit]]:
        present = re.compile(self.present)
        absent = re.compile(self.absent)
        files = _glob_files(root, self.path_glob)
        hits = []
        for path in files:
            lines = _read_lines(path)
            text = "".join(lines)
            match = present.search(text)
            if match and not absent.search(text):
                hits.append(Hit(self.probe_id, os.path.relpath(path, root),
                                _line_of(lines, match.start()), match.group(0)))
        return len(files), hits


def run_probe(probe, root: str) -> list[Hit]:
    return probe.find(root)[1]


def run_probes(probes: list, root: str, *,
               log_path: str | None = None, ref: str | None = None) -> list[Hit]:
    """Run every probe under root; optionally record each probe's match/fire state.

    ref labels the scan target in probe_eval_log (a repo name or corpus ref);
    defaults to root so a log row is always traceable to what was scanned.
    """
    all_hits: list[Hit] = []
    log_rows: list[tuple] = []
    for probe in probes:
        file_count, hits = probe.find(root)
        all_hits.extend(hits)
        log_rows.append((probe.probe_id, ref or root, int(file_count > 0), int(bool(hits))))
    if log_path is not None:
        _record(log_path, log_rows)
    return all_hits


# Vendored / generated code would drown the probes in findings outside
# the reviewed application, so it is excluded from every scan.
_EXCLUDED_DIRS = ("node_modules", ".git", ".venv", "venv", "dist", "build",
                  ".next", "__pycache__", ".pytest_cache", "fixtures", "golden",
                  "testdata")


def scan_contents(probes: list, files: dict[str, str]) -> list[Hit]:
    """Run probes against in-memory {path: content} (live review has API content,
    not a checkout). Materialises to a temp tree so the validated disk engine and
    its glob/whole-file semantics are reused verbatim."""
    with tempfile.TemporaryDirectory() as root:
        root_real = os.path.realpath(root)
        for path, content in files.items():
            full = os.path.join(root, path)
            # Confine writes to the temp root. The filename comes verbatim from
            # the review diff (diff.py reads the `+++ b/<path>` header with no
            # traversal check), so a crafted `../` or absolute path would write
            # outside the tree as the container user. Skip anything that escapes
            # rather than 500 the review, so the safe files still get scanned.
            full_real = os.path.realpath(full)
            if full_real != root_real and not full_real.startswith(root_real + os.sep):
                continue
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)
        return run_probes(probes, root)


def _glob_files(root: str, path_glob: str) -> list[str]:
    matches = globlib.glob(os.path.join(root, path_glob), recursive=True)
    return sorted(path for path in matches
                  if os.path.isfile(path) and not _is_vendored(path, root))


def _is_vendored(path: str, root: str) -> bool:
    parts = os.path.relpath(path, root).split(os.sep)
    return any(part in _EXCLUDED_DIRS for part in parts)


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readlines()
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: nothing to match, not an error


def _line_of(lines: list[str], char_offset: int) -> int:
    running = 0
    for number, line in enumerate(lines, start=1):
        running += len(line)
        if char_offset < running:
            return number
    return len(lines)


def _record(log_path: str, rows: list[tuple]) -> None:
    ran_at = datetime.now(timezone.utc).isoformat()
    conn = corpus.connect(log_path)
    try:
        conn.executemany(
            "INSERT INTO probe_eval_log (probe_id, pr_or_corpus_ref, path_matched, "
            "fired, ran_at) VALUES (?, ?, ?, ?, ?)",
            [(pid, ref, matched, fired, ran_at) for pid, ref, matched, fired in rows])
        conn.commit()
    finally:
        conn.close()


# Each probe covers a failure class validated against vulnerable and fixed
# code shapes in test_jarvis_probe_catalog.
# The FileProbe `absent` marker is what the fix added, so a probe fires on the
# known-bad shape and goes silent on the known-good shape.
INCIDENT_PROBES: list = [
    FileProbe(
        "unscoped_cypher_endpoint", "**/api/**/cypher.py",
        present=r"session\.run\(",
        # Either a named scoping helper or an inline {user_id: $user_id}
        # filter counts as scoped; only a truly unscoped run fires.
        absent=r"scope_cypher_to_user|user_id\s*:\s*\$user_id", severity="critical",
        note="A raw Cypher query run without per-user scoping can expose another tenant's graph.",
    ),
    FileProbe(
        "rfc1918_gate_trust", "**/nginx*.conf",
        present=r"allow\s+(127\.0\.0\.1|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)",
        absent=r"set_real_ip_from", severity="critical",
        note="An nginx IP allowlist trusting RFC1918/loopback without set_real_ip_from "
             "is a public bypass behind a reverse proxy or tunnel.",
    ),
    FileProbe(
        "client_trusted_price", "**/checkout/**/route.ts",
        present=r"(unitPrice|amount|price)\w*\s*:\s*z\.number",
        absent=r"getProduct|getCatalogPrice|catalog|\.price_", severity="critical",
        note="A client-supplied price/amount in a request schema with no server-side "
             "re-pricing lets a buyer set their own price.",
    ),
    FileProbe(
        "unguarded_admin_route_pii", "**/admin/**/*.tsx",
        present=r"db\.(select|query)\(|prisma\.|fetchOverview",
        absent=r"requireAdmin|requireAuth|requireRole|getServerSession|checkRole",
        severity="critical",
        note="An admin server component fetching data with no in-file guard leaks PII "
             "in the RSC payload (layout-only auth is not enough).",
    ),
    FileProbe(
        "service_worker_navigation_intercept", "**/sw.js",
        present=r"respondWith",
        # Safe if it returns early on navigations or only responds to assets;
        # a bare respondWith with neither guard fires.
        absent=r"mode\s*===\s*['\"]navigate['\"]|isAsset\(", severity="major",
        note="A service worker that respondWith()s navigations drops the oauth2-proxy CSRF "
             "cookie and breaks SSO.",
    ),
    Probe(
        "app_bound_to_all_interfaces", "**/docker-compose*.yml",
        # A host port MAP is "0.0.0.0:host:container" (two colons); an app bind is
        # --host/--bind 0.0.0.0. A single-colon 0.0.0.0:PORT in a URL or env value
        # is not a host exposure, so it is deliberately excluded.
        pattern=r"--host\s+0\.0\.0\.0|-b\s+0\.0\.0\.0|--bind\s+0\.0\.0\.0|[\"']0\.0\.0\.0:\d+:\d+",
        severity="major",
        note="Binding 0.0.0.0 exposes a service on every interface; loopback-only services "
             "should bind 127.0.0.1.",
    ),
]
