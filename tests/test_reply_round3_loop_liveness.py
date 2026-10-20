"""Confirmed fixes must not strand pending writes after a push."""
import httpx
import pytest

from argus import acceptance, github, replies
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop

database = fixtures.database
live = fixtures.live


def pull(monkeypatch, head, author="author"):
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        dict(state="open", head=dict(sha=head * 40), user=dict(login=author)), ""))


def test_deferred_addressed_reply_survives_a_push(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    pull(monkeypatch, "b")
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "0")
    acceptance.collect(live)
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "20")
    pull(monkeypatch, "c")
    acceptance.collect(live)
    acceptance.collect(live)
    assert [w[0] for w in writes] == ["reply", "resolve"]
    assert len(checks) == 1


def test_failed_addressed_reply_survives_a_push(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    pull(monkeypatch, "b")
    good = replies.reply

    def failing(*a):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(replies, "reply", failing)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "reply", good)
    pull(monkeypatch, "c")
    acceptance.collect(live)
    assert [w[0] for w in writes] == ["reply", "resolve"]
    assert len(checks) == 1


def test_request_after_confirmed_fix_on_reopened_thread_is_answered(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch)
    pull(monkeypatch, "b")
    acceptance.collect(live)
    assert [w[0] for w in writes] == ["reply", "resolve"]
    assert len(checks) == 1
    comments.append(dict(id=950, in_reply_to_id=101, body="recheck please", user=dict(login="author"),
                         author_association="OWNER", created_at="2026-10-16T10:00:00+00:00",
                         updated_at="2026-10-16T10:00:00+00:00"))
    pull(monkeypatch, "c")
    acceptance.collect(live)
    acceptance.collect(live)
    pending = [r["answered_head"] for r in rows(live, "reply_texts")]
    assert pending == ["c" * 40]
    assert [head for _, head in checks] == ["b" * 40, "c" * 40]
    assert len([w for w in writes if w[0] == "reply"]) == 2
    assert writes[-1][1].startswith("Fixed at ccccccc:")


def test_request_fixed_reply_failed_then_push(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False)
    pull(monkeypatch, "b")
    good = replies.reply

    def failing(*a):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(replies, "reply", failing)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "reply", good)
    pull(monkeypatch, "c")
    acceptance.collect(live)
    acceptance.collect(live)
    assert [w[0] for w in writes] == ["reply", "resolve"]
    assert [head for _, head in checks] == ["b" * 40, "c" * 40]
    assert writes[0][1].startswith("Fixed at ccccccc:")
    assert rows(live, "reply_texts")[0]["answered_head"] == "c" * 40


@pytest.mark.parametrize("requested", [False, True])
def test_failed_resolve_survives_a_push(live, monkeypatch, requested):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck" if requested else None)
    pull(monkeypatch, "b")
    good = replies.resolve_thread

    def failing(*a):
        raise RuntimeError("Thread resolution returned errors")

    monkeypatch.setattr(replies, "resolve_thread", failing)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "resolve_thread", good)
    pull(monkeypatch, "c")
    acceptance.collect(live)
    acceptance.collect(live)
    assert [w[0] for w in writes] == ["reply", "resolve"]
    assert len(checks) == 1
