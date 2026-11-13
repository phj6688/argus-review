"""Numbered post-image views, reserving patch coverage before richer context."""

import ast
import keyword
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

from .diff import REMOVED_STATUS, FilePatch, added_lines, split_lines
from .forge.errors import ForgeAuthError, ForgeDenied, ForgeRateLimited
from .tokens import HeuristicEstimator, TokenEstimator

FULL_FILE_MAX_LINES = 600
WINDOW_LINES = 30
REVIEW_CONTEXT_BUDGET_TOKENS = 120_000
# Avoid fetching more files when less than a small context window remains.
MIN_UPGRADE_TOKENS = 200
PAIRING_BUDGET_SHARE = 0.10
CALLEES_BUDGET_SHARE = 0.05
CALLERS_BUDGET_SHARE = 0.05
COCHANGE_BUDGET_SHARE = 0.01
_BLOCK_SHARES = {'pairing': PAIRING_BUDGET_SHARE, 'callees': CALLEES_BUDGET_SHARE,
                 'callers': CALLERS_BUDGET_SHARE, 'cochange': COCHANGE_BUDGET_SHARE}

FileReader = Callable[[str], str | None]
_HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
_SIGNATURE = re.compile(r'^\s*(?:async\s+)?(?:def|class|function|export)\b')
_GENERATED_DIRS = {'node_modules', 'dist', 'build', 'vendor', '.next',
                   '__pycache__', '__snapshots__'}
_LOCKFILES = {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'go.sum',
              'Cargo.lock', 'poetry.lock', 'uv.lock'}
_CONFIG_SUFFIXES = {'.md', '.rst', '.txt', '.adoc', '.yaml', '.yml', '.toml',
                    '.json', '.ini', '.cfg', '.conf', '.xml', '.env', '.properties'}
log = logging.getLogger('argus.context')


class RepoSource(Protocol):
    def grep_word(self, word: str) -> list[tuple[str, int, str]]: ...
    def read(self, path: str) -> str | None: ...
    def cochanged(self, path: str, limit: int) -> list[tuple[str, int]]: ...


@dataclass
class PairingEntry:
    symbol: str
    defined_in: str
    path: str
    line: int
    kind: str


@dataclass
class ContextBlockResult:
    text: str = ''
    symbols: int = 0
    references: int = 0
    files_searched: int = 0
    truncated: bool = False
    tokens: int = 0
    reason: str | None = None


@dataclass
class PairingResult(ContextBlockResult):
    entries: list[PairingEntry] = field(default_factory=list)


@dataclass
class RenderedContext:
    prompt: str = ''
    full: list[str] = field(default_factory=list)
    windowed: list[str] = field(default_factory=list)
    hunks_only: list[str] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    files_read_failed: int = 0
    # Keep accepted file views so coverage counts only files shown to the reviewer.
    views: dict[str, str] = field(default_factory=dict)
    context_blocks: dict[str, dict] = field(default_factory=dict)
    pairing_entries: list[PairingEntry] = field(default_factory=list)
    patchless: list[str] = field(default_factory=list)


def cached_reader(read: FileReader | None) -> FileReader | None:
    if read is None:
        return None
    contents: dict[str, str | None] = {}
    failures: dict[str, Exception] = {}

    def get(path: str) -> str | None:
        if path in failures:
            raise failures[path]
        if path not in contents:
            try:
                contents[path] = read(path)
            except Exception as error:
                failures[path] = error
                raise
        return contents[path]

    return get


def _generated(path: str) -> bool:
    name = PurePosixPath(path)
    return (bool(set(name.parts) & _GENERATED_DIRS) or name.name in _LOCKFILES
            or path.endswith(('.lock', '.min.js', '.min.css', '.map')))


def _rank(file: FilePatch) -> tuple[int, int]:
    path = PurePosixPath(file.filename)
    kind = 2 if _generated(file.filename) else int(
        path.suffix.lower() in _CONFIG_SUFFIXES or path.name.startswith(('Dockerfile', '.env')))
    changes = sum(line.startswith(('+', '-')) for line in split_lines(file.patch or ''))
    return kind, -changes


def _patch_lines(patch: str):
    number = None
    for line in split_lines(patch):
        match = _HUNK.match(line)
        if match:
            # A zero-length post-image range names the line before a deletion.
            number = int(match[1]) + (match[2] == '0')
        elif number is not None and (not line or line[:1] in ('+', '-', ' ')):
            # Some diff producers strip the context marker from blank lines.
            marker, code = (' ', '') if not line else (line[0], line[1:])
            yield number, marker, code
            if marker != '-':
                number += 1


def _line(number: int, marker: str, code: str) -> str:
    column = '' if marker == '-' else str(number)
    return f'{column:>5} {marker} {code}'


def _block(file: FilePatch, mode: str, lines: list[str]) -> str:
    return (f'## File: {file.filename} ({file.status}, {file.language}, {mode})\n'
            '```\n' + '\n'.join(lines) + '\n```')


def _file_views(file: FilePatch, read: FileReader | None, result: RenderedContext):
    patch = list(_patch_lines(file.patch or ''))
    content = None
    if read is not None and not _generated(file.filename):
        try:
            content = read(file.filename)
        except (ForgeRateLimited, ForgeAuthError, ForgeDenied):
            raise
        except Exception:
            result.files_read_failed += 1
            log.exception('file read failed for %s; using patch context', file.filename)
    if content is not None:
        lines = split_lines(content)
        added = added_lines(file.patch)
        removed: dict[int, list[str]] = {}
        for number, marker, code in patch:
            if marker == '-':
                removed.setdefault(number, []).append(code)

        def span(start: int, end: int) -> list[str]:
            result = []
            for number in range(start, end + 1):
                result.extend(_line(number, '-', code) for code in removed.get(number, []))
                if number <= len(lines):
                    result.append(_line(number, '+' if number in added else ' ', lines[number - 1]))
            return result

        if len(lines) <= FULL_FILE_MAX_LINES:
            yield 'full', _block(file, 'full', span(1, len(lines) + 1))
        windows: list[tuple[int, int]] = []
        for number in sorted(added | removed.keys()):
            start, end = max(1, number - WINDOW_LINES), min(len(lines) + 1, number + WINDOW_LINES)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        if windows:
            rendered: list[str] = []

            def gap(start: int, end: int) -> None:
                end = min(end, len(lines))
                if start <= end:
                    rendered.append(f'      ⋮ lines {start}-{end} unchanged')
                    rendered.extend(_line(n, ' ', lines[n - 1]) for n in range(start, end + 1)
                                    if _SIGNATURE.match(lines[n - 1]))

            previous = 0
            for start, end in windows:
                gap(previous + 1, start - 1)
                rendered.extend(span(start, end))
                previous = end
            gap(previous + 1, len(lines))
            yield 'windowed', _block(file, 'windowed', rendered)
    yield 'hunks_only', _block(file, 'hunks', [_line(*line) for line in patch])


_PYTHON_SUFFIXES = {'.py', '.pyi'}
_JS_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
_PYTHON_DECL = re.compile(r'^\s*(?:async\s+)?(?:def|class)\s+(\w+)')
_JS_DECL = re.compile(
    r'^\s*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:function\s*\*?\s*|class\s+|interface\s+|enum\s+)(\w+)'
    r'|(?:export\s+)?type\s+(\w+)\s*='
    r'|(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*'
    r'(?:async\s+)?(?:function\b|(?:\([^\n]*\)|\w+)\s*(?::[^=\n]+)?=>))'
    r'|^\s+(?:(?:public|private|protected|static|async|get|set|readonly)\s+)*'
    r'(\w+)\s*\([^\n]*\)\s*(?::[^\n{]+)?\{')
_GO_DECL = re.compile(r'^\s*(?:func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(|type\s+(\w+)\s)')
_RUST_DECL = re.compile(
    r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const)\s+)*'
    r'(?:fn|struct|enum|trait)\s+(\w+)')
_SHELL_DECL = re.compile(r'^\s*(?:function\s+(\w+)\b|(\w+)\s*\(\s*\))')
_DECLARATIONS = {**dict.fromkeys(_PYTHON_SUFFIXES, _PYTHON_DECL),
                 **dict.fromkeys(_JS_SUFFIXES, _JS_DECL), '.go': _GO_DECL,
                 '.rs': _RUST_DECL, '.sh': _SHELL_DECL, '.bash': _SHELL_DECL}
_COMMON_NAMES = {'main', 'init', 'run', 'test', 'setup', 'teardown', 'get', 'set', 'new', 'self'}
_JS_METHOD_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'function'}
_JS_KEYWORDS = set('if for while switch catch return function class interface type enum '
                   'new super typeof instanceof delete void throw try finally with '
                   'do else case default import export extends implements yield await'.split())
_GO_KEYWORDS = set('break default func interface select case defer go map struct chan else '
                   'goto package switch const fallthrough if range type continue for import '
                   'return var'.split())
_RUST_KEYWORDS = set('as async await break const continue crate dyn else enum extern false fn '
                     'for if impl in let loop match mod move mut pub ref return self Self '
                     'static struct super trait true type unsafe use where while'.split())
_SHELL_KEYWORDS = set('if then else elif fi case esac for select while until do done in '
                      'function time coproc'.split())
_CALL_KEYWORDS = {**dict.fromkeys(_PYTHON_SUFFIXES, set(keyword.kwlist)),
                  **dict.fromkeys(_JS_SUFFIXES, _JS_KEYWORDS), '.go': _GO_KEYWORDS,
                  '.rs': _RUST_KEYWORDS, '.sh': _SHELL_KEYWORDS, '.bash': _SHELL_KEYWORDS}
_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CONSTRUCTORS = {'__init__', '__new__', '__post_init__'}
_PAIRING_HEADER = ('## Cross-references for changed symbols\n'
                   'These lines name a function or class that this pull request changes. '
                   'They come from other changed files. Check that each one still agrees with the change.')


def context_setting(name: str) -> bool:
    return os.environ.get('ARGUS_CONTEXT_' + name.upper(), '').strip().lower() in {
        '1', 'true', 'yes', 'on'}


def mirror_context_enabled() -> bool:
    return any(context_setting(name) for name in ('callees', 'callers', 'cochange'))


def _declaration(path: str, line: str) -> str | None:
    regex = _DECLARATIONS.get(PurePosixPath(path).suffix.lower())
    match = regex.match(line) if regex else None
    if match is None or (regex is _JS_DECL and match[4] in _JS_METHOD_KEYWORDS):
        return None
    return next(value for value in match.groups() if value)


def _useful_name(name: str) -> bool:
    return (len(name) >= 3 and name not in _COMMON_NAMES
            and not name.startswith(('test_', 'Test')))


def _read_context(read: FileReader | None, path: str) -> str | None:
    try:
        return read(path) if read is not None else None
    except (ForgeRateLimited, ForgeAuthError, ForgeDenied):
        raise
    except Exception:
        log.exception('context read failed for %s; using patch context', path)
        return None


def _changed_lines(file: FilePatch) -> set[int]:
    return added_lines(file.patch) | {
        number for number, marker, _ in _patch_lines(file.patch or '') if marker == '-'}


def _python_definitions(content: str):
    definitions = []

    def visit(node, parents):
        if isinstance(node, _DEFINITION_NODES):
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            definitions.append((node, parents, start, node.end_lineno))
            parents = [*parents, node]
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(ast.parse(content), [])
    return definitions


def _python_owner(definitions, number):
    owners = [item for item in definitions if item[2] <= number <= item[3]]
    return max(owners, key=lambda item: len(item[1]), default=None)


def _python_symbol(node, parents) -> str:
    if node.name in _CONSTRUCTORS:
        enclosing = next((p for p in reversed(parents) if isinstance(p, ast.ClassDef)), None)
        if enclosing is not None:
            return enclosing.name
    return node.name


def _post_image_symbols(file: FilePatch, content: str) -> list[str] | None:
    changed = sorted(_changed_lines(file))
    if PurePosixPath(file.filename).suffix.lower() in _PYTHON_SUFFIXES:
        try:
            definitions = _python_definitions(content)
        except (SyntaxError, ValueError, RecursionError):
            return None
        return [_python_symbol(owner[0], owner[1]) for number in changed
                if (owner := _python_owner(definitions, number))]
    declarations = [(number, name) for number, line in enumerate(split_lines(content), 1)
                    if (name := _declaration(file.filename, line))]
    names = []
    for number in changed:
        owner = next((name for start, name in reversed(declarations) if start <= number), None)
        if owner:
            names.append(owner)
    return names


def _patch_symbol_names(file, markers, content):
    definitions = []
    python = PurePosixPath(file.filename).suffix.lower() in _PYTHON_SUFFIXES
    if python and content is not None:
        try:
            definitions = _python_definitions(content)
        except (SyntaxError, ValueError, RecursionError):
            pass
    for hunk in re.split(r'(?m)(?=^@@ )', file.patch):
        classes = []
        for number, marker, code in _patch_lines(hunk):
            indent = len(code) - len(code.lstrip())
            if python and code.strip() and not code.lstrip().startswith(('#', '@')):
                classes = [(level, name) for level, name in classes if level < indent]
            name = _declaration(file.filename, code)
            if python and re.match(r'^\s*class\s', code) and name:
                classes.append((indent, name))
            if marker not in markers or not name:
                continue
            if python and name in _CONSTRUCTORS:
                enclosing = [item for item in definitions if isinstance(item[0], ast.ClassDef)
                             and item[2] <= number <= item[3]]
                owner = max(enclosing, key=lambda item: len(item[1]), default=None)
                name = owner[0].name if owner else (classes[-1][1] if classes else None)
            if name:
                yield name


def changed_symbols(files: list[FilePatch], read: FileReader | None) -> dict[str, list[str]]:
    result = {}
    for file in files:
        if not file.patch or _generated(file.filename):
            continue
        names = content = None
        if file.status != REMOVED_STATUS:
            content = _read_context(read, file.filename)
            if content is not None:
                names = _post_image_symbols(file, content)
        markers = {'-'} if names is not None or file.status == REMOVED_STATUS else {'+', '-'}
        names = names or []
        names.extend(_patch_symbol_names(file, markers, content))
        unique = list(dict.fromkeys(name for name in names if _useful_name(name)))
        if unique:
            result[file.filename] = unique
    return result


def _post_lines(file: FilePatch, read: FileReader | None) -> dict[int, str]:
    content = _read_context(read, file.filename)
    if content is not None:
        return dict(enumerate(split_lines(content), 1))
    return {n: code for n, marker, code in _patch_lines(file.patch or '') if marker != '-'}


def _reference_kind(path: str) -> str:
    name = PurePosixPath(path)
    if (set(name.parts) & {'test', 'tests', '__tests__', 'spec'}
            or name.name.startswith('test_') or '.test.' in name.name or '.spec.' in name.name):
        return 'test'
    if (name.suffix.lower() in {'.md', '.rst', '.adoc', '.txt'}
            or name.stem.upper() in {'README', 'CHANGELOG'} or 'docs' in name.parts):
        return 'doc'
    return 'code' if name.suffix.lower() in _DECLARATIONS else 'config'


def _numbered_context(lines: dict[int, str], matches: list[int], before=2, after=2) -> str:
    shown = sorted({n for match in matches for n in range(max(1, match - before), match + after + 1)
                    if n in lines})
    return '\n'.join(_line(n, '>' if n in matches else ' ', lines[n][:200]) for n in shown)


def _pairing_text(entries: list[PairingEntry], contents: dict[str, dict[int, str]]) -> str:
    if not entries:
        return ''
    blocks = [_PAIRING_HEADER]
    previous_symbol = previous_file = None
    groups: dict[tuple[str, str, str, str], list[int]] = {}
    for entry in entries:
        groups.setdefault((entry.symbol, entry.defined_in, entry.path, entry.kind), []).append(entry.line)
    for (symbol, defined_in, path, kind), matches in groups.items():
        if (symbol, defined_in) != previous_symbol:
            blocks.append(f'### `{symbol}` (changed in {defined_in})')
            previous_symbol = symbol, defined_in
            previous_file = None
        if path != previous_file:
            blocks.append(f'#### {path} ({kind})\n```\n'
                          + _numbered_context(contents[path], matches) + '\n```')
            previous_file = path
    return '\n\n'.join(blocks)


def pairing_block(
    files: list[FilePatch], read: FileReader | None, symbols: dict[str, list[str]],
    max_tokens: int, estimator: TokenEstimator,
) -> PairingResult:
    contents = {f.filename: _post_lines(f, read) for f in files
                if f.status != REMOVED_STATUS and not _generated(f.filename)}
    result = PairingResult(symbols=sum(map(len, symbols.values())), files_searched=len(contents))
    kinds = {'test': 0, 'doc': 1, 'code': 2, 'config': 3}
    paths = sorted(contents, key=lambda path: (kinds[_reference_kind(path)], path))
    for file in sorted(files, key=_rank):
        defined_in = file.filename
        names = symbols.get(defined_in, [])
        for symbol in names:
            pattern = re.compile(r'(?<![\w])' + re.escape(symbol) + r'(?![\w])')
            matched_files = 0
            for path in paths:
                if path == defined_in:
                    continue
                matches = sorted(n for n, line in contents[path].items() if pattern.search(line))
                if not matches:
                    continue
                matched_files += 1
                if matched_files > 6:
                    result.truncated = True
                    continue
                result.truncated |= len(matches) > 3
                for number in matches[:3]:
                    entry = PairingEntry(symbol, defined_in, path, number, _reference_kind(path))
                    text = _pairing_text([*result.entries, entry], contents)
                    cost = estimator.estimate(text)
                    if cost > max_tokens:
                        result.truncated = True
                        return result
                    result.entries.append(entry)
                    result.text, result.tokens = text, cost
                    result.references += 1
    return result


_CALL = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
_CALLEES_HEADER = '## Definitions of functions the changed code calls'
_CALLERS_HEADER = '## Callers of changed symbols in unchanged files'
_COCHANGE_HEADER = ('## Files that often change together with the changed files\n'
                    'These files are not in this pull request and may need the same change.')


def _called_names(files, read, symbols):
    names = []
    changed_names = {name for values in symbols.values() for name in values}
    for file in sorted(files, key=_rank):
        if file.filename not in symbols or file.status == REMOVED_STATUS:
            continue
        content = _read_context(read, file.filename)
        definitions = None
        if content is not None and PurePosixPath(file.filename).suffix.lower() in _PYTHON_SUFFIXES:
            try:
                definitions = _python_definitions(content)
            except (SyntaxError, ValueError, RecursionError):
                pass
        if definitions is not None:
            changed = _changed_lines(file)
            functions = {id(owner[0]) for number in changed
                         if (owner := _python_owner(definitions, number))
                         and isinstance(owner[0], (ast.FunctionDef, ast.AsyncFunctionDef))}
            calls = []
            for node, _, _, _ in definitions:
                if id(node) not in functions:
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    owner = _python_owner(definitions, call.lineno)
                    if owner is None or id(owner[0]) != id(node):
                        continue
                    if not any(call.lineno <= n <= call.end_lineno for n in changed):
                        continue
                    if isinstance(call.func, ast.Name):
                        calls.append((call.lineno, call.col_offset, call.func.id))
                    elif isinstance(call.func, ast.Attribute):
                        calls.append((call.lineno, call.col_offset, call.func.attr))
            names.extend((name, file.filename) for _, _, name in sorted(calls))
        else:
            names.extend((match[1], file.filename) for _, marker, code in _patch_lines(file.patch or '')
                         if marker == '+' for match in _CALL.finditer(code))
    return list(dict.fromkeys(name for name, path in names
                              if len(name) >= 3 and name not in _CALL_KEYWORDS.get(
                                  PurePosixPath(path).suffix.lower(), set())
                              and name not in changed_names))


def _source_snippet(source, path, number, before, after):
    content = source.read(path)
    if content is None:
        return None
    lines = dict(enumerate(split_lines(content), 1))
    if number not in lines:
        return None
    return _numbered_context(lines, [number], before, after)


def _pack_context_entries(header, entries, result, max_tokens, estimator):
    blocks = []
    for text, references in entries:
        candidate = '\n\n'.join([header, *blocks, text])
        cost = estimator.estimate(candidate)
        if cost > max_tokens:
            result.truncated = True
            break
        blocks.append(text)
        result.text, result.tokens = candidate, cost
        result.references += references
    return result


def _callee_entries(names, files, source, result):
    changed = {file.filename for file in files}
    searched = set()
    for name in names:
        definitions = sorted({(path, number) for path, number, text in source.grep_word(name)
                              if path not in changed and not _generated(path)
                              and _declaration(path, text) == name})
        searched.update(path for path, _ in definitions)
        result.files_searched = len(searched)
        if len(definitions) > 2:
            continue
        for path, number in definitions:
            snippet = _source_snippet(source, path, number, 0, 11)
            if snippet is not None:
                yield f'### `{name}`\n#### {path}\n```\n{snippet}\n```', 1


def callee_block(files, read, symbols, source, max_tokens, estimator):
    names = _called_names(files, read, symbols)
    result = ContextBlockResult(symbols=len(names))
    return _pack_context_entries(_CALLEES_HEADER, _callee_entries(names, files, source, result),
                                 result, max_tokens, estimator)


def _caller_entries(symbols, files, source, result):
    changed = {file.filename for file in files}
    searched = set()
    for name in dict.fromkeys(name for names in symbols.values() for name in names):
        hits = source.grep_word(name)
        searched.update(path for path, _, _ in hits)
        result.files_searched = len(searched)
        if len(hits) > 40:
            continue
        pattern = re.compile(r'\b' + re.escape(name) + r'\s*\(')
        callers = sorted({(path, number) for path, number, text in hits
                          if path not in changed and not _generated(path)
                          and PurePosixPath(path).suffix.lower() in _DECLARATIONS
                          and pattern.search(text) and not _declaration(path, text)})
        result.truncated |= len(callers) > 5
        for path, number in callers[:5]:
            snippet = _source_snippet(source, path, number, 2, 2)
            if snippet is not None:
                yield f'### `{name}`\n#### {path}\n```\n{snippet}\n```', 1


def caller_block(files, symbols, source, max_tokens, estimator):
    result = ContextBlockResult(symbols=sum(map(len, symbols.values())))
    return _pack_context_entries(_CALLERS_HEADER, _caller_entries(symbols, files, source, result),
                                 result, max_tokens, estimator)


def _cochange_entries(files, source, result):
    changed = {file.filename for file in files}
    for file in sorted(files, key=_rank):
        if _generated(file.filename):
            continue
        result.files_searched += 1
        # Every changed path can outrank a useful peer, so leave room to remove them all.
        peers = [(path, count) for path, count in source.cochanged(file.filename, len(changed) + 3)
                 if path not in changed and not _generated(path)][:3]
        if peers:
            yield ('- ' + file.filename + ': '
                   + ', '.join(f'{path} ({count})' for path, count in peers)), len(peers)


def cochange_block(files, symbols, source, max_tokens, estimator):
    result = ContextBlockResult(symbols=sum(map(len, symbols.values())))
    return _pack_context_entries(_COCHANGE_HEADER, _cochange_entries(files, source, result),
                                 result, max_tokens, estimator)


def _mirror_block(name, files, read, symbols, source, max_tokens, estimator):
    if source is None:
        return ContextBlockResult(reason='no repository mirror')
    try:
        if name == 'callees':
            return callee_block(files, read, symbols, source, max_tokens, estimator)
        if name == 'callers':
            return caller_block(files, symbols, source, max_tokens, estimator)
        return cochange_block(files, symbols, source, max_tokens, estimator)
    except (ForgeRateLimited, ForgeAuthError, ForgeDenied):
        raise
    except Exception:
        log.exception('%s context source failed', name)
        return ContextBlockResult(reason='source error')


def context_blocks_preview(
    files: list[FilePatch], read: FileReader | None, budget_tokens: int,
    estimator: TokenEstimator, source: RepoSource | None = None, *,
    pairing=True, callees=False, callers=False, cochange=False,
) -> dict[str, ContextBlockResult]:
    read = cached_reader(read)
    symbols = (changed_symbols(sorted(files, key=_rank), read)
               if pairing or (source is not None and (callees or callers or cochange)) else {})
    results = {}
    if pairing:
        results['pairing'] = pairing_block(files, read, symbols,
                                           int(budget_tokens * PAIRING_BUDGET_SHARE), estimator)
    for name, enabled in [('callees', callees), ('callers', callers), ('cochange', cochange)]:
        if enabled:
            results[name] = _mirror_block(name, files, read, symbols, source,
                                           int(budget_tokens * _BLOCK_SHARES[name]), estimator)
    return results


def _append_context_blocks(result, files, read, budget_tokens, estimator, source):
    enabled = {name: context_setting(name) for name in _BLOCK_SHARES}
    if not any(enabled.values()):
        return
    additions = context_blocks_preview(files, read, budget_tokens, estimator, source, **enabled)
    for name, block in additions.items():
        result.context_blocks[name] = {
            'enabled': True, 'status': 'skipped' if block.reason else ('ran' if block.text else 'empty'),
            'reason': block.reason, 'symbols': block.symbols, 'references': block.references,
            'files_searched': block.files_searched, 'tokens': block.tokens,
            'budget_tokens': int(budget_tokens * _BLOCK_SHARES[name]), 'truncated': block.truncated}
        if block.text:
            result.prompt = '\n\n'.join(filter(None, [result.prompt, block.text]))
        if name == 'pairing':
            result.pairing_entries = block.entries


def render_review_context(
    files: list[FilePatch], read: Callable[[str], str | None] | None,
    budget_tokens: int, estimator: TokenEstimator | None = None,
    *, source: RepoSource | None = None,
) -> RenderedContext:
    estimator = estimator or HeuristicEstimator()
    if any(context_setting(name) for name in _BLOCK_SHARES):
        read = cached_reader(read)
    patchless = [f for f in files if f.status != REMOVED_STATUS and not f.patch]
    result = RenderedContext(deleted=[f.filename for f in files if f.status == REMOVED_STATUS],
                             patchless=[f.filename for f in patchless])
    blocks: list[str] = []
    accepted: list[FilePatch] = []
    tokens = 0
    candidates = sorted((f for f in files if f.status != REMOVED_STATUS and f.patch), key=_rank)
    for file in candidates:
        hunks = _block(file, 'hunks', [_line(*line) for line in _patch_lines(file.patch)])
        cost = estimator.estimate('\n\n'.join([*blocks, hunks]))
        if cost > budget_tokens:
            result.overflow.append(file.filename)
            continue
        blocks.append(hunks)
        accepted.append(file)
        tokens = cost

    modes = ['hunks_only'] * len(accepted)
    full_views: dict[int, str] = {}

    def upgrade(index: int, mode: str, block: str) -> None:
        nonlocal tokens
        previous = blocks[index]
        blocks[index] = block
        cost = estimator.estimate('\n\n'.join(blocks))
        if cost <= budget_tokens:
            modes[index], tokens = mode, cost
        else:
            blocks[index] = previous

    if read is not None:
        for index, file in enumerate(accepted):
            if budget_tokens - tokens < MIN_UPGRADE_TOKENS:
                break
            # Retain full views so the second sweep needs no additional reads.
            views = dict(_file_views(file, read, result))
            if 'full' in views:
                full_views[index] = views['full']
            if 'windowed' in views:
                upgrade(index, 'windowed', views['windowed'])
        for index, block in full_views.items():
            upgrade(index, 'full', block)

    for file, mode, block in zip(accepted, modes, blocks):
        getattr(result, mode).append(file.filename)
        result.views[file.filename] = block
    if result.deleted:
        note = '## Deleted files:\n' + '\n'.join(result.deleted)
        if estimator.estimate('\n\n'.join([*blocks, note])) <= budget_tokens:
            blocks.append(note)
    # With no patch-bearing file the reviewer lane is skipped, so the prompt is never sent.
    patchless_note = ''
    if patchless and result.views:
        patchless_note = ('## Changed files without a patch (content not shown):\n'
                          + '\n'.join(f'{f.filename} ({f.status})' for f in patchless))
        if estimator.estimate('\n\n'.join([*blocks, patchless_note])) > budget_tokens:
            patchless_note = (f'## Changed files without a patch: {len(patchless):,} '
                              '(names omitted, over budget)')
            if estimator.estimate('\n\n'.join([*blocks, patchless_note])) > budget_tokens:
                patchless_note = ''
    if result.overflow:
        blocks.append('## Other modified files (over budget, names only):\n' + '\n'.join(result.overflow))
    if patchless_note:
        blocks.append(patchless_note)
    result.prompt = '\n\n'.join(blocks)
    _append_context_blocks(result, files, read, budget_tokens, estimator, source)
    result.estimated_tokens = estimator.estimate(result.prompt)
    return result
