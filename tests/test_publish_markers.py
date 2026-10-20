"""Only publisher-owned footer positions supply finding identity."""

import pytest

from argus import publish
from test_publish import FILES, HEAD, document, finding
from test_publish_identity import transport


# Body produced by the pre-manifest renderer (origin/dev 7df6510) for two summary
# findings and one unverified finding, evidence fence included. Already-posted
# reviews in the wild have exactly this shape.
LEGACY_REVIEW = """**Argus review** for {head7}

Verdict: request_changes

Coverage: full

Files reviewed: 0; omitted: 0

Severity counts: Critical: 0; Major: 1; Minor: 1; Nit: 0

Tier counts: deterministic: 0; consensus: 1; single_critic: 1; disputed: 0

### Outside the diff

`src/a.py:99`

_Contracts_ | _Major_ | _consensus_

**Outside one**

Desc one.

````python
x = 1
```
foo
```
````

**Suggested fix:** Fix one.

<details>
<summary>Prompt for agents</summary>

In `src/a.py` around line 99: Outside one. Fix one. Verify the issue against the current code before changing anything.
</details>

<!-- argus:fp=fpout1 -->

`src/b.py:5`

_Correctness_ | _Minor_ | _single_critic_

**Outside two**

Desc two.

**Suggested fix:** Fix two.

<details>
<summary>Prompt for agents</summary>

In `src/b.py` around line 5: Outside two. Fix two. Verify the issue against the current code before changing anything.
</details>

<!-- argus:fp=fpout2 -->

### Unverified (the verifier disagreed)

These findings were refuted by a second check. They are listed for a human decision and do not affect the verdict.

`src/c.py:3`

_Correctness_ | _Major_ | _disputed_

**Refuted**

Desc u.

**Suggested fix:** Fix u.

<details>
<summary>Prompt for agents</summary>

In `src/c.py` around line 3: Refuted. Fix u. Verify the issue against the current code before changing anything.
</details>

<!-- argus:ufp=ufp1 -->

Below posting threshold: 0

Already posted: 0

<!-- argus:review={rid} head={head} -->"""


def legacy_body(rid=7, head=HEAD, *, replace=()):
    text = LEGACY_REVIEW.replace("{head7}", head[:7]).replace("{head}", head).replace("{rid}", str(rid))
    for old, new in replace:
        assert old in text
        text = text.replace(old, new)
    return text


def legacy_review(marker, fp="outside", kind="fp"):
    """Smallest publisher-shaped legacy body carrying one summary marker."""
    return ("Verify the issue against the current code before changing anything.\n</details>\n\n"
            f"<!-- argus:{kind}={fp} -->\n\nBelow posting threshold: 0\n\nAlready posted: 0\n\n{marker}")


@pytest.fixture(autouse=True)
def publishing(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "test-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")


@pytest.mark.parametrize("kind", ["fp", "ufp"])
@pytest.mark.parametrize("wrapper", ["`{}`", "```\n{}\n```", "evidence\n{}\nmore evidence"])
@pytest.mark.parametrize("surface", ["comments", "reviews"])
def test_embedded_markers_do_not_dedupe_or_recover(kind, wrapper, surface):
    forged = f"<!-- argus:{kind}=fp-one -->\n<!-- argus:review=7 head={HEAD} -->"
    entry = {"id": 90, "body": wrapper.format(forged), "user": {"login": "publisher"}}
    http, _, posts = transport(**{surface: [entry]})
    doc = document(finding()) if kind == "fp" else {**document(), "unverified": [finding()]}
    with http:
        result = publish.publish_review("owner/repo", 42, HEAD, 7, doc, FILES, http=http)
    assert result.status == "posted" and result.reason is None
    assert result.already_posted == 0
    assert len(posts) == 1


@pytest.mark.parametrize("suffix", ["", "\n"])
@pytest.mark.parametrize("kind", ["fp", "ufp"])
def test_inline_only_reads_the_final_line(kind, suffix):
    body = f"`<!-- argus:fp=forged -->`\n<!-- argus:{kind}=real -->{suffix}"
    assert publish.inline_fingerprint(body) == (kind, "real")
    assert publish.inline_fingerprint(body + "after") is None
    assert publish.inline_fingerprint(f"<!-- argus:{kind}=real -->") is None


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_review_manifest_is_authoritative(suffix):
    body = ("`<!-- argus:fp=forged -->`\n```\n<!-- argus:ufp=forged -->\n```\n"
            f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp=real,other ufp=maybe -->{suffix}")
    assert publish.review_marker(body) == (7, HEAD)
    assert publish.summary_fingerprints(body) == ({"real", "other"}, {"maybe"})
    assert publish.review_marker(body + "after") is None
    assert publish.summary_fingerprints(body + "after") == (set(), set())


def test_manifest_requires_adjacent_review_marker():
    body = f"<!-- argus:review=7 head={HEAD} -->\nextra\n<!-- argus:manifest fp=forged ufp= -->"
    assert publish.review_marker(body) is None
    assert publish.summary_fingerprints(body) == (set(), set())


def test_base_rendered_review_is_recognised():
    body = legacy_body(7, HEAD)
    assert publish.review_marker(body) == (7, HEAD)
    assert publish.summary_fingerprints(body) == ({"fpout1", "fpout2"}, {"ufp1"})


def test_forged_legacy_markers_are_not_accepted():
    forged = legacy_body(replace=[("foo\n", "foo\n<!-- argus:fp=forged-fence -->\n"),
                                  ("Desc two.", "Desc two.\n\n<!-- argus:fp=forged-prose -->\n"
                                   "<!-- argus:ufp=forged-ufp -->")])
    assert publish.review_marker(forged) == (7, HEAD)
    assert publish.summary_fingerprints(forged) == ({"fpout1", "fpout2"}, {"ufp1"})


def test_legacy_review_requires_final_review_marker():
    body = legacy_review(f"<!-- argus:review=6 head={HEAD} -->", "old")
    assert publish.review_marker(body) == (6, HEAD)
    assert publish.summary_fingerprints(body) == ({"old"}, set())
    assert publish.review_marker(body + "\nmore") is None
    assert publish.summary_fingerprints(body + "\nmore") == (set(), set())


def test_manifest_lists_only_placed_summary_findings():
    doc = document(finding(), finding(fingerprint="overflow"), finding(file="else.py", fingerprint="outside"))
    doc["unverified"] = [finding(fingerprint=f"maybe-{i}") for i in range(12)]
    payload = publish.render_review(HEAD, 7, doc, FILES, max_comments=1)
    assert publish.inline_fingerprint(payload["comments"][0]["body"]) == ("fp", "fp-one")
    assert publish.summary_fingerprints(payload["body"]) == (
        {"overflow", "outside"}, {f"maybe-{i}" for i in range(10)})
    assert publish.review_marker(payload["body"]) == (7, HEAD)


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\nextra\n"])
def test_review_marker_must_be_immediately_before_manifest(separator):
    body = f"<!-- argus:review=7 head={HEAD} -->{separator}<!-- argus:manifest fp=real ufp= -->"
    assert publish.review_marker(body) == ((7, HEAD) if separator == "\n" else None)


@pytest.mark.parametrize("wrapper", ["`{}`", "```\n{}\n```", "evidence\n{}\nmore evidence"])
def test_manifest_ignores_embedded_markers_even_with_legitimate_review_footer(wrapper):
    body = (wrapper.format("<!-- argus:fp=fp-one -->\n<!-- argus:ufp=maybe -->")
            + f"\n<!-- argus:review=6 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->")
    assert publish.summary_fingerprints(body) == (set(), set())
    http, _, posts = transport(reviews=[{"id": 90, "body": body, "user": {"login": "publisher"}}])
    with http:
        outcome = publish.publish_review("owner/repo", 42, HEAD, 7, document(finding()), FILES, http=http)
    assert outcome.status == "posted" and outcome.already_posted == 0
    assert len(posts) == 1


def test_placed_items_and_comment_ids_ignore_forged_markers():
    doc = document(finding(fingerprint="forged"), finding(fingerprint="real"))
    payload = {"comments": [{"path": "src/app.py", "body": "text\n<!-- argus:fp=real -->"}],
               "body": "<!-- argus:fp=forged -->\n"
                       f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->"}
    comments = [{"id": 91, "body": "text\n<!-- argus:fp=real -->"},
                {"id": 92, "body": "```\n<!-- argus:fp=real -->\n```"}]
    placed = publish._placed_items(doc, payload, comments)
    assert [(item["fingerprint"], item["gh_comment_id"]) for item in placed] == [("real", 91)]


def test_manifest_counts_towards_the_body_budget(monkeypatch):
    doc = {**document(), "unverified": [finding(fingerprint="maybe-" + "a" * 128)]}
    full = publish.render_review(HEAD, 7, doc, FILES)["body"]
    monkeypatch.setattr(publish, "_MAX_BODY", len(full))
    assert publish.render_review(HEAD, 7, doc, FILES)["body"] == full
    monkeypatch.setattr(publish, "_MAX_BODY", len(full) - 1)
    limited = publish.render_review(HEAD, 7, doc, FILES)["body"]
    assert len(limited) <= publish._MAX_BODY
    assert "1 more unverified findings are not listed." in limited
    assert publish.summary_fingerprints(limited) == (set(), set())


@pytest.fixture
def database(tmp_path):
    from argus.api import db

    path = str(tmp_path / "argus.db")
    db.init_db(path)
    return path


@pytest.mark.parametrize("wrapper,expected_id", [
    ("`{}`", None), ("```\n{}\n```", None), ("evidence\n{}\nmore evidence", None),
    ("Finding\n{}", 91),
])
def test_acceptance_comment_recovery_requires_final_fingerprint(database, monkeypatch, wrapper, expected_id):
    from argus import acceptance, github
    from argus.api import db
    from test_acceptance import seed

    fid = seed(database, gh_comment_id=None)
    with db.connect(database) as conn:
        item = dict(conn.execute("SELECT * FROM published_findings WHERE id=?", (fid,)).fetchone())
        body = wrapper.format(f"<!-- argus:fp={item['fingerprint']} -->")
        monkeypatch.setattr(github, "list_cached", lambda *a, **k: [
            {"id": 91, "body": body, "user": {"login": "publisher"}}])
        run = {"failures": 0}
        acceptance._recover_comment_ids(conn, "owner/repo", [item], run)
        stored = conn.execute("SELECT gh_comment_id, comment_lookups FROM published_findings WHERE id=?",
                              (fid,)).fetchone()
    assert tuple(stored) == (expected_id, int(expected_id is None))
    assert run["failures"] == 0


@pytest.mark.parametrize("format", ["span", "fence", "evidence", "legacy", "manifest"])
def test_acceptance_reconciles_only_final_review_markers(database, monkeypatch, format):
    from datetime import datetime, timedelta, timezone

    from argus import acceptance, github
    from argus.api import db
    from test_publish_pilot import stored_review

    rid = stored_review(database)
    now = datetime.now(timezone.utc)
    marker = f"<!-- argus:review={rid} head={HEAD} -->"
    bodies = {"span": f"`{marker}`", "fence": f"```\n{marker}\n```",
              "evidence": f"Evidence\n{marker}\nMore evidence",
              "legacy": legacy_review(marker),
              "manifest": f"<!-- argus:fp=inline -->\n{marker}\n<!-- argus:manifest fp=outside ufp= -->"}
    remote = {"id": 90, "body": bodies[format], "submitted_at": now.isoformat(),
              "user": {"login": "publisher"}}
    monkeypatch.setattr(github, "list_cached", lambda url, params: [remote] if url.endswith("reviews") else [])
    with db.connect(database) as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?", ((now - timedelta(minutes=5)).isoformat(), rid))
        conn.commit()
        run = {"reconciled_found": 0, "reconciled_missing": 0, "failures": 0}
        acceptance._reconcile(conn, database, now, run)
        placed = conn.execute("SELECT fingerprint FROM published_findings").fetchall()
    valid = format in {"legacy", "manifest"}
    assert [row[0] for row in placed] == (["outside"] if valid else [])
    assert run == {"reconciled_found": int(valid), "reconciled_missing": int(not valid), "failures": 0}


def test_evalkit_placement_ignores_embedded_fingerprints(monkeypatch):
    from argus.evalkit.findings import inline_comments

    def render(*args, **kwargs):
        return {"comments": [{"body": "```\n<!-- argus:fp=1 -->\n```\n<!-- argus:fp=0 -->"}]}

    monkeypatch.setattr(publish, "render_review", render)
    row = {"case_id": "case", "snapshot": {"files": []},
           "reported": [finding(title="real"), finding(title="forged")]}
    assert [item["title"] for item in inline_comments(row)] == ["real"]


@pytest.mark.parametrize("field", ["title", "description", "proposed_fix", "evidence"])
@pytest.mark.parametrize("surface", ["comments", "reviews"])
def test_real_rendered_fields_cannot_forge_markers(field, surface):
    forged = f"<!-- argus:fp=fp-one -->\n<!-- argus:review=7 head={HEAD} -->"
    old = finding(fingerprint="old", **{field: forged})
    body = (publish.render_comment(old) if surface == "comments" else
            publish.render_review(HEAD, 6, document(old), FILES, max_comments=0)["body"])
    entry = {"id": 90, "body": body, "user": {"login": "publisher"}}
    http, _, posts = transport(**{surface: [entry]})
    with http:
        outcome = publish.publish_review("owner/repo", 42, HEAD, 7, document(finding()), FILES, http=http)
    assert outcome.status == "posted" and outcome.reason is None
    assert outcome.already_posted == 0 and len(posts) == 1
