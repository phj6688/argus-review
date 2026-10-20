import json
from collections import Counter

import pytest

from argus import fingerprint, review, schema, verdict
from argus.api import db, main
from argus.diff import FilePatch, compress_diff
from argus.jarvis import live


GOOD = dict(file='a.py', line=2, severity='major', confidence=0.9, title='Wrong result',
            description='Returns the wrong value', proposed_fix='Return the value',
            end_line=None, category='contracts', evidence='return wrong')


@pytest.mark.parametrize('fields,expected', [
    ({'line': 12.0}, {'line': 12}),
    ({'proposed_fix': None}, {'proposed_fix': ''}),
    ({'proposed_fix': []}, {'proposed_fix': ''}),
    ({'category': None}, {'category': 'other'}),
    ({'category': []}, {'category': 'other'}),
    ({'category': 'unknown'}, {'category': 'other'}),
    ({'end_line': 1}, {'end_line': None}),
    ({'end_line': 2.0}, {'end_line': None}),
    ({'end_line': True}, {'end_line': None}),
    ({'evidence': None}, {'evidence': ''}),
])
def test_parser_repairs_new_fields_and_retains_findings(fields, expected):
    found, stats = schema.parse_findings({'findings': [{**GOOD, **fields}]})
    assert len(found) == 1
    for key, value in expected.items():
        assert getattr(found[0], key) == value
    assert stats['findings_repaired'] == 1
    assert stats['findings_dropped'] == 0


def test_missing_new_fields_count_as_repair():
    item = {k: v for k, v in GOOD.items() if k not in ('end_line', 'category', 'evidence')}
    found, stats = schema.parse_findings({'findings': [item]})
    assert (found[0].end_line, found[0].category, found[0].evidence) == (None, 'other', '')
    assert stats['findings_repaired'] == 1


def test_finding_tool_requires_explanations_and_nullable_span():
    spec = review.REVIEW_TOOL.input_schema['properties']['findings']['items']
    assert {'end_line', 'category', 'evidence'} <= set(spec['required'])
    assert spec['properties']['end_line']['type'] == ['integer', 'null']
    assert spec['properties']['category']['enum'] == [
        'contracts', 'error_paths', 'input_handling', 'boundaries', 'resources',
        'concurrency', 'security', 'tests', 'data_integrity', 'other']


def test_pr_section_truncates_description_and_commit_subjects():
    section = review._pr_section('Fix it', 'x' * 4001, [f'subject {n}\nbody' for n in range(25)])
    assert section.startswith('# Pull request\nTitle: Fix it\nDescription (')
    assert 'x' * 4000 in section and 'x' * 4001 not in section
    assert 'truncated' in section
    assert '- subject 19' in section and '- subject 20' not in section
    assert '\nbody' not in section
    assert review._pr_section(None, None, ['subject']) == ''


def test_prompt_order(monkeypatch):
    captured = []
    monkeypatch.setattr(review, 'complete_tool', lambda *args: captured.append(args) or {'findings': []})
    review._deep_review('NUMBERED FILES', 'HOUSE', 'model', '# Intent\nINTENT',
                        '# Pull request\nPR BODY')
    user = captured[0][2]
    pieces = ['# Rule index', '# Repository house rules', '# Intent', '# Pull request',
              '# Changed files', 'NUMBERED FILES',
              'Review the changes above and report every defect via report_findings.']
    positions = [user.index(part) for part in pieces]
    assert positions == sorted(positions)


@pytest.mark.parametrize('line,end,evidence,want,moved,missing,too_short,absent', [
    (2, 3, '\n  target()', 10, 1, 0, 0, 0),
    (10, 11, 'target()', 10, 0, 0, 0, 0),
    (8, None, 'target()', 8, 0, 0, 0, 0),
    (2, None, 'absent()', 2, 0, 1, 0, 0),
    (2, None, '', 2, 0, 0, 0, 1),
    (2, None, 'ok()', 2, 0, 0, 1, 0),
    (19, 20, 'target()', 19, 0, 0, 0, 0),
    (16, 17, 'target()', 20, 1, 0, 0, 0),
])
def test_reanchor_nearest_evidence(line, end, evidence, want, moved, missing, too_short, absent):
    lines = ['pass'] * 22
    lines[9] = lines[19] = '    target()'
    finding = schema.Finding(**{**GOOD, 'line': line, 'end_line': end, 'evidence': evidence})
    counts = review._reanchor_findings([finding], lambda _: '\n'.join(lines))
    assert finding.line == want
    assert finding.end_line == (None if end is None else end + want - line)
    assert counts == {
        'findings_reanchored': moved,
        'evidence_not_found': missing,
        'evidence_too_short': too_short,
        'evidence_missing': absent,
        'evidence_unreadable': 0,
        'evidence_removed_line': 0,
    }


def test_reanchor_strips_numbered_view_prefix():
    finding = schema.Finding(**{**GOOD, 'line': 2, 'evidence': '   10 + target()'})
    counts = review._reanchor_findings([finding], lambda _: 'pass\n' * 9 + '    target()')
    assert finding.line == 10
    assert counts == {
        'findings_reanchored': 1,
        'evidence_not_found': 0,
        'evidence_too_short': 0,
        'evidence_missing': 0,
        'evidence_unreadable': 0,
        'evidence_removed_line': 0,
    }


def test_reanchor_does_not_chase_short_evidence():
    finding = schema.Finding(**{**GOOD, 'line': 2, 'evidence': 'pass'})
    counts = review._reanchor_findings([finding], lambda _: 'pass\n' * 10)
    assert finding.line == 2
    assert counts == {
        'findings_reanchored': 0,
        'evidence_not_found': 0,
        'evidence_too_short': 1,
        'evidence_missing': 0,
        'evidence_unreadable': 0,
        'evidence_removed_line': 0,
    }


def test_reanchor_keeps_yaml_list_evidence_intact():
    finding = schema.Finding(**{**GOOD, 'line': 2, 'evidence': '- name: deployment'})
    counts = review._reanchor_findings(
        [finding], lambda _: 'items:\n- name: deployment\n  image: app')
    assert finding.line == 2
    assert counts == {
        'findings_reanchored': 0,
        'evidence_not_found': 0,
        'evidence_too_short': 0,
        'evidence_missing': 0,
        'evidence_unreadable': 0,
        'evidence_removed_line': 0,
    }


@pytest.mark.parametrize("remote", [False, True])
def test_pipeline_shares_cache_and_preserves_reviewer_context(monkeypatch, remote):
    contents = {'a.py': '\n'.join(['pass'] * 9 + ['value = missing']),
                'deploy/docker-compose.yml': 'services:\n  app:\n    build: .\n    ports: ["0.0.0.0:8080:80"]\n',
                'deploy/Dockerfile': 'FROM python:3.11\n'}
    calls = Counter()
    prompts = []
    payloads = iter([{'findings': [{**GOOD, 'evidence': 'value = missing', 'end_line': 3}]},
                     {'findings': []}])
    def read(path):
        calls[path] += 1
        return contents.get(path)
    def complete(model, system, user, tool):
        prompts.append((tool.name, user))
        if tool.name == 'report_findings':
            return next(payloads)
        return {'is_real': True, 'confidence': 0.9}
    def unexpected_fetch(*args):
        pytest.fail('file_loader must take precedence over remote fetching')
    monkeypatch.setattr(live, '_fetch_file_at', unexpected_fetch)
    monkeypatch.setattr(review, 'fetch_house_rules', lambda *args: None)
    monkeypatch.setattr(review, 'complete_tool', complete)
    monkeypatch.delenv(review.JUDGE_LIVE_ENV, raising=False)
    files = [FilePatch('a.py', 'modified', '@@ -10 +10 @@\n-old\n+value = missing'),
             FilePatch('deploy/docker-compose.yml', 'modified', '@@ -1 +1 @@\n+services:'),
             FilePatch('deploy/Dockerfile', 'modified', '@@ -1 +1 @@\n+FROM python:3.11')]
    result = review.run_diff_review(
        files, repo='o/r' if remote else None, head_sha='head' if remote else None,
        file_loader=read, pr_title='Fix', pr_body='Claim', base_sha='base')
    assert {'a.py', 'deploy/docker-compose.yml', 'deploy/Dockerfile'} <= calls.keys()
    assert set(calls.values()) == {1}
    records = {r['lane']: r for r in result.coverage}
    assert records['probe']['checks'] == 6
    assert any(item.lane == 'probe' and item.finding.rule_id == 'app_bound_to_all_interfaces'
               for item in result.bugs)
    assert records['ruff']['files_analyzed'] == 1
    assert records['config']['files_analyzed'] == 2
    assert records['llm']['files_full'] == 3
    assert records['llm']['findings_reanchored'] == 1
    assert records['llm']['evidence_too_short'] == 0
    finding = result.all_findings[0].finding
    assert finding.line == 10 and finding.end_line == 11
    assert result.all_findings[0].fingerprint == fingerprint.compute(
        "a.py", None, "Wrong result", "value = missing")
    user = next(user for tool, user in prompts if tool == 'report_verdict')
    reviewer_user = next(user for tool, user in prompts if tool == 'report_findings')
    assert '# Pull request' in reviewer_user
    assert '(modified, python, full)' in reviewer_user
    assert '10 + value = missing' in reviewer_user
    assert user.startswith('# Finding\n')
    payload, diff = user.removeprefix('# Finding\n').split('\n\n# Diff under review\n')
    assert json.loads(payload) == {
        'file': 'a.py', 'line': 10, 'severity': 'major', 'confidence': 0.9,
        'title': 'Wrong result', 'description': 'Returns the wrong value',
        'proposed_fix': 'Return the value', 'rule_id': None,
    }
    assert diff == (
        '## File: a.py (python)\n```diff\n@@ -10 +10 @@\n-old\n+value = missing\n```\n\n'
        '## File: deploy/Dockerfile (other)\n```diff\n@@ -1 +1 @@\n+FROM python:3.11\n```\n\n'
        '## File: deploy/docker-compose.yml (yaml)\n```diff\n@@ -1 +1 @@\n+services:\n```')
    assert '# File under verification' not in user
    assert '# Pull request' not in user


def test_db_migrates_old_rows_and_persists_explanation_fields(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = db.connect(path)
    conn.executescript(db._SCHEMA)
    conn.execute("INSERT INTO reviews (repo, pr, model, status, created_at) VALUES ('r', 1, 'm', 'done', 'now')")
    conn.execute("INSERT INTO findings (review_id, file, line, severity, confidence, title, description, proposed_fix, is_bug, verdict_is_real, verdict_confidence) VALUES (1, 'a.py', 2, 'major', .9, 't', 'd', '', 1, 1, .9)")
    conn.commit()
    conn.close()
    db.init_db(path)
    old = db.get_findings(path, 1)[0]
    assert old['end_line'] is None and old['category'] is None and old['evidence'] is None
    assert verdict.document(db.get_review(path, 1), [old])['findings'][0]['end_line'] is None
    vf = schema.VerifiedFinding(finding=schema.Finding(**{**GOOD, 'end_line': 3}),
                                verdict=schema.Verdict(is_real=True, confidence=.9))
    review_id = db.create_review(path, 'r', 2, 'm')
    db.save_result(path, review_id, review.ReviewResult(1, bugs=[vf], all_findings=[vf]))
    conn = db.connect(path)
    for table in ('findings', 'findings_raw'):
        row = dict(conn.execute(f'SELECT * FROM {table} WHERE review_id = ?', (review_id,)).fetchone())
        assert (row['end_line'], row['category'], row['evidence']) == (3, 'contracts', 'return wrong')
    conn.close()
    item = verdict.document(db.get_review(path, review_id), db.get_findings(path, review_id))['findings'][0]
    assert (item['end_line'], item['category'], item['evidence']) == (3, 'contracts', 'return wrong')


def test_api_accepts_and_limits_pr_metadata():
    request = main.DiffReviewRequest(title='Title', description='Body')
    assert (request.title, request.description) == ('Title', 'Body')
    for fields in ({'title': 'x' * 501}, {'description': 'x' * 20001}):
        with pytest.raises(ValueError):
            main.DiffReviewRequest(**fields)


def test_prefetched_intent_metadata_does_not_fetch_again(monkeypatch):
    def fail(*args):
        pytest.fail('metadata fetched twice')
    monkeypatch.setattr(live, '_pr_meta', fail)
    assert live.intent_section('o/r', 1, {'title': '', 'body': '', 'commits': []}) is None


def test_review_system_matches_approved_prompt():
    from pathlib import Path
    expected = Path(__file__).with_name('fixtures').joinpath('review_system.txt').read_text().rstrip('\n')
    assert review.REVIEW_SYSTEM == expected


def test_pr_metadata_fetched_once_and_head_reused(monkeypatch):
    fetches = []
    prompts = []
    def get(url, params=None):
        fetches.append(url)
        class Response:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                if url.endswith('/commits'):
                    return [{'commit': {'message': 'Fix return\nDetails'}}]
                return {'title': 'Fix', 'body': 'Claim', 'head': {'sha': 'head'}}
        return Response()
    reads = []
    def read(repo, path, sha):
        reads.append((path, sha))
        return 'value = 1'
    def complete(model, system, user, tool):
        prompts.append(user)
        return {'findings': []}
    monkeypatch.setattr(live.github, 'get', get)
    monkeypatch.setattr(live, '_fetch_file_at', read)
    monkeypatch.setattr(review, 'fetch_pr_files', lambda *args: [
        FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+value = 1')])
    monkeypatch.setattr(review, 'fetch_house_rules', lambda *args: None)
    monkeypatch.setattr(review, 'complete_tool', complete)
    monkeypatch.delenv(review.JUDGE_LIVE_ENV, raising=False)
    result = review.run_review('o/r', 1)
    assert result.head_sha == 'head'
    assert fetches == ['/repos/o/r/pulls/1', '/repos/o/r/pulls/1/commits']
    assert reads == [('a.py', 'head')]
    assert 'Title: Fix' in prompts[0] and 'Claim' in prompts[0] and '- Fix return' in prompts[0]
    assert next(r for r in result.coverage if r['lane'] == 'llm')['files_full'] == 1


def test_metadata_failure_uses_head_lookup_and_omits_pr_section(monkeypatch, caplog):
    calls = []
    prompts = []
    def metadata(*args):
        calls.append('meta')
        raise OSError('unavailable')
    def head(*args):
        calls.append('head')
        return 'fallback'
    def read(repo, path, sha):
        assert sha == 'fallback'
        return 'pass'
    monkeypatch.setattr(live, '_pr_meta', metadata)
    monkeypatch.setattr(live, '_pr_head_sha', head)
    monkeypatch.setattr(live, '_fetch_file_at', read)
    monkeypatch.setattr(review, 'fetch_pr_files', lambda *args: [
        FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+pass')])
    monkeypatch.setattr(review, 'fetch_house_rules', lambda *args: None)
    monkeypatch.setattr(review, 'complete_tool', lambda *args: prompts.append(args[2]) or {'findings': []})
    monkeypatch.delenv(review.JUDGE_LIVE_ENV, raising=False)
    result = review.run_review('o/r', 1)
    assert result.head_sha == 'fallback'
    assert calls == ['meta', 'head']
    assert '# Pull request' not in prompts[0]
    assert 'metadata lookup failed' in caplog.text


@pytest.mark.parametrize('entry_point', ['diff', 'pr'])
def test_verifier_reuses_compressed_diff_once_per_review(monkeypatch, entry_point):
    prompts = []
    compressed = []
    contents = {'a.py': '\n'.join(['pass'] * 49 + ['target()'] + ['pass'] * 650),
                'b.py': 'other()'}
    files = [FilePatch('a.py', 'modified', '@@ -50 +50 @@\n-old\n+target()'),
             FilePatch('b.py', 'modified', '@@ -1 +1 @@\n-old\n+other()')]
    findings = [{**GOOD, 'line': 50, 'evidence': 'target()', 'title': title} for title in ('one', 'two')]
    payloads = iter([{'findings': findings}, {'findings': []}])
    def compress(files, budget, estimator):
        result = compress_diff(files, budget, estimator)
        compressed.append((files, budget, estimator, result.prompt))
        return result
    def complete(model, system, user, tool):
        prompts.append((tool.name, user))
        if tool.name == 'report_findings':
            return next(payloads)
        assert tool.max_tokens == 2000
        return {'is_real': False, 'confidence': .9}
    monkeypatch.setattr(review, 'compress_diff', compress)
    monkeypatch.setattr(review, 'complete_tool', complete)
    monkeypatch.setattr(review, 'fetch_house_rules', lambda *args: None)
    monkeypatch.setattr(review, 'fetch_pr_files', lambda *args: files)
    monkeypatch.setattr(live, '_pr_meta', lambda *args: {
        'title': 'Fix', 'body': 'Claim', 'commits': [], 'head_sha': 'head'})
    monkeypatch.setattr(live, '_fetch_file_at', lambda repo, path, sha: contents.get(path))
    monkeypatch.delenv(review.JUDGE_LIVE_ENV, raising=False)
    if entry_point == 'pr':
        result = review.run_review('o/r', 1)
    else:
        result = review.run_diff_review(files, file_loader=contents.get,
                                        pr_title='Fix', pr_body='Claim')
    assert len(compressed) == 1
    assert compressed[0][0] == files
    assert compressed[0][1] == review.DIFF_BUDGET_TOKENS
    assert isinstance(compressed[0][2], review.HeuristicEstimator)
    verifier_users = [user for tool, user in prompts if tool == 'report_verdict']
    assert len(verifier_users) == 2
    expected_diff = (
        '## File: a.py (python)\n```diff\n@@ -50 +50 @@\n-old\n+target()\n```\n\n'
        '## File: b.py (python)\n```diff\n@@ -1 +1 @@\n-old\n+other()\n```')
    assert compressed[0][3] == expected_diff
    for user in verifier_users:
        assert user.startswith('# Finding\n')
        assert user.split('\n\n# Diff under review\n')[1] == expected_diff
        assert '# File under verification' not in user
        assert '50 + target()' not in user
    reviewer_user = next(user for tool, user in prompts if tool == 'report_findings')
    assert '# Pull request' in reviewer_user
    assert '(modified, python, windowed)' in reviewer_user
    assert '⋮ lines 81-700 unchanged' in reviewer_user
    assert result.dropped == 2
    assert next(r for r in result.coverage if r['lane'] == 'llm')['files_windowed'] == 1


def test_verify_system_matches_original_prompt():
    assert review.VERIFY_SYSTEM == (
        'You are a skeptical senior engineer verifying ONE finding from a code review. '
        'Try to REFUTE it using only the diff. Decide whether the flagged code is actually wrong and '
        'whether the proposed fix is needed. If you cannot confirm the issue is real from the evidence, '
        'mark it not real; default to refuted when uncertain. Report via the report_verdict tool.')


def test_verifier_finding_first_with_only_original_fields(monkeypatch):
    prompts = []
    monkeypatch.setattr(review, 'complete_tool', lambda *args: prompts.append(args) or {
        'is_real': True, 'confidence': .9})
    finding = schema.Finding(**{**GOOD, 'end_line': 3, 'rule_id': 'known_rule'})
    diff = '## File: a.py (python)\n```diff\n@@ -1 +1 @@\n-old\n+return wrong\n```'
    review._verify(finding, diff, 'model')
    assert len(prompts) == 1
    model, system, user, tool = prompts[0]
    assert model == 'model' and system == review.VERIFY_SYSTEM and tool == review.VERDICT_TOOL
    expected_payload = {
        'file': 'a.py', 'line': 2, 'severity': 'major', 'confidence': 0.9,
        'title': 'Wrong result', 'description': 'Returns the wrong value',
        'proposed_fix': 'Return the value', 'rule_id': 'known_rule',
    }
    assert user == f"# Finding\n{json.dumps(expected_payload, indent=2)}\n\n# Diff under review\n{diff}"
    payload = user.removeprefix('# Finding\n').split('\n\n# Diff under review\n')[0]
    assert json.loads(payload) == expected_payload
    assert '# File under verification' not in user


def test_failed_and_missing_reads_are_cached():
    calls = Counter()
    def read(path):
        calls[path] += 1
        if path == 'failed':
            raise OSError('unavailable')
        return None
    cached = review._file_reader(None, None, read)
    for _ in range(2):
        with pytest.raises(OSError, match='unavailable'):
            cached('failed')
        assert cached('missing') is None
    assert calls == {'failed': 1, 'missing': 1}


@pytest.mark.parametrize('mode', ['fast', 'full'])
def test_api_passes_metadata_to_review(tmp_path, monkeypatch, mode):
    from fastapi import BackgroundTasks
    monkeypatch.setenv('ARGUS_DB_PATH', str(tmp_path / 'api.db'))
    db.init_db()
    received = []
    def run(files, repo, head_sha, model, bug_cap, fast=False, **kwargs):
        received.append((fast, kwargs))
        return review.ReviewResult(0)
    monkeypatch.setattr(main, 'run_diff_review', run)
    request = main.DiffReviewRequest(
        diff='diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x = 1',
        title='Fix', description='Claim', mode=mode, base_sha='abcdef0')
    background = BackgroundTasks()
    main.review_diff(request, background)
    for task in background.tasks:
        task.func(*task.args, **task.kwargs)
    assert received == [(mode == 'fast', {'pr_title': 'Fix', 'pr_body': 'Claim', 'base_sha': 'abcdef0'})]


@pytest.mark.parametrize("separator", ["\f", "\u2028", "\r\n"])
def test_reanchor_uses_git_line_numbers(separator):
    finding = schema.Finding(**{**GOOD, "line": 10, "evidence": "target()"})
    counts = review._reanchor_findings(
        [finding], lambda _: f"first{separator}continued\nsecond\ntarget()\n")
    assert finding.line == (4 if separator == "\r\n" else 3)
    assert counts["findings_reanchored"] == 1


@pytest.mark.parametrize("prefix", ["", "   51 - "])
def test_reanchor_preserves_removed_line_evidence(prefix):
    evidence = "raise HTTPException(status_code=403)"
    finding = schema.Finding(**{**GOOD, "line": 51, "evidence": prefix + evidence})
    counts = review._reanchor_findings(
        [finding], lambda _: "pass\n" * 119 + evidence,
        {"a.py": f"@@ -51 +51 @@\n-    {evidence}\n+    return None"})
    assert finding.line == 51
    assert counts["evidence_removed_line"] == 1
    assert counts["findings_reanchored"] == 0


def test_reanchor_follows_a_moved_line_to_its_added_copy():
    evidence = "raise HTTPException(status_code=403)"
    finding = schema.Finding(**{**GOOD, "line": 51, "evidence": evidence})
    counts = review._reanchor_findings(
        [finding], lambda _: "pass\n" * 79 + "    " + evidence,
        {"a.py": f"@@ -51 +50,0 @@\n-    {evidence}\n@@ -80,0 +80 @@\n+    {evidence}"})
    assert finding.line == 80
    assert counts["findings_reanchored"] == 1
    assert counts["evidence_removed_line"] == 0


def test_reanchor_records_unreadable_evidence():
    def read(path):
        raise OSError("unavailable")
    finding = schema.Finding(**GOOD)
    counts = review._reanchor_findings([finding], read)
    assert finding.line == 2
    assert counts["evidence_unreadable"] == 1


def test_failed_reads_fail_dependent_lanes_and_preserve_hunks(monkeypatch):
    calls = Counter()
    prompts = []
    def read(path):
        calls[path] += 1
        raise RuntimeError("unavailable")
    def complete(model, system, user, tool):
        prompts.append(user)
        return {"findings": []}
    monkeypatch.setattr(review, "complete_tool", complete)
    files = [FilePatch("a.py", "modified", "@@ -1 +1 @@\n+value = 1"),
             FilePatch("docker-compose.yml", "modified", "@@ -1 +1 @@\n+services:")]
    result = review.run_diff_review(files, file_loader=read)
    lanes = {record["lane"]: record for record in result.coverage}
    assert lanes["ruff"]["status"] == "error"
    assert lanes["config"]["status"] == "error"
    assert lanes["probe"]["status"] == "error"
    assert lanes["llm"]["files_read_failed"] == 2
    assert lanes["llm"]["files_hunks_only"] == 2
    assert "1 + value = 1" in prompts[0] and "1 + services:" in prompts[0]
    assert verdict.coverage_level(result.coverage) != "full"
    assert calls == {"a.py": 1, "docker-compose.yml": 1}


def test_oversized_line_review_saves(tmp_path):
    path = str(tmp_path / "review.db")
    db.init_db(path)
    rid = db.create_review(path, "o/r", 1, "test")
    findings, _ = schema.parse_findings({"findings": [{**GOOD, "line": 2**63, "end_line": 2**63}]})
    item = schema.VerifiedFinding(finding=findings[0], verdict=schema.Verdict(is_real=True, confidence=.9))
    db.save_result(path, rid, review.ReviewResult(1, bugs=[item], all_findings=[item]))
    assert db.get_review(path, rid)["status"] == "done"
    assert db.get_findings(path, rid)[0]["line"] == 1
    assert db.get_findings(path, rid)[0]["end_line"] is None
