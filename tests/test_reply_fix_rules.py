"""A fix needs changed flagged code and substantial added evidence."""

import base64
import difflib

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
from test_acceptance import rows, seed
import test_replies as fixtures
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck


def check(monkeypatch, old, new, first, last, quote, evidence="", extra_files=()):
    patch = "\n".join(list(difflib.unified_diff(old, new, lineterm=""))[2:])
    finding = dict(path="app.py", head_sha="a" * 40, line=first, end_line=last, evidence=evidence,
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch), *extra_files]))
    monkeypatch.setattr(replies, "_read_file", lambda *a: "\n".join(new))
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    return REAL_RECHECK("owner/repo", finding, "b" * 40)


@pytest.mark.parametrize("distance,expected", [(0, "fixed"), (1, "fixed"), (3, "fixed"), (4, "unsure")])
def test_fix_quote_must_be_added_near_exact_anchor(live, monkeypatch, distance, expected):
    old = [f"context_{n}" for n in range(12)]
    old[2] = "unsafe_call(value)"
    new = list(old)
    new[2] = "safe_call(value)"
    new[2 + distance] = "validate(value)"
    result = check(monkeypatch, old, new, 3, 3, "validate(value)")
    assert result["verdict"] == expected


@pytest.mark.parametrize("quote,expected", [("abcdefgh", "fixed"), ("abcdefg", "unsure"),
                                            ("a b c d e f g", "unsure"), ("abcdefgh_more", "unsure")])
def test_fix_quote_counts_nonspace_characters_and_full_lines(live, monkeypatch, quote, expected):
    result = check(monkeypatch, ["original_call()", "abcdefgh_more"],
                   ["abcdefgh" if quote == "abcdefgh_more" else quote, "abcdefgh_more"], 1, 1, quote)
    assert result["verdict"] == expected


def test_context_quote_is_not_a_fix(live, monkeypatch):
    result = check(monkeypatch, ["original_call()", "unchanged_context"],
                   ["replacement_call()", "unchanged_context"], 1, 1, "unchanged_context")
    assert result == {"verdict": "unsure", "quote": ""}


def test_added_code_without_flagged_deletion_is_not_a_fix(live, monkeypatch):
    result = check(monkeypatch, ["original_call()", "unchanged_context"],
                   ["original_call()", "validate(value)", "unchanged_context"], 1, 1, "validate(value)")
    assert result == {"verdict": "unsure", "quote": ""}


@pytest.mark.parametrize("prefix", ["", "34 +     ", "34       ", "34 -     "])
def test_each_evidence_line_can_veto_even_outside_window(live, monkeypatch, prefix):
    old = ["unsafe_call(value)"] + [f"context_{n}" for n in range(60)]
    new = ["validate(value)", *old[1:], "unsafe_call( value )"]
    result = check(monkeypatch, old, new, 1, 1, "validate(value)", "short\n" + prefix + "unsafe_call(value)")
    assert result == {"verdict": "unsure", "quote": ""}


@pytest.mark.parametrize("other_file", [False, True])
def test_deleted_flagged_line_moved_anywhere_vetoes_fix_without_evidence(live, monkeypatch, other_file):
    old = ["unsafe_call(value)"] + [f"context_{n}" for n in range(60)]
    new = ["validate(value)", *old[1:]]
    extra = []
    if other_file:
        extra = [file_entry(filename="other.py", patch="@@ -0,0 +1 @@\n+unsafe_call( value )")]
    else:
        new.append("unsafe_call( value )")
    result = check(monkeypatch, old, new, 1, 1, "validate(value)", extra_files=extra)
    assert result == {"verdict": "unsure", "quote": ""}


@pytest.mark.parametrize("patch", [None, "", "@@ -1 +1 @@", "@@ -1,2 +1 @@\n-old\n+new"])
def test_missing_or_incomplete_patch_never_earns_fixed(live, monkeypatch, patch):
    monkeypatch.setattr(replies, "_read_file", lambda *a: "new")
    finding = dict(path="app.py", line=1, comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]))
    assert REAL_RECHECK("owner/repo", finding, "b" * 40) == {"verdict": "unsure", "quote": ""}


def test_original_head_cannot_earn_fixed_even_with_ahead_payload(live):
    finding = dict(path="app.py", head_sha="a" * 40, comparison=dict(status="ahead", files=[]))
    assert REAL_RECHECK("owner/repo", finding, "a" * 40) == {"verdict": "unsure", "quote": ""}


@pytest.mark.parametrize("text,label", [("Rejected because callers already validate the input", "rejected"),
                                        ("Deferred until the next release", "deferred")])
@pytest.mark.parametrize("login,association,allowed", [("AUTHOR", "NONE", False),
    ("visitor", "OWNER", True), ("visitor", "MEMBER", True), ("visitor", "COLLABORATOR", True),
    ("visitor", "CONTRIBUTOR", False), ("visitor", "NONE", False), ("visitor", None, False)])
def test_first_reply_authority_controls_writes_and_suppression(live, monkeypatch, text, label, login, association, allowed):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text=text, changed=False)
    comments[0].update(user=dict(login=login), author_association=association)
    acceptance.collect(live)
    assert not checks
    assert len(writes) == (2 if label == "rejected" else 1) * allowed
    assert bool(rows(live, "known_fps_live")) == (allowed and label == "rejected")
    assert acceptance.stats(live)["totals"][label] == int(allowed)
    assert rows(live, "reply_texts")[0]["author_login"] == login


def test_trusted_later_reply_cannot_replace_untrusted_first_rejection(live, monkeypatch):
    _, _, writes, comments = setup_loop(live, monkeypatch, text="Rejected because input is already checked", changed=False)
    comments[0].update(user=dict(login="visitor"), author_association="NONE")
    comments.append(dict(comments[0], id=901, user=dict(login="author")))
    acceptance.collect(live)
    assert not writes and not rows(live, "known_fps_live")
    assert len(rows(live, "reply_texts")) == 2


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("later", [False, True])
def test_only_later_confirmed_fix_removes_suppression(database, existing, later):
    fid = seed(database)
    now = db._now_iso()
    with db.connect(database) as conn:
        acceptance._record(conn, fid, "reply", "rejected", 900, now, body="Rejected", author_association="OWNER")
        findings = conn.execute("SELECT * FROM published_findings").fetchall()
        if existing:
            acceptance._sync_rejections(conn, findings, now)
        conn.execute("INSERT INTO bot_actions(finding_id,kind,value,evidence_id,created_at) VALUES (?,'recheck','fixed',?,?)",
                     (fid, ("b" if later else "a") * 40, now))
        acceptance._sync_rejections(conn, findings, now)
    assert bool(rows(database, "known_fps_live")) is not later


@pytest.mark.parametrize("body,kind", [
    ("<!-- argus:reply=rejected evidence=42 -->\nprose", None),
    ("`<!-- argus:reply=rejected evidence=42 -->`", None),
    ("```\n<!-- argus:reply=rejected evidence=42 -->\n```", None),
    ("text\n<!-- argus:reply=recheck evidence=9 -->\n\n ", "recheck"),
    ("<!-- argus:reply=rejected evidence=42 -->\ntext\n<!-- argus:reply=recheck evidence=9 -->", "recheck"),
])
def test_only_last_content_marker_is_recognized(body, kind):
    match = replies.MARKER.search(body)
    assert (match["kind"] if match else None) == kind


@pytest.mark.parametrize("status,raises", [(204, False), (404, False), (403, True), (429, True), (500, True)])
def test_unreadable_file_status_contract(live, monkeypatch, status, raises):
    monkeypatch.setattr(github, "get", lambda *a: httpx.Response(status,
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/contents/app.py")))
    if raises:
        with pytest.raises(httpx.HTTPStatusError):
            replies._read_file("owner/repo", "app.py", "b" * 40)
    else:
        assert replies._read_file("owner/repo", "app.py", "b" * 40) is None


def test_file_decodes_text(live, monkeypatch):
    monkeypatch.setattr(github, "get", lambda *a: httpx.Response(200,
        json=dict(encoding="base64", content=base64.b64encode(b"value = 42").decode()),
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/contents/app.py")))
    assert replies._read_file("owner/repo", "app.py", "b" * 40) == "value = 42"


def test_listing_failure_skips_checks_and_writes_only_for_that_repo(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    seed(live, repo="owner/second")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo,owner/second")
    with db.connect(live) as conn:
        conn.execute("INSERT INTO publishing_identities VALUES ('owner/second','publisher',?)", (db._now_iso(),))
    def listing(url, *args):
        if "/owner/repo/" in url:
            raise httpx.ConnectError("listing failed")
        return []
    def threads(query, variables):
        cid = 101 if variables["name"] == "repo" else 102
        return dict(repository=dict(pullRequest=dict(reviewThreads=dict(nodes=[
            dict(id=f"thread-{cid}", isResolved=False, comments=dict(nodes=[dict(databaseId=cid)]))]))))
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "graphql_read", threads)
    acceptance.collect(live)
    assert len(checks) == 1 and checks[0][0] == 2
    assert [kind for kind, _ in writes] == ["reply", "resolve"]
    assert acceptance.stats(live)["last_run"]["failures"] == 1


def test_old_closed_pull_gets_no_new_check_and_old_rows_use_current_rule(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, resolved=True)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        dict(state="closed", closed_at="2020-01-01T00:00:00Z", head=dict(sha="b" * 40)), ""))
    with db.connect(live) as conn:
        fid = conn.execute("SELECT id FROM published_findings").fetchone()[0]
        acceptance._record(conn, fid, "addressed", "true", "b" * 40, "2020-01-01T00:00:00Z")
        acceptance._record(conn, fid, "resolved", "true", "101", "2020-01-01T00:00:00Z")
    acceptance.collect(live)
    acceptance.collect(live)
    report = acceptance.stats(live)
    assert not checks and not writes
    assert report["totals"]["strict_accepted"] == 0
    assert report["totals"]["loose_accepted"] == 1
    assert "every row with the current rule" in report["note"]
    assert "closed more than 7 days ago get no new re-check" in report["note"]
