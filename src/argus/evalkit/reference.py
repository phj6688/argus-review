"""Read coverage and inline outcomes for a reference reviewer."""

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from argus import acceptance, github
from argus.repo_policy import validate_repo

from .reports import average, rate

MARKER = re.compile(r'✅\s*Addressed in commits?', re.I)


def _stamp(value):
    return acceptance._stamp(value)


def mine(repos, since, until, login):
    start, end = _stamp(since), _stamp(until)
    if start >= end:
        raise ValueError('since must precede until')
    rows = []
    pr_rows = []
    prs = reviewed = failed_prs = 0
    waited = 0

    def request(function, *args):
        nonlocal waited
        while True:
            try:
                return function(*args)
            except github.GitHubRateLimited as error:
                delay = max(0, error.reset_at - time.time()) + 30
                remaining = 10800 - waited
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                waited += min(delay, remaining)
                if delay > remaining:
                    raise

    for repo in sorted(set(repos)):
        validate_repo(repo)
        pulls = request(github.list_cached, f'/repos/{repo}/pulls', {
            'state': 'all', 'sort': 'created', 'direction': 'desc', 'per_page': 100})
        for pull in pulls:
            try:
                if pull.get('draft') or not start <= _stamp(pull['created_at']) < end:
                    continue
                findings, pr_row = _mine_pr(repo, pull, login, request)
            except github.GitHubRateLimited:
                raise
            except Exception:
                failed_prs += 1
                continue
            prs += 1
            reviewed += pr_row['reviewed_by_reference']
            rows.extend(findings)
            pr_rows.append(pr_row)
    counts = Counter(r['label'] for r in rows)
    summary = {'date': datetime.now(timezone.utc).isoformat(), 'since': since, 'until': until,
               'failed_prs': failed_prs, 'coverage': rate(reviewed, prs), 'inline_per_reviewed_pr': average(len(rows), reviewed),
               'strict': rate(counts['strict_accepted'], len(rows)),
               'loose': rate(sum(r['loose'] is True for r in rows), len(rows)),
               'loose_unknown': sum(r['loose'] is None for r in rows), 'labels': dict(counts)}
    return rows, summary, pr_rows


def _compare(path):
    response = github.get(path)
    response.raise_for_status()
    return response.json()


def _mine_pr(repo, pull, login, request):
    pr, head = pull['number'], pull['head']['sha']
    comments = request(github.list_cached, f'/repos/{repo}/pulls/{pr}/comments', {'per_page': 100})
    reviews = request(github.list_cached, f'/repos/{repo}/pulls/{pr}/reviews', {'per_page': 100})

    def author(item):
        return (item.get('user') or {}).get('login', '').casefold() == login.casefold()

    has_review = any(author(item) for item in [*reviews, *comments])
    pr_row = {'repo': repo, 'pr': pr, 'created_at': pull['created_at'],
              'reviewed_by_reference': has_review}
    rows = []
    comparisons = {}
    for root in comments:
        if not author(root) or root.get('in_reply_to_id'):
            continue
        replies = sorted((c for c in comments if c.get('in_reply_to_id') == root['id']
                          and not author(c) and (c.get('user') or {}).get('type') != 'Bot'
                          and (c.get('user') or {}).get('login')),
                         key=lambda c: (_stamp(c['created_at']), c['id']))
        reply = acceptance.classify_reply(replies[0].get('body', '')) if replies else 'none'
        marker = bool(MARKER.search(root.get('body') or ''))
        old = root.get('original_commit_id')
        end_line = root.get('original_line')
        start_line = root.get('original_start_line') or end_line
        changed = None
        if not marker and reply not in {'fixed', 'rejected', 'deferred'} and old and end_line:
            if old not in comparisons:
                try:
                    comparisons[old] = request(_compare, f'/repos/{repo}/compare/{old}...{head}')
                except github.GitHubRateLimited:
                    raise
                except Exception:
                    # Cache failed compares too; every affected root remains unknown.
                    comparisons[old] = {}
            comparison = comparisons[old]
            if comparison.get('status') == 'identical':
                changed = False
            elif comparison.get('status') == 'ahead':
                changed = acceptance.region_changed(
                    {'path': root['path'], 'line': start_line, 'end_line': end_line},
                    comparison['files'])
        label, loose = acceptance.labels(reply, changed, rechecked=marker)
        if label == 'loose_accepted':
            label = 'unconfirmed'
        if label == 'unconfirmed' and changed is None:
            loose = None
        row = {'repo': repo, 'pr': pr, 'comment_id': root['id'], 'original_commit': old,
               'final_head': head, 'path': root['path'], 'start_line': start_line,
               'end_line': end_line, 'label': label, 'loose': loose}
        if label in {'strict_accepted', 'rejected'}:
            row['polarity'] = 'positive' if label == 'strict_accepted' else 'negative'
        rows.append(row)
    return rows, pr_row


def repositories(value):
    path = Path(value)
    text = path.read_text(encoding='utf-8') if path.is_file() else value
    if text.lstrip().startswith('['):
        return json.loads(text)
    return [item for item in re.split(r'[,\s]+', text.strip()) if item]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repos', 'since', 'until', 'out-rows', 'out-summary'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--out-prs')
    parser.add_argument('--login', default='coderabbitai[bot]')
    args = parser.parse_args(argv)
    try:
        rows, summary, prs = mine(repositories(args.repos), args.since, args.until, args.login)
    except Exception as error:
        print(f'Reference read failed: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    outputs = [(args.out_rows, ''.join(json.dumps(r) + '\n' for r in rows)),
               (args.out_summary, json.dumps(summary, indent=2) + '\n')]
    if args.out_prs:
        outputs.append((args.out_prs, ''.join(json.dumps(r) + '\n' for r in prs)))
    for filename, content in outputs:
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
