"""Finding audit helpers.

`argus explain <finding_id>` traces one finding to its review, operator mark, and
corpus membership. `argus why-quiet <repo> <pr>` says why Argus said nothing on a
PR: never reviewed, failed, ran clean, or actually had findings. Both are
read-only.
"""

import sqlite3

from . import corpus
from .api import db as argus_db


def explain_finding(finding_id: int, *, argus_path: str | None = None,
                    corpus_path: str | None = None) -> str:
    finding = _finding_with_review(argus_path, finding_id)
    if finding is None:
        return f"No finding #{finding_id} found."
    return "\n".join([
        f"Finding #{finding['id']}: {finding['title']}",
        f"  where:    {finding['repo']} PR #{finding['pr']}  ·  "
        f"{finding['file']}:{finding['line']}  "
        f"(severity {finding['severity']}, model-confidence {finding['confidence']})",
        f"  verifier: {_verdict(finding)}   rule: {finding['rule_id'] or 'none'}",
        f"  operator: {_mark_line(_latest_mark(argus_path, finding_id))}",
        f"  corpus:   {_corpus_line(_corpus_rows(corpus_path, finding_id))}",
    ])


def why_quiet(repo: str, pr: int, *, argus_path: str | None = None) -> str:
    reviews = _reviews_for(argus_path, repo, pr)
    if not reviews:
        return (f"{repo} PR #{pr}: no review on record. Argus never ran on it "
                "(not triggered, or the repo is not a watched allowlist entry).")
    latest = reviews[0]
    head = latest["head_sha"] or "unknown"
    header = (f"{repo} PR #{pr}: {len(reviews)} review(s) on record. Latest review "
              f"#{latest['id']} ({latest['status']}) at {latest['created_at']}, "
              f"head_sha={head}.")
    return f"{header}\n  {_quiet_reason(argus_path, latest)}"


def _quiet_reason(argus_path: str | None, review: sqlite3.Row) -> str:
    status = review["status"]
    if status == argus_db.STATUS_ERROR:
        return (f"FAILED: {review['error'] or 'no reason recorded'} "
                f"[{review['error_class'] or 'unclassified'}]. No findings were posted.")
    if status == argus_db.STATUS_RUNNING:
        return "still running; no findings posted yet."
    count = _finding_count(argus_path, review["id"])
    if count == 0:
        return (f"ran clean: 0 findings reported "
                f"(raw_count={review['raw_count']}, dropped={review['dropped']}). "
                "Quiet because nothing survived verification and the thresholds.")
    return f"not quiet: reported {count} finding(s). Use `argus explain` on each."


def _finding_with_review(argus_path: str | None, finding_id: int) -> sqlite3.Row | None:
    conn = argus_db.connect(argus_path)
    try:
        return conn.execute(
            "SELECT f.id, f.file, f.line, f.severity, f.confidence, f.title, "
            "f.rule_id, f.verdict_is_real, f.verdict_confidence, "
            "r.repo, r.pr, r.model, r.created_at FROM findings f "
            "JOIN reviews r ON r.id = f.review_id WHERE f.id = ?", (finding_id,),
        ).fetchone()
    finally:
        conn.close()


def _latest_mark(argus_path: str | None, finding_id: int) -> sqlite3.Row | None:
    conn = argus_db.connect(argus_path)
    try:
        return conn.execute(
            "SELECT mark, marker, note, marked_at FROM marks WHERE finding_id = ? "
            "ORDER BY id DESC LIMIT 1", (finding_id,),
        ).fetchone()
    finally:
        conn.close()


def _corpus_rows(corpus_path: str | None, finding_id: int) -> list[sqlite3.Row]:
    conn = corpus.connect(corpus_path)
    try:
        return conn.execute(
            "SELECT source, label, confidence, corpus_version FROM corpus "
            "WHERE json_extract(payload_json, '$.finding_id') = ?", (finding_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # corpus never built; that is itself the answer
    finally:
        conn.close()


def _reviews_for(argus_path: str | None, repo: str, pr: int) -> list[sqlite3.Row]:
    conn = argus_db.connect(argus_path)
    try:
        return conn.execute(
            "SELECT id, status, created_at, head_sha, error, error_class, "
            "raw_count, dropped FROM reviews WHERE repo = ? AND pr = ? "
            "ORDER BY created_at DESC, id DESC", (repo, pr),
        ).fetchall()
    finally:
        conn.close()


def _finding_count(argus_path: str | None, review_id: int) -> int:
    conn = argus_db.connect(argus_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE review_id = ?", (review_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


def _verdict(finding: sqlite3.Row) -> str:
    call = "real" if finding["verdict_is_real"] else "refuted"
    return f"{call} (confidence {finding['verdict_confidence']})"


def _mark_line(mark: sqlite3.Row | None) -> str:
    if mark is None:
        return "unlabeled (no operator ground truth)"
    note = f' - "{mark["note"]}"' if mark["note"] else ""
    return f"{mark['mark']} by {mark['marker'] or 'unknown'}{note} ({mark['marked_at']})"


def _corpus_line(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "not in corpus (no version references this finding)"
    return "; ".join(f"{r['source']}/{r['label']}/{r['confidence']} in {r['corpus_version']}"
                     for r in rows)

