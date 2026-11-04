"""Collect discussion outcomes independently of GitHub requests and quotas."""

from collections import defaultdict
from datetime import timedelta

from argus import acceptance, github, replies
from argus.forge.keys import parse_repo_key
from argus.forge.skips import count_skip
from argus.forge.types import ChangeRef, RepoRef
from argus.gitlab import client as client_module, projects
from argus.gitlab.body import parse_marker_kind, parse_reply_marker
from argus.gitlab.client import GitLabHTTPError, GitLabRateLimited
from argus.gitlab.reader import GitLabForge
from argus.gitlab.threads import is_bot_note, list_threads
from argus.gitlab.trust import TrustCache, author_trusted


class _MembershipClient:
    """Keep quota exceptions visible when membership lookup returns unknown."""

    def __init__(self, client):
        self.client = client
        self.rate_limit = None

    def get(self, *args, **kwargs):
        try:
            return self.client.get(*args, **kwargs)
        except (GitLabRateLimited, github.GitHubRateLimited) as error:
            self.rate_limit = error
            raise


def _mapped_threads(conn, repo, findings, threads, run):
    by_id = {thread.id: thread for thread in threads}
    by_marker = {}
    for thread in threads:
        if thread.notes and is_bot_note(thread.notes[0], repo, conn=conn):
            marker = parse_marker_kind(thread.notes[0].body)
            if marker is not None:
                by_marker.setdefault(marker, thread)
    for finding in findings:
        if finding["placement"] != "inline":
            continue
        if finding["gh_thread_id"]:
            thread = by_id.get(finding["gh_thread_id"])
            if thread is None:
                run["gitlab_threads_missing"] = run.get("gitlab_threads_missing", 0) + 1
                acceptance.log.info("outcomes: gitlab discussion missing: repo=%s pr=%s finding=%s",
                                    repo, finding["pr"], finding["id"])
                continue
        else:
            marker = ("ufp" if finding["unverified"] else "fp", finding["fingerprint"])
            thread = by_marker.get(marker)
            if thread is None:
                continue
            conn.execute(
                "UPDATE published_findings SET gh_thread_id=?, gh_comment_id=COALESCE(gh_comment_id, ?) "
                "WHERE id=? AND gh_thread_id IS NULL", (thread.id, thread.notes[0].id, finding["id"]),
            )
            finding["gh_thread_id"] = thread.id
            if finding["gh_comment_id"] is None:
                finding["gh_comment_id"] = thread.notes[0].id
        if thread.notes and is_bot_note(thread.notes[0], repo, conn=conn):
            if finding["gh_comment_id"] is None:
                conn.execute(
                    "UPDATE published_findings SET gh_comment_id=? WHERE id=? AND gh_comment_id IS NULL",
                    (thread.notes[0].id, finding["id"]),
                )
                finding["gh_comment_id"] = thread.notes[0].id
            yield finding, thread


def _thread_outcomes(conn, repo, findings, threads, pull, now, run, client, cache):
    recent = pull["state"] == "open" or (
        pull["closed_at"] and acceptance._stamp(pull["closed_at"]) >= acceptance._stamp(now) - timedelta(days=7)
    )
    for finding, thread in _mapped_threads(conn, repo, findings, threads, run):
        ref = parse_repo_key(repo)
        finding.update(thread_resolved=thread.resolved if thread.resolvable else None,
                       gitlab_thread=thread, gitlab_change=ChangeRef(RepoRef("gitlab", ref.host, None, ref.project_id),
                                                                    finding["pr"]))
        for note in thread.notes[1:]:
            if (is_bot_note(note, repo, conn=conn) or parse_marker_kind(note.body) is not None
                    or parse_reply_marker(note.body) is not None):
                continue
            lookup = _MembershipClient(client)
            trusted = author_trusted(repo, note.author_id, client=lookup, cache=cache)
            if lookup.rate_limit is not None:
                raise lookup.rate_limit
            acceptance._record(
                conn, finding["id"], "reply", acceptance.classify_reply(note.body), note.id,
                now, note.created_at, note.body, None, note.username,
                author_trusted=int(trusted) if trusted is not None else None,
            )
        if thread.resolvable and recent:
            acceptance._record(conn, finding["id"], "resolved", str(thread.resolved).lower(),
                               thread.notes[0].id, now)


def collect_repo(conn, repo, findings, now, since_default, run, *, cache: TrustCache, path=None, comparisons=None):
    comparisons = {} if comparisons is None else comparisons
    client = client_module.client()
    decision = projects.live_deny(repo, client=client, db_path=path)
    if not decision.allowed:
        acceptance.log.info("outcomes: gitlab project denied: %s", decision.reason)
        count_skip(run)
        if decision.reason == "lookup failed: rate_limited":
            raise GitLabRateLimited(client.pause_until())
        if decision.reason.startswith("lookup failed: "):
            raise GitLabHTTPError("GitLab project lookup failed", reason="http")
        return
    run["repos"] += 1
    ref = parse_repo_key(repo)
    reader = GitLabForge(client)
    by_pr = defaultdict(list)
    for finding in findings:
        by_pr[finding["pr"]].append(finding)
    for pr, items in by_pr.items():
        try:
            stored = conn.execute("SELECT state, closed_at FROM outcome_prs WHERE repo=? AND pr=?",
                                  (repo, pr)).fetchone()
            if (stored and stored["state"] == "closed" and stored["closed_at"]
                    and acceptance._stamp(stored["closed_at"]) < now - timedelta(days=7)
                    and not conn.execute(
                        "SELECT 1 FROM pr_heads WHERE repo=? AND pr=? AND is_open=1", (repo, pr),
                    ).fetchone()):
                run["prs_skipped_final"] += 1
                continue
            run["prs_checked"] += 1
            pull = reader.outcome_meta(repo, pr)
            threads = list_threads(ChangeRef(RepoRef("gitlab", ref.host, None, ref.project_id), pr),
                                   client=client)
            # Warm the shared cache, including unknown results, before taking a write lock.
            for thread in threads:
                if not thread.notes or not is_bot_note(thread.notes[0], repo, conn=conn):
                    continue
                for note in thread.notes[1:]:
                    if (is_bot_note(note, repo, conn=conn) or parse_marker_kind(note.body) is not None
                            or parse_reply_marker(note.body) is not None):
                        continue
                    lookup = _MembershipClient(client)
                    author_trusted(repo, note.author_id, client=lookup, cache=cache)
                    if lookup.rate_limit is not None:
                        raise lookup.rate_limit
            with conn:
                conn.execute(
                    "INSERT INTO outcome_prs VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(repo, pr) DO UPDATE SET state=excluded.state, merged=excluded.merged, "
                    "closed_at=excluded.closed_at, head_sha=excluded.head_sha, checked_at=excluded.checked_at",
                    (repo, pr, pull["state"], int(pull["merged"]), pull["closed_at"], pull["head_sha"], now.isoformat()),
                )
                _thread_outcomes(conn, repo, items, threads, pull, now.isoformat(), run, client, cache)
            replies.run(conn, repo, items, pull["head_sha"], comparisons, now.isoformat(), run)
        except (GitLabRateLimited, github.GitHubRateLimited):
            raise
        except Exception as error:
            acceptance._failure(run, repo, error, pr)
    with conn:
        conn.execute(
            "INSERT INTO outcome_cursors(repo, since, last_collected_at) VALUES (?, ?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET last_collected_at=excluded.last_collected_at",
            (repo, since_default, now.isoformat()),
        )
