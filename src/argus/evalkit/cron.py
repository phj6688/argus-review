"""Small container helpers for private cron inputs."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from argus.api import db, watch_config

from .reports import readonly

ENV_KEYS = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'GITHUB_TOKEN', 'ARGUS_MODEL', 'OPENROUTER_API_KEY',
            'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ARGUS_PROVIDER',
            'ARGUS_OPENAI_TOKEN_PARAM')


def environment(values):
    selected = {}
    for entry in values:
        name, separator, value = entry.partition('=')
        if separator and name in ENV_KEYS:
            if any(char in value for char in '\r\n\0'):
                raise ValueError('container environment contains an invalid value')
            selected[name] = value
    return ''.join(f'{key}={selected[key]}\n' for key in ENV_KEYS if key in selected)


def write_environment(values, path):
    content = environment(values)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)


def snapshot(source, out):
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with readonly(source) as live:
            copy = sqlite3.connect(out)
            try:
                live.backup(copy)
                # A single read-only mount cannot create WAL sidecars.
                mode = copy.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
                if mode != 'delete':
                    raise RuntimeError('database copy must use DELETE journal mode')
            finally:
                copy.close()
    except BaseException:
        Path(out).unlink(missing_ok=True)
        raise


def watched_repos(path):
    with readonly(path) as conn:
        row = conn.execute('SELECT * FROM config WHERE id=1').fetchone()
    if row is None:
        return []
    config = dict(row)
    config['repos'] = json.loads(config.pop('repo_allowlist'))
    config['deny'] = json.loads(config.get('deny') or '[]')
    config['owned_repos'] = json.loads(config.get('owned_repos') or '[]')
    return watch_config.effective(config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('environment').add_argument('--reference-only', action='store_true')
    commands.add_parser('model')
    backup = commands.add_parser('snapshot')
    backup.add_argument('--db')
    backup.add_argument('--out', required=True)
    repos = commands.add_parser('repos')
    repos.add_argument('--db', required=True)
    repos.add_argument('--out', required=True)
    summary = commands.add_parser('gate-summary')
    summary.add_argument('--report', required=True)
    alert = commands.add_parser('alert-summary')
    alert.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    if args.command == 'environment':
        values = [f'{k}={v}' for k, v in os.environ.items()
                  if not args.reference_only or k == 'GITHUB_TOKEN']
        print(environment(values), end='')
    elif args.command == 'model':
        with readonly(db.db_path()) as conn:
            row = conn.execute('SELECT model FROM config WHERE id=1').fetchone()
        print((row['model'] if row else None) or os.environ.get('ARGUS_MODEL', ''))
    elif args.command == 'snapshot':
        snapshot(args.db or db.db_path(), args.out)
    elif args.command == 'repos':
        Path(args.out).write_text(json.dumps(watched_repos(args.db)) + '\n', encoding='utf-8')
    elif args.command == 'alert-summary':
        report = json.loads(Path(args.report).read_text(encoding='utf-8'))
        print('Regression alert:', json.dumps(report['alert']))
    else:
        gate = json.loads(Path(args.report).read_text(encoding='utf-8'))['gate']
        print(gate['summary'], gate['decision'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
