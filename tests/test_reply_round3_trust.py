"""Only trusted replies can change suppression or trigger writes."""
import pytest

from argus import acceptance, github
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop

database = fixtures.database
live = fixtures.live


@pytest.mark.parametrize("login,association,seeded_first,seeded_later", [
    ("author", "NONE", False, False), ("owner-user", "OWNER", True, True), ("visitor", "NONE", False, False)])
def test_pull_read_failure_then_recovery(live, monkeypatch, login, association, seeded_first, seeded_later):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="Rejected because callers validate this input",
                                             changed=False)
    comments[0].update(user=dict(login=login), author_association=association)
    good = github.get_json_cached

    def broken(*a):
        raise RuntimeError("pull read failed")

    monkeypatch.setattr(github, "get_json_cached", broken)
    acceptance.collect(live)
    first = bool(rows(live, "known_fps_live"))
    assert acceptance.stats(live)["last_run"]["failures"] == 1
    monkeypatch.setattr(github, "get_json_cached", good)
    acceptance.collect(live)
    later = bool(rows(live, "known_fps_live"))
    assert (first, later) == (seeded_first, seeded_later)


@pytest.mark.parametrize("association,allowed", [("NONE", False), ("MEMBER", True)])
def test_deferral_reply_needs_trust(live, monkeypatch, association, allowed):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="Deferred until next release", changed=False)
    comments[0].update(user=dict(login="visitor"), author_association=association)
    acceptance.collect(live)
    assert bool(writes) is allowed


@pytest.mark.parametrize("association", [None, "NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"])
@pytest.mark.parametrize("text", ["Fixed", "Rejected because callers validate this input", "Recheck please"])
def test_pr_author_without_repository_role_cannot_change_state(live, monkeypatch, association, text):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text=text, changed=False)
    comments[0]["author_association"] = association
    acceptance.collect(live)
    assert rows(live, "known_fps_live") == []
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 0
    assert checks == [] and writes == []


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_repository_roles_can_mark_acceptance(live, monkeypatch, association):
    _, _, _, comments = setup_loop(live, monkeypatch, text="Fixed", changed=False)
    comments[0]["author_association"] = association
    acceptance.collect(live)
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 1
