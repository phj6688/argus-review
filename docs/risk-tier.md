# Risk tier

Every completed review labels the change `safe` or `risky` using paths and size
only. This is a category, never a score or a correctness verdict. `risky` needs
a human checkpoint before merge. Argus only labels; the merge decision stays with you.

A change is `risky` when any changed path matches a tripwire, added plus deleted
lines exceed 300, or the file list is incomplete. Exactly 300 changed lines
remains `safe` if no path matches and the list is complete. An empty change is
`safe`. Both the old and new paths of a renamed file count. Deleted files count
too. Dropped unsafe paths, omitted file sections and API file caps make the list
incomplete, since an unseen path might match.

The default tripwires are:

```text
nginx*, Caddyfile*, traefik/, cloudflared/, docker-compose*, compose.yml, compose.yaml, *auth*, *proxy*
```

Patterns use case-insensitive glob matching against each individual path segment.
A trailing `/` matches directories only. For example, `traefik/dynamic/apps.yml`
matches, while `docs/traefik.md` and a file literally named `traefik` do not.
Likewise, a file literally named `cloudflared` does not match the directory rule.
`*auth*` also matches names such as `authors.md`.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_RISK_TRIPWIRES` | List above | Comma-separated patterns replacing the whole default list. Whitespace and empty entries are ignored. A blank list uses the defaults. |
| `ARGUS_RISK_MAX_LINES` | `300` | Positive integer limit on added plus deleted lines. Invalid values log a warning and use 300. |

Both settings are read when classifying a change. The implementation is in
`src/argus/risk_tier.py`; it uses files already held by the review and makes no
additional requests. A classifier failure leaves risk absent without failing the
review.

The tier appears in the reviews list, review detail, verdict document, MCP
`get_verdict`, and published review body. The list includes a compact tier, label,
and reasons. Detail and verdict responses also include line counts, the limit,
file count, completeness, and matching paths. Up to 20 matches and five path
reasons are displayed, with totals for the rest. Older reviews have `risk: null`
and show no chip or note.
