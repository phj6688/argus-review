"""Finding identity and the inline comments the publisher would post for a replay row."""

import hashlib

from argus import publish
from argus.diff import FilePatch

from .cases import normalize_path


def finding_key(case_id: str, finding: dict) -> str:
    title = " ".join(finding.get("title", "").casefold().split())
    text = f"{case_id}|{normalize_path(finding['file'])}|{finding['line']}|{title}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _placed(row: dict, fingerprint) -> list[dict] | None:
    if row.get('error'):
        return []
    if not row.get('snapshot') or 'files' not in row['snapshot']:
        return None
    findings = [{**f, 'fingerprint': fingerprint(index, f),
                 'evidence_tier': f.get('evidence_tier', 'single_critic'),
                 'description': f.get('description', '')} for index, f in enumerate(row.get('reported', []))]
    document = {'findings': findings, 'verdict': 'review', 'coverage': {}}
    payload = publish.render_review('', 0, document, [FilePatch(**f) for f in row['snapshot']['files']])
    keys = {marker[1] for comment in payload['comments']
            if (marker := publish.inline_fingerprint(comment['body'])) and marker[0] == 'fp'}
    return [f for f in findings if f['fingerprint'] in keys]


def inline_findings(row: dict) -> list[dict] | None:
    """Return the reported findings that land inline, or None when the row has no diff to place them on."""
    return _placed(row, lambda _, finding: finding_key(row['case_id'], finding))


def inline_comments(row: dict) -> list[dict] | None:
    """Return one finding per inline comment the publisher would emit, or None without a diff.

    Findings that share a key each match any comment carrying that key, so counting
    inline_findings can exceed the comment cap. A per-index marker maps every comment
    to exactly one finding; with nothing posted before, markers do not affect placement.
    """
    placed = _placed(row, lambda index, _: str(index))
    if placed is None:
        return None
    return [{**f, 'fingerprint': finding_key(row['case_id'], f)} for f in placed]
