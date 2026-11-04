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


def test_changed_symbols_post_image_and_removed_declarations():
    content = ('module_value = 2\nclass Loader:\n    def __init__(self):\n'
               '        self.value = 2\n    @decorate\n    def load_value(self):\n'
               '        return 2\n\ndef unchanged():\n    return 1\n')
    files = [
        FilePatch('loader.py', 'modified', '@@ -1 +1 @@\n-old\n+module_value = 2\n'
                  '@@ -4 +4 @@\n-        self.value = 1\n+        self.value = 2\n'
                  '@@ -7 +7 @@\n-        return 1\n+        return 2\n'
                  '@@ -11,2 +10,0 @@\n-def removed_loader():\n-    pass'),
        FilePatch('module.py', 'modified', '@@ -1 +1 @@\n-old\n+module_value = 2'),
        FilePatch('loader.ts', 'modified', '@@ -2 +2 @@\n-  return 1;\n+  return 2;'),
        FilePatch('broken.py', 'modified', '@@ -1 +1 @@\n-def old_loader():\n+def new_loader(:'),
        FilePatch('gone.py', 'removed', '@@ -1,2 +0,0 @@\n-def deleted_loader():\n-    pass'),
        FilePatch('dist/skip.py', 'modified', '@@ -1 +1 @@\n+def generated_loader(): pass'),
    ]
    contents = {'loader.py': content, 'module.py': 'module_value = 2\n',
                'loader.ts': 'export function load_item() {\n  return 2;\n}\n',
                'broken.py': 'def new_loader(:\n'}
    assert context.changed_symbols(files, contents.get) == {
        'loader.py': ['Loader', 'load_value', 'removed_loader'],
        'loader.ts': ['load_item'], 'broken.py': ['old_loader', 'new_loader'],
        'gone.py': ['deleted_loader'],
    }


@pytest.mark.parametrize('path,signature,name', [
    ('a.pyi', 'async def load_item():', 'load_item'),
    ('a.ts', 'export const load_item = (value) => value;', 'load_item'),
    ('a.ts', 'let load_item = function(value) {};', 'load_item'),
    ('a.ts', '  async load_item(value) {', 'load_item'),
    ('a.ts', 'interface Loadable {', 'Loadable'),
    ('a.ts', 'type Loadable = string;', 'Loadable'),
    ('a.ts', 'enum Loadable {', 'Loadable'),
    ('a.go', 'func (r Reader) load_item() {', 'load_item'),
    ('a.go', 'type Loadable struct {', 'Loadable'),
    ('a.rs', 'pub async fn load_item() {', 'load_item'),
    ('a.rs', 'pub struct Loadable {', 'Loadable'),
    ('a.bash', 'load_item() {', 'load_item'),
    ('a.sh', 'function load_item {', 'load_item'),
])
def test_changed_symbols_signature_fallback(path, signature, name):
    file = FilePatch(path, 'modified', '@@ -0,0 +1 @@\n+' + signature)
    assert context.changed_symbols([file], None) == {path: [name]}


def test_changed_symbols_ignores_common_names_and_module_lines():
    patch = '@@ -0,0 +1,6 @@\n+def go(): pass\n+def main(): pass\n+def test_load(): pass\n' \
            '+class TestLoad: pass\n+def load_item(): pass\n+value = 1'
    assert context.changed_symbols([FilePatch('a.py', 'added', patch)], None) == {
        'a.py': ['load_item']}


def pairing_fixture():
    files = [FilePatch('loader.py', 'modified', '@@ -2 +2 @@\n-    return 1\n+    return 2'),
             FilePatch('tests/test_loader.py', 'modified', '@@ -8 +8 @@\n-old\n+load_x()'),
             FilePatch('notes.md', 'modified', '@@ -1 +1 @@\n+load_xy()')]
    contents = {'loader.py': 'def load_x():\n    return 2\n\nload_x()\n',
                'tests/test_loader.py': '\n'.join(['# context'] * 7 + ['load_x()', 'load_x()']),
                'notes.md': 'load_xy()'}
    return files, contents


def test_pairing_searches_only_other_changed_files_with_exact_words():
    files, contents = pairing_fixture()
    reads = []
    def read(path):
        reads.append(path)
        return contents[path]
    result = context.pairing_block(files, read, {'loader.py': ['load_x']}, 2000,
                                   HeuristicEstimator())
    assert '## Cross-references for changed symbols' in result.text
    assert '#### tests/test_loader.py (test)' in result.text
    assert '    8 > load_x()\n    9 > load_x()' in result.text
    assert result.text.count('    7   # context') == 1
    assert '#### loader.py' not in result.text and 'notes.md' not in result.text
    assert set(reads) <= set(contents)
    assert [(e.symbol, e.defined_in, e.path, e.line, e.kind) for e in result.entries] == [
        ('load_x', 'loader.py', 'tests/test_loader.py', 8, 'test'),
        ('load_x', 'loader.py', 'tests/test_loader.py', 9, 'test')]
    assert result.references == 2


def test_pairing_budget_and_patch_line_numbers():
    files, _ = pairing_fixture()
    result = context.pairing_block(files, None, {'loader.py': ['load_x']}, 2000,
                                   HeuristicEstimator())
    assert '    8 > load_x()' in result.text
    cut = context.pairing_block(files, None, {'loader.py': ['load_x']}, 1,
                                HeuristicEstimator())
    assert cut.text == '' and cut.entries == [] and cut.truncated
    assert cut.tokens <= 1


def test_context_blocks_default_prompt_is_unchanged(monkeypatch):
    files = [FilePatch('a.py', 'modified', PATCH)]
    for name in ('PAIRING', 'CALLEES', 'CALLERS', 'COCHANGE'):
        monkeypatch.delenv('ARGUS_CONTEXT_' + name, raising=False)
    unset = render(files)
    assert unset.prompt == ('## File: a.py (modified, python, hunks)\n```\n'
                            '    2   keep\n      - old\n    3 + new\n    4   tail\n```')
    for name in ('PAIRING', 'CALLEES', 'CALLERS', 'COCHANGE'):
        monkeypatch.setenv('ARGUS_CONTEXT_' + name, '0')
    off = render(files)
    assert off == unset
    assert off.context_blocks == {} and off.pairing_entries == []
    from argus.review import _llm_file_counts
    assert 'context_blocks' not in _llm_file_counts(files, off)


@pytest.mark.parametrize('value', ['1', ' true ', 'YES', 'On'])
def test_pairing_opt_in_appends_without_changing_file_views(monkeypatch, value):
    files, contents = pairing_fixture()
    monkeypatch.delenv('ARGUS_CONTEXT_PAIRING', raising=False)
    before = render(files, contents.get)
    monkeypatch.setenv('ARGUS_CONTEXT_PAIRING', value)
    after = render(files, contents.get)
    assert after.prompt.startswith(before.prompt + '\n\n')
    assert after.views == before.views
    assert after.context_blocks['pairing'] == {
        'enabled': True, 'status': 'ran', 'reason': None, 'symbols': 1,
        'references': 2, 'files_searched': 3, 'tokens': HeuristicEstimator().estimate(
            after.prompt[len(before.prompt) + 2:]), 'budget_tokens': 12000, 'truncated': False}
    assert len(after.pairing_entries) == 2
    assert after.estimated_tokens == HeuristicEstimator().estimate(after.prompt)
    from argus.review import _llm_file_counts
    assert _llm_file_counts(files, after)['context_blocks'] == after.context_blocks
    preview = context.context_blocks_preview(files, contents.get, 120000, HeuristicEstimator())
    assert preview['pairing'].text == after.prompt[len(before.prompt) + 2:]


@pytest.mark.parametrize('error', [context.GitHubAuthError, context.GitHubDenied,
                                  context.GitHubRateLimited])
def test_changed_symbols_propagates_github_errors(error):
    def read(path):
        raise error('unavailable')
    with pytest.raises(error):
        context.changed_symbols([FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+def load_x():')], read)


def test_changed_symbols_logs_read_failure_and_falls_back(caplog):
    def read(path):
        raise OSError('unavailable')
    result = context.changed_symbols([
        FilePatch('a.py', 'modified', '@@ -1 +1 @@\n+def load_x():')], read)
    assert result == {'a.py': ['load_x']}
    assert 'unavailable' in caplog.text


@pytest.fixture
def context_mirror(tmp_path):
    import os
    import subprocess
    from argus.evalkit.replay import MirrorSource
    work, mirror = tmp_path / 'work', tmp_path / 'mirror.git'
    work.mkdir()
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    def git(*args):
        return subprocess.check_output(['git', '-C', str(work), *args], env=environment,
                                       text=True).strip()
    git('init', '-q')
    contents = {'changed.py': 'def load_data():\n    return helper_value()\n',
                'helper.py': 'def helper_value():\n    return 42\n',
                'caller.py': 'def use_data():\n    return load_data()\n',
                'peer.py': 'value = 1\n', 'deleted.py': 'value = 1\n',
                'uv.lock': 'version = 1\n'}
    for path, text in contents.items():
        (work / path).write_text(text)
    def commit(message):
        git('add', '.')
        git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test', 'commit', '-qm', message)
    commit('initial')
    (work / 'changed.py').write_text(contents['changed.py'] + '# updated\n')
    (work / 'peer.py').write_text('value = 2\n')
    (work / 'uv.lock').write_text('version = 2\n')
    commit('together')
    (work / 'deleted.py').unlink()
    commit('delete')
    head = git('rev-parse', 'HEAD')
    subprocess.run(['git', 'clone', '--bare', '-q', str(work), str(mirror)],
                   env=environment, check=True)
    source = MirrorSource(mirror, head)
    files = [FilePatch('changed.py', 'modified',
                      '@@ -2 +2 @@\n-    return 1\n+    return helper_value()')]
    return files, source


def test_mirror_blocks_use_head_definitions_callers_and_history(context_mirror, monkeypatch):
    files, source = context_mirror
    assert source.grep_word('helper_value') == [
        ('changed.py', 2, '    return helper_value()'), ('helper.py', 1, 'def helper_value():')]
    assert source.grep_word('no_such_symbol') == []
    assert source.read('missing.py') is None
    assert source.cochanged('changed.py', 10) == [
        ('peer.py', 2), ('caller.py', 1), ('helper.py', 1)]
    preview = context.context_blocks_preview(files, source.read, 120000, HeuristicEstimator(),
                                             source, pairing=False, callees=True,
                                             callers=True, cochange=True)
    assert '## Definitions of functions the changed code calls' in preview['callees'].text
    assert '#### helper.py' in preview['callees'].text
    assert '    1 > def helper_value():' in preview['callees'].text
    assert '#### caller.py' in preview['callers'].text
    assert '    2 >     return load_data()' in preview['callers'].text
    assert '- changed.py: peer.py (2), caller.py (1), helper.py (1)' in preview['cochange'].text
    for name, share in [('callees', .05), ('callers', .05), ('cochange', .01)]:
        assert preview[name].tokens <= int(120000 * share)
        monkeypatch.setenv('ARGUS_CONTEXT_' + name.upper(), 'true')
    rendered = context.render_review_context(files, source.read, 120000, source=source)
    assert [record['status'] for record in rendered.context_blocks.values()] == ['ran'] * 3
    assert list(rendered.context_blocks) == ['callees', 'callers', 'cochange']
    for name in ('CALLEES', 'CALLERS', 'COCHANGE'):
        monkeypatch.setenv('ARGUS_CONTEXT_' + name, '0')
    off = context.render_review_context(files, source.read, 120000, source=source)
    assert off.context_blocks == {} and rendered.prompt.startswith(off.prompt)


def test_mirror_settings_without_a_mirror_record_skip(monkeypatch):
    files, contents = pairing_fixture()
    for name in ('CALLEES', 'CALLERS', 'COCHANGE'):
        monkeypatch.setenv('ARGUS_CONTEXT_' + name, '1')
    result = render(files, contents.get)
    for record in result.context_blocks.values():
        assert record['status'] == 'skipped' and record['reason'] == 'no repository mirror'
        assert record['references'] == 0 and record['tokens'] == 0
    assert '## Callers' not in result.prompt and '## Definitions' not in result.prompt


class FixtureSource:
    def __init__(self, hits=(), contents=None, error=None):
        self.hits, self.contents, self.error = hits, contents or {}, error

    def grep_word(self, word):
        if self.error:
            raise self.error
        return list(self.hits)

    def read(self, path):
        if self.error:
            raise self.error
        return self.contents.get(path)

    def cochanged(self, path, limit):
        if self.error:
            raise self.error
        return [(f'peer{n}.py', 10 - n) for n in range(6)][:limit]


@pytest.mark.parametrize('name', ['callees', 'callers', 'cochange'])
def test_mirror_source_errors_leave_no_partial_block(name, caplog):
    files = [FilePatch('a.py', 'added', '@@ -0,0 +1,2 @@\n+def load_data():\n+    helper_value()')]
    result = context.context_blocks_preview(
        files, None, 120000, HeuristicEstimator(), FixtureSource(error=OSError('failed')),
        pairing=False, **{name: True})[name]
    assert result.text == '' and result.reason == 'source error' and result.references == 0
    assert 'failed' in caplog.text


@pytest.mark.parametrize('name', ['callees', 'callers', 'cochange'])
def test_mirror_blocks_stop_at_entry_boundaries(name):
    files = [FilePatch('a.py', 'added', '@@ -0,0 +1,2 @@\n+def load_data():\n+    helper_value()')]
    source = FixtureSource([('other.py', 1, 'def helper_value():'),
                            ('other.py', 2, '    load_data()')],
                           {'other.py': 'def helper_value():\n    load_data()\n'})
    result = context.context_blocks_preview(files, None, 1, HeuristicEstimator(), source,
                                             pairing=False, **{name: True})[name]
    assert result.text == '' and result.truncated and result.tokens == 0


def test_callees_skip_ambiguous_definitions_and_callers_skip_common_names():
    files = [FilePatch('a.py', 'added', '@@ -0,0 +1,2 @@\n+def load_data():\n+    helper_value()')]
    hits = [(f'helper{n}.py', 1, 'def helper_value():') for n in range(3)]
    result = context.context_blocks_preview(files, None, 120000, HeuristicEstimator(),
                                             FixtureSource(hits), pairing=False, callees=True)
    assert result['callees'].text == ''
    hits = [(f'caller{n}.py', 1, 'load_data()') for n in range(41)]
    result = context.context_blocks_preview(files, None, 120000, HeuristicEstimator(),
                                             FixtureSource(hits), pairing=False, callers=True)
    assert result['callers'].text == ''


@pytest.mark.parametrize('constructor', ['__init__', '__new__', '__post_init__'])
def test_changed_constructor_signature_names_only_the_class(constructor):
    content = f'class Loader:\n    def {constructor}(self, value):\n        pass\n'
    patch = (f'@@ -2 +2 @@\n-    def {constructor}(self):\n'
             f'+    def {constructor}(self, value):')
    assert context.changed_symbols([FilePatch('a.py', 'modified', patch)], lambda _: content) == {
        'a.py': ['Loader']}


def test_removed_constructor_uses_class_in_patch_without_reader():
    patch = ('@@ -1,3 +1 @@\n class Loader:\n-    def __init__(self):\n-        pass')
    assert context.changed_symbols([FilePatch('a.py', 'modified', patch)], None) == {
        'a.py': ['Loader']}


def test_pairing_order_limits_and_maximum_line_length():
    files = [FilePatch('small.py', 'modified', '@@ -1 +1 @@\n+def small_load(): pass'),
             FilePatch('large.py', 'modified', '@@ -1 +1,2 @@\n+def large_load():\n+    pass')]
    contents = {}
    for n, name in enumerate(['config.yaml', 'other.py', 'docs/usage.md',
                              'tests/a.py', 'tests/b.py', 'tests/c.py', 'tests/d.py', 'tests/e.py']):
        files.append(FilePatch(name, 'modified', '@@ -1 +1 @@\n+new'))
        contents[name] = '\n'.join(['large_load() small_load() ' + 'x' * 250] * 5)
    result = context.pairing_block(files, contents.get,
                                   {'small.py': ['small_load'], 'large.py': ['large_load']},
                                   120000, HeuristicEstimator())
    assert result.entries[0].symbol == 'large_load'
    for name in ('large_load', 'small_load'):
        entries = [entry for entry in result.entries if entry.symbol == name]
        assert list(dict.fromkeys(entry.path for entry in entries)) == [
            'tests/a.py', 'tests/b.py', 'tests/c.py', 'tests/d.py', 'tests/e.py', 'docs/usage.md']
        assert all(count == 3 for count in Counter(entry.path for entry in entries).values())
    assert result.truncated
    assert all(len(line.split(' > ', 1)[1]) <= 200 for line in result.text.splitlines() if ' > ' in line)


def test_pairing_cut_keeps_complete_entries():
    files, contents = pairing_fixture()
    contents['tests/test_loader.py'] = '\n'.join(
        ['# context'] * 7 + ['load_x()'] + ['# gap'] * 10 + ['load_x()'])
    full = context.pairing_block(files, contents.get, {'loader.py': ['load_x']}, 12000,
                                 HeuristicEstimator())
    cap = full.tokens - 1
    cut = context.pairing_block(files, contents.get, {'loader.py': ['load_x']}, cap,
                                HeuristicEstimator())
    assert cut.truncated and cut.tokens <= cap and cut.references == 1
    assert cut.text.endswith('\n```') and len(cut.entries) == 1


def test_preview_ignores_environment_and_render_reads_each_file_once(monkeypatch):
    files, contents = pairing_fixture()
    monkeypatch.setenv('ARGUS_CONTEXT_PAIRING', 'yes')
    reads = Counter()
    def read(path):
        reads[path] += 1
        return contents.get(path)
    render(files, read)
    assert reads == Counter({path: 1 for path in contents})
    preview = context.context_blocks_preview(files, read, 120000, HeuristicEstimator(), pairing=False)
    assert preview == {} and reads == Counter({path: 1 for path in contents})


def test_callees_keep_names_that_are_only_excluded_as_changed_symbols():
    content = 'def load_data():\n    return setup()\n'
    files = [FilePatch('a.py', 'modified', '@@ -2 +2 @@\n-    return 1\n+    return setup()')]
    source = FixtureSource([('helper.py', 1, 'def setup():')], {'helper.py': 'def setup():\n    pass\n'})
    result = context.context_blocks_preview(files, lambda _: content, 120000,
                                             HeuristicEstimator(), source, pairing=False, callees=True)
    assert 'def setup():' in result['callees'].text


def test_cochange_symbol_counts_do_not_depend_on_other_blocks(context_mirror):
    files, source = context_mirror
    alone = context.context_blocks_preview(files, source.read, 120000, HeuristicEstimator(),
                                           source, pairing=False, cochange=True)
    together = context.context_blocks_preview(files, source.read, 120000, HeuristicEstimator(),
                                              source, pairing=False, cochange=True, callers=True)
    assert alone['cochange'].symbols == together['cochange'].symbols == 1


def test_constructor_class_context_does_not_leak_across_hunks():
    import difflib
    before = ('class Alpha:\n    def first_method(self):\n        return 1\n'
              + '\n' * 10 + 'class Beta:\n    flag = True\n'
              '    def __init__(self):\n        pass\n')
    after = before.replace('first_method(self)', 'first_method(self, value)').replace(
        '__init__(self)', '__init__(self, value)')
    patch = ''.join(list(difflib.unified_diff(before.splitlines(keepends=True),
                                             after.splitlines(keepends=True), n=1))[2:])
    assert context.changed_symbols([FilePatch('a.py', 'modified', patch)], lambda _: after) == {
        'a.py': ['first_method', 'Beta']}


@pytest.mark.parametrize('entrypoint', ['pr', 'diff'])
def test_review_source_hook_is_only_called_for_mirror_settings(monkeypatch, entrypoint):
    from types import SimpleNamespace
    from argus import review
    from argus.jarvis import live
    files, contents = pairing_fixture()
    calls, prompts = [], []
    source = FixtureSource([('caller.py', 2, '    load_x()')],
                           {'caller.py': 'def call_loader():\n    load_x()\n'})
    def context_source(repo, head_sha):
        calls.append((repo, head_sha))
        return source
    monkeypatch.setattr(live, 'context_source', context_source)
    monkeypatch.setattr(live, '_pr_meta', lambda *args: {'head_sha': 'head'})
    monkeypatch.setattr(review, 'fetch_pr_files', lambda *args: files)
    monkeypatch.setattr(review, '_file_reader', lambda *args: contents.get)
    monkeypatch.setattr(review, 'fetch_house_rules', lambda *args: None)
    monkeypatch.setattr(review, '_intent_for_prompt', lambda *args: None)
    monkeypatch.setattr(review, '_probe_lane', lambda *args: ([], [], {}))
    monkeypatch.setattr(review, '_ruff_lane', lambda *args: ([], {}))
    monkeypatch.setattr(review, '_config_lane', lambda *args: ([], {}))
    monkeypatch.setattr(live, 'probe_scan_for_files', lambda *args: SimpleNamespace(findings=[], coverage={}))
    monkeypatch.setattr(review, '_suppress_known_fps', lambda repo, pr, findings: findings)
    def complete(model, system, user, tool):
        prompts.append(user)
        return {'findings': []}
    monkeypatch.setattr(review, 'complete_tool', complete)
    for name in ('PAIRING', 'CALLEES', 'CALLERS', 'COCHANGE'):
        monkeypatch.delenv('ARGUS_CONTEXT_' + name, raising=False)
    def run(repo='owner/project', head='head'):
        if entrypoint == 'pr':
            return review.run_review(repo, 1)
        return review.run_diff_review(files, repo=repo, head_sha=head)
    off = run()
    assert calls == []
    assert 'context_blocks' not in next(r for r in off.coverage if r.get('lane') == 'llm')
    monkeypatch.setenv('ARGUS_CONTEXT_PAIRING', '1')
    run()
    assert calls == []
    monkeypatch.setenv('ARGUS_CONTEXT_CALLERS', '1')
    on = run()
    assert calls == [('owner/project', 'head')]
    assert '#### caller.py' in prompts[-1]
    assert next(r for r in on.coverage if r.get('lane') == 'llm')['context_blocks']['callers']['status'] == 'ran'
    if entrypoint == 'diff':
        run(None, 'head')
        run('owner/project', None)
        assert calls == [('owner/project', 'head')]


@pytest.mark.parametrize('name', ['callees', 'callers', 'cochange'])
@pytest.mark.parametrize('error', [context.GitHubAuthError, context.GitHubDenied,
                                  context.GitHubRateLimited])
def test_mirror_blocks_propagate_github_errors(name, error):
    files = [FilePatch('a.py', 'added', '@@ -0,0 +1,2 @@\n+def load_data():\n+    helper_value()')]
    with pytest.raises(error):
        context.context_blocks_preview(files, None, 120000, HeuristicEstimator(),
                                       FixtureSource(error=error('unavailable')),
                                       pairing=False, **{name: True})


@pytest.mark.parametrize('name', ['match', 'func', 'super'])
def test_changed_symbols_keeps_valid_names_outside_the_explicit_skip_list(name):
    file = FilePatch('a.py', 'added', f'@@ -0,0 +1 @@\n+def {name}(): pass')
    assert context.changed_symbols([file], None) == {'a.py': [name]}


@pytest.mark.parametrize('path,content,patch,definition', [
    ('a.py', 'def load_data():\n    return super()\n',
     '@@ -2 +2 @@\n-    return 1\n+    return super()', ('helper.py', 'def super():')),
    ('a.ts', 'function load_data() {\n    return service.match(value);\n}\n',
     '@@ -2 +2 @@\n-    return 1;\n+    return service.match(value);',
     ('helper.ts', 'export function match(value) {')),
])
def test_callees_exclude_keywords_of_the_actual_language(path, content, patch, definition):
    helper_path, signature = definition
    source = FixtureSource([(helper_path, 1, signature)], {helper_path: signature + '\n'})
    result = context.context_blocks_preview([FilePatch(path, 'modified', patch)], lambda _: content,
                                             120000, HeuristicEstimator(), source,
                                             pairing=False, callees=True)
    assert signature in result['callees'].text


def test_changed_symbols_survives_unparsable_python_source():
    patch = '@@ -1 +1,2 @@\n a\n+def changed_name():\n'
    file = FilePatch('nul.py', 'modified', patch)

    # Python 3.11 raises ValueError for a NUL byte, where later versions raise SyntaxError.
    assert context.changed_symbols([file], lambda _: 'a\x00b\n') == {'nul.py': ['changed_name']}
