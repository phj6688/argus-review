from collections import Counter

import pytest

from argus import context
from argus.diff import FilePatch
from argus.tokens import HeuristicEstimator


PATCH = '@@ -2,3 +2,3 @@\n keep\n-old\n+new\n tail'


def render(files, read=None, budget=120_000):
    return context.render_review_context(files, read, budget, HeuristicEstimator())


@pytest.mark.parametrize('read,mode', [(None, 'hunks'), (lambda _: None, 'hunks'),
                                      (lambda _: 'first\nkeep\nnew\ntail\n', 'full')])
def test_numbering_and_removals(read, mode):
    result = render([FilePatch('a.py', 'modified', PATCH)], read)
    assert f'## File: a.py (modified, python, {mode})' in result.prompt
    assert '    2   keep\n      - old\n    3 + new\n    4   tail' in result.prompt
    assert (result.full if mode == 'full' else result.hunks_only) == ['a.py']
    assert result.estimated_tokens == HeuristicEstimator().estimate(result.prompt)


def test_patch_lines_counts_blank_context_lines():
    patch = '@@ -1,3 +1,3 @@\n first\n\n+third'
    assert list(context._patch_lines(patch)) == [
        (1, ' ', 'first'),
        (2, ' ', ''),
        (3, '+', 'third'),
    ]


@pytest.mark.parametrize('patch,content,expected', [
    ('@@ -0,0 +1 @@\n+new', 'new', '    1 + new'),
    ('@@ -1,2 +1 @@\n first\n-last', 'first', '    1   first\n      - last'),
    ('@@ -1 +0,0 @@\n-last', '', '      - last'),
    ('@@ -4 +3,0 @@\n-old', 'one\ntwo\nthree\nfour',
     '    3   three\n      - old\n    4   four'),
])
def test_removals_and_insertions_at_boundaries(patch, content, expected):
    result = render([FilePatch('a.py', 'modified', patch)], lambda _: content)
    assert expected in result.prompt


def test_window_gaps_outlines_and_merged_windows():
    lines = [f'value_{n} = {n}' for n in range(1, 701)]
    lines[199] = 'async def middle():'
    lines[399] = 'export function helper() {'
    patch = '@@ -50 +50 @@\n-old\n+value_50 = 50\n@@ -65 +65 @@\n-old\n+value_65 = 65'
    result = render([FilePatch('a.ts', 'modified', patch)], lambda _: '\n'.join(lines))
    assert result.windowed == ['a.ts']
    assert '⋮ lines 1-19 unchanged' in result.prompt
    assert '⋮ lines 96-700 unchanged' in result.prompt
    assert '  200   async def middle():' in result.prompt
    assert '  400   export function helper() {' in result.prompt
    assert '   50 + value_50 = 50' in result.prompt
    assert result.prompt.count('   60   value_60 = 60') == 1
    assert 'value_150' not in result.prompt


@pytest.mark.parametrize('name', [
    'deps.lock', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'go.sum',
    'Cargo.lock', 'poetry.lock', 'uv.lock', 'app.min.js', 'app.min.css', 'app.js.map',
    *[f'a/{part}/file.py' for part in ('node_modules', 'dist', 'build', 'vendor',
                                    '.next', '__pycache__', '__snapshots__')],
])
def test_generated_files_never_read_full_content(name):
    def read(_):
        pytest.fail('generated content must not be read')
    result = render([FilePatch(name, 'modified', PATCH)], read)
    assert result.hunks_only == [name]


def test_degrades_without_dropping_a_smaller_form():
    content = '\n'.join('x = ' + '1' * 100 for _ in range(500))
    files = [FilePatch('a.py', 'modified', '@@ -250 +250 @@\n-x\n+x')]
    assert render(files, lambda _: content).full == ['a.py']
    assert render(files, lambda _: content, 4000).windowed == ['a.py']
    assert render(files, lambda _: content, 100).hunks_only == ['a.py']
    assert render(files, lambda _: content, 1).overflow == ['a.py']


def test_ranking_and_partition():
    files = [FilePatch('dist/a.py', 'modified', PATCH),
             FilePatch('README.md', 'modified', PATCH),
             FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+x'),
             FilePatch('b.py', 'modified', PATCH),
             FilePatch('gone', 'removed', PATCH), FilePatch('binary', 'added', None)]
    result = render(files)
    assert result.hunks_only == ['b.py', 'a.py', 'README.md', 'dist/a.py']
    assert result.deleted == ['gone']
    assert Counter(result.full + result.windowed + result.hunks_only + result.overflow) == \
        Counter(['a.py', 'b.py', 'README.md', 'dist/a.py'])


def test_failed_read_falls_back_to_hunks():
    def read(_):
        raise OSError('unavailable')
    assert render([FilePatch('a.py', 'modified', PATCH)], read).hunks_only == ['a.py']


@pytest.mark.parametrize('count,mode', [(600, 'full'), (601, 'windowed')])
def test_full_file_line_limit(count, mode):
    file = FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+pass')
    result = render([file], lambda _: 'pass\n' * count)
    assert getattr(result, mode) == ['a.py']


def test_deletion_only_window_contains_the_removed_line():
    file = FilePatch('a.py', 'modified', '@@ -350 +349,0 @@\n-deleted')
    result = render([file], lambda _: 'pass\n' * 700)
    assert result.windowed == ['a.py']
    assert '  349   pass\n      - deleted\n  350   pass' in result.prompt


def test_later_file_can_fit_after_an_overflow():
    files = [FilePatch('large.py', 'modified', '@@ -1 +1 @@\n+' + 'x' * 2000),
             FilePatch('small.py', 'modified', '@@ -1 +1 @@\n+x')]
    result = render(files, budget=100)
    assert result.overflow == ['large.py']
    assert result.hunks_only == ['small.py']
    assert result.prompt.endswith('## Other modified files (over budget, names only):\nlarge.py')


def test_mixed_views_are_mutually_exclusive():
    files = [FilePatch(name, 'modified', '@@ -1 +1 @@\n+x')
             for name in ('full.py', 'window.py', 'hunks.py', 'overflow.py')]
    files[-1] = FilePatch('overflow.py', 'modified', '@@ -1 +1 @@\n+' + 'x' * 10000)
    contents = {'full.py': 'x', 'window.py': 'x\n' * 601}
    result = render(files, contents.get, budget=300)
    assert result.full == ['full.py']
    assert result.windowed == ['window.py']
    assert result.hunks_only == ['hunks.py']
    assert result.overflow == ['overflow.py']
    assert Counter(result.full + result.windowed + result.hunks_only + result.overflow) == \
        Counter(f.filename for f in files)


@pytest.mark.parametrize("separator", ["\f", "\u2028", "\v", "\x1c", "\x85", "\r"])
@pytest.mark.parametrize("read_full", [False, True])
def test_numbering_only_splits_newlines(separator, read_full):
    content = f"first{separator}still first\nsecond\nthird\n"
    patch = f"@@ -1,2 +1,3 @@\n first{separator}still first\n+second\n third"
    result = render([FilePatch("a.py", "modified", patch)],
                    (lambda _: content) if read_full else None)
    assert f"    1   first{separator}still first\n    2 + second\n    3   third" in result.prompt


def test_crlf_content_numbering():
    result = render([FilePatch("a.py", "added", "@@ -0,0 +1,2 @@\r\n+first\r\n+second\r\n")],
                    lambda _: "first\r\nsecond\r\n")
    assert "    1 + first\n    2 + second\n" in result.prompt
    assert "\r" not in result.prompt


def test_overflow_files_are_not_read():
    files = [FilePatch(f"{i}.py", "modified", PATCH) for i in range(20)]
    baseline = render(files[:1], lambda _: "first\nkeep\nnew\ntail\n")
    reads = []
    result = render(files, lambda path: reads.append(path) or "first\nkeep\nnew\ntail\n",
                    budget=baseline.estimated_tokens)
    assert reads == ["0.py"]
    assert result.views == baseline.views
    assert result.prompt.startswith(baseline.prompt)
    assert result.overflow == [f"{i}.py" for i in range(1, 20)]
