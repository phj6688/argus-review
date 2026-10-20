"""Run the production reviewer on cached historical changes."""

import argparse
import inspect
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from argus import client, providers, review
from argus.client import DEFAULT_MODEL
from argus.jarvis import live

from .cases import Case, load_cases
from .snapshot import SnapshotStore

RUNNING_CHECKOUT = Path(__file__).resolve().parents[3]
BUILD_SHA_ENV = "ARGUS_GIT_SHA"
UNKNOWN_VERSION = ("unknown", True)
EVAL_TOKEN_ENV = "ARGUS_EVAL_AUTH_TOKEN"
MIRROR_ROOT_ENV = "ARGUS_MIRROR_ROOT"
CACHE_ENV = "ARGUS_EVAL_CACHE"
GIT_TIMEOUT_S = 30


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _git_output(root: Path, *args: str) -> str:
    # GIT_DIR and its relatives, set for example inside a git hook, would point git at
    # another repository and label rows with its SHA.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, timeout=GIT_TIMEOUT_S, env=environment).stdout


def _checkout_version(root: Path) -> tuple[str, bool]:
    try:
        sha = _git_output(root, "rev-parse", "HEAD").strip()
        changes = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VERSION
    return sha, bool(changes.strip())


def code_version(root: Path | None = None) -> tuple[str, bool]:
    """Name the reviewer code that runs, as (sha, dirty).

    A git checkout at root, by default the one that holds this module, gives
    its HEAD; it is dirty when a tracked file has a staged or unstaged change.
    Untracked files do not count. A checkout is present when root holds a
    .git entry (a directory, a file in a worktree, or a link, even a dangling
    one); a checkout that git cannot read is ("unknown", True), never the
    build SHA. Without a checkout, an image gives the build SHA it carries in
    ARGUS_GIT_SHA, with dirty false. Anything else is ("unknown", True). A
    caller cannot supply the label.
    """
    root = root or RUNNING_CHECKOUT
    # Presence is checked without git, so a failed or slow git cannot pass as an image.
    marker = root / ".git"
    # exists() follows links, and a dangling .git link is still a checkout git cannot read.
    if marker.is_symlink() or marker.exists():
        return _checkout_version(root)
    build_sha = os.environ.get(BUILD_SHA_ENV, "").strip()
    if build_sha and build_sha != UNKNOWN_VERSION[0]:
        return build_sha, False
    return UNKNOWN_VERSION


@contextmanager
def model_credential(model: str):
    """Yield the variable name of the credential the reviewer model uses.

    A set ARGUS_EVAL_AUTH_TOKEN replaces the gateway token until the block
    ends, so offline replays are not booked to production and later work in
    the same process gets its own token back.
    """
    provider = providers.resolve(model)
    if provider != providers.GATEWAY:
        credentials = {
            providers.ANTHROPIC: client.ENV_ANTHROPIC_KEY,
            providers.OPENROUTER: client.ENV_OPENROUTER_KEY,
            providers.OPENAI: client.ENV_OPENAI_KEY,
            providers.CLAUDE_CLI: (
                "CLAUDE_CODE_OAUTH_TOKEN" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                else "claude login"
            ),
            providers.CODEX_CLI: "codex login",
        }
        yield credentials[provider]
        return
    eval_token = os.environ.get(EVAL_TOKEN_ENV)
    original = os.environ.get(client.ENV_AUTH_TOKEN)
    if eval_token:
        os.environ[client.ENV_AUTH_TOKEN] = eval_token
    try:
        yield EVAL_TOKEN_ENV if eval_token else client.ENV_AUTH_TOKEN
    finally:
        if eval_token and original is None:
            os.environ.pop(client.ENV_AUTH_TOKEN, None)
        elif eval_token:
            os.environ[client.ENV_AUTH_TOKEN] = original


def _eval_data_root() -> Path:
    return Path.home() / "data/argus-eval"


def default_mirror_root() -> str | None:
    configured = os.environ.get(MIRROR_ROOT_ENV)
    if configured:
        return configured
    fallback = _eval_data_root() / "mirrors"
    return str(fallback) if fallback.is_dir() else None


def default_cache_dir() -> str:
    return os.environ.get(CACHE_ENV) or str(_eval_data_root() / "cache")


# A live review reads CLAUDE.md from the default branch at review time, and the
# PR base stands in for that commit. The mirror HEAD holds rules written later.
_HOUSE_RULES_REF: ContextVar[str] = ContextVar("house_rules_ref", default="HEAD")


@contextmanager
def _house_rules_at(ref: str):
    token = _HOUSE_RULES_REF.set(ref)
    try:
        yield
    finally:
        _HOUSE_RULES_REF.reset(token)


def install_mirror_mode(mirror_root: str | Path):
    root = Path(mirror_root)
    original_file_at = live._fetch_file_at
    original_house_rules = review.fetch_house_rules

    def mirror(repo: str) -> Path | None:
        path = root / (repo.rsplit("/", 1)[-1] + ".git")
        return path if path.is_dir() else None

    def fetch_file_at(repo: str, path: str, ref: str) -> str | None:
        repo_mirror = mirror(repo)
        if repo_mirror is None:
            return original_file_at(repo, path, ref)
        result = subprocess.run(["git", "--git-dir", str(repo_mirror), "show", f"{ref}:{path}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            return None
        return live.decode_file_text(result.stdout)

    def fetch_house_rules(repo: str) -> str | None:
        repo_mirror = mirror(repo)
        if repo_mirror is None:
            return original_house_rules(repo)
        result = subprocess.run(["git", "--git-dir", str(repo_mirror), "show",
                                 f"{_HOUSE_RULES_REF.get()}:CLAUDE.md"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            return None
        return result.stdout.decode("utf-8", "replace")[:review.HOUSE_RULES_MAX_CHARS]

    live._fetch_file_at = fetch_file_at
    review.fetch_house_rules = fetch_house_rules

    def restore():
        live._fetch_file_at = original_file_at
        review.fetch_house_rules = original_house_rules

    return restore


def snapshot_summary(snapshot) -> dict:
    data = snapshot.to_dict()
    return {key: data[key] for key in ("repo", "base_sha", "head_sha", "source", "patch_sha256",
                                       "truncated", "files")}


def serialize_finding(item) -> dict:
    data = item.model_dump(mode="json")
    if hasattr(item, "finding"):
        # Dump the actual finding too: base-typed nested fields omit subclass fields.
        data.update(item.finding.model_dump(mode="json"))
        data.pop("finding", None)
    verifier = data.get("verdict") or {}
    data.setdefault("verdict_is_real", verifier.get("is_real"))
    data.setdefault("verdict_confidence", verifier.get("confidence"))
    for name, default in (("lane", "llm"), ("judge", None), ("fingerprint", ""),
                          ("rule_id", None)):
        data.setdefault(name, default)
    return data


def _accepted(function, kwargs: dict) -> dict:
    parameters = inspect.signature(function).parameters
    arbitrary = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    return {key: value for key, value in kwargs.items()
            if arbitrary or (key in parameters and parameters[key].kind != inspect.Parameter.POSITIONAL_ONLY)}


def replay_case(case: Case, run: int, variant: str, store: SnapshotStore,
                model: str, bug_cap: int, provenance: dict, github_mode: str = "api") -> dict:
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    record = {"variant": variant, "case_id": case.case_id, "run": run,
              **provenance, "model": model,
              "github_mode": github_mode,
              "started_at": started, "error": None, "coverage": [],
              "reported": [], "raw": [], "snapshot": None}
    try:
        snapshot = store.get(case)
        record["snapshot"] = snapshot_summary(snapshot)
        optional = _accepted(review.run_diff_review, {
            "pr_title": case.pr_title, "pr_body": case.pr_body, "base_sha": case.base_sha,
            "file_loader": store.file_loader(case), "fast": False,
            # Live reviews of a change without a PR (POST /review-diff) send no commits.
            "commits": store.commit_messages(case) if case.pr else None})
        with _house_rules_at(snapshot.base_sha):
            result = review.run_diff_review(list(snapshot.files), repo=case.repo,
                                            head_sha=case.head_sha, model=model,
                                            bug_cap=bug_cap, **optional)
        record["coverage"] = getattr(result, "coverage", [])
        record["reported"] = [serialize_finding(f) for f in result.bugs + result.nits]
        raw = getattr(result, "all_findings", getattr(result, "raw_findings", []))
        record["raw"] = [serialize_finding(f) for f in raw]
        record["snapshot"] = snapshot_summary(snapshot)
    except Exception as error:
        record["error"] = {"class": type(error).__name__, "message": str(error)}
    record["ended_at"] = datetime.now(timezone.utc).isoformat()
    record["duration_s"] = time.monotonic() - clock
    return record


def read_complete_rows(path: Path, what: str = "replay output") -> list[dict]:
    """Read the JSON lines of an append-only file, repairing the tail a stopped process left.

    A torn last line is cut off and a missing final newline is added, so the next
    append starts on a clean line. A bad line anywhere else raises.
    """
    if not path.exists():
        return []
    content = path.read_bytes()
    lines = content.splitlines(keepends=True)
    offset = 0
    rows = []
    for index, line in enumerate(lines):
        if not line.strip():
            offset += len(line)
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise ValueError(f"invalid {what} at line {index + 1}") from None
            # A process stopped mid-write; keep every complete record.
            with path.open("r+b") as stream:
                stream.truncate(offset)
            return rows
        rows.append(row)
        offset += len(line)
    if content and not content.endswith(b"\n"):
        with path.open("ab") as stream:
            stream.write(b"\n")
    return rows


def _resume(path: Path) -> set[tuple[str, str, int]]:
    return {(row["variant"], row["case_id"], row["run"]) for row in read_complete_rows(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--split", choices=("dev", "heldout"))
    parser.add_argument("--case-ids")
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument("--runs", type=positive_int, default=1)
    parser.add_argument("--concurrency", type=positive_int, default=1)
    parser.add_argument("--mirror-root", default=default_mirror_root())
    parser.add_argument("--github-mode", choices=("api", "mirror"))
    parser.add_argument("--cache-dir", default=default_cache_dir())
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--bug-cap", type=positive_int, default=10)
    args = parser.parse_args(argv)
    if args.github_mode is None:
        args.github_mode = "mirror" if args.mirror_root else "api"
    cases = load_cases(args.cases)
    ids = {s.strip() for s in args.case_ids.split(",")} if args.case_ids else None
    cases = [c for c in cases if (not args.split or c.split == args.split)
             and (ids is None or c.case_id in ids)]
    if args.limit:
        cases = cases[:args.limit]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _resume(out)
    tasks = [(case, run) for case in cases for run in range(1, args.runs + 1)
             if (args.variant, case.case_id, run) not in seen]
    if not tasks:
        return 0
    store = SnapshotStore(args.mirror_root, args.cache_dir)
    code_sha, code_dirty = code_version()
    restore = install_mirror_mode(args.mirror_root) if args.github_mode == "mirror" else None
    try:
        # The pool exits first, so every worker finishes before the credential is restored.
        with (model_credential(args.model) as credential_env,
              out.open("a", encoding="utf-8") as stream,
              ThreadPoolExecutor(args.concurrency) as pool):
            provenance = {"code_sha": code_sha, "code_dirty": code_dirty,
                          "credential_env": credential_env}
            futures = [pool.submit(replay_case, case, run, args.variant, store,
                                   args.model, args.bug_cap, provenance, args.github_mode)
                       for case, run in tasks]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
                stream.flush()
                status = record["error"]["class"] if record["error"] else f"{len(record['reported'])} findings"
                print(f"{args.variant} {record['case_id']} run={record['run']} "
                      f"{record['duration_s']:.2f}s {status}", file=sys.stderr)
    finally:
        if restore:
            restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
