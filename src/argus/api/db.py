"""SQLite persistence for review runs and their findings.

Thin storage layer: no business logic, parameterized queries only. The path is
configurable via ARGUS_DB_PATH so tests point at a temp file and the running
service uses the repo data dir by default.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import verdict
from ..publish import PublishOutcome
from ..redact import redact
from ..repo_policy import valid_catalogue_name
from ..review import ReviewResult
from ..schema import VerifiedFinding

ENV_DB_PATH = "ARGUS_DB_PATH"
_ROOT = Path(__file__).resolve().parents[3]
# Local default: a gitignored folder, so a source checkout never writes the
# database into the tracked data/ tree. Docker and prod set ARGUS_DB_PATH.
_DEFAULT_DB = _ROOT / ".argus" / "argus.db"
# Older checkouts kept the file in data/. Keep using it until the new one exists.
_LEGACY_DB = _ROOT / "data" / "argus.db"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT    NOT NULL,
    pr          INTEGER NOT NULL,
    model       TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    raw_count   INTEGER,
    dropped     INTEGER,
    head_sha    TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file              TEXT    NOT NULL,
    line              INTEGER NOT NULL,
    severity          TEXT    NOT NULL,
    confidence        REAL    NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    proposed_fix      TEXT    NOT NULL,
    rule_id           TEXT,
    is_bug            INTEGER NOT NULL,
    verdict_is_real   INTEGER NOT NULL,
    verdict_confidence REAL   NOT NULL
);

-- Every single-pass (pre-verification) finding, including the ones the verifier
-- later refuted. findings holds the verified survivors the dashboard shows;
-- findings_raw is the lossless single-pass record, so verifier precision can be
-- measured against single-pass precision later without re-running anything. Same
-- columns as findings; verdict_* carry the shadow verifier call.
CREATE TABLE IF NOT EXISTS findings_raw (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file              TEXT    NOT NULL,
    line              INTEGER NOT NULL,
    severity          TEXT    NOT NULL,
    confidence        REAL    NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    proposed_fix      TEXT    NOT NULL,
    rule_id           TEXT,
    is_bug            INTEGER NOT NULL,
    verdict_is_real   INTEGER NOT NULL,
    verdict_confidence REAL   NOT NULL
);

-- Operator ground-truth labels on findings. A finding with no row here has an
-- unknown truth value (NULL truth): that is the point, truth comes from the
-- operator, not from the engine's self-grade. mark is one of
-- real | fp | stale | cant_locate | unsure.
CREATE TABLE IF NOT EXISTS marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id  INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    mark        TEXT    NOT NULL,
    marker      TEXT,
    marked_at   TEXT    NOT NULL,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_review_id ON findings(review_id);
CREATE INDEX IF NOT EXISTS idx_findings_raw_review_id ON findings_raw(review_id);
CREATE INDEX IF NOT EXISTS idx_marks_finding_id ON marks(finding_id);

CREATE TABLE IF NOT EXISTS pr_heads (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, pr_updated_at TEXT, head_committed_at TEXT,
    draft INTEGER NOT NULL DEFAULT 0, author TEXT, author_type TEXT, base_ref TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('queued','running','done','error','parked','skipped','superseded','closed')),
    skip_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error_class TEXT,
    next_attempt_at TEXT, claimed_at TEXT, review_id INTEGER REFERENCES reviews(id),
    completed_at TEXT, last_listed_at TEXT, is_open INTEGER NOT NULL DEFAULT 1,
    UNIQUE(repo, pr, head_sha)
);
CREATE INDEX IF NOT EXISTS idx_heads_queue ON pr_heads(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS coverage_reports (
    day TEXT PRIMARY KEY, created_at TEXT NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_attempts (
    id INTEGER PRIMARY KEY, head_id INTEGER NOT NULL REFERENCES pr_heads(id),
    review_id INTEGER REFERENCES reviews(id), finished_at TEXT NOT NULL, error_class TEXT
);
CREATE TABLE IF NOT EXISTS published_findings (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
    fingerprint TEXT NOT NULL, severity TEXT NOT NULL, category TEXT,
    path TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER, title TEXT NOT NULL,
    placement TEXT NOT NULL CHECK(placement IN ('inline','summary')),
    mode TEXT NOT NULL CHECK(mode IN ('posted','dry_run')),
    gh_review_id INTEGER, gh_comment_id INTEGER, posted_at TEXT NOT NULL,
    comment_lookups INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repo, pr, fingerprint, head_sha, mode)
);
CREATE INDEX IF NOT EXISTS idx_published_comments ON published_findings(gh_comment_id);
CREATE INDEX IF NOT EXISTS idx_published_recent ON published_findings(mode, posted_at);
CREATE TABLE IF NOT EXISTS publish_reconciliations (
    review_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, found INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS publishing_identities (
    repo TEXT NOT NULL, bot_login TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    PRIMARY KEY(repo, bot_login)
);
CREATE TABLE IF NOT EXISTS outcome_cursors (
    repo TEXT PRIMARY KEY, since TEXT NOT NULL, last_collected_at TEXT
);
CREATE TABLE IF NOT EXISTS outcome_prs (
    repo TEXT NOT NULL, pr INTEGER NOT NULL, state TEXT NOT NULL,
    merged INTEGER NOT NULL DEFAULT 0, closed_at TEXT, head_sha TEXT,
    checked_at TEXT NOT NULL, PRIMARY KEY(repo, pr)
);
CREATE TABLE IF NOT EXISTS finding_outcomes (
    finding_id INTEGER NOT NULL REFERENCES published_findings(id),
    kind TEXT NOT NULL CHECK(kind IN ('reply','resolved','addressed')),
    value TEXT NOT NULL, evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    occurred_at TEXT,
    UNIQUE(finding_id, kind, evidence_id)
);
CREATE TABLE IF NOT EXISTS reply_texts (
    finding_id INTEGER NOT NULL REFERENCES published_findings(id),
    evidence_id TEXT NOT NULL, body TEXT NOT NULL, author_association TEXT,
    author_login TEXT, answered_head TEXT,
    UNIQUE(finding_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS reply_attempts (
    finding_id INTEGER NOT NULL REFERENCES published_findings(id),
    evidence_id TEXT NOT NULL, kind TEXT NOT NULL, head_sha TEXT NOT NULL, verdict TEXT,
    UNIQUE(finding_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS bot_actions (
    finding_id INTEGER NOT NULL REFERENCES published_findings(id),
    kind TEXT NOT NULL CHECK(kind IN ('recheck','reply','resolve')),
    value TEXT NOT NULL, evidence_id TEXT NOT NULL, created_at TEXT NOT NULL,
    gh_comment_id INTEGER, quote TEXT,
    UNIQUE(finding_id, kind, evidence_id)
);
CREATE TABLE IF NOT EXISTS known_fps_live (
    repo TEXT NOT NULL, file TEXT NOT NULL, title TEXT NOT NULL,
    fingerprint TEXT NOT NULL, reply_id TEXT NOT NULL, rejected_at TEXT NOT NULL,
    UNIQUE(repo, file, title)
);

CREATE TABLE IF NOT EXISTS config (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    repo_allowlist TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    bug_cap        INTEGER NOT NULL,
    min_confidence REAL    NOT NULL
);
"""


def _now_iso() -> str:
    """Canonical created_at string: single source of truth for the write side.

    Every row's created_at is minted here (tz-aware UTC ISO-8601) so reads can
    assume one format. _parse_created_at still tolerates other valid shapes
    defensively, so a future or external writer can't silently break time order.
    """
    return datetime.now(timezone.utc).isoformat()


def _parse_created_at(value: str) -> datetime:
    """Parse a stored created_at to an aware UTC datetime for chronological compare.

    Tolerant by design: accepts tz-aware ISO-8601 (the canonical write), a naive
    ISO-8601 (assumed UTC), a trailing 'Z', and any sub-second precision. This is
    what makes time ordering robust to format variation instead of relying on
    lexical string order, which only matches chronology for one exact format.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def db_path() -> str:
    configured = os.environ.get(ENV_DB_PATH)
    if configured:
        return configured
    if _LEGACY_DB.is_file() and not _DEFAULT_DB.exists():
        return str(_LEGACY_DB)
    return str(_DEFAULT_DB)


def connect(path: str | None = None) -> sqlite3.Connection:
    target = path or db_path()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Two writer families (the watcher thread + POST /reviews BackgroundTasks)
    # share this DB; WAL + a busy_timeout let readers and one writer proceed
    # concurrently instead of erroring the review with "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def journal_mode(path: str | None = None) -> str:
    """The DB's active journal mode (wal after the connect() PRAGMA). Surfaced on
    /stats.ops so an operator can see WAL is in effect without shelling in."""
    conn = connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        for name in ("head_sha", "error", "completed_at", "error_class",
                     "source_kind", "base_sha", "diff_hash", "baseline_json",
                     "model_used", "fallback_from", "fallback_reason",
                     "coverage_json"):
            _ensure_column(conn, "reviews", name)
        for name, decl in (("branch", "TEXT"), ("author", "TEXT"),
                           ("files_changed", "INTEGER"), ("lines_added", "INTEGER"),
                           ("lines_removed", "INTEGER")):
            _ensure_column(conn, "reviews", name, decl)
        for name, decl in (("publish_status", "TEXT"), ("published_at", "TEXT"),
                           ("publish_error", "TEXT"), ("published_comments", "INTEGER"),
                           ("publish_payload", "TEXT")):
            _ensure_column(conn, "reviews", name, decl)
        _ensure_column(conn, "published_findings", "unverified", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "published_findings", "gh_thread_id")
        _ensure_column(conn, "bot_actions", "head_sha")
        _ensure_column(conn, "bot_actions", "verdict")
        for table in ("findings", "findings_raw"):
            for name, decl in (("fingerprint", "TEXT"), ("lane", "TEXT"),
                               ("end_line", "INTEGER"), ("category", "TEXT"),
                               ("evidence", "TEXT"),
                               ("judge_verdict", "TEXT"),
                               ("judge_confidence", "REAL"),
                               ("judge_model", "TEXT")):
                _ensure_column(conn, table, name, decl)
        for name in ("ended_at", "eligible_at", "eligible_pr_updated_at", "merged_at", "author_association"):
            _ensure_column(conn, "pr_heads", name)
        _ensure_column(conn, "pr_heads", "baseline", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "pr_heads", "merged_before_seen", "INTEGER NOT NULL DEFAULT 0")
        # Legacy fast merges stored their merge time in eligible_pr_updated_at.
        conn.execute(
            "UPDATE pr_heads SET merged_at=eligible_pr_updated_at "
            "WHERE merged_before_seen=1 AND merged_at IS NULL "
            "AND eligible_pr_updated_at IS NOT NULL"
        )
        conn.execute(
            "UPDATE pr_heads SET eligible_at=first_seen_at, eligible_pr_updated_at=pr_updated_at "
            "WHERE skip_reason IS NULL AND eligible_at IS NULL"
        )
        _ensure_column(conn, "config", "deny", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "config", "watch_mode")
        _ensure_column(conn, "config", "owned_repos", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "config", "publish_repos")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_head ON reviews(repo, pr, head_sha)")
        row = conn.execute("SELECT repo_allowlist FROM config WHERE watch_mode IS NULL").fetchone()
        if row:
            repos = json.loads(row[0])
            kept = [repo for repo in repos if valid_catalogue_name(repo["name"])]
            logging.getLogger("argus.db").info("watcher: pruned %d invalid catalogue repos", len(repos) - len(kept))
            repos = kept
            for repo in repos:
                repo["watched"] = None
            conn.execute("UPDATE config SET repo_allowlist=?, watch_mode='auto' WHERE id=1",
                         (json.dumps(repos),))
            logging.getLogger("argus.db").info("watcher: migrated %d repos to auto", len(repos))
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, name: str,
                   decl: str = "TEXT") -> None:
    # CREATE TABLE IF NOT EXISTS never alters a pre-existing table, so a DB
    # created before a column landed lacks it. Add it once, nullable, so the
    # column survives restart on databases that predate it. On reviews:
    # head_sha backs PR-dedup; error/error_class record why a run failed;
    # completed_at makes latency measurable; source_kind/base_sha/diff_hash/
    # baseline_json describe non-PR (diff) review sources. On findings:
    # fingerprint is the cross-run identity, lane the producing lane, judge_*
    # the decorrelated second opinion.
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def create_review(
    path: str | None, repo: str, pr: int, model: str, head_sha: str | None = None,
    *, source_kind: str = "pr", base_sha: str | None = None,
    diff_hash: str | None = None, baseline_json: str | None = None,
) -> int:
    conn = connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO reviews (repo, pr, model, status, created_at, head_sha, "
            "source_kind, base_sha, diff_hash, baseline_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, pr, model, STATUS_RUNNING, _now_iso(), head_sha,
             source_kind, base_sha, diff_hash, baseline_json),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def head_shas_for_pr(path: str | None, repo: str, pr: int) -> set[str]:
    """Head SHAs already reviewed for one PR, regardless of run status.

    Kept for callers that want the raw set (every non-null head_sha on a
    (repo, pr) row). The watcher's dedup uses suppressed_head_shas_for_pr, which
    is status-aware: this one would wrongly suppress an errored or dead run.
    NULLs (pre-watcher rows and manual POSTs) are excluded.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT head_sha FROM reviews "
            "WHERE repo = ? AND pr = ? AND head_sha IS NOT NULL",
            (repo, pr),
        ).fetchall()
    finally:
        conn.close()
    return {row["head_sha"] for row in rows}


def suppressed_head_shas_for_pr(
    path: str | None, repo: str, pr: int, stale_running_before: str
) -> set[str]:
    """Head SHAs the watcher must NOT re-review for one PR (state-aware dedup).

    A SHA suppresses a retrigger only when it has either a successful run (done)
    or a genuinely in-flight run (running whose created_at is at or after
    stale_running_before, an ISO timestamp the caller derives from the poll
    interval). Excluded on purpose, so they stay retryable:
      - error rows: a transient blip must not drop the PR forever,
      - stale running rows (created before the cutoff): the process is presumed
        dead, so the run is reclaimed rather than suppressed.
    NULL head_shas (pre-watcher rows, manual POSTs) never suppress.

    The stale window is decided by parsing both sides to aware UTC datetimes, not
    by SQL string comparison: lexical order only equals chronological order for
    one exact timestamp format, so a row written naive or in another offset would
    otherwise be misclassified and silently drop or double-fire the PR.
    """
    cutoff = _parse_created_at(stale_running_before)
    conn = connect(path)
    try:
        done_rows = conn.execute(
            "SELECT DISTINCT head_sha FROM reviews "
            "WHERE repo = ? AND pr = ? AND head_sha IS NOT NULL AND status = ?",
            (repo, pr, STATUS_DONE),
        ).fetchall()
        running_rows = conn.execute(
            "SELECT head_sha, created_at FROM reviews "
            "WHERE repo = ? AND pr = ? AND head_sha IS NOT NULL AND status = ?",
            (repo, pr, STATUS_RUNNING),
        ).fetchall()
    finally:
        conn.close()
    suppressed = {row["head_sha"] for row in done_rows}
    for row in running_rows:
        if _parse_created_at(row["created_at"]) >= cutoff:
            suppressed.add(row["head_sha"])
    return suppressed


def error_attempts_for_pr(path: str | None, repo: str, pr: int, head_sha: str) -> int:
    """How many times this exact (repo, pr, head_sha) has errored.

    Each retry is its own review row (trigger_review -> create_review), so the
    count of error rows is the attempt count. The watcher uses it to bound
    retries: park the SHA once it reaches the cap instead of re-firing every tick
    (which would burn model spend on a deterministically-failing PR).
    """
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM reviews "
            "WHERE repo = ? AND pr = ? AND head_sha = ? AND status = ?",
            (repo, pr, head_sha, STATUS_ERROR),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


def last_error_at_for_pr(
    path: str | None, repo: str, pr: int, head_sha: str
) -> str | None:
    """ISO created_at of the most recent errored run for this exact SHA, or None.

    Drives the watcher's light backoff: only retry an errored SHA once this is
    older than one poll interval, so a flapping upstream is not hammered.

    The latest row is chosen by parsing each error timestamp to an aware UTC
    instant and taking the chronological max, not SQL MAX(created_at): a lexical
    MAX over mixed-format rows can return an earlier instant, which would shorten
    the backoff window. The stored string is returned unchanged so the caller's
    own parse keeps working.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT created_at FROM reviews "
            "WHERE repo = ? AND pr = ? AND head_sha = ? AND status = ?",
            (repo, pr, head_sha, STATUS_ERROR),
        ).fetchall()
    finally:
        conn.close()
    stamps = [row["created_at"] for row in rows if row["created_at"]]
    if not stamps:
        return None
    return max(stamps, key=_parse_created_at)


def set_status(path: str | None, review_id: int, status: str) -> None:
    conn = connect(path)
    try:
        conn.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
        conn.commit()
    finally:
        conn.close()


_ERROR_REASON_MAX = 500


def set_error(
    path: str | None, review_id: int, reason: str, error_class: str | None = None
) -> None:
    """Mark a run errored and record why + when, in one write.

    reason is a short 'ExceptionType: message' string, truncated so a stray giant
    payload (e.g. a dumped response body) can never bloat the row. error_class is
    the exception type alone, stored split out so error buckets are a cheap GROUP
    BY instead of re-parsing the message every read. completed_at stamps the
    terminal instant so an errored run still has a latency datapoint.
    """
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE reviews SET status = ?, error = ?, error_class = ?, "
            "completed_at = ? WHERE id = ?",
            (STATUS_ERROR, (redact(reason) or "")[:_ERROR_REASON_MAX], error_class,
             _now_iso(), review_id),
        )
        conn.commit()
    finally:
        conn.close()


_INTERRUPTED_REASON = (
    "interrupted: the review process exited before reaching a terminal state "
    "(redeploy/restart/crash); reclaimed on startup"
)


def reconcile_orphaned_running(path: str | None) -> list[int]:
    """Retire every still-`running` row to error/interrupted. Call once on startup.

    A run records its own terminal status from inside the review task (set_error /
    save_result). If that process dies first (a container redeploy is the common
    case, also OOM or crash) neither handler runs, so the row is frozen at
    `running` with no completed_at, and the console shows it as a phantom live
    review forever, its age ticking up indefinitely. At app startup the process
    that owned any pre-existing running row is gone by construction (compose stops
    the old container before the new one boots, and this runs before the watcher
    or any request can create a fresh run), so every running row is orphaned: mark
    it error / error_class='interrupted', stamp completed_at, and return the ids
    for logging. The watcher's own stale-running reclaim already re-reviews the
    head SHA separately; this only retires the dead label so it stops counting as
    live and inflating the running metric.
    """
    conn = connect(path)
    try:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM reviews WHERE status = ?", (STATUS_RUNNING,)
            )
        ]
        if ids:
            conn.execute(
                "UPDATE reviews SET status = ?, error = ?, error_class = ?, "
                "completed_at = ? WHERE status = ?",
                (STATUS_ERROR, _INTERRUPTED_REASON, "interrupted", _now_iso(),
                 STATUS_RUNNING),
            )
            conn.commit()
        return ids
    finally:
        conn.close()


def set_fallback(path: str | None, review_id: int, model_used: str,
                 fallback_from: str, reason: str) -> None:
    """Record that this review ran on the fallback model after the primary failed.
    Row-level provenance so a fallback is always visible, never a silent override.
    Called before save_result flips the row to done."""
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE reviews SET model_used = ?, fallback_from = ?, "
            "fallback_reason = ? WHERE id = ?",
            (model_used, fallback_from, (redact(reason) or "")[:_ERROR_REASON_MAX], review_id),
        )
        conn.commit()
    finally:
        conn.close()


UNRECORDED_LANES = "the run reported no lane records"


def _coverage_json(result: ReviewResult) -> str:
    """The run's lane records, always written.

    NULL in this column means one thing only: the row predates the column, and the
    verdict document grandfathers those into the legacy reading. A run that happens
    now and reports nothing is a different claim and must not inherit that, or the
    permissive branch becomes the default for every future call site that forgets
    to record its lanes. So an empty record is written explicitly, as `none`.
    """
    lanes = getattr(result, "coverage", None) or [
        {"lane": "engine", "status": "error", "checks": 0, "note": UNRECORDED_LANES}]
    return json.dumps({"level": verdict.coverage_level(lanes), "lanes": lanes})


def save_result(path: str | None, review_id: int, result: ReviewResult) -> None:
    """Persist a completed run: counts + completion stamp on the row, findings as
    child rows, status done.

    findings holds the verified survivors (what the dashboard shows). When the run
    carries its full single-pass output (result.all_findings), every pre-verification
    finding is also written to findings_raw with the verifier's shadow verdict, so
    single-pass vs verified precision can be compared later. All in one transaction
    so a row never shows done without its findings.
    """
    conn = connect(path)
    try:
        # A head resolved after fetching files cannot identify the reviewed snapshot.
        conn.execute(
            "UPDATE reviews SET status = ?, raw_count = ?, dropped = ?, "
            "completed_at = ?, coverage_json = ?, branch = ?, author = ?, "
            "files_changed = ?, lines_added = ?, lines_removed = ? WHERE id = ?",
            (STATUS_DONE, result.raw_count, result.dropped, _now_iso(),
             _coverage_json(result), result.branch, result.author,
             result.files_changed, result.lines_added, result.lines_removed, review_id),
        )
        if result.head_sha:
            row = conn.execute("SELECT head_sha FROM reviews WHERE id = ?", (review_id,)).fetchone()
            if row is not None and row["head_sha"] and row["head_sha"] != result.head_sha:
                logging.getLogger("argus.db").warning(
                    "review %s: reviewed head %s differs from stored head %s; keeping stored head",
                    review_id, result.head_sha, row["head_sha"],
                )
        if result.head_committed_at:
            conn.execute("UPDATE pr_heads SET head_committed_at=? WHERE review_id=?",
                         (result.head_committed_at, review_id))
        conn.executemany(
            "INSERT INTO findings ("
            "review_id, file, line, severity, confidence, title, description, "
            "proposed_fix, rule_id, is_bug, verdict_is_real, verdict_confidence, "
            "fingerprint, lane, judge_verdict, judge_confidence, judge_model, "
            "end_line, category, evidence"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [_finding_row(review_id, item, is_bug=True) for item in result.bugs]
            + [_finding_row(review_id, item, is_bug=False) for item in result.nits],
        )
        if result.all_findings:
            conn.executemany(
                "INSERT INTO findings_raw ("
                "review_id, file, line, severity, confidence, title, description, "
                "proposed_fix, rule_id, is_bug, verdict_is_real, verdict_confidence, "
                "fingerprint, lane, judge_verdict, judge_confidence, judge_model, "
                "end_line, category, evidence"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _finding_row(
                        review_id, item,
                        is_bug=item.finding.severity.value in ("critical", "major"),
                    )
                    for item in result.all_findings
                ],
            )
        conn.commit()
    finally:
        conn.close()


def save_publish_outcome(path: str | None, review_id: int, outcome: PublishOutcome) -> None:
    """Record publishing separately so posting cannot change the review's status."""
    payload = None
    if outcome.payload is not None:
        payload = json.dumps(outcome.payload)
        if len(payload) > 200_000:
            # Keep oversized records valid JSON and make the loss explicit. ASCII
            # JSON makes the character limit equal the stored UTF-8 byte limit.
            original = payload
            low, high = 0, 200_000
            while low < high:
                middle = (low + high + 1) // 2
                candidate = json.dumps({"truncated": True, "original_bytes": len(original),
                                        "preview": original[:middle]})
                if len(candidate) <= 200_000:
                    low = middle
                else:
                    high = middle - 1
            payload = json.dumps({"truncated": True, "original_bytes": len(original),
                                  "preview": original[:low]})
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE reviews SET publish_status = ?, published_at = ?, publish_error = ?, "
            "published_comments = ?, publish_payload = ? WHERE id = ?",
            (outcome.status, (outcome.posted_at or _now_iso()) if outcome.status == "posted" else None,
             outcome.reason, outcome.comments_posted, payload, review_id),
        )
        if outcome.status in {"posted", "dry_run"} or outcome.gh_review_id:
            _save_published_items(conn, review_id, outcome)
        conn.commit()
    finally:
        conn.close()


def _save_published_items(conn, review_id, outcome):
    review = conn.execute("SELECT repo, pr, head_sha FROM reviews WHERE id=?", (review_id,)).fetchone()
    mode = "dry_run" if outcome.status == "dry_run" else "posted"
    if outcome.bot_login:
        conn.execute("INSERT OR IGNORE INTO publishing_identities VALUES (?, ?, ?)",
                     (review["repo"], outcome.bot_login, outcome.posted_at or _now_iso()))
    for item in outcome.items:
        # Review ids increase over time; the newest review's rendering wins regardless of write order.
        conn.execute(
            "INSERT INTO published_findings "
            "(review_id,repo,pr,head_sha,fingerprint,severity,category,path,line,end_line,title,"
            "placement,mode,gh_review_id,gh_comment_id,posted_at,unverified) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo,pr,fingerprint,head_sha,mode) DO UPDATE SET "
            "gh_review_id=CASE WHEN excluded.placement=published_findings.placement "
            "THEN COALESCE(excluded.gh_review_id,published_findings.gh_review_id) ELSE excluded.gh_review_id END, "
            "gh_comment_id=CASE WHEN excluded.placement=published_findings.placement "
            "THEN COALESCE(excluded.gh_comment_id,published_findings.gh_comment_id) ELSE excluded.gh_comment_id END, "
            "placement=excluded.placement, unverified=excluded.unverified, review_id=excluded.review_id, "
            "severity=excluded.severity, category=excluded.category, path=excluded.path, line=excluded.line, "
            "end_line=excluded.end_line, title=excluded.title, posted_at=excluded.posted_at "
            "WHERE excluded.review_id >= published_findings.review_id",
            (review_id, review["repo"], review["pr"], review["head_sha"], item["fingerprint"],
             item["severity"], item.get("category"), item["file"], item["line"], item.get("end_line"),
             item["title"], item["placement"], mode, outcome.gh_review_id, item.get("gh_comment_id"),
             outcome.posted_at or _now_iso(), int(item.get("unverified", False))),
        )


def _finding_row(review_id: int, item: VerifiedFinding, *, is_bug: bool) -> tuple:
    f = item.finding
    judge = item.judge
    return (
        review_id, f.file, f.line, f.severity.value, f.confidence, f.title,
        f.description, f.proposed_fix, f.rule_id, int(is_bug),
        int(item.verdict.is_real), item.verdict.confidence,
        item.fingerprint or None, item.lane,
        judge.verdict if judge else None,
        judge.confidence if judge else None,
        judge.model if judge else None,
        f.end_line, f.category, f.evidence,
    )


def list_reviews(path: str | None = None) -> list[dict]:
    conn = connect(path)
    try:
        columns = ", ".join('"' + row["name"].replace('"', '""') + '"'
                            for row in conn.execute("PRAGMA table_info(reviews)")
                            if row["name"] != "publish_payload")
        rows = conn.execute(
            f"SELECT {columns} FROM reviews ORDER BY created_at DESC, id DESC"  # nosec B608 - quoted schema names
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_review(path: str | None, review_id: int) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_findings(path: str | None, review_id: int) -> list[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM findings WHERE review_id = ? ORDER BY id", (review_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_unverified_findings(path: str | None, review_id: int) -> list[dict]:
    """Keep refuted serious findings visible without duplicating survivors."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT r.* FROM findings_raw r WHERE r.review_id = ? "
            "AND r.lane = 'llm' AND r.verdict_is_real = 0 "
            "AND r.severity IN ('critical', 'major') "
            "AND NOT EXISTS (SELECT 1 FROM findings f "
            "WHERE f.review_id = r.review_id AND f.fingerprint = r.fingerprint) "
            "ORDER BY r.id", (review_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


MARK_VALUES = ("real", "fp", "stale", "cant_locate", "unsure")


def add_mark(
    path: str | None, finding_id: int, mark: str, *, marker: str | None = None,
    note: str | None = None,
) -> int:
    """Record one operator label on a finding. Append-only: a re-label is a new
    row, and the latest by id wins, so the labeling history is never overwritten.

    The finding must exist (FK + explicit check) so a typo'd id is a 404 at the
    API, not a silently orphaned label.
    """
    if mark not in MARK_VALUES:
        raise ValueError(f"mark must be one of {MARK_VALUES}, got {mark!r}")
    conn = connect(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if exists is None:
            raise KeyError(finding_id)
        cursor = conn.execute(
            "INSERT INTO marks (finding_id, mark, marker, marked_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (finding_id, mark, marker, _now_iso(), note),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def latest_marks_for_review(path: str | None, review_id: int) -> dict[int, dict]:
    """{finding_id: latest mark row} for one review's findings, newest label wins.

    One query joined to findings so the detail view can show the operator's call
    next to each finding without an N+1.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT m.finding_id, m.mark, m.marked_at, m.note FROM marks m "
            "JOIN findings f ON f.id = m.finding_id "
            "WHERE f.review_id = ? ORDER BY m.id",
            (review_id,),
        ).fetchall()
    finally:
        conn.close()
    latest: dict[int, dict] = {}
    for row in rows:  # ordered by id asc, so the last write for a finding wins
        latest[row["finding_id"]] = dict(row)
    return latest


_SEVERITIES = ("critical", "major", "minor", "nit")


def severity_counts_by_review(path: str | None = None) -> dict[int, dict[str, int]]:
    """Per-review finding counts grouped by severity, in one query.

    Returns {review_id: {"critical": n, "major": n, "minor": n, "nit": n}} with
    every severity zero-filled. Unverified counts (the raw findings) match what
    the console's per-row tally shows; verdict filtering lives in metrics.py.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT review_id, severity, COUNT(*) AS n FROM findings "
            "GROUP BY review_id, severity"
        ).fetchall()
    finally:
        conn.close()
    counts: dict[int, dict[str, int]] = {}
    for row in rows:
        bucket = counts.setdefault(row["review_id"], {s: 0 for s in _SEVERITIES})
        if row["severity"] in bucket:
            bucket[row["severity"]] = row["n"]
    return counts


def get_config(path: str | None = None) -> dict | None:
    """Return the stored config (repo_allowlist parsed to a list), or None if unset."""
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    data = dict(row)
    data["repos"] = json.loads(data.pop("repo_allowlist"))
    data["deny"] = json.loads(data.get("deny") or "[]")
    data["owned_repos"] = json.loads(data.get("owned_repos") or "[]")
    data["publish_repos"] = _parse_publish_repos(data.get("publish_repos"))
    return data


def _parse_publish_repos(value) -> list[str] | None:
    if value is None:
        return None
    try:
        repos = json.loads(value)
    except (ValueError, TypeError):
        return None
    return repos if isinstance(repos, list) and all(isinstance(repo, str) for repo in repos) else None


class PublishReposUnreadable(RuntimeError):
    """The stored publish list exists but cannot be trusted (locked DB, corrupt value)."""


def _strict_publish_repos(value) -> list[str] | None:
    if value is None:
        return None
    try:
        repos = json.loads(value)
    except (ValueError, TypeError) as error:
        raise PublishReposUnreadable("stored publish list is not valid JSON") from error
    if not isinstance(repos, list) or not all(isinstance(repo, str) for repo in repos):
        raise PublishReposUnreadable("stored publish list is not a list of names")
    return repos


def _read_publish_row(conn: sqlite3.Connection):
    """(publish_repos, deny) for the singleton row, or None. A database from before the
    publish column keeps its deny policy: the deny column is read on its own, and only a
    schema that has no deny column either reads as an empty policy."""
    try:
        return conn.execute("SELECT publish_repos, deny FROM config WHERE id = 1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" not in str(error):
            raise
    try:
        row = conn.execute("SELECT deny FROM config WHERE id = 1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" not in str(error):
            raise
        row = conn.execute("SELECT 1 FROM config WHERE id = 1").fetchone()
        return None if row is None else (None, None)
    return None if row is None else (None, row[0])


def get_publish_state(path: str | None = None) -> tuple[list[str] | None, list[str]]:
    """Return (dashboard publish list or None, config deny list) from one read-only query.

    None means the list was never set (no file, table, column, row or a NULL value), so the
    caller may fall back to the environment. Anything else that stops the read raises
    PublishReposUnreadable: falling back then could re-enable posting an operator turned off."""
    target = Path(path or db_path())
    if not target.is_file():
        return None, []
    try:
        # Read-only mode also prevents creation if the file disappears after the check.
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        try:
            row = _read_publish_row(conn)
        finally:
            conn.close()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None, []
        raise PublishReposUnreadable("publish list could not be read") from error
    if row is None:
        return None, []
    repos = _strict_publish_repos(row[0])
    # An unreadable deny list would silently lift exclusions, on the stored list and on the env one.
    try:
        deny = json.loads(row[1] or "[]")
    except (ValueError, TypeError) as error:
        raise PublishReposUnreadable("stored deny list is not valid JSON") from error
    if not isinstance(deny, list) or not all(isinstance(entry, str) for entry in deny):
        raise PublishReposUnreadable("stored deny list is not a list of names")
    return repos, deny


def get_publish_repos(path: str | None = None) -> list[str] | None:
    return get_publish_state(path)[0]


def set_publish_repos(path: str | None, repos: list[str]) -> None:
    conn = connect(path)
    try:
        conn.execute("UPDATE config SET publish_repos = ? WHERE id = 1", (json.dumps(repos),))
        conn.commit()
    finally:
        conn.close()


def save_config(
    path: str | None,
    *,
    repos: list[dict],
    model: str,
    bug_cap: int,
    min_confidence: float,
    deny: list[str] | None = None,
    watch_mode: str | None = None,
    owned_repos: list[dict] | None = None,
) -> None:
    """Upsert the singleton config row (id = 1)."""
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO config (id, repo_allowlist, model, bug_cap, min_confidence, deny, watch_mode, owned_repos) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "repo_allowlist = excluded.repo_allowlist, model = excluded.model, "
            "bug_cap = excluded.bug_cap, min_confidence = excluded.min_confidence, "
            "deny = COALESCE(?, config.deny), watch_mode = COALESCE(?, config.watch_mode), "
            "owned_repos = COALESCE(?, config.owned_repos)",
            (json.dumps(repos), model, bug_cap, min_confidence,
             json.dumps(deny or []), watch_mode or "auto", json.dumps(owned_repos or []),
             json.dumps(deny) if deny is not None else None, watch_mode,
             json.dumps(owned_repos) if owned_repos is not None else None),
        )
        conn.commit()
    finally:
        conn.close()
