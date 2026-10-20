import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from argus.api import db
from argus.evalkit import cron


def test_environment_allowlist(tmp_path):
    values = ['ANTHROPIC_BASE_URL=https://local', 'ANTHROPIC_AUTH_TOKEN=secret',
              'GITHUB_TOKEN=read', 'ARGUS_MODEL=model', 'ARGUS_PUBLISH_TOKEN=publish-secret',
              'OTHER=hidden']
    text = cron.environment(values)
    assert text.splitlines() == values[:4]
    assert 'publish-secret' not in text and 'OTHER' not in text
    with pytest.raises(ValueError):
        cron.environment(values + ['GITHUB_TOKEN=read\nARGUS_PUBLISH_TOKEN=bad'])
    out = tmp_path / 'env'
    cron.write_environment(values, out)
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert out.read_text() == text


def test_provider_settings_are_forwarded_to_scheduled_evaluations():
    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ARGUS_PROVIDER",
             "ARGUS_OPENAI_TOKEN_PARAM")
    assert set(names) <= set(cron.ENV_KEYS)
    values = [f"{name}=test-value" for name in names]
    assert set(cron.environment(values).splitlines()) == set(values)


def test_snapshot_and_repos(tmp_path, monkeypatch):
    source, dest = tmp_path / 'live.db', tmp_path / 'copy.db'
    db.init_db(source)
    monkeypatch.setenv('ARGUS_WATCH_OWNERS', 'owner')
    db.save_config(source, repos=[{'name': 'owner/repo', 'watched': True}], model='m',
                   bug_cap=10, min_confidence=.5, watch_mode='manual',
                   owned_repos=[{'full_name': 'owner/repo', 'archived': False}])
    before = source.read_bytes()
    cron.snapshot(source, dest)
    assert source.read_bytes() == before
    assert cron.watched_repos(dest) == ['owner/repo']
    with sqlite3.connect(dest) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert os.stat(dest).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cron.snapshot(source, dest)


@pytest.mark.parametrize('name', ['argus_weekly.sh', 'argus_monthly.sh'])
def test_shell_syntax(name):
    path = Path('deploy/cron') / name
    subprocess.run(['bash', '-n', str(path)], check=True)
    text = path.read_text()
    assert 'python -m argus.evalkit.' in text
    assert '--user 1000:1000' in text
    assert 'ARGUS_PUBLISH=0' in text


def test_common_shell_syntax():
    subprocess.run(['bash', '-n', 'deploy/cron/common.sh'], check=True)


@pytest.mark.parametrize('name', ['argus_weekly.sh', 'argus_monthly.sh'])
def test_wrapper_disables_inherited_healthcheck_and_parses_body_before_running(name):
    lines = (Path('deploy/cron') / name).read_text().splitlines()
    assert lines[2] == '{'
    assert lines[-2:] == ['exit', '}']
    for line in lines:
        if 'docker run' in line:
            assert '--no-healthcheck' in line


def test_common_disables_inherited_healthcheck():
    for line in Path('deploy/cron/common.sh').read_text().splitlines():
        if 'docker run' in line:
            assert '--no-healthcheck' in line


def test_eval_image_and_wrappers():
    directory = Path('deploy/cron')
    image = (directory / 'Dockerfile.eval').read_text()
    assert image.startswith('FROM argus-api:latest\n')
    assert 'USER root' in image
    assert 'apt-get install -y --no-install-recommends git' in image
    assert 'rm -rf /var/lib/apt/lists/*' in image
    assert image.rstrip().endswith('USER argus')
    weekly = (directory / 'argus_weekly.sh').read_text()
    assert 'IMAGE="argus-eval:latest"' in weekly
    assert 'docker build -q -t "$IMAGE" -f "$DIR/Dockerfile.eval" "$DIR"' in weekly
    assert 'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in weekly
    assert weekly.index('docker build ') < weekly.index('docker run ')
    assert '-e HOME=/tmp' in weekly
    monthly = (directory / 'argus_monthly.sh').read_text()
    assert 'IMAGE="argus-api:latest"' in monthly
    assert '--out-prs /output/reference_prs.jsonl' in monthly
    assert '--reference-prs /output/reference_prs.jsonl' in monthly


def test_gate_summary(tmp_path, capsys):
    report = tmp_path / 'report.json'
    report.write_text('{"gate":{"summary":"Not enough findings.","decision":"The operator decides."}}')
    assert cron.main(['gate-summary', '--report', str(report)]) == 0
    assert capsys.readouterr().out.strip() == 'Not enough findings. The operator decides.'


def test_snapshot_readonly_directory(tmp_path):
    from argus.evalkit import labels, monthly

    source = tmp_path / 'live.db'
    db.init_db(source)
    directory = tmp_path / 'private'
    directory.mkdir()
    dest = directory / 'copy.db'
    with sqlite3.connect(source) as conn:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        cron.snapshot(source, dest)
    directory.chmod(0o555)
    dest.chmod(0o444)
    try:
        labels.export(dest, tmp_path / 'labels.jsonl')
        assert monthly.build(dest, {}, '2026-09')['month'] == '2026-09'
        with sqlite3.connect(f'{dest.as_uri()}?mode=ro', uri=True) as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert sorted(p.name for p in directory.iterdir()) == ['copy.db']
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize('stored', [None, 'configured'])
def test_effective_model(tmp_path, monkeypatch, capsys, stored):
    source = tmp_path / 'live.db'
    db.init_db(source)
    monkeypatch.setattr(db, 'db_path', lambda: source)
    monkeypatch.setenv('ARGUS_MODEL', 'fallback')
    if stored:
        db.save_config(source, repos=[], model=stored, bug_cap=10, min_confidence=.5)
    assert cron.main(['model']) == 0
    assert capsys.readouterr().out.strip() == (stored or 'fallback')


def test_optional_router_key():
    assert cron.environment(['OPENROUTER_API_KEY=key', 'ARGUS_PUBLISH_TOKEN=secret']) == 'OPENROUTER_API_KEY=key\n'
    assert cron.environment([]) == ''


def test_reference_environment_only_token(monkeypatch, capsys):
    for key in cron.ENV_KEYS:
        monkeypatch.setenv(key, 'private')
    monkeypatch.setenv('ARGUS_PUBLISH_TOKEN', 'publish')
    assert cron.main(['environment', '--reference-only']) == 0
    assert capsys.readouterr().out == 'GITHUB_TOKEN=private\n'


def test_wrapper_limits_and_private_inputs():
    directory = Path('deploy/cron')
    weekly = (directory / 'argus_weekly.sh').read_text()
    assert 'argus.evalkit.cron model' in weekly
    assert 'docker image inspect' in weekly and 'argus-api:latest' in weekly
    assert 'ARGUS_EVAL_IMAGE_ID=' in weekly and 'ARGUS_EVAL_CODE_SHA' not in weekly
    assert '--name argus-weekly' in weekly
    assert 'docker rm -f argus-weekly' in weekly
    assert 'timeout --signal=TERM --kill-after=300 11h' in weekly
    assert '--replay-timeout-s 21600 --deadline-s 36000' in weekly
    common = (directory / 'common.sh').read_text()
    assert '"$status" -eq 3' in common and '"$status" -eq 2' not in common
    assert 'alert-summary' in common
    assert '"$SNAPSHOT_REMOTE-wal"' in common and '"$SNAPSHOT_REMOTE-shm"' in common
    assert 'docker rm -f "$WEEKLY_CONTAINER"' in common
    monthly = (directory / 'argus_monthly.sh').read_text()
    assert 'ARGUS_GITHUB_MIN_REMAINING=2500' in monthly
    assert 'build_environment --reference-only' in monthly
    assert '--until "$UNTIL"' in monthly.split('run_module labels', 1)[1]
    assert '"$SNAPSHOT_DIR/copy.db:/copy.db:ro"' in monthly
    assert '--network none' in monthly.split('run_module()', 1)[1]
    assert '--env-file' not in monthly.split('run_module()', 1)[1]
    assert 'mktemp -d /tmp/argus-monthly.' in monthly
    assert 'rm -rf "$SNAPSHOT_DIR"' in common
    assert '-regex' in monthly and '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}' in monthly


@pytest.mark.parametrize('month,expected', [('2026-09', '2026-09 2026-09-01 2026-10-01'),
                                            ('2025-12', '2025-12 2025-12-01 2026-01-01')])
def test_month_override(month, expected):
    text = Path('deploy/cron/argus_monthly.sh').read_text().split('\nsource ', 1)[0] + '\n}\n'
    for zone in ('Pacific/Honolulu', 'Europe/Berlin'):
        result = subprocess.run(['bash', '-c', text + '\nprintf "%s %s %s" "$MONTH" "$SINCE" "$UNTIL"'],
                                env={**os.environ, 'ARGUS_MONTH': month, 'TZ': zone,
                                     'ARGUS_EVAL_OUTPUT': '/unused', 'ARGUS_MONTHLY_OUTPUT': '/unused'},
                                check=True, capture_output=True, text=True)
        assert result.stdout == expected
    assert '+%d' not in Path('deploy/cron/argus_monthly.sh').read_text()


def test_alert_summary(tmp_path, capsys):
    report = tmp_path / 'report.json'
    report.write_text('{"alert":true}')
    assert cron.main(['alert-summary', '--report', str(report)]) == 0
    assert 'true' in capsys.readouterr().out
