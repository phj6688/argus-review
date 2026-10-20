"""Immutable patch inputs and cached whole-file reads at the recorded head."""

import hashlib
import json
import re
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from argus import github
from argus.diff import FilePatch, added_lines
from argus.jarvis.live import PR_COMMITS_PAGE, _fetch_file_at, decode_file_text

from .cases import Case, normalize_path


def patch_digest(files: list[FilePatch]) -> str:
    # A pipeline change must not invalidate fixed inputs through prompt compression.
    rendered = "\n\n".join(
        f"## File: {json.dumps(f.filename)} ({f.status})\n{f.patch or ''}" for f in files)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    repo: str
    base_sha: str
    head_sha: str
    files: list[FilePatch]
    source: str
    patch_sha256: str
    truncated: bool = False
    contents: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        # Other runs may add cached whole-file reads while this record is written.
        return {**vars(self), "files": [asdict(f) for f in self.files],
                "contents": self.contents.copy()}

    @classmethod
    def from_dict(cls, data: dict):
        snapshot = cls(**{**data, "files": [FilePatch(**f) for f in data["files"]]})
        if snapshot.patch_sha256 != patch_digest(snapshot.files):
            raise ValueError("snapshot rendered patch hash mismatch")
        return snapshot


def _git(mirror: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--git-dir", str(mirror), *args],
                                   stderr=subprocess.PIPE)


def _mirror_files(mirror: Path, case: Case) -> list[FilePatch]:
    revision = f"{case.base_sha}...{case.head_sha}"
    options = ("--no-color", "--no-ext-diff", "--no-textconv", "-M", "--no-relative")
    text = _git(mirror, "diff", *options, "--unified=3", "--diff-algorithm=myers",
                "--no-indent-heuristic", revision).decode("utf-8", "replace")
    names = _git(mirror, "diff", *options, "--name-status", "-z", revision).split(b"\0")
    metadata = []
    index = 0
    while index < len(names) and names[index]:
        status = names[index].decode("ascii")
        path = names[index + 1]
        index += 2
        if status.startswith(("R", "C")):
            path = names[index]
            index += 1
        metadata.append((path.decode("utf-8", "surrogateescape"),
                         {"A": "added", "D": "removed", "R": "renamed"}.get(status[0], "modified")))
    blocks = re.split(r"^diff --git .*\n", text, flags=re.MULTILINE)[1:]
    if len(blocks) != len(metadata):
        raise ValueError("git diff file metadata and patch count disagree")
    # The production parser drops binary/mode-only files and retains quoted paths.
    # NUL-delimited metadata preserves those entries and exact post-image names.
    files = []
    for (path, status), block in zip(metadata, blocks, strict=True):
        match = re.search(r"^@@", block, flags=re.MULTILINE)
        patch = block[match.start():].rstrip("\n") if match else None
        files.append(FilePatch(path, status, patch))
    return files


def _in_tree(mirror: Path, sha: str, path: str) -> bool:
    return bool(_git(mirror, "ls-tree", "-z", sha, "--", path))


class SnapshotStore:
    def __init__(self, mirror_root: str | Path | None = None,
                 cache_dir: str | Path = ".argus-eval-cache"):
        self.mirror_root = Path(mirror_root) if mirror_root else None
        self.cache_dir = Path(cache_dir)
        self._lock = threading.RLock()
        self._snapshots: dict[tuple[str, str, str], Snapshot] = {}

    def _mirror(self, case: Case) -> Path | None:
        path = self.mirror_root / (case.repo.split("/")[1] + ".git") if self.mirror_root else None
        return path if path and path.is_dir() else None

    def _key(self, case: Case) -> tuple[str, str, str]:
        return case.repo, case.base_sha, case.head_sha

    def _cache_path(self, case: Case) -> Path:
        key = json.dumps(self._key(case), separators=(",", ":"))
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _save(self, case: Case, snapshot: Snapshot):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_path(case)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(snapshot.to_dict(), stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, case: Case) -> Snapshot:
        with self._lock:
            key = self._key(case)
            if key in self._snapshots:
                return self._snapshots[key]
            truncated_error = f"{case.case_id}: diff may be truncated; a local mirror is needed"
            path = self._cache_path(case)
            if path.exists():
                snapshot = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if snapshot.truncated:
                    raise ValueError(truncated_error)
                if (snapshot.repo, snapshot.base_sha, snapshot.head_sha) != key:
                    raise ValueError("snapshot cache identity mismatch")
            else:
                mirror = self._mirror(case)
                if mirror:
                    files = _mirror_files(mirror, case)
                    source, truncated = "mirror", False
                else:
                    response = github.get(f"/repos/{case.repo}/compare/{case.base_sha}...{case.head_sha}")
                    response.raise_for_status()
                    records = response.json()["files"]
                    if len(records) >= 300:
                        raise ValueError(truncated_error)
                    files = [FilePatch(f["filename"], f["status"], f.get("patch")) for f in records]
                    source, truncated = "github_compare", False
                snapshot = Snapshot(*key, files, source, patch_digest(files), truncated)
                self._save(case, snapshot)
            self._snapshots[key] = snapshot
            return snapshot

    def commit_messages(self, case: Case) -> list[str] | None:
        """The PR's commit messages as the live listing returns them: oldest first, one page."""
        mirror = self._mirror(case)
        if mirror is None:
            return None
        output = _git(mirror, "log", "-z", "--reverse", "--format=%B",
                      f"{case.base_sha}..{case.head_sha}")
        messages = output.decode("utf-8", "replace").split("\0")[:-1]
        return [message.rstrip("\n") for message in messages][:PR_COMMITS_PAGE]

    def file_loader(self, case: Case):
        def load(path: str) -> str | None:
            with self._lock:
                snapshot = self.get(case)
                mirror = self._mirror(case)
                if path in snapshot.contents:
                    cached = snapshot.contents[path]
                    # Older code cached None for any file with a NUL byte.
                    if cached is not None or mirror is None or not _in_tree(mirror, case.head_sha, path):
                        return cached
                if mirror:
                    try:
                        content = _git(mirror, "show", f"{case.head_sha}:{path}")
                    except subprocess.CalledProcessError:
                        # Absence is expected for deleted paths; other failures matter.
                        if _in_tree(mirror, case.head_sha, path):
                            raise
                        text = None
                    else:
                        text = decode_file_text(content)
                else:
                    text = _fetch_file_at(case.repo, path, case.head_sha)
                snapshot.contents[path] = text
                self._save(case, snapshot)
                return text
        return load


def changed_lines(files: list[FilePatch]) -> dict[str, set[int]]:
    result = {}
    for file in files:
        if file.status == "removed" or not file.patch:
            continue
        result[normalize_path(file.filename)] = added_lines(file.patch)
    return result
