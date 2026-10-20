# Offline evaluation

Use offline evaluation to measure a change to the prompt, the context or the verifier before you ship it. You replay a fixed set of historical cases and score the findings against labelled defects. Scoring needs no model and no network.

This is for people who tune Argus. You do not need it to run reviews.

## Case files

A case file is JSONL, one object per line. Each case pins a repository and two commits. Labels use line numbers in the head revision.

```json
{"case_id":"example-42","repo":"owner/project","pr":42,"base_sha":"abc123","head_sha":"def456","split":"heldout","pr_title":"Fix timeout","pr_body":"","expected":[{"id":"example-42-timeout","file":"src/client.py","start_line":20,"end_line":24,"severity":"major","cls":"timeout","summary":"Request can hang indefinitely","source":"manual","weight":1.0}],"negatives":[]}
```

- `split` is `dev` or `heldout`. Keep the held-out split for final checks only.
- `pr_title` and `pr_body` are optional.
- Expectation ids must be unique across the file.
- `severity` is `critical`, `major`, `minor` or `nit`.
- Positive `source` values are `cr_accepted`, `incident`, `szz` or `manual`.
- `negatives` use the same shape with source `cr_rejected` or `known_fp`. They mark known false positives.
- `weight` defaults to 1.0 and must be finite and not negative.

Your case file describes real code. Keep it outside the repository.

## Replay

```bash
uv run python -m argus.evalkit.replay --cases cases.jsonl --out baseline.jsonl --variant baseline \
  --split heldout --mirror-root mirrors --cache-dir eval-cache
uv run python -m argus.evalkit.score --cases cases.jsonl --results baseline.jsonl candidate.jsonl \
  --out-json scores.json --out-md scores.md --split heldout
```

Replay runs the full review on each case with the configured provider. It needs the same credentials as a live review. `ARGUS_MODEL` gives the default model and `--model` overrides it. Set `ARGUS_EVAL_AUTH_TOKEN` to use a separate token for replay, so offline traffic does not count against your live token.

With `--mirror-root`, replay reads file contents and the repository's `CLAUDE.md` from a local bare mirror at `mirrors/<name>.git`, where `<name>` is the repository name. It then makes no GitHub API calls for them. Without a mirror, snapshots fall back to the GitHub compare API. A 300-file API response is rejected as possibly truncated. Such a repository needs a mirror.

`--mirror-root` defaults to `ARGUS_MIRROR_ROOT`. `--cache-dir` defaults to `ARGUS_EVAL_CACHE`. The cache holds patches, rendered hashes and whole-file reads at head. Use the same case file and cache directory for the baseline and the candidate, and a different `--variant` label for each.

Replay appends each result as it finishes and skips completed `(variant, case_id, run)` records on restart, errors included. Use `--case-ids a,b`, `--limit N`, `--runs N` and `--concurrency N` to choose and repeat cases. A new variant label retries errors.

Each result row records `code_sha` and `code_dirty`. A checkout is dirty when a tracked file has a staged or unstaged change. Without a checkout, rows use `ARGUS_GIT_SHA`, then `unknown`.

`deploy/cron/argus_replay.sh` runs replay from its own checkout and virtual environment, so rows carry a real SHA. It refuses to start when the environment imports `argus` from somewhere else.

## Scoring

Score uses windows `0,3,10` lines by default. Recall averages each expectation's hits across its runs. Any-run recall counts a hit in at least one run. Errors count as misses. Unevaluated cases are listed and excluded. The report also covers volume (findings per case), the duplicate rate, severity groups and Wilson 95% intervals. Pairwise comparisons use exact McNemar tests on any-run hits, with gained and lost labels. Cases with different snapshot inputs are rejected.

Inline counts render each case through the publisher, with its severity threshold and comment cap, and count one per inline comment. A recall gain can then be read next to the inline noise that it would post.

`--emit-pairs pairs.jsonl` exports location candidates for external judging. `--semantic-cache decisions.jsonl` adds confirmed recall from same-issue decisions shaped as `{"expectation_id":"...","finding_key":"<sha256>","same_issue":true}`. An absent or false decision never confirms a hit.

## Verifier-only reruns

`argus.evalkit.reverify` reruns only the verifier on the raw findings of a replay result, refuted ones included. It makes no reviewer call. Its output rows have the replay format, so `score` reads them.

```bash
uv run python -m argus.evalkit.reverify --cases cases.jsonl --results baseline.jsonl \
  --out default_keep.jsonl --variant default_keep --model MODEL \
  --mirror-root mirrors --cache-dir eval-cache
```

`--variant` is a key of `review.VERIFIER_VARIANTS`. `--label` names the output arm. Verdicts are cached in an append-only file next to the output, so a restart verifies no finding twice. Each row carries an `arm_sha256`. The run stops with a message when existing rows of the same label came from another arm. Use a new `--label` or `--out` to start a new arm. The run exits with status 1 when it wrote any error row.

## Scheduled runs

`deploy/cron/` holds optional wrappers for a weekly regression replay and a monthly report. Read the comments at the top of each script. They take their paths from environment variables (`ARGUS_EVAL_DIR`, `ARGUS_EVAL_STATE`, `ARGUS_EVAL_OUTPUT`, `ARGUS_MIRROR_ROOT`, `ARGUS_EVAL_JUDGE`, `ARGUS_MONTHLY_OUTPUT`). The containers run on the Docker network named by `ARGUS_EVAL_NETWORK` (default `bridge`). Set it to your compose network when your model endpoint is a container hostname.

Notification is optional: set `ARGUS_NOTIFY_CMD`, or `ARGUS_NTFY_CONTAINER` with `ARGUS_NTFY_TOPIC`. With none set, the log is the only record. Logs go to `ARGUS_CRON_LOG_DIR`.

The weekly job replays a frozen subset of held-out cases and every incident case. It never changes the reviewer. It alerts when the visible recall, the incident catches or the error share cross a threshold set against stored baselines. Exit code 3 means an alert, 1 an operational failure and 0 a pass.

The wrappers run these commands inside the eval container, where the paths are container paths and `python` is the image interpreter. On the host, put `uv run` in front.

```bash
python -m argus.evalkit.weekly freeze-subset --eval-dir /evaluation --state-dir /state --heldout 20 --seed 1
python -m argus.evalkit.weekly run --eval-dir /evaluation --state-dir /state --out-dir /output \
  --mirror-root /mirrors --model "$ARGUS_MODEL" --judge-model "$JUDGE_MODEL" --concurrency 1
```

Matching a finding to a known defect uses a judge model and a strict rule. Judge decisions are cached with a hash of the judge prompt. Change the judge model or prompt only with a new state directory and a new baseline. `uv run python -m argus.evalkit.judge calibrate` samples cached decisions and reports agreement and Cohen's kappa.

Install cron entries only after you configure the paths and freeze the subset:

```cron
30 1 * * 0 /path/to/argus-review/deploy/cron/argus_weekly.sh
15 5 1 * * /path/to/argus-review/deploy/cron/argus_monthly.sh
```
