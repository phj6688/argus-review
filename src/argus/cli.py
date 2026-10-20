"""Argus command line."""

import argparse
import json

from . import doctor, providers, publish, verdict
from .client import DEFAULT_MODEL
from .diff import compress_diff, fetch_pr_files
from .review import run_review
from .repo_policy import validate_repo
from .schema import VerifiedFinding
from .tokens import HeuristicEstimator

DEFAULT_BUDGET_TOKENS = 8000


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

    args = parser.parse_args()
    if hasattr(args, "repo"):
        validate_repo(args.repo)
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


def _run_publish_placement(review_id: int, path: str | None) -> None:
    from contextlib import closing
    from .api import db

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
    files = fetch_pr_files(repo, pr)
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
            raise SystemExit(detail)


def _run_review(repo: str, pr: int) -> None:
    result = run_review(repo, pr)
    print(
        f"raw findings: {result.raw_count} | confirmed: {len(result.bugs)} bug-class, "
        f"{len(result.nits)} nit | dropped by verification: {result.dropped}"
    )
    for item in result.bugs + result.nits:
        _print_finding(item)


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
