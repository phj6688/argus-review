"""Current labels and coverage values over a fixed reporting window."""

import json
from datetime import timedelta

from argus import acceptance
from argus.api import db, watch_metrics
from argus.evalkit import labels, monthly
from argus.evalkit.reports import average, rate

from .test_golden_acceptance import HEAD, NEXT_HEAD, REPO, seed_published


SINCE = "2026-01-01T00:00:00+00:00"
UNTIL = "2026-02-01T00:00:00+00:00"


def _labels(path, stamp):
    findings = seed_published(path, stamp, [
        {}, {"title": "Rejected contract"}, {"title": "Deferred change"},
        {"title": "Changed region"}, {"title": "Unconfirmed change"},
        {"title": "Later verified fix"}, {"title": "Summary without acceptance"},
        {"title": "Untrusted fixed reply"}, {"title": "Upper date boundary"},
    ])
    with db.connect(path) as conn:
        for index, value in ((0, "fixed"), (1, "rejected"), (2, "deferred"),
                             (5, "rejected"), (6, "fixed"), (7, "fixed")):
            acceptance._record(conn, findings[index]["id"], "reply", value, 900 + index,
                               stamp, stamp, body=value,
                               author_association="CONTRIBUTOR" if index == 7 else "MEMBER",
                               author_login="maintainer" if index != 7 else "visitor")
        acceptance._record(conn, findings[3]["id"], "addressed", "true", NEXT_HEAD, stamp)
        conn.execute("INSERT INTO bot_actions(finding_id,kind,value,evidence_id,created_at) "
                     "VALUES (?,'recheck','fixed',?,?)", (findings[5]["id"], NEXT_HEAD, stamp))
        conn.execute("UPDATE published_findings SET placement='summary',unverified=1 WHERE id=?",
                     (findings[6]["id"],))
        conn.execute("UPDATE published_findings SET posted_at=? WHERE id=?", (UNTIL, findings[8]["id"]))
    db.add_mark(path, 1, "real", marker="maintainer", note="Unchecked input reaches the parser.")
    db.add_mark(path, 2, "fp", marker="maintainer", note="The caller enforces the contract.")


def test_labelled_findings(golden, db_path, frozen_now):
    _labels(db_path, frozen_now.isoformat())
    with labels.readonly(db_path) as conn:
        result = list(labels.labelled_findings(conn, SINCE, UNTIL))
    golden.check("labels-monthly", "labelled-findings", result)


def test_monthly_coverage(golden, db_path, frozen_now, tmp_path):
    _labels(db_path, frozen_now.isoformat())
    first_seen = frozen_now - timedelta(hours=1)
    with db.connect(db_path) as conn:
        conn.execute("UPDATE reviews SET completed_at=? WHERE id=1",
                     ((first_seen + timedelta(minutes=10)).isoformat(),))
        for head_id, pr, status, baseline, ended, merged_before in (
            (1, 3, "done", 0, None, 0), (2, 4, "error", 0, None, 0),
            (3, 5, "superseded", 0, (first_seen + timedelta(minutes=5)).isoformat(), 0),
            (4, 6, "done", 1, None, 0), (5, 7, "queued", 0, None, 1),
        ):
            conn.execute(
                "INSERT INTO pr_heads(id,repo,pr,head_sha,first_seen_at,status,eligible_at,"
                "eligible_pr_updated_at,head_committed_at,baseline,ended_at,merged_before_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (head_id, REPO, pr, HEAD, first_seen.isoformat(), status, first_seen.isoformat(),
                 first_seen.isoformat(), (first_seen - timedelta(minutes=2)).isoformat(),
                 baseline, ended, merged_before),
            )
        for head_id, error in ((1, None), (2, "github_timeout")):
            conn.execute("INSERT INTO watch_attempts(head_id,review_id,finished_at,error_class) VALUES (?,?,?,?)",
                         (head_id, 1 if head_id == 1 else None, frozen_now.isoformat(), error))
    reference_path = tmp_path / "reference-prs.jsonl"
    reference_path.write_text("".join(json.dumps({"repo": REPO, "pr": pr}) + "\n" for pr in (3, 4, 8)),
                              encoding="utf-8")
    weekly = {"date": frozen_now.isoformat(), "sets": {
        "incidents": {"new": {"incidents": rate(7, 17)}},
        "heldout": {"new": {"visible": rate(5, 10)}, "v5p1": {"visible": rate(3, 10)},
                    "after": {"visible": rate(4, 10)}},
    }}
    report = monthly.build(db_path, weekly, "2026-01", reference_prs=str(reference_path))
    start, end = monthly.month_window("2026-01")
    with db.connect(db_path) as conn:
        coverage = watch_metrics.coverage(conn, start, end)
    passing = {"fair_head_coverage": rate(95, 100), "strict": rate(65, 200),
               "incidents": rate(7, 17), "inline_per_pr": average(700, 200)}
    golden.check("labels-monthly", "monthly-coverage", {
        "report": report, "markdown": monthly.markdown(report), "coverage": coverage,
        "passing_gate": monthly.retirement_gate(passing, 2.52),
        "failing_gate": monthly.retirement_gate({**passing, "fair_head_coverage": rate(94, 100)}, 2.52),
    })
