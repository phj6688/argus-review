"""Cached checks retain their head; explicit requests check the current code."""

import httpx
import pytest

from argus import acceptance, github, replies
from test_acceptance import rows
import test_replies as fixtures
from test_replies import setup_loop
from test_reply_regressions import pull
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck


def request(comments, cid=950):
    comments.append(dict(id=cid, in_reply_to_id=101, body="recheck please",
                         user=dict(login="author"), author_association="OWNER",
                         created_at="2026-10-16T10:00:00+00:00",
                         updated_at="2026-10-16T10:00:00+00:00"))


@pytest.mark.parametrize("verdict,prefix", [("fixed", "Fixed"), ("present", "Still present")])
def test_new_request_checks_new_head_after_cached_fix(live, monkeypatch, verdict, prefix):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    request(comments)
    pull(monkeypatch, "c")
    def check(repo, finding, head):
        checks.append((finding["id"], head))
        return dict(verdict=verdict, quote="current code")
    monkeypatch.setattr(replies, "recheck", check)
    acceptance.collect(live)
    assert [head for _, head in checks] == ["b" * 40, "c" * 40]
    assert writes[2][1].startswith(prefix + " at ccccccc: ")
    assert acceptance.stats(live)["last_run"]["rechecks_attempted"] == 1


def test_fresh_request_waits_for_check_cap(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    request(comments)
    pull(monkeypatch, "c")
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "0")
    acceptance.collect(live)
    assert len(checks) == 1 and len(writes) == 2
    assert rows(live, "reply_texts")[0]["answered_head"] is None
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "1")
    acceptance.collect(live)
    assert [head for _, head in checks] == ["b" * 40, "c" * 40]
    assert writes[2][1].startswith("Fixed at ccccccc:")


@pytest.mark.parametrize("cap", [0, 1])
def test_pending_addressed_reply_and_resolve_keep_checked_head(live, monkeypatch, cap):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", str(cap))
    acceptance.collect(live)
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "20")
    pull(monkeypatch, "c")
    acceptance.collect(live)
    assert [head for _, head in checks] == ["b" * 40]
    assert [kind for kind, _ in writes] == ["reply", "resolve"]
    assert writes[0][1].startswith("Addressed in commit bbbbbbb.")
    assert all(a["head_sha"] == "b" * 40 for a in rows(live, "bot_actions")
               if a["kind"] in {"reply", "resolve"})


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("real_check", [False, True])
def test_reviewed_head_never_gets_fixed_or_addressed_text(live, monkeypatch, cached, requested, real_check):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    if cached:
        # Simulate a force-push back after a checked fix with its write still pending.
        monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "0")
        acceptance.collect(live)
        monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "20")
    if requested:
        request(comments)
    if real_check:
        monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    pull(monkeypatch, "a")
    acceptance.collect(live)
    assert all("Fixed at" not in body and "Addressed in commit" not in body for _, body in writes)
    assert not any(kind == "resolve" for kind, _ in writes)
    assert len(checks) == int(cached)
    if requested:
        assert writes[0][1].startswith("No change to this code since aaaaaaa.")
        assert next(a for a in rows(live, "bot_actions") if a["evidence_id"] == "a" * 40
                    and a["kind"] == "recheck")["value"] == "unsure"


def test_reviewed_head_request_is_answered_before_next_push(live, monkeypatch):
    _, _, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    pull(monkeypatch, "a")
    acceptance.collect(live)
    assert writes[0][1].startswith("No change to this code since aaaaaaa.")
    assert rows(live, "reply_texts")[0]["answered_head"] == "a" * 40
    pull(monkeypatch, "b")
    acceptance.collect(live)
    assert len(writes) == 1


@pytest.mark.parametrize("count,body", [(299, "No change to this code since aaaaaaa."),
                                        (300, "Not sure at bbbbbbb."), (301, "Not sure at bbbbbbb.")])
def test_missing_file_in_capped_comparison_is_unknown(live, monkeypatch, count, body):
    _, _, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    files = [file_entry(filename=f"other_{n}.py", patch="@@ -1 +1 @@\n-old\n+new") for n in range(count)]
    monkeypatch.setattr(github, "get", lambda url, *a: httpx.Response(
        200, json=dict(status="ahead", files=files),
        request=httpx.Request("GET", "https://api.github.com" + url)))
    acceptance.collect(live)
    assert [text.splitlines()[0] for _, text in writes] == [body]
    assert next(a for a in rows(live, "bot_actions") if a["kind"] == "recheck")["value"] == "unsure"
    addressed = next(o for o in rows(live, "finding_outcomes") if o["kind"] == "addressed")
    assert addressed["value"] == ("false" if count == 299 else "unknown")


@pytest.mark.parametrize("shape", ["unchanged", "short", "surviving"])
def test_fresh_request_after_cached_fix_keeps_structural_vetoes(live, monkeypatch, shape):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    request(comments)
    pull(monkeypatch, "c")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    old = "\n".join(["context"] * 9 + ["unsafe_call(value)"] + ["context"] * 10)
    code = old.replace("unsafe_call(value)", "pass")
    files = [] if shape == "unchanged" else [file_entry(filename="app.py", patch=(
        "@@ -10 +10 @@\n-unsafe_call(value)\n+pass" if shape == "short" else
        "@@ -10 +10 @@\n-unsafe_call(value)\n+validate(value)\n"
        "@@ -20,0 +21 @@\n+unsafe_call(value)"))]
    if shape == "surviving":
        code = code.replace("pass", "validate(value)") + "\nunsafe_call(value)"
    monkeypatch.setattr(replies, "_read_file", lambda *a: old if a[2] == "a" * 40 else code)
    monkeypatch.setattr(github, "get", lambda url, *a: httpx.Response(
        200, json=dict(status="ahead", files=files),
        request=httpx.Request("GET", "https://api.github.com" + url)))
    acceptance.collect(live)
    assert len(checks) == 1
    assert writes[2][1].startswith("No change to this code" if shape == "unchanged" else "Not sure at ccccccc.")
    assert acceptance.stats(live)["last_run"]["rechecks_attempted"] == 1
    assert next(a for a in rows(live, "bot_actions") if a["kind"] == "recheck"
                and a["evidence_id"] == "c" * 40)["value"] == "unsure"


def test_force_push_back_after_completed_fix_never_claims_original_head_fixed(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    request(comments)
    pull(monkeypatch, "a")
    acceptance.collect(live)
    assert len(checks) == 1
    assert [kind for kind, _ in writes] == ["reply", "resolve", "reply"]
    assert writes[2][1].startswith("No change to this code since aaaaaaa.")
