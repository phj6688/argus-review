"""Numbered post-image views, packed from whole files down to patch hunks."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .diff import REMOVED_STATUS, FilePatch, added_lines, split_lines
from .github import GitHubAuthError, GitHubDenied, GitHubRateLimited
from .tokens import HeuristicEstimator, TokenEstimator

FULL_FILE_MAX_LINES = 600
WINDOW_LINES = 30
REVIEW_CONTEXT_BUDGET_TOKENS = 120_000

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
        except (GitHubRateLimited, GitHubAuthError, GitHubDenied):
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


def render_review_context(
    files: list[FilePatch], read: Callable[[str], str | None] | None,
    budget_tokens: int, estimator: TokenEstimator | None = None,
) -> RenderedContext:
    estimator = estimator or HeuristicEstimator()
    result = RenderedContext(deleted=[f.filename for f in files if f.status == REMOVED_STATUS])
    blocks: list[str] = []
    candidates = sorted((f for f in files if f.status != REMOVED_STATUS and f.patch), key=_rank)
    for file in candidates:
        hunks = _block(file, 'hunks', [_line(*line) for line in _patch_lines(file.patch)])
        if estimator.estimate('\n\n'.join([*blocks, hunks])) > budget_tokens:
            result.overflow.append(file.filename)
            continue
        for mode, block in _file_views(file, read, result):
            cost = estimator.estimate('\n\n'.join([*blocks, block]))
            if cost <= budget_tokens:
                blocks.append(block)
                getattr(result, mode).append(file.filename)
                result.views[file.filename] = block
                break
        else:
            result.overflow.append(file.filename)
    if result.deleted:
        note = '## Deleted files:\n' + '\n'.join(result.deleted)
        if estimator.estimate('\n\n'.join([*blocks, note])) <= budget_tokens:
            blocks.append(note)
    if result.overflow:
        blocks.append('## Other modified files (over budget, names only):\n' + '\n'.join(result.overflow))
    result.prompt = '\n\n'.join(blocks)
    result.estimated_tokens = estimator.estimate(result.prompt)
    return result
