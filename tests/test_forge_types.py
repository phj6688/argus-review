from dataclasses import FrozenInstanceError, fields

import pytest

from argus.forge import (
    ChangeListing,
    ChangeMeta,
    ChangeRef,
    Comparison,
    DiffRefs,
    Note,
    PostedComment,
    RepoRecord,
    RepoRef,
    Thread,
)


REPO = RepoRef(forge="github", host="github.com", path="owner/name", project_id=None)
NOTE = Note(
    id=10,
    thread_id="thread-1",
    in_reply_to=None,
    author_login="reviewer",
    author_is_bot=False,
    trusted=None,
    body="Please check this branch.",
    created_at="2026-11-02T10:00:00Z",
    updated_at=None,
)


pytestmark = pytest.mark.parametrize(
    "record_type,values",
    [
        pytest.param(
            RepoRef,
            dict(forge="github", host="github.com", path="owner/name", project_id=None),
            id="github-repo",
        ),
        pytest.param(
            RepoRef,
            dict(forge="gitlab", host="gitlab.example.com", path=None, project_id=42),
            id="gitlab-repo",
        ),
        pytest.param(ChangeRef, dict(repo=REPO, number=7), id="change"),
        pytest.param(
            DiffRefs,
            dict(base_sha="base", start_sha="start", head_sha="head"),
            id="diff-refs",
        ),
        pytest.param(
            ChangeMeta,
            dict(
                head_sha="head",
                base_sha="base",
                state="open",
                merged=False,
                draft=True,
                author_id="12",
                author_login="contributor",
                author_is_bot=False,
                title="Update checks",
                updated_at="2026-11-02T10:00:00Z",
                source_repo=RepoRef("github", "github.com", "contributor/name", None),
            ),
            id="fork-meta",
        ),
        pytest.param(
            ChangeMeta,
            dict(
                head_sha=None,
                base_sha=None,
                state="closed",
                merged=True,
                draft=False,
                author_id=None,
                author_login=None,
                author_is_bot=True,
                title="Update checks",
                updated_at=None,
                source_repo=None,
            ),
            id="unknown-meta",
        ),
        pytest.param(
            Comparison,
            dict(status="ahead", files=("src/example.py",), complete=True),
            id="comparison",
        ),
        pytest.param(
            PostedComment,
            dict(id=10, thread_id="thread-1", anchored=True, placement="inline"),
            id="inline-comment",
        ),
        pytest.param(
            PostedComment,
            dict(id="comment-1", thread_id=None, anchored=False, placement="summary"),
            id="summary-comment",
        ),
        pytest.param(
            Note,
            dict(
                id=10,
                thread_id="thread-1",
                in_reply_to=None,
                author_login="reviewer",
                author_is_bot=False,
                trusted=None,
                body="Please check this branch.",
                created_at="2026-11-02T10:00:00Z",
                updated_at=None,
            ),
            id="root-note",
        ),
        pytest.param(
            Note,
            dict(
                id="note-2",
                thread_id=None,
                in_reply_to="note-1",
                author_login=None,
                author_is_bot=True,
                trusted=False,
                body="Checked.",
                created_at=None,
                updated_at="2026-11-02T11:00:00Z",
            ),
            id="reply-note",
        ),
        pytest.param(
            Thread,
            dict(
                id="thread-1",
                resolved=False,
                resolvable=True,
                root_comment_id=10,
                head_sha="head",
                notes=(NOTE,),
            ),
            id="thread",
        ),
        pytest.param(
            RepoRecord,
            dict(path="owner/name", archived=False, default_branch="main", visibility="private"),
            id="repo-record",
        ),
        pytest.param(
            ChangeListing,
            dict(
                changes=(ChangeRef(REPO, 7),),
                closed=(ChangeRef(REPO, 3),),
                cursor="next-page",
                complete=False,
            ),
            id="change-listing",
        ),
    ]
)


def test_record_construction(record_type, values):
    record = record_type(**values)

    assert {field.name: getattr(record, field.name) for field in fields(record)} == values


def test_record_fields_are_immutable(record_type, values):
    record = record_type(**values)

    for field in fields(record):
        with pytest.raises(FrozenInstanceError):
            setattr(record, field.name, getattr(record, field.name))


def test_records_have_value_equality_and_hashes(record_type, values):
    record = record_type(**values)
    equal_record = record_type(**values)

    assert equal_record is not record
    assert record == equal_record
    assert hash(record) == hash(equal_record)
    assert {record: "found"}[equal_record] == "found"
