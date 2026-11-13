"""Argus command line.

The CLI publish-placement command stays GitHub-only.
"""

import argparse
import json
import os
import sys

import httpx

from . import doctor, github, providers, publish, verdict
from .client import DEFAULT_MODEL
from .diff import compress_diff, fetch_pr_files
from .errors import HeadMoved
from .forge import forge_for
from .forge.errors import ForgeError
from .forge.keys import is_gitlab_key
from .gitlab import entry as gitlab_entry
from .redact import redact
from .review import fallback_eligible, fallback_for, from_github, run_review
from .repo_policy import validate_repo
from .schema import VerifiedFinding
from .tokens import HeuristicEstimator

DEFAULT_BUDGET_TOKENS = 8000


class RepoRejected(Exception):
    """The repo argument failed validation or the deny policy."""


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus")
    subcommands = parser.add_subparsers(dest="command", required=True)

    compress = subcommands.add_parser("compress", help="Fetch a PR diff and compress it to a token budget")
    compress.add_argument("repo", help="owner/name, e.g. example/app")
    compress.add_argument("pr", type=int)
    compress.add_argument("--budget", type=int, default=DEFAULT_BUDGET_TOKENS)

    subcommands.add_parser("doctor", help="Check the configured model provider works")

    review = subcommands.add_parser("review", help="Review a PR: deep pass, verify, rank, print findings")
    review.add_argument("repo", help="owner/name, e.g. example/app")
    review.add_argument("pr", type=int)

    preview = subcommands.add_parser("publish-preview", help="Preview a stored PR review without posting")
    preview.add_argument("review_id", type=int)
    preview.add_argument("--db", metavar="PATH", help="Review database path (default: ARGUS_DB_PATH)")

    placement = subcommands.add_parser("publish-placement", help="Check recorded publishing placement")
    placement.add_argument("review_id", type=int)
    placement.add_argument("--db", metavar="PATH", help="Review database path (default: ARGUS_DB_PATH)")

    explain = subcommands.add_parser("explain", help="Trace a finding's provenance")
    explain.add_argument("finding_id", type=int)

    quiet = subcommands.add_parser("why-quiet", help="Why Argus was quiet on a PR")
    quiet.add_argument("repo", help="owner/name, e.g. example/app")
    quiet.add_argument("pr", type=int)

    automark = subcommands.add_parser(
        "automark", help="Auto-label findings (probe/locate/decorrelated judge)")
    automark.add_argument("--repo", default=None, help="owner/name; default: all repos")
    automark.add_argument("--pr", type=int, default=None, help="only this PR number")
    automark.add_argument("--since", type=int, default=None,
                          help="only reviews within the last N days (approx)")
    automark.add_argument("--commit", action="store_true",
                          help="write the marks; default is a dry-run that prints them")
    automark.add_argument("--threshold", type=float, default=None,
                          help="min judge confidence to write a real/fp mark (default 0.75)")

    try:
        args = parser.parse_args(_normalize_args(sys.argv[1:]))
        _dispatch(args)
    except (github.GitHubAuthError, github.GitHubDenied, github.GitHubRateLimited,
            providers.BackendNotConfigured, httpx.HTTPError, RepoRejected, ForgeError, HeadMoved,
            gitlab_entry.EntryRefused, gitlab_entry.EntryDenied,
            gitlab_entry.EntryUnavailable) as error:
        raise SystemExit(_failure_line(error)) from None


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in {"review", "compress"} or any(
            flag in argv for flag in ("-h", "--help")):
        return argv
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if argv[0] == "compress" and token == "--budget":
            index += 2
        elif argv[0] == "compress" and token.startswith("--budget="):
            index += 1
        else:
            break
    if index < len(argv) and "://" in argv[index]:
        repo, pr = gitlab_entry.mr_url_args(argv[index])
        return argv[:index] + [repo, pr] + argv[index + 1:]
    return argv


def _failure_line(error: Exception) -> str:
    """One line for an expected failure, so a missing setting never prints a traceback."""
    if isinstance(error, HeadMoved):
        return "error: the merge request head moved during the read; run the command again"
    if isinstance(error, github.GitHubAuthError) and not os.environ.get("GITHUB_TOKEN", "").strip():
        return ("error: GITHUB_TOKEN is not set. Export a GitHub token with Contents:read "
                "and Pull requests:read, then run the command again.")
    if isinstance(error, github.GitHubRateLimited):
        return f"error: {error}. Try again after the reset."
    if isinstance(error, httpx.HTTPStatusError):
        source = "GitHub" if from_github(error) else error.request.url.host
        return f"error: {source} answered {error.response.status_code} for {error.request.url.path}"
    if isinstance(error, gitlab_entry.EntryUnavailable):
        return f"error: gitlab lookup failed: {error.classification}"
    if isinstance(error, (gitlab_entry.EntryRefused, gitlab_entry.EntryDenied)) or (
            isinstance(error, ForgeError) and error.forge != "github"):
        return f"error: {redact(' '.join(str(error).splitlines()))}"
    return f"error: {redact(str(error))}"


def _dispatch(args: argparse.Namespace) -> None:
    if hasattr(args, "repo") and args.command not in {"why-quiet", "automark"}:
        if is_gitlab_key(args.repo):
            args.repo = gitlab_entry.canonical_repo(args.repo)
            from .api import db
            db.init_db()
            gitlab_entry.check_change(args.repo, args.pr)
        else:
            try:
                validate_repo(args.repo)
            except ValueError as error:
                raise RepoRejected(str(error)) from error
    if args.command == "compress":
        _run_compress(args.repo, args.pr, args.budget)
    elif args.command == "doctor":
        _run_doctor()
    elif args.command == "review":
        _run_review(args.repo, args.pr)
    elif args.command == "publish-preview":
        _run_publish_preview(args.review_id, args.db)
    elif args.command == "publish-placement":
        _run_publish_placement(args.review_id, args.db)
    elif args.command == "explain":
        from . import cli_findings
        print(cli_findings.explain_finding(args.finding_id))
    elif args.command == "why-quiet":
        from . import cli_findings
        print(cli_findings.why_quiet(args.repo, args.pr))
    elif args.command == "automark":
        from . import automark
        threshold = args.threshold if args.threshold is not None else automark.DEFAULT_THRESHOLD
        automark.run_cli(repo=args.repo, pr=args.pr, since=args.since,
                         threshold=threshold, commit=args.commit)


def _run_publish_placement(review_id: int, path: str | None) -> None:
    from contextlib import closing
    from .api import db

    review = db.get_review(path, review_id)
    if review is not None and is_gitlab_key(review["repo"]):
        raise RepoRejected("publish-placement supports GitHub repositories only")
    with closing(db.connect(path)) as conn:
        items = conn.execute("SELECT * FROM published_findings WHERE review_id=? ORDER BY id",
                             (review_id,)).fetchall()
    patches = {}
    for item in items:
        check = ""
        if item["placement"] == "inline":
            key = (item["repo"], item["pr"])
            if key not in patches:
                patches[key] = {f.filename: publish.commentable_lines(f.patch)
                                for f in fetch_pr_files(*key) if f.status != "removed"}
            allowed = patches[key].get(item["path"], set())
            start, end = item["line"], item["end_line"] or item["line"]
            valid = (start > 0 and end >= start and end - start < len(allowed)
                     and all(n in allowed for n in range(start, end + 1)))
            check = f" commentable={'yes' if valid else 'no'}"
        flag = " unverified" if "unverified" in item.keys() and item["unverified"] else ""
        print(f"{item['mode']} {item['placement']} {item['path']}:{item['line']}"
              f"{check}{flag} {item['fingerprint']} {item['title']}")


def _run_publish_preview(review_id: int, path: str | None) -> None:
    from .api import db

    review = db.get_review(path, review_id)
    if review is None:
        raise SystemExit(f"Review {review_id} not found")
    if is_gitlab_key(review["repo"]):
        raise RepoRejected("publish-preview supports GitHub repositories only")
    if review["status"] != db.STATUS_DONE or review["pr"] <= 0:
        raise SystemExit("Preview requires a completed PR review")
    if not review.get("head_sha"):
        raise SystemExit("Review has no recorded head SHA")
    files = fetch_pr_files(review["repo"], review["pr"])
    document = verdict.document(review, db.get_findings(path, review_id),
                                unverified=db.get_unverified_findings(path, review_id))
    payload = publish.render_review(review["head_sha"], review_id, document, files,
                                    **publish.render_options())
    print(payload["body"])
    for comment in payload["comments"]:
        start = comment.get("start_line")
        span = f"{start}-{comment['line']}" if start else str(comment["line"])
        print(f"\n--- {comment['path']}:{span} (RIGHT) ---\n\n{comment['body']}")


def _run_compress(repo: str, pr: int, budget: int) -> None:
    files = forge_for(repo).change_files(repo, pr) if is_gitlab_key(repo) else fetch_pr_files(repo, pr)
    result = compress_diff(files, budget, HeuristicEstimator())
    print(json.dumps({
        "files_total": len(files),
        "included": result.included,
        "overflow": result.overflow,
        "deleted": result.deleted,
        "estimated_tokens": result.estimated_tokens,
        "budget": budget,
        "prompt_chars": len(result.prompt),
    }, indent=2))


def _run_doctor() -> None:
    try:
        provider = providers.resolve(DEFAULT_MODEL)
    except providers.BackendNotConfigured:
        provider = "unconfigured"
    print(f"provider: {provider}")
    print(f"model: {DEFAULT_MODEL}")
    for name, ok, detail in doctor.run_checks(DEFAULT_MODEL):
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            raise SystemExit(1)


def _run_review(repo: str, pr: int) -> None:
    result, model_used = _review_with_fallback(repo, pr)
    print(f"model used: {model_used}")
    print(
        f"raw findings: {result.raw_count} | confirmed: {len(result.bugs)} bug-class, "
        f"{len(result.nits)} nit | dropped by verification: {result.dropped}"
    )
    for item in result.bugs + result.nits:
        _print_finding(item)


def _review_with_fallback(repo: str, pr: int):
    """Review on the default model; on failure, once more on ARGUS_FALLBACK_MODEL.

    Same rule as POST /reviews and the watcher: opt-in, never for a GitHub-side
    failure, and the switch is printed, not silent. Returns (result, model that ran).
    """
    fallback_runner, fallback_model = fallback_for(
        DEFAULT_MODEL, lambda m: lambda: run_review(repo, pr, m))
    try:
        return run_review(repo, pr, DEFAULT_MODEL), DEFAULT_MODEL
    except Exception as error:  # noqa: BLE001 - any engine or transport failure may fall back
        if fallback_runner is None or not fallback_eligible(error):
            raise
        reason = redact(f"{type(error).__name__}: {error}")
        print(f"primary model {DEFAULT_MODEL} failed ({reason}); "
              f"trying fallback {fallback_model}", file=sys.stderr)
        return fallback_runner(), fallback_model


def _print_finding(item: VerifiedFinding) -> None:
    finding = item.finding
    print(f"\n[{finding.severity.value}] {finding.file}:{finding.line} "
          f"(finding conf {finding.confidence:.2f}, verify conf {item.verdict.confidence:.2f})")
    print(f"  {finding.title}")
    print(f"  {finding.description}")
    print(f"  fix: {finding.proposed_fix}")
    if finding.rule_id:
        print(f"  rule: {finding.rule_id}")


if __name__ == "__main__":
    main()
