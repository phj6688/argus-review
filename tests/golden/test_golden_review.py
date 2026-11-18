"""Pin full review output and incomplete file-list risk behavior."""

import base64

import pytest

from argus import review, risk_tier
from argus.diff import parse_unified_diff

from .harness import DEFAULT_CONTENTS, DEFAULT_FILES, HEAD, NOW, register_review

FINDINGS = [
    {'file': 'app.py', 'line': 2, 'end_line': 2, 'severity': 'major', 'confidence': 0.91,
     'title': 'Zero input raises an exception', 'description': 'A zero value divides by zero.',
     'proposed_fix': 'Reject zero before dividing.', 'category': 'input_handling',
     'evidence': '    return 10 / value', 'rule_id': 'input-validation'},
    {'file': 'app.py', 'line': 5, 'end_line': 5, 'severity': 'minor', 'confidence': 0.72,
     'title': 'Display returns an integer', 'description': 'The display function returns the input integer.',
     'proposed_fix': 'Convert the result to text.', 'category': 'contracts',
     'evidence': '    return str(value)', 'rule_id': None},
]
VERDICTS = [
    {'is_real': True, 'confidence': 0.98, 'reasoning': 'The changed expression divides by the unchecked value.'},
    {'is_real': False, 'confidence': 0.97, 'reasoning': 'The changed return explicitly converts the value to text.'},
]
DIFF = '\n'.join(
    f"diff --git a/{item['filename']} b/{item['filename']}\n"
    f"--- a/{item['filename']}\n+++ b/{item['filename']}\n{item['patch']}"
    for item in DEFAULT_FILES
)


def test_pr_basic(golden, transport, provider):
    register_review(transport, provider, findings=FINDINGS)
    provider.register('report_verdict', VERDICTS)
    result = review.run_review('example/app', 7, model='model-a')
    assert result.raw_count == 2 and result.dropped == 1
    assert [item.verdict.is_real for item in result.all_findings] == [True, False]
    assert not result.risk_tier['incomplete']
    assert {lane['lane'] for lane in result.coverage if lane['status'] == 'ran'} >= {'ruff', 'config', 'llm'}
    transport.assert_all_used()
    golden.check('review', 'pr-basic', result)


def _metadata(transport):
    transport.add('GET', '/repos/example/app/pulls/7', (200, {
        'title': 'Update notes', 'body': 'Refresh documentation.', 'head': {'sha': HEAD, 'ref': 'notes'},
        'user': {'login': 'example-dev'}, 'author_association': 'MEMBER',
    }, {}))
    transport.add('GET', '/repos/example/app/pulls/7/commits', (200, [{
        'sha': HEAD, 'commit': {'message': 'Update notes', 'committer': {'date': NOW.isoformat()}}},
    ], {}))


@pytest.mark.parametrize('case', ['pr-over-cap', 'pr-unsafe-filename'])
def test_pr_incomplete_file_list(golden, transport, case):
    if case == 'pr-over-cap':
        for page in range(1, 31):
            batch = [{'filename': f'notes/file-{index:04}.txt', 'status': 'modified'}
                     for index in range((page - 1) * 100, page * 100)]
            transport.add('GET', '/repos/example/app/pulls/7/files', (200, batch, {}),
                          query={'per_page': '100', 'page': str(page)})
    else:
        transport.add('GET', '/repos/example/app/pulls/7/files', (200, [
            {'filename': '../escape.py', 'status': 'modified'},
            {'filename': 'notes/readme.txt', 'status': 'modified'},
        ], {}), query={'per_page': '100', 'page': '1'})
    _metadata(transport)
    result = review.run_review('example/app', 7, model='model-a')
    assert result.risk_tier['incomplete'] is True
    assert result.risk_tier['reasons'] == ['file list incomplete, an unseen path might match']
    assert not golden.llm_calls
    transport.assert_all_used()
    golden.check('review', case, result)


def test_diff_unparseable_section(golden, transport):
    files = parse_unified_diff('diff --git unusable-section\nindex 1111111..2222222 100644\n')
    tier = risk_tier.classify_files(files)
    result = review.run_diff_review(files, model='model-a')
    assert files.unseen is True
    assert result.risk_tier == tier.to_dict()
    assert result.risk_tier['reasons'] == ['file list incomplete, an unseen path might match']
    transport.assert_all_used()
    golden.check('review', 'diff-unparseable-section', {
        'files': list(files), 'unseen': files.unseen, 'risk': tier, 'review': result,
    })


@pytest.mark.parametrize('fast', [False, True], ids=['diff-full', 'diff-fast'])
def test_diff_review(golden, transport, provider, fast):
    files = parse_unified_diff(DIFF)
    for path, content in DEFAULT_CONTENTS.items():
        transport.add('GET', f'/repos/example/app/contents/{path}', (200, {
            'content': base64.b64encode(content.encode()).decode(), 'encoding': 'base64',
        }, {}), query={'ref': HEAD})
    if not fast:
        transport.add('GET', '/repos/example/app/contents/CLAUDE.md', (404, {'message': 'Not Found'}, {}))
        provider.register('report_findings', {'findings': FINDINGS})
        provider.register('report_verdict', VERDICTS)
    result = review.run_diff_review(files, repo='example/app', head_sha=HEAD, model='model-a', fast=fast,
                                    pr_title='Guard numeric input', pr_body='Handle zero input.')
    assert files.unseen is False
    assert result.risk_tier['incomplete'] is False
    assert len(golden.llm_calls) == (0 if fast else 3)
    transport.assert_all_used()
    golden.check('diff-review', 'diff-fast' if fast else 'diff-full', result)
