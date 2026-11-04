import json
from contextlib import closing

import pytest

from argus.api import db
from argus.forge.keys import gitlab_key
from argus.publish import PublishOutcome


KEY = gitlab_key("gitlab.example.test", 42)
HEAD = "a" * 40
FAKE_PREFIX = "gl" + "pat-"
SECRET = FAKE_PREFIX + "s" * 24


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "publish.db")
    db.init_db(path)
    with closing(db.connect(path)) as conn:
        yield path, conn


def item(number=1, **updates):
    return {"fingerprint": f"finding-{number}", "severity": "major", "category": "contracts",
            "file": "app.py", "line": number, "end_line": number + 1, "title": "Check input",
            "placement": "inline", "gh_comment_id": 100 + number,
            "gh_thread_id": str(number) * 40, "anchor_status": "anchored", **updates}


def save(database, outcome, *, key=KEY, review_id=None):
    path, _ = database
    if review_id is None:
        review_id = db.create_review(path, key, 3, "test", HEAD)
    db.save_publish_outcome(path, review_id, outcome)
    return review_id


def rows(database):
    return [dict(row) for row in database[1].execute("SELECT * FROM published_findings ORDER BY fingerprint")]


def test_partial_post_records_exactly_confirmed_items(database):
    items = [item(n) for n in range(1, 4)] + [item(4, gh_thread_id=None), item(5, gh_comment_id=None)]
    review_id = save(database, PublishOutcome("error", reason="partial_post", items=items))
    actual = rows(database)
    fields = ("review_id", "repo", "fingerprint", "gh_comment_id", "gh_thread_id", "anchor_status",
              "placement", "mode", "pr", "head_sha")
    assert [tuple(row[name] for name in fields) for row in actual] == [
        (review_id, KEY, f"finding-{n}", 100 + n, str(n) * 40, "anchored", "inline", "posted", 3, HEAD)
        for n in range(1, 4)
    ]


@pytest.mark.parametrize("status", ["posted", "error"])
@pytest.mark.parametrize("summary_id", [None, 500])
def test_summary_requires_confirmed_summary_note(database, status, summary_id):
    save(database, PublishOutcome(status, reason="partial_post", gh_review_id=summary_id,
                                  items=[item(placement="summary", gh_comment_id=None, gh_thread_id=None)]))
    assert len(rows(database)) == int(summary_id is not None)
    if summary_id is not None:
        assert rows(database)[0]["gh_review_id"] == summary_id


@pytest.mark.parametrize("missing", [{"gh_comment_id": None}, {"gh_thread_id": None}, {"gh_thread_id": ""}])
def test_posted_requires_both_inline_ids(database, missing):
    save(database, PublishOutcome("posted", gh_review_id=500, items=[item(**missing)]))
    assert rows(database) == []


@pytest.mark.parametrize("reason", ["boom", "partial_post extra", None])
def test_other_errors_do_not_record_findings(database, reason):
    save(database, PublishOutcome("error", reason=reason, gh_review_id=500, items=[item()]))
    assert rows(database) == []


def test_dry_run_records_all_items_without_ids(database):
    items = [item(n, gh_comment_id=None, gh_thread_id=None, anchor_status=None,
                  placement="summary" if n == 5 else "inline") for n in range(1, 6)]
    save(database, PublishOutcome("dry_run", items=items))
    assert len(rows(database)) == 5
    assert all((row["mode"], row["gh_comment_id"], row["gh_thread_id"]) == ("dry_run", None, None)
               for row in rows(database))


def test_newer_review_updates_ids_and_preserves_nulls_until_placement_changes(database):
    save(database, PublishOutcome("posted", items=[item()]))
    latest = save(database, PublishOutcome("posted", items=[item(gh_comment_id=201, gh_thread_id="b" * 40,
                                                               anchor_status="outdated")]))
    row = rows(database)[0]
    assert (row["review_id"], row["gh_comment_id"], row["gh_thread_id"], row["anchor_status"]) == (
        latest, 201, "b" * 40, "outdated")
    save(database, PublishOutcome("posted", items=[item(gh_comment_id=201, gh_thread_id="b" * 40,
                                                       anchor_status=None)]))
    assert rows(database)[0]["anchor_status"] == "outdated"
    save(database, PublishOutcome("posted", gh_review_id=500,
                                  items=[item(placement="summary", gh_comment_id=None,
                                              gh_thread_id=None, anchor_status=None)]))
    row = rows(database)[0]
    assert (row["placement"], row["gh_review_id"], row["gh_comment_id"], row["gh_thread_id"],
            row["anchor_status"]) == ("summary", 500, None, None, None)


def test_same_placement_keeps_missing_thread_and_anchor(database):
    save(database, PublishOutcome("posted", gh_review_id=500, items=[item(placement="summary")]))
    save(database, PublishOutcome("posted", gh_review_id=501,
                                  items=[item(placement="summary", gh_comment_id=None,
                                              gh_thread_id=None, anchor_status=None)]))
    row = rows(database)[0]
    assert (row["gh_review_id"], row["gh_comment_id"], row["gh_thread_id"], row["anchor_status"]) == (
        501, 101, "1" * 40, "anchored")


def test_older_review_cannot_overwrite_newer_row(database):
    old = save(database, PublishOutcome("posted", items=[item()]))
    save(database, PublishOutcome("posted", items=[item(gh_thread_id="b" * 40, anchor_status="outdated")]))
    before = rows(database)
    save(database, PublishOutcome("posted", items=[item(placement="summary")], gh_review_id=500), review_id=old)
    assert rows(database) == before


def test_identity_retains_numeric_user_id(database):
    save(database, PublishOutcome("posted", bot_login="review-bot", bot_user_id=7))
    save(database, PublishOutcome("posted", bot_login="review-bot", bot_user_id=None))
    row = database[1].execute("SELECT * FROM publishing_identities").fetchone()
    assert (row["repo"], row["bot_login"], row["bot_user_id"]) == (KEY, "review-bot", 7)
    assert db.bot_user_id(database[1], KEY) == 7
    save(database, PublishOutcome("posted", bot_login="review-bot", bot_user_id=8))
    assert db.bot_user_id(database[1], KEY) == 8


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("text", [SECRET, "https://example.test/path?token=" + SECRET,
                                 'token="' + SECRET + '"'])
def test_stored_secrets_are_redacted_and_payload_remains_json(database, monkeypatch, oversized, text):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome("error", reason="Rejected " + SECRET,
                                              payload={"body": text, "padding": "x " * (105_000 if oversized else 0)}))
    row = db.get_review(database[0], review_id)
    assert SECRET not in row["publish_error"] and "[redacted]" in row["publish_error"]
    assert SECRET not in row["publish_payload"] and "[redacted]" in row["publish_payload"]
    payload = json.loads(row["publish_payload"])
    if oversized:
        assert payload["truncated"] is True
        assert len(row["publish_payload"]) <= 200_000
    else:
        assert payload["padding"] == ""


def test_payload_redaction_retains_credential_key_context(database):
    credential = "opaque-credential-value"
    review_id = save(database, PublishOutcome("error", payload={
        "nested": {"password": credential, "access_token": credential},
        "url": "https://example.test/path?private=value", "path": "src/app.py",
    }))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert credential not in stored
    assert json.loads(stored) == {
        "nested": {"password": "[redacted]", "access_token": "[redacted]"},
        "url": "https://example.test/path?[redacted]", "path": "src/app.py",
    }


def test_payload_redacts_secrets_in_property_names(database, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome("error", payload={
        SECRET: 1, "nested": [{f"before {SECRET} after": "ordinary"}],
    }))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert SECRET not in stored
    assert json.loads(stored) == {
        "[redacted]": 1, "nested": [{"before [redacted] after": "ordinary"}],
    }


def test_payload_redacts_value_using_original_property_name(database, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "access_token")
    review_id = save(database, PublishOutcome("error", payload={"access_token": "opaque-credential-value"}))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert json.loads(stored) == {"[redacted]": "[redacted]"}


def test_payload_size_is_bounded_after_redaction(database):
    payload = {"nested": [{"token": "x"}] * 12_000}
    assert len(json.dumps(payload)) < 200_000
    review_id = save(database, PublishOutcome("error", payload=payload))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert len(stored) <= 200_000
    bounded = json.loads(stored)
    assert bounded["truncated"] is True
    assert '"token": "[redacted]"' in bounded["preview"]


def test_payload_redacts_long_password_before_truncation(database):
    credential = "x" * 300_000
    review_id = save(database, PublishOutcome("error", payload={"password": credential}))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert credential[:8] not in stored
    assert json.loads(stored) == {"password": "[redacted]"}


@pytest.mark.parametrize("padding_length", [199_880, 199_910])
def test_payload_redacts_configured_credential_across_truncation_boundary(database, monkeypatch, padding_length):
    credential = "".join(f"{number:02x}" for number in range(128))
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", credential)
    payload = {"body": " " * padding_length + credential + " " * 200}
    serialized = json.dumps(payload)
    assert serialized.index(credential) < 200_000 < serialized.index(credential) + len(credential)
    review_id = save(database, PublishOutcome("error", payload=payload))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert all(credential[index:index + 8] not in stored for index in range(len(credential) - 7))
    assert len(stored) <= 200_000
    bounded = json.loads(stored)
    assert bounded["truncated"] is True
    assert "[redacted]" in bounded["preview"]


@pytest.mark.parametrize("key", [
    "api_key", "api-key", "apikey", "access_token", "access-token", "accesstoken",
    "auth_token", "auth-token", "authtoken", "token", "secret", "password",
])
@pytest.mark.parametrize("credential", ["x", ""])
def test_payload_redacts_short_credentials_in_nested_lists(database, key, credential):
    key = key.upper()
    review_id = save(database, PublishOutcome("error", payload={"nested": [{key: credential}]}))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert json.loads(stored) == {"nested": [{key: "[redacted]"}]}


def test_payload_redacts_list_strings_and_preserves_other_values(database, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome("error", payload={"nested": [
        SECRET, ["https://example.test/path?private=value"],
        {"body": 'C:\\src\\app.py\n"café"', "token_count": "two", "my_secret": "ordinary",
         "token": 2, "secret": None, "password": False},
    ]}))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert SECRET not in stored
    assert stored.isascii()
    assert json.loads(stored) == {"nested": [
        "[redacted]", ["https://example.test/path?[redacted]"],
        {"body": 'C:\\src\\app.py\n"café"', "token_count": "two", "my_secret": "ordinary",
         "token": 2, "secret": None, "password": False},
    ]}


def test_payload_redacts_credentials_in_tuples(database, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome("error", payload={
        "nested": (SECRET, ({"password": "opaque-credential-value"},)),
    }))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert json.loads(stored) == {"nested": ["[redacted]", [{"password": "[redacted]"}]]}


def test_payload_preserves_nonstring_property_names(database, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome("error", payload={1: "ordinary", 2.5: SECRET, None: "value"}))
    stored = db.get_review(database[0], review_id)["publish_payload"]
    assert json.loads(stored) == {"1": "ordinary", "2.5": "[redacted]", "null": "value"}


@pytest.mark.parametrize("status,reason,summary_id,recorded", [
    ("posted", None, None, True), ("dry_run", None, None, True),
    ("error", "boom", 500, True), ("error", "partial_post", None, False),
])
def test_github_recording_keeps_existing_behavior(database, monkeypatch, status, reason, summary_id, recorded):
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SECRET)
    review_id = save(database, PublishOutcome(status, reason=reason, gh_review_id=summary_id,
                                              items=[item()], bot_login="review-bot", bot_user_id=7,
                                              payload={"body": SECRET}), key="owner/repo")
    assert len(rows(database)) == int(recorded)
    assert json.loads(db.get_review(database[0], review_id)["publish_payload"]) == {"body": SECRET}
    if recorded:
        row = rows(database)[0]
        assert (row["gh_comment_id"], row["gh_thread_id"], row["anchor_status"]) == (101, None, None)
        identity = database[1].execute("SELECT * FROM publishing_identities").fetchone()
        assert identity["bot_login"] == "review-bot" and identity["bot_user_id"] is None
