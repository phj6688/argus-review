# Publishing, the reply loop and acceptance tracking

This page explains what Argus posts to GitHub and how it tracks what happens next. The short setup is in the [README](../README.md#github-setup).

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `ARGUS_PUBLISH` | `0` | `0` is off. `dry-run` renders and stores the payload without posting. `1` posts. |
| `ARGUS_PUBLISH_REPOS` | empty | Comma-separated `owner/name` seed list. The console list replaces it once you save from the dashboard. Empty means no publishing, dry-run included. |
| `ARGUS_PUBLISH_TOKEN` | empty | Write token of a separate bot account. Never falls back to `GITHUB_TOKEN`. |
| `ARGUS_PUBLISH_BOT_LOGIN` | empty | Login of the posting account. Empty reads it from the token. |
| `ARGUS_PUBLISH_AUTO_INVITE` | `0` | `1` lets the console invite the bot to a repository with `GITHUB_TOKEN` when the bot cannot push. Needs Administration:write on that token. |
| `ARGUS_PUBLISH_MIN_SEVERITY` | `minor` | Lowest severity that gets an inline comment. Nits are only counted in the summary. |
| `ARGUS_PUBLISH_MAX_COMMENTS` | `4` | Inline comments per review. Extra findings go in the review body. |
| `ARGUS_REPLY_LOOP` | `1` | Reply to authors and resolve fixed threads. Needs `ARGUS_PUBLISH=1`. |
| `ARGUS_RECHECK_MAX_PER_RUN` | `10` | Maximum finding re-checks per collection run. |
| `ARGUS_REPLY_MAX_WRITES_PER_RUN` | `20` | Maximum reply and resolve attempts per collection run. |
| `ARGUS_OUTCOME_INTERVAL_TICKS` | `10` | Collect outcomes every N watcher polls. |

A publishing failure never changes the status of a completed review.

## Choose repositories in the console

The Config page has a "GitHub posting" section with two lists: "Review only" and "Review and post to GitHub". Drag a repository from one list to the other, or press the arrow button on its row. Each move saves at once.

- The dashboard is the primary way to manage the list. `ARGUS_PUBLISH_REPOS` is only the starting value. Until you save from the dashboard, the console shows `Source: ARGUS_PUBLISH_REPOS`. After the first save the stored list wins and the environment value is ignored for the list.
- `ARGUS_PUBLISH` stays an environment setting. The console shows the mode as read-only text. With the mode `off`, a repository you add gets no GitHub posts until the operator sets `ARGUS_PUBLISH` to `1`. Use `dry-run` to render and store payloads without posting.
- The console lists repositories that Argus watches. The API also accepts any well-formed `owner/name`.
- A repository can never be added when its owner is in `ARGUS_DENY_OWNERS`, or when it or its owner is in the config deny list. The API answers `422` and saves nothing, whatever the console shows. Entries stored earlier are dropped at read time if their owner or repository later becomes denied. If the stored list cannot be read, nothing is posted until it can.
- The API is `PUT /config/publish-repos` with `{"repos": ["owner/name"]}`. It replaces the whole list and returns the `publish` object that `GET /config` also carries. Each change writes one INFO log line with the old and new list.

### Bot access

Posting needs the bot account to have write access to the repository. When you add a repository, Argus:

1. Checks the access of the bot with `ARGUS_PUBLISH_TOKEN` (`permissions.push` on the repository). The result is cached, 10 minutes when the bot can push and 1 minute otherwise.
2. If the bot cannot push, `ARGUS_PUBLISH_AUTO_INVITE=1` is set, and the owner is in `ARGUS_WATCH_OWNERS`, invites the bot as a collaborator with `GITHUB_TOKEN`, then accepts the invitation with `ARGUS_PUBLISH_TOKEN`.
3. If the invite is off or any step fails, saves the repository anyway. The row shows a "no bot access" badge, the reason, and a link to `https://github.com/<owner>/<repo>/settings/access`. Add the bot there by hand.

The auto-invite is off by default, so `GITHUB_TOKEN` stays a read-only token and Argus makes no write call with it. `ARGUS_PUBLISH_AUTO_INVITE=1` is the one opt-in exception. It needs a `GITHUB_TOKEN` with the Administration repository permission set to write (a classic token needs the `repo` scope and admin rights on the repository). Without that permission the invite fails and step 3 applies. `GET /integrations` reports the setting as `github.publishing.autoInvite`.

One request grants access to at most 10 newly added repositories. Repositories beyond that are saved and only checked, with no invite, so add the bot to them by hand or save them in smaller batches. The console adds one repository per move, so it never reaches the cap. Token values are never logged or returned.

## What a review post contains

Argus posts one pull request review with `event=COMMENT`. The verdict is in the summary.

- Inline comments are ordered by severity, then by reviewer confidence. Equal confidence keeps the finding order. Missing confidence counts as zero.
- Findings over the cap go in the review body under "Comment limit".
- Refuted critical and major findings from the model lane go under "Unverified (the verifier disagreed)" in the body. They do not change the verdict or the counts and use no inline slots. The section lists at most 10 items and says how many it left out.
- Fingerprint markers prevent duplicate inline comments across commits. A later head with nothing new posts no review.
- With publishing on, collection recovers a lost post from GitHub within seven days. It checks completed reviews that are at least two minutes old.

## Dry run

Start with `ARGUS_PUBLISH=dry-run` and one allowlisted repository. Complete a review, then run:

```bash
argus publish-preview <review_id> --db <database>
argus publish-placement <review_id> --db <database>
```

Preview renders the review without posting. Placement prints the recorded inline and summary items. It fetches the current patch with the read token and checks every line of an inline span. A later head can change that answer. Dry runs are recorded apart and never enter acceptance rates. Stored payloads are capped at 200 KB. A larger record holds a truncated JSON preview.

Set `ARGUS_PUBLISH=1` after you check placement.

## Reply loop

The loop needs live publishing and an allowlisted repository.

- When the flagged lines change at a new head, Argus re-checks the finding. A confirmed fix gets a reply, and the thread is resolved.
- A request in a thread triggers a re-check when its first non-empty line opens with "recheck", "re-check" or "check again", or when it mentions the bot login. Only the PR author and people with OWNER, MEMBER or COLLABORATOR association can ask. Each finding gets at most one request reply per head.
- A request at the reviewed head gets `No change to this code since <sha>.` when the file is unchanged. Argus never claims a fix at that head.
- A comparison of 300 or more files never confirms a fix. Every patch in the comparison must be complete.
- A trusted first reply that rejects the finding, with at least four words after its opening, gets an acknowledgement and the thread is resolved. A trusted deferral gets "Noted.".
- Replies carry a hidden evidence marker. Collection uses it to recover a reply after an ambiguous write.

A re-check is not proof of a correct fix. A fix that moves the defect into a helper, or that only rewrites the flagged line, can pass. Treat a confirmed fix as a strong signal and read the diff for the cases that matter.

## Acceptance tracking

Collection runs in the background. It reads recent replies and thread state for open pull requests and for those closed within seven days. `GET /stats` includes `ops.acceptance`, grouped by repository, severity and placement.

| Label | Meaning |
| --- | --- |
| `strict_accepted` | The author's first reply opens with a fixed verb, or a re-check confirms a fix at a later head. Resolving a thread alone does not count. |
| `loose_accepted` | Strict acceptance, or a change in the original flagged region, unless the first reply rejects or defers the finding. |
| `rejected`, `deferred` | The first reply says so. |
| `unconfirmed` | Everything else. |

The strict re-check is stricter than a publisher's "Addressed in commit" marker. Deletions, fixes outside the flagged lines, and fixes that keep an evidence line do not count as strict. It mostly under-counts, but a model that misjudges can still over-count. A helper extracted with renamed arguments, a moved short call and an in-place edit such as an added comment can still pass.

Rates divide by all posted inline findings, including unconfirmed ones, with Wilson 95% intervals. Summary findings have no reply thread and cannot be accepted through a reply. Open pull requests show lower early rates because authors have not acted yet.

For an offline report, make a consistent snapshot of the database and run the scripts against the snapshot. The database runs in WAL mode, so a plain file copy of a live database can miss recent writes or be unreadable. `VACUUM INTO` writes a clean snapshot without stopping the API:

```bash
docker compose exec -T argus-api python -m sqlite3 /data/argus.db "VACUUM INTO '/tmp/argus-copy.db'"
docker compose cp argus-api:/tmp/argus-copy.db /tmp/argus-copy.db
docker compose exec -T argus-api rm /tmp/argus-copy.db
.venv/bin/python scripts/acceptance_report.py /tmp/argus-copy.db --since 2026-10-14
.venv/bin/python scripts/export_known_fps.py /tmp/argus-copy.db > known_fps.jsonl
rm /tmp/argus-copy.db
```

For a host install, run the first command against your `ARGUS_DB_PATH` with `uv run python -m sqlite3`. The snapshot holds review data. Delete it when you finish.

## Learning from rejections

A rejected finding enters the live known-false-positive feed. The next review suppresses the exact repository, file and normalized title. Titles are lowercased with whitespace collapsed. The live cache lasts at most 60 seconds.

`scripts/export_known_fps.py` writes the same rows as JSONL. Review them before you add them to a file that `ARGUS_KNOWN_FPS_PATH` points at. See `data/fixtures/known_fps.example.jsonl` for the format.
