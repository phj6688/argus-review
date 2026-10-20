"""Failure summaries retain useful causes without response contents."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from argus import review
from argus.api import db, main
from argus.errors import HeadMoved
from argus.jarvis import automark, judges
from argus.schema import Finding, Verdict, VerifiedFinding


def test_judge_failure_causes_are_counted_logged_and_saved(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(review.JUDGE_LIVE_ENV, "1")
    monkeypatch.setattr(judges, "judge_for", lambda _: SimpleNamespace(model="judge", family="second"))
    monkeypatch.setattr(judges, "family_of", lambda _: "first")
    response = httpx.Response(503, text="private body", request=httpx.Request(
        "POST", "https://example.invalid", headers={"Authorization": "private key"}))
    status_error = httpx.HTTPStatusError("private body", request=response.request, response=response)
    outcomes = [status_error, status_error, httpx.ReadTimeout("private key"),
                ValueError("private body"), {"verdict": "real", "confidence": 0.9}]
    monkeypatch.setattr(automark, "judge_finding", Mock(side_effect=outcomes))
    items = [VerifiedFinding(finding=Finding(
        file="app.py", line=1, severity="major", confidence=0.9, title=f"Bug {i}",
        description="Wrong result", proposed_fix="Return the expected value"), verdict=Verdict(is_real=True, confidence=0.9, reasoning="confirmed"))
        for i in range(5)]
    with caplog.at_level(logging.WARNING, logger="argus.review"):
        record = review._judge_lane(items, {"app.py": "+changed()"}, "reviewer")
    assert record["failure_causes"] == {"http_503": 2, "ReadTimeout": 1, "ValueError": 1}
    assert record["failures"] == 4
    assert record["checks"] == 1
    assert all(item.judge is None for item in items[:4])
    assert [r.getMessage() for r in dict.fromkeys(caplog.records)] == [
        "judge lane: 4/5 second opinions failed (http_503 x2, ReadTimeout x1, ValueError x1); "
        "those findings stay single_critic"]
    path = str(tmp_path / "argus.db")
    db.init_db(path)
    rid = db.create_review(path, "example/a", 1, "reviewer")
    db.save_result(path, rid, review.ReviewResult(raw_count=0, coverage=[record]))
    stored = db.get_review(path, rid)["coverage_json"]
    assert json.loads(stored)["lanes"][0]["failure_causes"] == record["failure_causes"]
    assert "private" not in stored + caplog.text


@pytest.mark.parametrize("in_fallback", [False, True])
def test_moved_head_logs_info_without_traceback(tmp_path, monkeypatch, caplog, in_fallback):
    path = str(tmp_path / "argus.db")
    db.init_db(path)
    rid = db.create_review(path, "example/a", 1, "reviewer")
    error = HeadMoved("PR head changed after listing")
    runner = Mock(side_effect=RuntimeError("unavailable") if in_fallback else error)
    fallback = Mock(side_effect=error)
    with caplog.at_level(logging.INFO, logger="argus.api"):
        result = main.run_one_review(path, rid, "example/a", 1, runner,
                                     fallback_runner=fallback)
    assert result is error
    if not in_fallback:
        fallback.assert_not_called()
    row = db.get_review(path, rid)
    assert row["error_class"] == "HeadMoved"
    assert row["error"] == "HeadMoved: PR head changed after listing"
    records = list({id(r): r for r in caplog.records if "head moved" in r.getMessage()}.values())
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"review {rid}: PR head moved during the review; this review is superseded")
    assert records[0].levelno == logging.INFO
    assert all(r.exc_info is None and r.levelno < logging.ERROR for r in caplog.records)
