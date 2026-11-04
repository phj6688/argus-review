# GitLab support

Argus reviews GitLab merge requests the same way it reviews GitHub pull requests. It works with self-managed GitLab and with gitlab.com. You can start a review from the console, the API, the CLI and the MCP server. With publishing enabled, Argus posts inline discussions and one summary note.

GitLab support is off until you set `ARGUS_GITLAB_URL`. One Argus process talks to one GitLab instance. A GitLab merge request never reads or changes GitHub data, and GitHub behaviour is unchanged.

Argus can also poll allowed projects for open merge requests and queue them for review (see [Watcher](#watcher)). Replies to GitLab discussions are off by default: see [Reply loop](#reply-loop).

## Setup

1. Create a dedicated bot account and add it to each project you want reviewed as Reporter. On self-managed GitLab, a project access token can create the bot user for you. The console cannot invite the bot. Do not use a personal account.
2. Create the token described under [Token choice](#token-choice). Use `read_api` for reads only, or `api` if Argus should comment.
3. Set `ARGUS_GITLAB_URL` to the HTTPS address of your instance, for example `https://gitlab.example.com`. Pass `ARGUS_GITLAB_TOKEN` through the environment. If your instance uses a private certificate authority, mount its CA bundle read-only and set `ARGUS_GITLAB_CA_BUNDLE` to the path inside the container.
4. Set `ARGUS_GITLAB_ALLOW` to the groups or projects Argus may read, for example `gitlab:gitlab.example.com#123`. An empty list permits no project. A fork source project needs its own allow entry.
5. Start with a review without posting. Set `ARGUS_GITLAB_PUBLISH=dry-run`, put one canonical project key in `ARGUS_GITLAB_PUBLISH_PROJECTS`, and set the shared `ARGUS_PUBLISH` to `dry-run` or `1`. The lower mode wins, ordered `0`, `dry-run`, `1`. If either setting is `0`, publishing is off, including the dry run.
6. Rebuild and recreate the containers with `docker compose up -d --build --force-recreate`. Every setting must be in the `environment:` list of the API service in `docker-compose.yml`. The shipped file already lists all GitLab settings.
7. Call `GET /integrations` with your API token. Its `gitlab` block must show `identity.status: ok` and `refusal: null`. In the console Trigger form, enter `gitlab:gitlab.example.com#123#7` for project 123 and merge request 7. Check the review and its stored publishing payload. A dry run reads metadata and discussions and prepares placements, but posts nothing.
8. After you check that run, set `ARGUS_GITLAB_PUBLISH=1` and `ARGUS_PUBLISH=1`. Keep only that project in `ARGUS_GITLAB_PUBLISH_PROJECTS`, recreate the containers, and trigger a review.

The CLI works the same way once the environment is set:

```bash
uv run argus review 'gitlab:gitlab.example.com#123' 7
uv run argus review https://gitlab.example.com/group/app/-/merge_requests/7
```

## Token choice

On gitlab.com Free, use a personal access token (PAT) that belongs to a dedicated bot user. Project and group access tokens need a paid tier there. See the GitLab documentation for [project access tokens](https://docs.gitlab.com/user/project/settings/project_access_tokens/) and [group access tokens](https://docs.gitlab.com/user/group/settings/group_access_tokens/).

On self-managed GitLab, use a project access token, which creates a bot user, or the PAT of a dedicated bot user. A project access token is limited to its own project. A review that spans several private projects, or a fork source in another project, can fail even when both projects are on the allow list. Use the PAT of a dedicated bot user for cross-project and private-fork reviews.

Argus reads the token from the environment only and never prints it. `GET /integrations` reports only whether it is configured. A freshly created project token can answer 404 for a fraction of a second, so a first call right after creation may need a retry.

## Scopes and roles

`read_api` is enough for every read Argus makes: merge request metadata, diffs, versions, files and membership. Posting a comment needs `api`. A `read_repository` token is not enough, because it cannot read merge requests. The bot needs Reporter access or higher on the target project. A Reporter can resolve only the threads it started. Resolving a thread that someone else started needs Developer access (see [Reply loop](#reply-loop)).

Before it posts, Argus reads `members/all/:user_id` for the bot's numeric id. It requires an integer `access_level` of 20 or more. A lower or missing membership reports `Reporter access required`. This checks membership, not the token scopes.

## Minimum GitLab version

Argus is tested on GitLab CE 19.5.3. `author.bot` first appears in merge request payloads in 19.5. If the field is missing, Argus treats the author as not a bot. The `collapsed` and `too_large` diff flags first appear in 18.4.

Argus reads changes with the `/diffs` endpoint. The inferred minimum for that endpoint is 15.7. That minimum is not verified on a live older instance. Without the 18.4 flags, an empty diff marks the review as incomplete unless it is a pure rename or mode change. That fallback is tested with modified recordings only. Older releases are not validated end to end, and gitlab.com has not been validated end to end either.

## Settings

Blank values use these defaults. GitLab stays disabled when the URL is unset.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_GITLAB_URL` | empty | One HTTPS instance base URL with an optional path prefix. No credentials, query or fragment. |
| `ARGUS_GITLAB_TOKEN` | empty | Bot credential, read only from the environment. Without it, authenticated calls are refused. |
| `ARGUS_GITLAB_CA_BUNDLE` | empty | Optional CA bundle path. Blank uses the normal TLS trust store. In Docker, use the mounted container path. |
| `ARGUS_GITLAB_ALLOW` | empty | Comma-separated group or project path entries, or numeric project keys. Empty permits no project. |
| `ARGUS_GITLAB_PUBLISH` | `0` | `0` is off, `dry-run` prepares and stores placements, `1` posts. The shared publishing mode can lower it. |
| `ARGUS_GITLAB_PUBLISH_PROJECTS` | empty | Comma-separated exact canonical project keys for publishing, including dry runs. No wildcards or whitespace. Empty permits none. |
| `ARGUS_GITLAB_NOTES_PER_MINUTE` | `30` | Positive local note rate per bot user. |
| `ARGUS_GITLAB_TIMEOUT_S` | `30` | Finite positive request timeout in seconds. |
| `ARGUS_GITLAB_MAX_RESPONSE_BYTES` | `10485760` | Maximum response size in bytes, including decoded content. Minimum 1024. |
| `ARGUS_GITLAB_TRUSTED_LEVEL` | `30` | Minimum author membership level for listing trust. Accepts 0, 5, 10, 15, 20, 30, 40 or 50. |
| `ARGUS_GITLAB_WATCH` | off | Poll allowed projects into the shared review queue. Accepts `1`, `true`, `yes` or `on`. Needs a URL and a credential. |
| `ARGUS_GITLAB_RECONCILE_TICKS` | `10` | Positive number of GitLab polling ticks between full open merge request listings. |
| `ARGUS_GITLAB_REPLY_LOOP` | off | Enable replies and resolution with `1`, `true`, `yes` or `on`. Needs both publish modes at `1`, an allowed publishing project and the global reply loop on. |

GitLab publishing also uses these shared settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_DENY_OWNERS` | empty | Comma-separated hard-deny owners or policy entries. A bare owner also matches the current top-level GitLab group, ignoring case. |
| `ARGUS_PUBLISH` | `0` | Shared publishing ceiling: `0`, `dry-run` or `1`. GitLab uses the lower of this and its own mode. |
| `ARGUS_PUBLISH_MIN_SEVERITY` | `minor` | Posting threshold. Accepts `critical`, `major`, `minor` or `nit`. |
| `ARGUS_PUBLISH_MAX_COMMENTS` | `4` | Non-negative inline comment cap per review. Extra findings go in the summary. |

Allow entries use `gitlab:HOST/group[/sub]` for a path or `gitlab:HOST#ID` for a numeric project id. A path matches whole segments, so `gitlab:gitlab.example.com/team` never matches the group `team-other`. Publishing always needs the exact canonical id key in `ARGUS_GITLAB_PUBLISH_PROJECTS`, for example `gitlab:gitlab.example.com#123`.

## Limits and pacing

- Argus posts at most 30 notes per minute per bot user by default, with a burst capacity of 5. A lock per bot, project and merge request serializes the whole publish flow, including recovery reads.
- A 429 response pauses that bot's note bucket for the `Retry-After` time, then Argus retries once. The HTTP client also honors `RateLimit-Reset`, and uses 60 seconds when neither header is usable.
- Responses are capped at 10485760 bytes by default. Compressed content obeys the same cap. Requests time out after 30 seconds by default.
- Each review posts at most 4 inline comments by default. Extra findings move to the summary. The summary body limit is 60000 characters after rendering and escaping, and an oversized summary is refused.
- GitLab pause and pacing state stay separate from GitHub.

## Safety rules

Argus treats everything that comes from a merge request, a diff, or the server as untrusted.

- **Hard deny.** A project is denied when its current top-level group matches an `ARGUS_DENY_OWNERS` entry, ignoring case. Argus resolves the project by numeric id on every review. It follows renames, and it refuses a transfer into a denied group even when an id allow entry still matches. A failed lookup denies access too. An allow entry cannot override a hard-deny owner, and database settings cannot remove one.
- **Forks.** For a fork merge request, both the target project and the fork source project must be on the allow list and pass the live deny check before Argus reads diffs, versions or files. A denied fork stops the review before any diff read. A private fork the bot cannot read is refused as `fork_source_denied`. File content is read through the target project at the merge request head, never through the fork.
- **URLs.** Argus builds every request URL from fixed routes. It rejects credentials, fragments, traversal, unsafe encoding and any host, port or path prefix other than the configured one. It refuses redirects, sends the token only in the `PRIVATE-TOKEN` header, disables proxies from the environment, and keeps TLS verification on.
- **Quick actions and mentions.** GitLab runs a line that starts with a slash, such as `/rebase` or `/merge`, when a user with enough rights posts it. Argus puts a backslash before every such line and a zero-width space after every `@` in text it posts, then checks the final body again right before posting. An evidence block can therefore show a literal `\/`.
- **No blind retries.** Only GET requests retry. A failed discussion POST is never repeated, because it may already have created a note. Argus rereads the discussions to find out what was written.

## Review entry points

`POST /reviews` and the MCP tool `review_pr(repo, pr)` accept a canonical `gitlab:HOST#PROJECT_ID` key or a path such as `gitlab:HOST/group/sub/project`. The `pr` value is the merge request number (iid) inside the project. A path resolves once through the project endpoint. Argus stores every review under the numeric id key, so a path and an id name the same project after a rename.

The CLI commands `review` and `compress` accept either key form followed by the iid. They also accept a single merge request URL such as `https://HOST/PREFIX/group/sub/project/-/merge_requests/7`. The URL must use HTTPS and the configured host, port and path prefix, matched by whole path segments. A trailing slash is allowed, and a query string or fragment is dropped. The API `repo` field accepts keys only.

Every GitLab review checks the target project first, then the merge request, then the fork source if there is one. Policy denials return 403 with `repository denied: <key>`. A failed lookup caused by a timeout, an upstream error, a rate limit or authentication returns 502 with a safe classification. An unknown, renamed or malformed path returns 422. The CLI prints one error line and exits with status 1. With GitLab unset, GitLab keys and URLs fail with `GitLab is not configured` and make no request.

These stay GitHub-only: `POST /review-diff`, the MCP tool `review_diff`, the CLI commands `publish-placement` and `publish-preview`, and `doctor`. Commands that take a repository or a stored review refuse a GitLab key before any request.

### What Argus reads

- **Head match.** Argus reads the first page of merge request versions and selects the newest. Its head commit must equal the merge request `sha`, because `/diffs` always serves the newest version. A mismatch, including the short gap after a merge request is created, raises a retryable `HeadMoved` error and does not trigger a model fallback. Argus checks the head again after the last diff page.
- **Incomplete diffs.** A file that is `too_large`, `collapsed`, generated or binary has no patch, and an unsafe path is skipped. Both mark the file list as incomplete, so the review reports `partial` coverage instead of `full`. A `changes_count` that ends in `+`, is null, or is larger than the number of fetched entries marks the list incomplete too. The summary then says that some changed files were not visible.
- **Files.** File content comes from the target project at the merge request head. A missing file returns nothing. Other denials still abort the review.
- **House rules.** Argus reads `CLAUDE.md` from the target project's default branch and passes it to the reviewer, cut to the shared house-rule limit.

### Integration status

When GitLab is configured, `GET /integrations` adds a `gitlab` block:

| Field | Meaning |
| --- | --- |
| `host` | Canonical host and any non-default port, without a path or credentials. |
| `baseUrl` | Configured HTTPS base URL with any non-default port and path prefix, without credentials or a trailing slash. |
| `token` | `configured` or `missing`. Never the value. |
| `identity` | `status: ok` with `username` and a boolean `bot`, or `status: failed` with a safe `reason`. A missing token gives `status: skipped` with `reason: token_missing` and makes no request. |
| `publish` | `mode` is `0`, `dry-run`, `1` or `invalid`. `projectCount` counts configured publish entries. |
| `allowCount` | Number of configured allow entries. |
| `refusal` | The policy refusal reason, or null when the policy is valid. |
| `trustUnknown` | Number of open heads skipped because the author's membership could not be resolved. A positive count also adds a health warning. |
| `watch` | `enabled`, `projectCount`, `lastPolledAt`, `lastError` (one of `gitlab_auth`, `gitlab_denied`, `gitlab_rate_limited`, `gitlab_http` or null), `trustUnknown` and `forkSourceDenied`. Failures also appear as health warnings. Credentials and server error text are never returned. |

The identity comes from `GET /user`. Argus caches a success for 300 seconds and a failure for 60 seconds, and a change of host or token clears the cache. Without `ARGUS_GITLAB_URL` the block is absent and the response is unchanged.

## Console

When GitLab is configured, the Trigger form accepts `gitlab:HOST#PROJECT_ID#iid`, `gitlab:HOST/group/sub/project#iid`, or a merge request URL under the configured host and path prefix. GitHub input such as `owner/repo#123` still works. Review rows and detail pages show a merge request chip and the live project path, or the canonical key when no path was recorded. A detail page links to the merge request only when the configured base URL and a verified project path are available. The Integrations page shows a GitLab card with connection, identity, publishing and policy status.

## Publishing

The publisher posts discussions first, then one plain summary note. It never uses draft notes, because bulk publication can silently drop a draft whose position does not resolve. In order, it does this:

1. Pick the lower of `ARGUS_PUBLISH` and `ARGUS_GITLAB_PUBLISH`. If that is off, make no request. Require an exact publish project key, a positive merge request number and a 40-digit hexadecimal head.
2. Read the merge request, apply the live deny checks to the target and any fork source, resolve the bot identity and require Reporter access.
3. Take the lock for this bot, project and merge request. Concurrent publishes wait.
4. Read the head again and select its diff version. A changed head or a missing version skips publishing.
5. Read every discussion page. Only notes written by this bot's numeric user id count. Recover an existing review from its summary footer.
6. Apply the severity threshold and the comment cap. Build positions from the reviewed patches. Findings without a position go under "Outside the diff", and findings over the cap go under "Comment limit".
7. Post each discussion through the safe writer, which checks the body and obeys the pacer.
8. If a write is ambiguous (a 500, a gateway error, a timeout), read the discussions for the body's marker before doing anything else. A recovered note counts as posted. A 400, or a 500 with no matching note, moves the finding to the summary. A 502, 503 or 504 with no matching note stays unknown, because the write may still finish, so publishing stops with `partial_post` before the summary.
9. Check the type, path and lines of the returned note. A matching diff note is `anchored`. A plain note or a mismatched position becomes `downgraded` and its finding moves to the summary.
10. Render and post the summary note with severity, evidence and coverage counts, the risk category and the review markers.

Positions anchor on the new side of a patch. Added lines use `new_line` only, context lines carry the correct `old_line` and `new_line`, and removed lines have no new-side anchor. A renamed file keeps its previous name as `old_path`. Only single-line anchors are enabled.

A dry run performs the reads and builds the bodies and positions, but writes nothing. It stores the planned placements without remote ids.

Retrying a partially posted review posts its own missing summary note, even when another review of the same head already posted one. A new review of an unchanged head posts only findings that were not posted before, and nothing when there are none. Plain notes count against GitLab's per-user note limit. Discussions do not.

### What the database holds

The shared tables keep their GitHub column names. For GitLab rows:

| Column | GitLab value |
| --- | --- |
| `published_findings.gh_review_id` | Summary note id, an integer. |
| `published_findings.gh_comment_id` | Inline diff note id, an integer. |
| `published_findings.gh_thread_id` | Discussion id, a string. |
| `publishing_identities.bot_user_id` | Numeric GitLab user id of the bot. |

Argus records an inline item only with both a note id and a discussion id, and a summary item only with a confirmed summary note id. Any unresolved attempt is stored as `error` with the reason `partial_post`. Stored errors and payloads are redacted.

## Watch store

The watch store keeps GitLab merge request heads next to the GitHub ones. A listing that covers only part of a project updates just the merge requests it lists. A complete listing also closes the heads it no longer returns. Fast merges use the existing queue and requeue rules.

Poll helpers compare the stored head sha and overlap the previous cursor by five minutes, because a push may not move `updated_at`.

With trusted-only enabled, a stored `author_trusted=1` passes. A value of `0` is skipped as `untrusted author (gitlab membership)`, and NULL is skipped as `trust_unknown`. GitHub author associations never grant GitLab trust. A rate limit retries at the reset time without counting an attempt. Each forge pauses only its own queued heads.

The additive table `gitlab_poll_cursors` stores the update cursor, the last full reconcile, the last error and the last poll time for each project key.

GitHub polls preserve queued GitLab heads. Workers check the expanded GitLab set and the live project policy before a review, and each forge keeps its own quota pause.

## Listing and trust

Allow entries expand nested groups without shared or archived projects. Matching uses whole path segments from the live response. Every candidate, including numeric project ids, is checked again against live project policy. A renamed path is refused: use the current path or the numeric id.

Incremental listings read all pages with a five minute overlap and keep explicit closures. Full listings return the open heads so they can be compared with the stored shas, because a push may leave `updated_at` unchanged. A full listing is incomplete while any open head has a missing or malformed sha, so stored work stays open. Both kinds keep UTC timestamps and server precision, and hold the cursor at the earliest preparing head until its sha is usable.

Author trust uses direct or inherited project membership. Developer access (30) is trusted by default and `ARGUS_GITLAB_TRUSTED_LEVEL` changes the level. A missing member is untrusted. A failed or malformed lookup stays unknown and is cached for the current tick. Bot flags and project or group bot usernames identify bots.

The recovery sweep retries the oldest open heads that were skipped for unknown trust, within its limit. Unresolved heads, including those without an author id, move to the back of the sweep order so later heads get checked. Resolved membership reuses the queue rules: draft and bot exclusions, completed reviews and attempt limits. `/integrations` reports `gitlab.trustUnknown` and adds a GitLab trust warning when the count is positive. GitHub behaviour is unchanged.

## Watcher

Start the shared watcher with `ARGUS_WATCH_ENABLED=1`, then turn on GitLab polling with `ARGUS_GITLAB_WATCH=1`. Projects come from `ARGUS_GITLAB_ALLOW`. Argus never adds them to the GitHub owner list. GitLab runs after the GitHub pass, even when GitHub is paused or fails, and each forge keeps its own quota pause.

The allow set refreshes every `ARGUS_WATCH_DISCOVERY_TICKS` GitLab ticks. A temporary refresh failure keeps the cached set. A group or project that is permanently unavailable is skipped, so discovery continues with the remaining entries. At startup, when a project re-enters the set, and every `ARGUS_GITLAB_RECONCILE_TICKS` ticks, Argus reads a full listing to recover missed closures. Other ticks use a stored cursor with a five minute overlap. A changed head sha queues another review even when the update timestamp did not move. The cursor advances only after the queue write succeeds.

Because projects are listed explicitly, `ARGUS_WATCH_ACTIVE_DAYS` filters open merge requests by staleness on both full and incremental listings. The draft, bot and trusted-author settings work as they do for GitHub. Merge requests from denied fork sources are skipped. A temporary failure while reading the source project leaves the merge request queued for the worker's live check. Each pass retries at most five heads with unknown membership while GitLab is not paused.

## Reply loop

Replies stay off until all of these hold:

- `ARGUS_GITLAB_REPLY_LOOP` is enabled.
- `ARGUS_PUBLISH` and `ARGUS_GITLAB_PUBLISH` are both `1`.
- The project is in `ARGUS_GITLAB_PUBLISH_PROJECTS`.
- `ARGUS_REPLY_LOOP` is not `0`. That global switch stops GitLab and GitHub replies.

Each pass checks live project policy and the recorded numeric bot id before it compares code. The bot id must match the id of the token in use. A missing or different id skips the project and counts `bot_id_missing` or `bot_id_mismatch`.

The loop works like the GitHub loop. When the flagged lines change at a new head, Argus re-checks the finding. A confirmed fix gets a reply and the thread is resolved. A trusted author can ask for a re-check in a thread, and each finding gets at most one request reply per head. An incomplete comparison never confirms a fix. When a trusted author rejects a finding, Argus answers that the finding will not be posted again and stores the rejection, so a later review of the same file and title drops it. A confirmed fix at a later head removes that stored rejection.

Resolution goes through the API and also works for outdated threads. A thread is outdated when its position head differs from the current head, and a resolve of such a thread is counted as `resolved_outdated`. Replies share the publisher's note pacing. They carry their own evidence marker, so a write with an uncertain outcome is found again on the next pass instead of being posted twice. A discussion that no longer exists counts as `discussion_missing`.

Roles: a Developer bot can resolve any thread. A Reporter bot can resolve only threads it started. When GitLab answers a resolve with 403, Argus degrades: the reply stays posted, the project gets no more resolve attempts for the rest of that run, `resolve_forbidden` is counted and `/integrations` shows the refusal with a hint to give the bot the Developer role.

## Thread reader

`argus.gitlab.threads.list_threads` reads every discussion page of a merge request through the guarded client. The frozen `GitLabThread` and `ThreadNote` records keep the API order, leave out system notes and discussions that hold only system notes, and keep the resolution state, the numeric id of the user who resolved the thread and the `head_sha` of the first kept note's position.

`is_bot_note` compares the author's numeric id with the recorded bot id. A rename cannot make the bot answer itself, and a human who takes over the bot's old username does not look like the bot. Only when no id is recorded does it fall back to the newest recorded login, compared with `casefold()`. Each fallback adds one to `COUNTERS["bot_id_missing"]`, even when no login exists.

`is_outdated` compares the thread's position `head_sha` with the current head of the merge request. Read the merge request first and use its `diff_refs.head_sha`, or `sha`. GitLab provides no `outdated`, `original_position` or `line_code` field, so a note without a position head is never marked outdated.

## Outcome collection

Collection checks live project policy before it reads merge requests or discussions. A denied project records no outcomes. Only posted inline findings map to discussions: first by the stored discussion id, then by an exact trailing fingerprint marker on the first bot note. Recovery saves a missing id once. Bot identity uses the recorded numeric user id, so a username change cannot turn a bot note into a human reply.

Human replies store membership trust as 1 for trusted, 0 for untrusted, or NULL when the lookup is unknown. A later unknown lookup keeps a known value. Membership results are cached for one collection run. A missing discussion records no replies or resolutions and adds one to `gitlab_threads_missing`.

Each merge request stores its current state, merge flag, close time and head. Resolvable discussions record both resolved and unresolved states while the merge request is open or was closed within seven days. A stored request that closed more than seven days ago is skipped unless an open watched head exists. Each request commits on its own. GitLab does not use the repository reply cursor to filter discussions.

GitLab request failures are counted and do not stop GitHub collection. A rate limit sets `gitlab_paused` and skips the remaining GitLab projects for that run, also when the rate limit occurs during a project or membership lookup. Projects are skipped as before when GitLab is not configured.

## Compare and re-check

Comparisons use `repository/compare` and `merge_base`. The old head must be the common ancestor for the status `ahead`. A missing ref or ancestor gives an incomplete `diverged` result. A timeout, 1000 diffs or a hidden patch makes the coverage incomplete. Binary files alone keep the coverage complete, but a re-check needs a patch for every file.

An incomplete comparison stays unknown and cannot prove that a file is unchanged. Re-checks read the old and the current file through the GitLab reader. Insertions above a finding shift its anchor without marking its region changed.

Acceptance collection and the reply loop share these helpers, so GitHub and GitLab findings use the same re-check rules. The GitHub adapter now exposes `compare` too, and GitHub results are unchanged.

## Acceptance measurement

When GitLab is configured and posted GitLab findings exist, the acceptance report adds a separate `gitlab` section next to the GitHub figures. The scripts print it under its own heading. Its denominator holds only posted inline findings that were confirmed as diff notes (`anchor_status='anchored'`). The counts `rejected_400`, `rejected_500` and `downgraded` sit outside every rate. A reply counts only when `author_trusted` is 1. NULL and 0 do not count. Repositories appear under their project path, or under the full key when two projects share a path.

GitLab rates are not comparable with GitHub rates. Resolve controls and outdated threads behave differently on the two forges, and a resolve rate undercounts after pushes, because outdated threads lose their resolve control. The report note says so, and the GitHub baseline figures do not change.

`mark_precision`, `verified_severity_counts`, `verified_bug_split` and `verified_finding_count` take `forge="gitlab"`. Their default stays GitHub.

## Known limits

- The URL guard accepts only branch and tag names that start with a letter or digit and use only letters, digits, dot, underscore, slash and hyphen. A request that carries any other name is refused, for example a branch name with a plus sign or a non-ASCII letter.
- Multi-line ranges are disabled, because GitLab accepts ranges without validating them.
- There is no automatic invitation. Add the bot to each project yourself.
- Collapsed, too-large, generated and binary diffs lower coverage.
- `POST /review-diff`, `publish-preview`, `publish-placement` and `doctor` are GitHub-only.
- GitLab replies need their own opt-in flag and the live publishing gates listed under Reply loop.
- Post-merge reviews of GitLab merge requests do not follow `ARGUS_WATCH_FAST_MERGES`. That setting applies to GitHub only.
- A full listing returns open merge requests only. With `ARGUS_GITLAB_RECONCILE_TICKS=1`, a merge request that is merged between two polls is closed without a post-merge review. Use a larger value to keep post-merge reviews.
- Outcome collection recognises only the newest recorded bot user id for a project. After you switch the publishing account, discussions that the earlier account started are not matched, and their replies and resolutions are not collected.
- Outcome collection does not filter other bot accounts. A bot with Developer access that writes a reply opening with a fixed verb counts as a trusted human reply.
- The watcher reads one GitLab instance and only the projects in `ARGUS_GITLAB_ALLOW`. It never adds them to the GitHub owner list.
- A merge request that was merged before the watcher saw it can be skipped as `trust_unknown`. The membership sweep retries open merge requests only, so that skip is not retried.
- Quick-action behaviour, limits and token tiers can differ on other GitLab versions and on gitlab.com. Test a dry run on your instance first.

## Measurement filter

Baseline readers count GitHub keys only: the GitHub part of acceptance reports and stats, head coverage
and coverage stats, review and finding metrics, benchmark inputs, monthly reports,
acceptance labels, corpus finding and mark readers, and known false-positive exports.
Weekly API reports and the overview's measurement counters inherit these filters.

Operational readers stay forge-wide: the watcher queue, claims and worker health,
store operations and console lists, and the `reviews_window`, `top_error_class` and
`review_health` functions used by the watchdog, health endpoint and stats operations block.
Lookups by review id and suppression by repository keep their existing scope.
Acceptance collection now reads GitLab outcomes, including compare and re-check
inputs. Reconciliation and per-repository replies still skip GitLab.

Use `is_github_key` in Python and `github_key_sql` in SQL. A key with a colon never
feeds a GitHub baseline number, including a future forge prefix.
The configured-only monthly `skipped_gitlab` count includes findings plus posted reviews.
With GitHub-only data, measurements and output shapes stay unchanged.
