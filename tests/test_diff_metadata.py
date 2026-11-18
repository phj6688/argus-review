import httpx
import pytest

from argus import diff, risk_tier


def _response(files):
    return httpx.Response(200, json=files, request=httpx.Request("GET", "https://example.test"))


def test_file_list_preserves_list_behavior_and_keyword_only_completeness():
    files = diff.FileList([diff.FilePatch("src/app.py", "modified", None)], unseen=True)
    assert files == [diff.FilePatch("src/app.py", "modified", None)]
    assert files.unseen is True
    assert diff.FileList().unseen is False
    with pytest.raises(TypeError):
        diff.FileList([], True)


def test_files_api_preserves_rename_and_marks_dropped_paths_incomplete(monkeypatch):
    monkeypatch.setattr(diff.github, "get", lambda *_: _response([
        {"filename": "src/app.py", "previous_filename": "infra/nginx.conf",
         "status": "renamed", "additions": 11, "deletions": 7},
        {"filename": "../unsafe.py", "status": "modified"},
    ]))
    files = diff.fetch_pr_files("o/r", 1)
    assert len(files) == 1
    assert files[0].previous_filename == "infra/nginx.conf"
    assert (files[0].additions, files[0].deletions) == (11, 7)
    assert files.unseen is True


def test_files_api_marks_3000_listed_files_incomplete(monkeypatch):
    def get(_path, params):
        page = params["page"]
        batch = [{"filename": f"src/file{(page - 1) * 100 + i}.py", "status": "modified"}
                 for i in range(100)] if page <= 30 else []
        return _response(batch)

    monkeypatch.setattr(diff.github, "get", get)
    files = diff.fetch_pr_files("o/r", 1)
    assert len(files) == 3000
    assert files.unseen is True


def test_files_api_below_cap_is_complete(monkeypatch):
    monkeypatch.setattr(diff.github, "get", lambda *_: _response([
        {"filename": "src/app.py", "status": "modified"},
    ]))
    files = diff.fetch_pr_files("o/r", 1)
    assert files.unseen is False


@pytest.mark.parametrize("headers", [
    "rename from infra/nginx.conf\nrename to src/app.py",
    "rename to src/app.py\n--- a/infra/nginx.conf\n+++ b/src/app.py",
])
def test_unified_diff_preserves_previous_rename_path(headers):
    files = diff.parse_unified_diff(
        f"diff --git a/infra/nginx.conf b/src/app.py\n{headers}\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert len(files) == 1
    assert files[0].filename == "src/app.py"
    assert files[0].status == "renamed"
    assert files[0].previous_filename == "infra/nginx.conf"
    assert files.unseen is False


def test_unified_diff_marks_dropped_unsafe_file_incomplete():
    files = diff.parse_unified_diff(
        "diff --git a/x b/x\n--- a/../x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert [item.filename for item in files] == ["ok.py"]
    assert files.unseen is True


@pytest.mark.parametrize("omitted", [
    "diff --git a/infra/auth.bin b/infra/auth.bin\nindex 1111111..2222222 100644\n"
    "Binary files a/infra/auth.bin and b/infra/auth.bin differ\n",
    "diff --git a/infra/nginx.conf b/infra/nginx.conf\nold mode 100644\nnew mode 100755\n",
])
@pytest.mark.parametrize("omitted_first", [False, True])
def test_unified_diff_omitted_file_section_makes_risk_incomplete(omitted, omitted_first):
    ordinary = (
        "diff --git a/docs/readme.md b/docs/readme.md\n"
        "--- a/docs/readme.md\n+++ b/docs/readme.md\n@@ -1 +1 @@\n-old\n+new\n"
    )
    files = diff.parse_unified_diff(omitted + ordinary if omitted_first else ordinary + omitted)
    assert [item.filename for item in files] == ["docs/readme.md"]
    assert files.unseen is True
    risk = risk_tier.classify_files(files).to_dict()
    assert risk["tier"] == "risky"
    assert risk["incomplete"] is True
    assert risk["reasons"] == ["file list incomplete, an unseen path might match"]


def test_unified_diff_single_complete_safe_text_is_complete():
    files = diff.parse_unified_diff(
        "diff --git a/docs/readme.md b/docs/readme.md\n"
        "--- a/docs/readme.md\n+++ b/docs/readme.md\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert files.unseen is False
    assert risk_tier.classify_files(files).tier == "safe"
