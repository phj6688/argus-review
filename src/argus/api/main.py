"""FastAPI layer over the Argus review engine.

Thin wrapper: POST /reviews kicks run_review off in a background task and returns
an id immediately with status running; the task persists the ReviewResult and
flips the row to done (or error). No review logic lives here.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from .. import acceptance, diff, doctor, github, providers, publish, rules, verdict
from ..client import DEFAULT_MODEL, ping
from ..errors import HeadMoved
from ..diff import FilePatch, parse_unified_diff
from ..review import (
    DEFAULT_BUG_CAP, ENV_FALLBACK_MODEL, ReviewResult, fallback_eligible, fallback_for, run_diff_review,
    run_review,
)
from ..repo_policy import RepoDenied, validate_repo, watch_owners
from . import benchmark, db, metrics, watch_config, watch_metrics, watcher

log = logging.getLogger("argus.api")

def _resolve_cr_threads_path() -> Path:
    """CR thread corpus path. Defaults to the repo-root data dir (parents[3] ==
    project dir, the same anchor db.py uses), correct when the package runs from a
    source checkout. ARGUS_CR_THREADS_PATH overrides it for the installed/container
    layout, where the package lives under site-packages and the computed parent is
    not the repo root."""
    override = os.environ.get("ARGUS_CR_THREADS_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "cr_threads.jsonl"


# Module-level so tests can monkeypatch the resolved value.
CR_THREADS_PATH = _resolve_cr_threads_path()

# Verdict-confidence floor surfaced to the console. The engine ranks on severity,
# not a hard confidence gate yet, so this is the documented default, not enforced.
MIN_CONFIDENCE = 0.55

# Public API base; deployments override the local development default.
API_BASE_URL = os.environ.get("ARGUS_PUBLIC_API_URL", "http://127.0.0.1:8350")

_REPORT_WINDOWS = {"daily": 1, "weekly": 7, "monthly": 30}


async def require_api_token(request: Request) -> None:
    if os.environ.get("ARGUS_AUTH_MODE", "token") == "proxy":
        return
    if request.method == "GET" and request.scope["route"].path == "/health":
        try:
            if int(request.query_params.get("deep", "0")) == 0:
                return
        except ValueError:
            pass
    token = os.environ.get("ARGUS_API_TOKEN", "")
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if not (token and scheme.lower() == "bearer"
            and hmac.compare_digest(credentials.encode(), token.encode())):
        raise HTTPException(status_code=401, detail="Invalid or missing API token",
                            headers={"WWW-Authenticate": "Bearer"})


def configure_logging():
    logger = logging.getLogger("argus")
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "_argus_stdout", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler._argus_stdout = True
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    auth_mode = os.environ.get("ARGUS_AUTH_MODE", "token")
    if auth_mode not in {"token", "proxy"}:
        raise RuntimeError("ARGUS_AUTH_MODE must be token or proxy")
    if auth_mode == "token" and len(os.environ.get("ARGUS_API_TOKEN", "")) < 32:
        raise RuntimeError(
            "ARGUS_API_TOKEN must be at least 32 characters when ARGUS_AUTH_MODE=token. "
            "Generate one with openssl rand -hex 32."
        )
    if auth_mode == "proxy":
        log.warning("ARGUS_AUTH_MODE=proxy: API authentication relies on an authenticating reverse proxy")
    db.init_db()
    # Any row still `running` at boot was owned by a process that is now gone (a
    # redeploy is the usual cause): retire it so it stops showing as a phantom
    # live review. Runs before the watcher so it only ever sees pre-existing rows.
    # Single-instance assumption: it retires every running row, so with multiple
    # replicas on one DB a booting instance would wrongly kill a peer's live runs.
    # True for this deploy (one fixed-name container); revisit before scaling out.
    reclaimed = db.reconcile_orphaned_running(None)
    if reclaimed:
        log.warning(
            "startup: reclaimed %d orphaned running review(s) to interrupted: %s",
            len(reclaimed), reclaimed,
        )
    watch_task: asyncio.Task | None = None
    if watcher.is_enabled():
        watch_task = asyncio.create_task(watcher.run_forever())
    else:
        log.info("PR watcher disabled (%s not set)", watcher.ENV_ENABLED)
    try:
        yield
    finally:
        if watch_task is not None:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task


app = FastAPI(title="Argus", version="0.1.0", lifespan=lifespan,
              dependencies=[Depends(require_api_token)])

# Built-in docs and schema routes bypass dependencies until registered as API routes.
for _route in list(app.routes):
    app.router.routes.remove(_route)
    app.add_api_route(_route.path, _route.endpoint, methods=sorted(_route.methods),
                      include_in_schema=False)

# The console runs as a browser SPA and may call this API cross-origin. Origins
# from ARGUS_CORS_ORIGINS (comma-separated); no wildcard since responses are
# read by credentialed-capable clients.
_cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "ARGUS_CORS_ORIGINS", "http://localhost:3000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


def _validated_repo(value, *, extra_deny=None):
    if value is None:
        return value
    try:
        return validate_repo(value, extra_deny)
    except RepoDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class ReviewRequest(BaseModel):
    repo: str = Field(min_length=1, description="owner/name, e.g. example/app")
    pr: int = Field(ge=1)
    _repo_policy = field_validator("repo")(_validated_repo)


class RepoWatch(BaseModel):
    name: str = Field(min_length=1)
    watched: bool | None = None
    _repo_policy = field_validator("name")(lambda value: _validated_repo(value, extra_deny=[]))


class ConfigUpdate(BaseModel):
    """Operator-facing config. camelCase matches the web/lib/types.ts contract."""

    repos: list[RepoWatch] = Field(default_factory=list)
    deny: list[str] | None = None
    watch_mode: str | None = Field(default=None, pattern="^(auto|manual)$")

    @field_validator("deny")
    @classmethod
    def valid_deny(cls, values):
        if values is not None:
            for value in values:
                try:
                    validate_repo(value if "/" in value else f"{value}/repo", [])
                except RepoDenied:
                    pass
        return values

    model: str = Field(min_length=1)
    bugCap: int = Field(ge=1, le=100)
    minConfidence: float = Field(ge=0.0, le=1.0)


class ReviewCreated(BaseModel):
    id: int
    status: str


class MarkRequest(BaseModel):
    """Operator label on a finding. mark is the ground-truth verdict; note is an
    optional one-liner (the bake-off requires a justification on a dismissal)."""

    mark: str = Field(description="one of: real, fp, stale, cant_locate, unsure")
    marker: str | None = Field(default=None, max_length=64,
                               description="provenance, e.g. 'automark:judge'; null = operator")
    note: str | None = Field(default=None, max_length=2000)


# Kept as a module attribute: the watcher and tests reach the shared rule through here.
_fallback_for = fallback_for


def run_one_review(
    path: str | None,
    review_id: int,
    repo: str,
    pr: int,
    runner: Callable[[], ReviewResult],
    *,
    fallback_runner: Callable[[], ReviewResult] | None = None,
    primary_model: str | None = None,
    fallback_model: str | None = None,
) -> Exception | None:
    """Execute one review and persist the outcome; mark the row error on failure.

    runner is a zero-arg callable so the background path closes over run_review
    while tests inject a stub. If a fallback_runner is supplied (operator opted
    into ARGUS_FALLBACK_MODEL) and the primary fails, the review runs once more on
    the fallback model and the switch is recorded on the row (never silent).
    """
    try:
        result = runner()
    except HeadMoved as exc:
        log.info("review %s: PR head moved during the review; this review is superseded",
                 review_id)
        reason = f"{type(exc).__name__}: {exc}"
        db.set_error(path, review_id, reason, "HeadMoved")
        return exc
    except Exception as exc:  # noqa: BLE001 - any engine/transport failure -> error status
        reason = f"{type(exc).__name__}: {exc}"
        if fallback_runner is not None and fallback_eligible(exc):
            log.warning("review %s: primary model %s failed (%s); trying fallback %s",
                        review_id, primary_model, reason, fallback_model)
            try:
                result = fallback_runner()
            except HeadMoved as fb_exc:
                log.info("review %s: PR head moved during the review; this review is superseded",
                         review_id)
                db.set_error(path, review_id, f"{type(fb_exc).__name__}: {fb_exc}", "HeadMoved")
                return fb_exc
            except Exception as fb_exc:  # noqa: BLE001 - fallback also failed -> error
                log.exception("review %s failed on the fallback model too", review_id)
                db.set_error(
                    path, review_id,
                    f"{type(fb_exc).__name__}: {fb_exc} "
                    f"(fallback {fallback_model}; primary {primary_model} failed: {reason})",
                    type(fb_exc).__name__)
                return fb_exc
            db.set_fallback(path, review_id, fallback_model, primary_model, reason)
            db.save_result(path, review_id, result)
            _publish_completed_review(path, review_id, repo, pr)
            return
        log.exception("review %s failed for %s#%s", review_id, repo, pr)
        db.set_error(path, review_id, reason, type(exc).__name__)
        return exc
    db.save_result(path, review_id, result)
    _publish_completed_review(path, review_id, repo, pr)


def _publish_completed_review(path: str | None, review_id: int, repo: str, pr: int) -> None:
    if pr <= 0 or not publish.enabled_for(repo):
        return
    try:
        review = db.get_review(path, review_id)
        head = (review or {}).get("head_sha") or ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
            log.info("review %s: publishing skipped for %s#%s: %s head SHA",
                     review_id, repo, pr, "invalid" if head else "missing")
            return
        files = diff.fetch_pr_files(repo, pr)
        document = verdict.document(review, db.get_findings(path, review_id),
                                    unverified=db.get_unverified_findings(path, review_id))
        outcome = publish.publish_review(repo, pr, head, review_id, document, files)
    except Exception as error:  # noqa: BLE001 - publishing cannot invalidate a saved review
        outcome = publish.error_outcome(error)
    try:
        db.save_publish_outcome(path, review_id, outcome)
    except Exception as error:  # noqa: BLE001 - storage failure must not escape either
        publish.error_outcome(error)


ENV_HEALTH_TOKEN = "ARGUS_HEALTH_TOKEN"


def _deep_allowed(request: Request) -> bool:
    """deep=1 makes a real backend/LLM call, so it is gated off the public edge:
    only a loopback caller (in-container or on-host) or a request carrying the
    shared ARGUS_HEALTH_TOKEN may trigger it. After API authentication, callers
    without deep-health access get the shallow body to avoid backend calls."""
    client = request.client.host if request.client else ""
    if client in {"127.0.0.1", "::1", "localhost"}:
        return True
    token = os.environ.get(ENV_HEALTH_TOKEN)
    return bool(token) and hmac.compare_digest(
        request.headers.get("x-argus-health-token", "").encode(), token.encode())


@app.get("/health")
def health(request: Request, deep: int = 0) -> dict:
    """Shallow: report the model reviews will use (stored config, else the env
    default) and whether a model provider is configured, with no network call.
    The status stays ok when no provider is set, so a liveness probe does not
    flap; read provider_configured for that. deep=1 pings the backend, but only
    from loopback or with the ARGUS_HEALTH_TOKEN header, after any required API
    authentication."""
    stored = db.get_config(db.db_path())
    model = stored["model"] if stored else DEFAULT_MODEL
    provider = _provider_name(model)
    # Additive, public: shallow /health now says whether the review lane is
    # healthy, so a probe pointed here sees lane death instead of a static "ok".
    body: dict = {"status": "ok", "model": model, "provider": provider,
                  "provider_configured": doctor.configuration_problem(model) is None,
                  "reviews": metrics.review_health()}
    if deep and _deep_allowed(request):
        try:
            body["ping"] = ping(model)
        except Exception as error:  # noqa: BLE001 - liveness probe reports, never 500s
            body["status"] = "degraded"
            body["ping"] = None
            body["error"] = type(error).__name__
    return body


def require_provider(model: str, *, allow_fallback: bool = False) -> None:
    """Refuse to start a model run that cannot work. A review created without a
    provider only errors later, in the background, where nobody is looking.

    With allow_fallback, a primary that cannot start is accepted when the
    configured ARGUS_FALLBACK_MODEL can, because the run then switches to it and
    records the switch."""
    problem = doctor.configuration_problem(model)
    if not problem:
        return
    fallback = os.environ.get(ENV_FALLBACK_MODEL, "").strip()
    if (allow_fallback and fallback and fallback != model
            and doctor.configuration_problem(fallback) is None):
        return
    raise HTTPException(status_code=503, detail=problem)


@app.post("/reviews", response_model=ReviewCreated, status_code=201)
def create_review(request: ReviewRequest, background: BackgroundTasks) -> ReviewCreated:
    from ..jarvis import live

    path = db.db_path()
    stored = db.get_config(path)
    model = stored["model"] if stored else DEFAULT_MODEL
    require_provider(model, allow_fallback=True)
    bug_cap = stored["bug_cap"] if stored else DEFAULT_BUG_CAP
    head_sha = None
    try:
        head_sha = live._pr_head_sha(request.repo, request.pr)
    except Exception as error:
        log.info("PR head lookup failed for %s#%s (%s)",
                 request.repo, request.pr, type(error).__name__)
    review_id = db.create_review(path, request.repo, request.pr, model, head_sha=head_sha)
    fb_runner, fb_model = _fallback_for(
        model, lambda m: lambda: run_review(
            request.repo, request.pr, m, bug_cap, expected_head_sha=head_sha))
    background.add_task(
        run_one_review, path, review_id, request.repo, request.pr,
        lambda: run_review(request.repo, request.pr, model, bug_cap, expected_head_sha=head_sha),
        fallback_runner=fb_runner, primary_model=model, fallback_model=fb_model,
    )
    return ReviewCreated(id=review_id, status=db.STATUS_RUNNING)


class DiffReviewRequest(BaseModel):
    """A review of a change that need not belong to any GitHub PR.

    Exactly one source: `diff` (unified `git diff` text, reviewed as-is) or
    `base_sha`+`head_sha` with `repo` (fetched via the compare API). `repo` on a
    raw diff is optional context that unlocks house rules + full-fidelity probes.
    `baseline` carries fingerprints from a prior verdict so the response reports
    per-finding baseline_state and which baseline findings are resolved.
    """

    repo: str | None = Field(default=None, description="owner/name")
    _repo_policy = field_validator("repo")(_validated_repo)
    diff: str | None = Field(default=None, max_length=2_000_000)
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,40}$")
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,40}$")
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    mode: str = Field(default="full", pattern="^(fast|full)$")
    baseline: list[str] = Field(default_factory=list, max_length=1000)


# A real change never touches this many files; caps the per-file temp-tree
# fan-out of the probe scan lane (defence in depth behind the traversal fix).
MAX_DIFF_FILES = 2000


def _cap_files(files: list) -> list:
    if len(files) > MAX_DIFF_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"diff declares {len(files)} files (cap {MAX_DIFF_FILES})",
        )
    return files


def _files_for_diff_request(request: DiffReviewRequest) -> list:
    if request.diff:
        files = parse_unified_diff(request.diff)
        if not files:
            raise HTTPException(status_code=422, detail="diff parsed to zero files")
        return _cap_files(files)
    if request.repo and request.base_sha and request.head_sha:
        response = github.get(
            f"/repos/{request.repo}/compare/{request.base_sha}...{request.head_sha}")
        if response.status_code != 200:
            raise HTTPException(status_code=422,
                                detail=f"compare fetch failed ({response.status_code})")
        files = [
            FilePatch(filename=f["filename"], status=f["status"], patch=f.get("patch"))
            for f in response.json().get("files", [])
            if diff._safe_path(f["filename"])
            and (not f.get("previous_filename") or diff._safe_path(f["previous_filename"]))
        ]
        if not files:
            raise HTTPException(status_code=422, detail="diff parsed to zero files")
        return _cap_files(files)
    raise HTTPException(status_code=422,
                        detail="provide diff text, or repo + base_sha + head_sha")


@app.post("/review-diff", status_code=201)
def review_diff(request: DiffReviewRequest, background: BackgroundTasks) -> dict:
    """Pre-PR review surface for agents. mode=fast runs only the deterministic
    lanes synchronously (sub-10s, no LLM) and returns the full verdict document.
    mode=full behaves like POST /reviews: 201 {id, status} and the verdict lands
    at GET /reviews/{id}/verdict when the run completes."""
    files = _files_for_diff_request(request)
    path = db.db_path()
    stored = db.get_config(path)
    model = stored["model"] if stored else DEFAULT_MODEL
    if request.mode == "full":
        require_provider(model)
    bug_cap = stored["bug_cap"] if stored else DEFAULT_BUG_CAP
    diff_hash = hashlib.sha256(
        (request.diff or f"{request.base_sha}..{request.head_sha}").encode()
    ).hexdigest()[:32]
    review_id = db.create_review(
        path, request.repo or "", 0, model, request.head_sha, source_kind="diff",
        base_sha=request.base_sha, diff_hash=diff_hash,
        baseline_json=json.dumps(request.baseline) if request.baseline else None,
    )
    # Passed only when present, so callers built against the older signature keep working.
    context = {key: value for key, value in (("pr_title", request.title),
                                             ("pr_body", request.description),
                                             ("base_sha", request.base_sha)) if value}
    if request.mode == "fast":
        result = run_diff_review(files, request.repo, request.head_sha,
                                 model, bug_cap, fast=True, **context)
        db.save_result(path, review_id, result)
        review = db.get_review(path, review_id)
        return verdict.document(review, db.get_findings(path, review_id),
                                request.baseline)
    background.add_task(
        run_one_review, path, review_id, request.repo or "diff", 0,
        lambda: run_diff_review(files, request.repo, request.head_sha, model, bug_cap,
                               **context),
    )
    return {"id": review_id, "status": db.STATUS_RUNNING}


@app.get("/reviews/{review_id}/verdict")
def get_verdict(review_id: int) -> dict:
    """The machine-facing verdict document for any review (PR or diff): findings
    with fingerprints, lanes, judge opinions and evidence tiers, plus the derived
    decision. A still-running review returns its status so a poller can wait on
    this one endpoint."""
    path = db.db_path()
    review = db.get_review(path, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    if review["status"] == db.STATUS_RUNNING:
        return {"review_id": review_id, "status": db.STATUS_RUNNING}
    baseline = json.loads(review["baseline_json"]) if review.get("baseline_json") else []
    return verdict.document(review, db.get_findings(path, review_id), baseline)


@app.get("/reviews")
def list_reviews() -> dict:
    """Overview/reviews-screen feed: a LiveReview (the one running row, if any)
    plus the done/error rows as ReviewSummary objects. Matches the
    {live, reviews} shape web/lib/api.ts getReviews expects (web/lib/types.ts)."""
    path = db.db_path()
    rows = db.list_reviews(path)
    counts_by_review = db.severity_counts_by_review(path)
    now = datetime.now(timezone.utc)

    live = next(
        (_to_live_review(r, now) for r in rows if r["status"] == db.STATUS_RUNNING),
        None,
    )
    summaries = [
        _to_review_summary(r, counts_by_review.get(r["id"], {}), now)
        for r in rows
        if r["status"] != db.STATUS_RUNNING
    ]
    return {"live": live, "reviews": summaries}


@app.get("/reviews/{review_id}")
def get_review(review_id: int) -> dict:
    """One review as a ReviewDetail: findings split into bugs/nits (by is_bug),
    each a VerifiedFinding. branch, author and the diff stats appear when the run
    recorded them. model_used, fallback_from and fallback_reason say which model
    answered, and error carries the failure text of an errored run. The comment
    thread is not mirrored; see web/lib/types.ts."""
    path = db.db_path()
    review = db.get_review(path, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    findings = db.get_findings(path, review_id)
    marks = db.latest_marks_for_review(path, review_id)
    return _to_review_detail(review, findings, marks)


@app.post("/findings/{finding_id}/mark", status_code=201)
def mark_finding(finding_id: int, request: MarkRequest) -> dict:
    """Record the operator's ground-truth label on a finding (the only real
    precision signal; the engine's own verdict is not ground truth). Append-only:
    a re-label is a new row and the latest wins."""
    try:
        mark_id = db.add_mark(db.db_path(), finding_id, request.mark,
                              marker=request.marker, note=request.note)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail="finding not found") from error
    return {"id": mark_id, "finding_id": finding_id, "mark": request.mark}


# --- DB row -> web view-model mappers (web/lib/types.ts) -------------------

# Decision the engine would post given a finding mix. Delegates to the verdict
# module so the console and the machine verdict endpoint can never disagree
# about what the engine would post. Matches the web Decision union.
def _decision_for(review: dict, findings: list[dict]) -> str:
    return verdict.decision(findings, verdict.coverage_of(review))


def _relative_age(created_at: str | None, now: datetime) -> str:
    """Compact age label (e.g. '3m', '2h', '5d') from an ISO timestamp."""
    parsed = metrics._parse(created_at) if created_at else None
    if parsed is None:
        return "—"
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _verified_finding(row: dict, mark: dict | None = None) -> dict:
    """One findings row -> the VerifiedFinding shape (finding + verdict + id + mark).

    id is exposed so the console can POST a label to /findings/{id}/mark. mark is
    the operator's latest ground-truth label (None when unlabeled)."""
    return {
        "id": row["id"],
        "finding": {
            "file": row["file"],
            "line": row["line"],
            "severity": row["severity"],
            "confidence": row["confidence"],
            "title": row["title"],
            "description": row["description"],
            "proposed_fix": row["proposed_fix"],
            "rule_id": row["rule_id"],
        },
        "verdict": {
            "is_real": bool(row["verdict_is_real"]),
            "confidence": row["verdict_confidence"],
            # The engine keeps no per-finding verdict prose, so reasoning is empty.
            "reasoning": "",
        },
        # Operator ground truth: {"mark","marked_at","note"} or None until labeled.
        "mark": {"mark": mark["mark"], "marked_at": mark["marked_at"], "note": mark["note"]}
        if mark else None,
    }


def _run_truth(row: dict) -> dict:
    """The model that ran and any fallback note. `model` on a row is what was
    configured; these fields say what happened, so a failed run never reads as if
    the configured model had answered."""
    facts = verdict.run_facts(row)
    return {key: facts[key] for key in ("model_used", "fallback_from", "fallback_reason")}


def _to_review_summary(row: dict, counts: dict, now: datetime) -> dict:
    """reviews row -> ReviewSummary. counts is the severity tally for this row."""
    zero = {s: 0 for s in metrics.SEVERITIES}
    return {
        "id": str(row["id"]),
        "repo": row["repo"],
        "pr": row["pr"],
        # The engine has no PR title; fall back to a repo#pr label.
        "title": f"{row['repo']} #{row['pr']}",
        "model": row["model"],
        "status": row["status"],
        **_run_truth(row),
        "counts": {**zero, **counts},
        # No GitHub acted/merged signal is wired, so every row reads as pending.
        "action": "pending",
        "age": _relative_age(row.get("created_at"), now),
    }


def _to_live_review(row: dict, now: datetime) -> dict:
    """A running reviews row -> LiveReview (the overview/reviews live strip)."""
    return {
        "repo": row["repo"],
        "pr": row["pr"],
        # No PR title persisted; show the repo#pr the run is on.
        "title": f"{row['repo']} #{row['pr']}",
        "model": row["model"],
        # The engine does not emit a per-stage signal mid-run.
        "stage": "deep review",
        "elapsed": _relative_age(row.get("created_at"), now),
    }


def _to_review_detail(
    review: dict, findings: list[dict], marks: dict[int, dict] | None = None
) -> dict:
    """reviews row + its findings -> ReviewDetail (bugs/nits split on is_bug)."""
    marks = marks or {}
    bugs = [_verified_finding(f, marks.get(f["id"])) for f in findings if f["is_bug"]]
    nits = [_verified_finding(f, marks.get(f["id"])) for f in findings if not f["is_bug"]]
    now = datetime.now(timezone.utc)
    facts = verdict.run_facts(review)
    # Header facts the run recorded. A value the source never supplied (rows from
    # before the columns existed, raw diffs) is left out, not shown as zero.
    header = {key: review[column] for key, column in (
        ("branch", "branch"), ("author", "author"), ("filesChanged", "files_changed"),
        ("added", "lines_added"), ("removed", "lines_removed"),
    ) if review.get(column) is not None}
    return {
        "id": str(review["id"]),
        "repo": review["repo"],
        "pr": review["pr"],
        "title": f"{review['repo']} #{review['pr']}",
        **header,
        "model": review["model"],
        "model_used": facts["model_used"],
        "fallback_from": facts["fallback_from"],
        "fallback_reason": facts["fallback_reason"],
        "error": facts["error"] if review["status"] == db.STATUS_ERROR else None,
        # The run's own lifecycle, independent of what it concluded. A poller must
        # be able to tell "finished" from "still going" without recognising the
        # decision vocabulary, or every value added to that vocabulary reads as
        # unfinished to any consumer that has not been taught the new value.
        "status": review["status"],
        "time": f"{_relative_age(review.get('created_at'), now)} ago",
        "decision": _decision_for(review, findings),
        "bugs": bugs,
        "nits": nits,
        # No PR comment thread is mirrored into the store yet.
        "thread": [],
    }


# --------------------------------------------------------------------------
# Console read endpoints (web/lib/types.ts contract). All GET, plain dict.
# Real numbers are DB/corpus-derived; fields with no source yet are documented
# placeholders, marked inline. See web/lib/fixtures.ts for the target shapes.
# --------------------------------------------------------------------------


@app.get("/stats")
def get_stats() -> dict:
    """Overview counters + 14-day trend series. prsReviewed/openFindings/bugVsNit/
    avgComments/trends are DB-computed; rate deltas need historical snapshots we
    do not keep yet."""
    done_reviews = metrics.done_review_count()
    prs = metrics.distinct_pr_count()
    severity = metrics.verified_severity_counts()
    split = metrics.verified_bug_split()
    verified = metrics.verified_finding_count()
    labels = metrics.mark_precision()
    reviews_series = metrics.daily_buckets("reviews", 14)
    findings_series = metrics.daily_buckets("findings", 14)

    # avgComments is per done review (the unit a reviewer sees), not per distinct PR.
    avg = round(verified / done_reviews, 1) if done_reviews else 0.0
    stored_cfg = db.get_config(db.db_path())
    stored_model = stored_cfg["model"] if stored_cfg else DEFAULT_MODEL
    return {
        # prsReviewed is now distinct PRs, not done-review count (the watcher
        # re-reviews each push). reviews is the raw run count, kept for context.
        # deltaPct compares to a prior window we do not retain -> 0 until snapshots exist.
        "prsReviewed": {"value": prs, "reviews": done_reviews, "deltaPct": 0},
        "openFindings": {"total": sum(severity.values()), **severity},
        "bugVsNit": {"bug": split["bug"], "nit": split["nit"]},
        # Ground-truth precision from operator labels: the only real precision number.
        # precision is None until findings are labeled (no self-grade substituted).
        "labeling": labels,
        # actionRate needs GitHub acted/merged signal (not wired) -> 0/0 placeholder.
        "actionRate": {"pct": 0, "prevPct": 0},
        # prev needs a prior-window snapshot we do not keep yet.
        "avgComments": {"value": avg, "prev": 0},
        "trends": {
            "reviewsPerDay": {
                "value": round(sum(reviews_series) / 14, 1),
                "deltaPct": 0,  # prior-window delta not instrumented
                "series": reviews_series,
            },
            "findingsPerDay": {
                "value": round(sum(findings_series) / 14, 1),
                "deltaPct": 0,  # prior-window delta not instrumented
                "series": findings_series,
            },
        },
        # Additive operational surface: what the watchdog and probe need in one
        # place instead of raw-DB spelunking. New keys only; nothing renamed.
        "ops": {
            "watch": watcher.stats(db.db_path()),
            "coverage": watch_metrics.stats(db.db_path()),
            "acceptance": acceptance.stats(db.db_path()),
            "reviews_24h": metrics.reviews_window(24),
            "last_error_class": metrics.top_error_class(24),
            "lanes": {
                "primary": stored_model,
                "last_error_class": metrics.top_error_class(6),
            },
            "corpus_loaded": len(rules.index()),
            "journal_mode": db.journal_mode(),
            "image_revision": os.environ.get("ARGUS_GIT_SHA", "unknown"),
            "judge_live": os.environ.get("ARGUS_JUDGE_LIVE", "").strip().lower()
            in {"1", "true", "yes", "on"},
        },
    }


_SEVERITY_LABELS = [
    ("Critical", "critical"), ("Major", "major"), ("Minor", "minor"), ("Nit", "nit"),
]


@app.get("/reports/{period}")
def get_report(period: str) -> dict:
    """daily/weekly/monthly rollup. reviews/findings/severity/topRepos/volume are
    DB-computed over the window; acted/merged/category need GitHub + labeling
    instrumentation that is not wired, so those are documented placeholders."""
    days = _REPORT_WINDOWS.get(period)
    if days is None:
        raise HTTPException(status_code=404, detail=f"unknown period: {period}")

    roll = metrics.window_rollup(days)
    sev = roll["severity_counts"]
    now = datetime.now(timezone.utc)

    title = {"daily": "Daily report", "weekly": "Weekly report",
             "monthly": "Monthly report"}[period]
    return {
        "period": period,
        "title": title,
        "range": _report_range(period, now),
        "reviews": roll["reviews"],
        "findings": roll["findings"],
        # acted/merged/actedPct need GitHub PR outcomes (not wired) -> 0 placeholders.
        "acted": 0,
        "merged": 0,
        "actedPct": 0,
        "severity": [
            {"label": label, "value": sev[key], "severity": key}
            for label, key in _SEVERITY_LABELS
        ],
        # category needs a finding-classifier/labels we do not persist -> empty until wired.
        "category": [],
        "volume": _volume_buckets(period, roll["review_timestamps"], now),
        "volumeLabel": {"daily": "per 3-hour block", "weekly": "per day",
                        "monthly": "per week"}[period],
        "topRepos": roll["top_repos"],
    }


@app.get("/benchmark")
def get_benchmark() -> dict:
    """Argus vs CodeRabbit. CR volume/action numbers from data/cr_threads.jsonl
    (falls back to documented baselines if absent); Argus numbers from the DB.
    perRepo is CodeRabbit's per-repo breakdown; argusEmpty flags a fresh DB with
    no runs so the board reads as explicitly one-sided. Recall/FP-rate stay
    pending until the scoring corpora exist."""
    cr = benchmark.coderabbit_side(CR_THREADS_PATH)
    split = metrics.verified_bug_split()
    done_reviews = metrics.done_review_count()
    verified = metrics.verified_finding_count()
    argus = benchmark.argus_side(
        verified_findings=verified,
        done_reviews=done_reviews,
        bug_findings=split["bug"],
    )
    gate = benchmark.build_gate(
        status_counts=metrics.review_status_counts(),
        first_review_at=metrics.first_review_at(),
        last_error_at=metrics.last_error_at(),
        verified_findings=verified,
        bug_findings=split["bug"],
        severity=metrics.verified_severity_counts(),
        repo_counts=metrics.review_repo_counts(),
        error_kinds=metrics.error_reason_counts(),
    )
    return {
        "rows": benchmark.build_rows(cr, argus),
        "note": benchmark.NOTE,
        "perRepo": benchmark.per_repo_rows(CR_THREADS_PATH),
        "argusEmpty": done_reviews == 0,
        "gate": gate,
    }


ENV_MODEL_OPTIONS = "ARGUS_MODEL_OPTIONS"


def _model_options(current: str) -> list[str]:
    """Reviewer models the operator may pick, in display order.

    The list is env-owned (ARGUS_MODEL_OPTIONS, comma-separated: bare ids go to
    the configured provider, slash-prefixed ids to OpenRouter). The engine default and the
    currently-stored model are always present so the picker can never orphan the
    active configuration."""
    raw = os.environ.get(ENV_MODEL_OPTIONS, "")
    listed = [item.strip() for item in raw.split(",") if item.strip()]
    options: list[str] = []
    for candidate in [*listed, DEFAULT_MODEL, current]:
        if candidate and candidate not in options:
            options.append(candidate)
    return options


def _config_payload(
    *, repos: list[dict], model: str, bug_cap: int, min_confidence: float
) -> dict:
    """Build the /config response shape (web/lib/types.ts ConfigData).

    The pipeline is two passes over one model: a deep review then a skeptical
    verification. No triage stage, no second model -> both report `model`.
    """
    return {
        "repos": repos,
        "hiddenCount": 0,
        "totalRepos": len(repos),
        "models": [
            {"name": "Deep review", "caption": "reports every candidate",
             "model": model, "tier": "deep"},
            {"name": "Verification", "caption": "skeptical adjudicator, drops the unverifiable",
             "model": model, "tier": "deep"},
        ],
        "modelOptions": _model_options(model),
        "bugCap": bug_cap,
        "minConfidence": min_confidence,
    }


@app.get("/config")
def get_config() -> dict:
    """Return stored operator config when set, else the engine defaults
    (DEFAULT_MODEL, DEFAULT_BUG_CAP, MIN_CONFIDENCE, empty repo allowlist)."""
    stored = db.get_config(db.db_path()) or {
        "repos": [], "model": DEFAULT_MODEL, "bug_cap": DEFAULT_BUG_CAP,
        "min_confidence": MIN_CONFIDENCE,
    }
    return {**_config_payload(
        repos=watch_config.public_repos(stored), model=stored["model"],
        bug_cap=stored["bug_cap"], min_confidence=stored["min_confidence"],
    ), **watch_config.policy_payload(stored)}


@app.put("/config")
def put_config(update: ConfigUpdate) -> dict:
    """Persist the operator config (singleton) and echo back the stored shape.

    The model must come from the options list (env-owned + current + default):
    a typo'd id would brick every subsequent review, so it is rejected here."""
    stored = db.get_config(db.db_path())
    current = stored["model"] if stored else DEFAULT_MODEL
    allowed = _model_options(current)
    if update.model not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"model {update.model!r} is not in ARGUS_MODEL_OPTIONS {allowed}",
        )
    repos = watch_config.console_update(stored, [r.model_dump() for r in update.repos])
    db.save_config(
        db.db_path(), repos=repos, model=update.model,
        bug_cap=update.bugCap, min_confidence=update.minConfidence,
        deny=update.deny, watch_mode=update.watch_mode,
    )
    return get_config()


@app.get("/integrations")
def get_integrations() -> dict:
    """Integration status from the running config: credentials are reported as set or missing.

    Never returns a credential value. api.baseUrl, the endpoint list and the
    MCP tool names come from the app itself."""
    stored = db.get_config(db.db_path())
    model = stored["model"] if stored else DEFAULT_MODEL
    github_token = bool(os.environ.get("GITHUB_TOKEN", "").strip())
    auth_mode = _auth_mode()
    provider = _provider_name(model)
    watcher_facts = {
        "enabled": watcher.is_enabled(),
        "owners": sorted(watch_owners()),
        "intervalSeconds": watcher.poll_interval(),
        "watchedRepos": len(watch_config.effective(stored)),
    }
    publishing = {
        "mode": publish.mode(),
        "repoCount": len(publish.repo_allowlist()),
        "tokenConfigured": bool(os.environ.get("ARGUS_PUBLISH_TOKEN", "").strip()),
    }
    return {
        "health": [
            {"label": "GitHub token",
             "status": "configured" if github_token else "missing",
             "state": "ok" if github_token else "warn"},
            {"label": "PR watcher", "status": _watcher_status(watcher_facts),
             "state": "ok" if watcher_facts["enabled"] else "warn"},
            {"label": "Publishing", "status": _publishing_status(publishing),
             "state": _publishing_state(publishing)},
            {"label": "Model provider",
             "status": f"{provider} · {model}" if provider else "not configured",
             "state": "ok" if provider else "warn"},
            {"label": "REST API", "status": "healthy", "state": "ok"},
            {"label": "Auth", "status": auth_mode, "state": "ok"},
        ],
        "warning": None,
        "github": {
            "token": "configured" if github_token else "missing",
            "watcher": watcher_facts,
            "publishing": publishing,
        },
        "api": {
            "baseUrl": _safe_url(API_BASE_URL),
            "auth": auth_mode,
            "endpoints": _real_endpoints(),
        },
        "mcp": {
            # The real server is stdio, launched by the MCP client; the tool list
            # is imported from the server module so this page cannot drift from
            # what it exposes.
            "status": "stdio",
            "config": MCP_CONTAINER_CONFIG,
            "tools": list(_mcp_tool_names()),
            "resources": [],
        },
    }


# The server runs inside the API container, so the client starts it through
# compose by service name. That works for any project name or container name.
# -T drops the TTY and keeps stdin attached, which a stdio MCP server needs.
MCP_CONTAINER_CONFIG = (
    '{\n  "mcpServers": {\n    "argus": {\n      "command": "docker",\n'
    '      "args": ["compose", "-f", "/path/to/argus/docker-compose.yml",\n'
    '               "exec", "-T", "-e", "ARGUS_API=http://127.0.0.1:8000",\n'
    '               "argus-api", "/opt/venv/bin/argus-mcp"]\n    }\n  }\n}'
)


def _auth_mode() -> str:
    return "proxy" if os.environ.get("ARGUS_AUTH_MODE", "token") == "proxy" else "token"


def _provider_name(model: str) -> str | None:
    try:
        return providers.resolve(model)
    except providers.BackendNotConfigured:
        return None


def _watcher_status(facts: dict) -> str:
    if not facts["enabled"]:
        return "off"
    owners = len(facts["owners"])
    return f"on · {owners} owner{'' if owners == 1 else 's'} · every {facts['intervalSeconds']}s"


def _publishing_status(facts: dict) -> str:
    if facts["mode"] == "off":
        return "off"
    repos = facts["repoCount"]
    return f"{facts['mode']} · {repos} repo{'' if repos == 1 else 's'}"


def _publishing_state(facts: dict) -> str:
    if facts["mode"] == "on" and not (facts["repoCount"] and facts["tokenConfigured"]):
        return "warn"
    return "ok"


def _safe_url(url: str) -> str:
    """Drop userinfo, query and fragment so a URL that carries credentials is
    never echoed."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if not (parts.scheme and host):
        return ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _first_sentence(doc: str) -> str:
    """First sentence of a docstring's opening paragraph, line wraps joined."""
    paragraph = " ".join((re.split(r"\n\s*\n", doc.strip(), maxsplit=1)[0]).split())
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", paragraph, maxsplit=1)[0]


def _real_endpoints() -> list[dict]:
    """The API's actual routes (method + path + first docstring sentence), derived
    from the app so the page can never advertise a route that does not exist."""
    from fastapi.routing import APIRoute

    out: list[dict] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            description = _first_sentence(route.endpoint.__doc__ or "")
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append({"method": method, "path": route.path,
                            "description": description})
    return sorted(out, key=lambda entry: (entry["path"], entry["method"]))


def _mcp_tool_names() -> tuple[str, ...]:
    """The real MCP tool names, imported from the server (single source of truth).
    Falls back to the known set if the mcp package is not installed in this env."""
    try:
        from ..mcp_server import TOOL_NAMES
        return TOOL_NAMES
    except Exception:  # noqa: BLE001 - mcp optional in some envs; use the known set
        return ("review_diff", "review_pr", "get_verdict", "mark_finding")


def _report_range(period: str, now: datetime) -> str:
    """Human label for the window, e.g. 'Jun 10 - Jun 16, 2026'."""
    if period == "daily":
        return f"Today · {now:%b} {now.day}, {now.year}"
    span = {"weekly": 6, "monthly": 29}[period]
    start = now - timedelta(days=span)
    return f"{start:%b} {start.day} – {now:%b} {now.day}, {now.year}"


def _volume_buckets(period: str, timestamps: list[str], now: datetime) -> list[dict]:
    """Bucket reviews into the period's natural slots (daily=3h, weekly=day,
    monthly=week), oldest first, zero-filled."""
    if period == "daily":
        slots, width = 8, timedelta(hours=3)
        start = now - timedelta(hours=24)
    elif period == "weekly":
        slots, width = 7, timedelta(days=1)
        start = now - timedelta(days=7)
    else:
        slots, width = 4, timedelta(days=7)
        start = now - timedelta(days=28)

    buckets = [0] * slots
    for ts in timestamps:
        parsed = metrics._parse(ts)
        if parsed is None:
            continue
        idx = int((parsed - start) / width)
        if 0 <= idx < slots:
            buckets[idx] += 1
    return [{"label": str(i), "value": v} for i, v in enumerate(buckets)]
