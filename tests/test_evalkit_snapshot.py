import json
import subprocess

import httpx
import pytest

from argus import github
from argus.evalkit.cases import Case
from argus.evalkit.snapshot import SnapshotStore, changed_lines


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def mirror_case(tmp_path):
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    for name, content in {"modified.txt": "old\ncontext\n", "deleted.txt": "gone\n",
                          "old name.txt": "rename me\n", "mode.txt": "mode\n",
                          "binary.bin": b"\x00old", "unicodé.txt": "before\n"}.items():
        path = repo / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "modified.txt").write_text("new\ncontext\nadded\n")
    (repo / "added.txt").write_text("brand new\n")
    (repo / "deleted.txt").unlink()
    (repo / "old name.txt").rename(repo / "new name.txt")
    (repo / "binary.bin").write_bytes(b"\x00new")
    (repo / "unicodé.txt").write_text("after\n")
    (repo / "mode.txt").chmod(0o755)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    head = git(repo, "rev-parse", "HEAD")
    # Moving the base branch ahead must not turn a three-dot diff into a two-dot diff.
    git(repo, "checkout", "-q", "-b", "base-ahead", base)
    (repo / "base-only.txt").write_text("not in head\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base only")
    base = git(repo, "rev-parse", "HEAD")
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(mirrors / "project.git")],
                   check=True)
    return Case("sample", "owner/project", 7, base, head, "heldout"), mirrors


def test_mirror_snapshot_preserves_all_file_types_and_caches(tmp_path, mirror_case, monkeypatch):
    case, mirrors = mirror_case
    store = SnapshotStore(mirrors, tmp_path / "cache")
    snapshot = store.get(case)
    files = {f.filename: f for f in snapshot.files}
    assert {p: f.status for p, f in files.items()} == {
        "modified.txt": "modified", "added.txt": "added", "deleted.txt": "removed",
        "new name.txt": "renamed", "binary.bin": "modified", "mode.txt": "modified",
        "unicodé.txt": "modified",
    }
    assert files["binary.bin"].patch is None
    assert files["mode.txt"].patch is None
    assert files["new name.txt"].patch is None
    assert files["deleted.txt"].patch == "@@ -1 +0,0 @@\n-gone"
    assert files["modified.txt"].patch == "@@ -1,2 +1,3 @@\n-old\n+new\n context\n+added"
    assert changed_lines(snapshot.files)["modified.txt"] == {1, 3}
    assert store.file_loader(case)("modified.txt") == "new\ncontext\nadded\n"
    assert store.file_loader(case)("deleted.txt") is None
    assert store.file_loader(case)("binary.bin") == "\x00new"
    assert snapshot.source == "mirror"
    assert len(snapshot.patch_sha256) == 64
    cache = next((tmp_path / "cache").glob("*.json"))
    before = cache.read_bytes()
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    cached = SnapshotStore(None, tmp_path / "cache")
    assert cached.get(case).files == snapshot.files
    assert cached.file_loader(case)("modified.txt") == "new\ncontext\nadded\n"
    assert cache.read_bytes() == before


@pytest.mark.parametrize("count", [299, 300, 301])
def test_compare_fallback_rejects_possible_truncation(tmp_path, monkeypatch, count):
    case = Case("sample", "owner/project", 7, "abc", "def", "dev")
    def response(path, params=None):
        assert path == "/repos/owner/project/compare/abc...def"
        return httpx.Response(200, request=httpx.Request("GET", "https://example.invalid"),
                              json={"files": [{"filename": f"{i}.txt", "status": "modified"}
                                              for i in range(count)]})
    monkeypatch.setattr(github, "get", response)
    store = SnapshotStore(None, tmp_path / "cache")
    if count >= 300:
        with pytest.raises(ValueError, match="sample.*may be truncated.*local mirror"):
            store.get(case)
        assert not list((tmp_path / "cache").glob("*.json"))
    else:
        snapshot = store.get(case)
        assert snapshot.source == "github_compare"
        assert snapshot.truncated is False
        assert len(snapshot.files) == count


def test_changed_lines_handles_multiple_hunks_and_deletions():
    from argus.diff import FilePatch
    files = [FilePatch("./a.py", "modified", "@@ -2,3 +2,3 @@\n ctx\n-old\n+new\n ctx\n"
                       "@@ -20,0 +21,2 @@\n+x\n+y\n\\ No newline at end of file"),
             FilePatch("gone", "removed", "@@ -1 +0,0 @@\n-gone")]
    assert changed_lines(files) == {"a.py": {3, 21, 22}}


def test_github_content_cache_survives_without_credentials(tmp_path, monkeypatch):
    import base64
    case = Case("sample", "owner/project", 1, "abc", "def", "dev")
    def response(path, params=None):
        if "/compare/" in path:
            body = {"files": [{"filename": "a.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"}]}
        else:
            assert path == "/repos/owner/project/contents/a.py" and params == {"ref": "def"}
            body = {"encoding": "base64", "content": base64.b64encode(b"x\n").decode()}
        return httpx.Response(200, request=httpx.Request("GET", "https://example.invalid"), json=body)
    monkeypatch.setattr(github, "get", response)
    store = SnapshotStore(cache_dir=tmp_path / "cache")
    assert store.file_loader(case)("a.py") == "x\n"
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    assert SnapshotStore(cache_dir=tmp_path / "cache").file_loader(case)("a.py") == "x\n"


def test_snapshot_cache_detects_corruption(tmp_path, mirror_case):
    case, mirrors = mirror_case
    store = SnapshotStore(mirrors, tmp_path / "cache")
    store.get(case)
    path = next((tmp_path / "cache").glob("*.json"))
    data = json.loads(path.read_text())
    data["patch_sha256"] = "corrupt"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="hash mismatch"):
        SnapshotStore(None, tmp_path / "cache").get(case)


def test_patch_hash_covers_binary_metadata_and_deletion_only_hunks():
    from argus.diff import FilePatch
    from argus.evalkit.snapshot import patch_digest
    assert patch_digest([FilePatch("a.bin", "added", None)]) != patch_digest([
        FilePatch("b.bin", "added", None)])
    assert patch_digest([FilePatch("a.txt", "modified", "@@ -1 +0,0 @@\n-old")]) != patch_digest([
        FilePatch("a.txt", "modified", "@@ -1 +0,0 @@\n-other")])


def test_cached_truncated_snapshot_is_rejected(tmp_path, monkeypatch):
    from argus.evalkit.snapshot import Snapshot, patch_digest
    case = Case("sample", "owner/project", 7, "abc", "def", "dev")
    store = SnapshotStore(cache_dir=tmp_path)
    snapshot = Snapshot(case.repo, case.base_sha, case.head_sha, [], "github_compare",
                        patch_digest([]), truncated=True)
    store._save(case, snapshot)
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    with pytest.raises(ValueError, match="sample.*may be truncated.*local mirror"):
        store.get(case)


def test_changed_lines_counts_empty_context():
    from argus.diff import FilePatch
    files = [FilePatch("./a.py", "modified", "@@ -1 +1,3 @@\n+first\n\n+third"),
             FilePatch("missing", "modified", None)]
    assert changed_lines(files) == {"a.py": {1, 3}}


def test_mirror_loader_decodes_a_file_with_a_nul_byte_like_the_live_reader(tmp_path, mirror_case,
                                                                         monkeypatch):
    import base64
    from argus.jarvis import live
    case, mirrors = mirror_case
    def contents(path, params=None):
        body = {"encoding": "base64", "content": base64.b64encode(b"\x00new").decode()}
        return httpx.Response(200, request=httpx.Request("GET", "https://example.invalid"), json=body)
    monkeypatch.setattr(github, "get", contents)
    live_text = live._fetch_file_at(case.repo, "binary.bin", case.head_sha)
    assert live_text == "\x00new"
    assert SnapshotStore(mirrors, tmp_path / "cache").file_loader(case)("binary.bin") == live_text


def test_mirror_loader_rereads_a_cached_none_that_exists_at_head(tmp_path, mirror_case):
    case, mirrors = mirror_case
    store = SnapshotStore(mirrors, tmp_path / "cache")
    snapshot = store.get(case)
    snapshot.contents.update({"binary.bin": None, "deleted.txt": None})
    store._save(case, snapshot)
    fresh = SnapshotStore(mirrors, tmp_path / "cache")
    assert fresh.file_loader(case)("binary.bin") == "\x00new"
    assert fresh.file_loader(case)("deleted.txt") is None
    cached = json.loads(next((tmp_path / "cache").glob("*.json")).read_text())["contents"]
    assert cached == {"binary.bin": "\x00new", "deleted.txt": None}


def test_cached_none_without_a_mirror_stays_none(tmp_path, mirror_case, monkeypatch):
    case, mirrors = mirror_case
    store = SnapshotStore(mirrors, tmp_path / "cache")
    snapshot = store.get(case)
    snapshot.contents["binary.bin"] = None
    store._save(case, snapshot)
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    assert SnapshotStore(None, tmp_path / "cache").file_loader(case)("binary.bin") is None


@pytest.fixture
def commit_range(tmp_path):
    repo = tmp_path / "range"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "pr")
    for message in ("feat: one\n\ndetail", "fix: two", "chore: three"):
        git(repo, "commit", "-q", "--allow-empty", "-m", message)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base moved on")
    moved = git(repo, "rev-parse", "HEAD")
    mirrors = tmp_path / "range-mirrors"
    mirrors.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(mirrors / "project.git")],
                   check=True)
    return mirrors, base, moved, head


def test_commit_messages_list_the_range_oldest_first(tmp_path, commit_range):
    mirrors, base, moved, head = commit_range
    store = SnapshotStore(mirrors, tmp_path / "cache")
    expected = ["feat: one\n\ndetail", "fix: two", "chore: three"]
    for start in (base, moved):
        assert store.commit_messages(Case("c", "owner/project", 1, start, head, "dev")) == expected


def test_commit_messages_keep_the_oldest_page_like_the_live_listing(tmp_path, commit_range,
                                                                    monkeypatch):
    from argus.evalkit import snapshot
    mirrors, base, _, head = commit_range
    monkeypatch.setattr(snapshot, "PR_COMMITS_PAGE", 2)
    case = Case("c", "owner/project", 1, base, head, "dev")
    assert SnapshotStore(mirrors, tmp_path / "cache").commit_messages(case) == [
        "feat: one\n\ndetail", "fix: two"]


def test_commit_messages_are_absent_without_a_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    case = Case("c", "owner/project", 1, "abc", "def", "dev")
    assert SnapshotStore(None, tmp_path / "cache").commit_messages(case) is None
