"""Untrusted file names cannot escape the repository read boundary."""

import httpx
import pytest

from argus import diff, github, replies
from argus.jarvis import live

UNSAFE = [
    "../../../victim/private/contents/x", "/etc/passwd", "src/../x",
    "src\\x", "src/\x00x", "%2e%2e/x", "%252e%252e/x",
    "..%2f..%2fvictim/private/contents/x", "%2fx", "src%5cx", "src%00x",
]


@pytest.mark.parametrize("path", UNSAFE)
@pytest.mark.parametrize("header", ["+++ b/{}", "--- a/{}\n+++ b/safe.py",
                                    "rename from {}\nrename to safe.py", "rename to {}"])
def test_diff_drops_whole_file_with_unsafe_header(path, header, caplog):
    text = ("diff --git a/old.py b/new.py\n" + header.format(path)
            + "\n@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n+ok\n")
    assert [f.filename for f in diff.parse_unified_diff(text)] == ["ok.py"]
    assert any(r.name == "argus.diff" and "path" in r.message for r in caplog.records)


@pytest.mark.parametrize("path", UNSAFE)
@pytest.mark.parametrize("field", ["filename", "previous_filename"])
def test_files_api_drops_unsafe_paths(monkeypatch, path, field, caplog):
    files = [{"filename": "safe.py", "status": "renamed", field: path},
             {"filename": "ok.py", "status": "added"}]
    monkeypatch.setattr(github, "get", lambda *a, **k: httpx.Response(
        200, json=files, request=httpx.Request("GET", "https://api.github.com/x")))
    assert [f.filename for f in diff.fetch_pr_files("owner/repo", 1)] == ["ok.py"]
    assert "path" in caplog.text


@pytest.mark.parametrize("path", UNSAFE)
@pytest.mark.parametrize("reader", [live._fetch_file_at, replies._read_file])
def test_contents_read_rejects_unsafe_path_before_http(monkeypatch, path, reader):
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("Unsafe HTTP read"))
    with pytest.raises(ValueError):
        reader("owner/repo", path, "a" * 40)


@pytest.mark.parametrize("route", ["contents", "raw"])
@pytest.mark.parametrize("path", UNSAFE)
def test_guard_rejects_traversal_without_denied_owner(monkeypatch, route, path):
    monkeypatch.delenv("ARGUS_DENY_OWNERS", raising=False)
    with pytest.raises(github.GitHubDenied):
        github.guard_url(f"https://api.github.com/repos/owner/repo/{route}/{path}")


@pytest.mark.parametrize("reader", [live._fetch_file_at, replies._read_file])
def test_contents_read_quotes_each_segment(monkeypatch, reader):
    paths = []
    def get(path, params):
        paths.append(path)
        assert params == {"ref": "head"}
        return httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    monkeypatch.setattr(github, "get", get)
    assert reader("owner/repo", "src/a #?% file.py", "head") is None
    assert paths == ["/repos/owner/repo/contents/src/a%20%23%3F%25%20file.py"]


@pytest.mark.parametrize("header", ['+++ "b/../../../victim/private/x"',
                                    '--- "a/../x"\n+++ b/safe.py',
                                    'rename to "/etc/passwd"'])
def test_quoted_diff_headers_are_validated(header, caplog):
    assert diff.parse_unified_diff(f"diff --git a/x b/x\n{header}\n@@ -1 +1 @@\n+x") == []
    assert "unsafe path" in caplog.text


@pytest.mark.parametrize("control", [chr(i) for i in range(32)] + ["\x7f"])
def test_file_path_rejects_every_ascii_control(control):
    with pytest.raises(ValueError):
        github.quote_file_path(f"src/{control}name.py")
    with pytest.raises(ValueError):
        github.quote_file_path(f"src/%{ord(control):02x}name.py")


def test_filename_cannot_inject_markdown_line_breaks():
    with pytest.raises(ValueError):
        github.quote_file_path("src/\n\n![p](https://a.example/p).py")


@pytest.mark.parametrize("field", ["filename", "previous_filename"])
@pytest.mark.parametrize("path", ["src/\n\n![p](https://a.example/p).py", "src/\rname.py", "src/\tname.py"])
def test_files_api_drops_names_with_control_chars(monkeypatch, field, path):
    files = [{"filename": "safe.py", "status": "renamed", field: path},
             {"filename": "ok.py", "status": "added"}]
    monkeypatch.setattr(github, "get", lambda *a, **k: httpx.Response(
        200, json=files, request=httpx.Request("GET", "https://api.github.com/x")))
    assert [f.filename for f in diff.fetch_pr_files("owner/repo", 1)] == ["ok.py"]


@pytest.mark.parametrize("path", [r"src/\nname.py", "src/%0aname.py", "src/\nname.py", "src/\rname.py",
                                  "src/\tname.py", "src/name.py\t"])
@pytest.mark.parametrize("header", ['+++ "b/{}"', "+++ b/{}", "rename to {}"])
def test_diff_drops_newlines_and_controls_in_headers(path, header):
    text = ("diff --git a/x b/x\n" + header.format(path) + "\n@@ -1 +1 @@\n+x\n"
            "diff --git a/ok.py b/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n+ok\n")
    assert [f.filename for f in diff.parse_unified_diff(text)] == ["ok.py"]


@pytest.mark.parametrize("header,expected", [
    ("+++ b/docs/my file.md\t", "docs/my file.md"),
    ("+++ b/docs/my file.md", "docs/my file.md"),
    ("+++ b/plain.py", "plain.py"),
])
def test_diff_keeps_spaced_path_with_git_trailing_tab(header, expected):
    old = header.replace("+++ b/", "--- a/").removesuffix("\t") + ("\t" if header.endswith("\t") else "")
    text = f"diff --git a/x b/x\n{old}\n{header}\n@@ -1 +1 @@\n+x\n"
    assert [f.filename for f in diff.parse_unified_diff(text)] == [expected]


@pytest.mark.parametrize("header", ["+++ b/my file.md\t\t", "+++ b/plain.py\t", "--- a/my file.md\t\t"])
def test_diff_still_rejects_stray_tab(header):
    text = ("diff --git a/x b/x\n" + header + "\n@@ -1 +1 @@\n+x\n"
            "diff --git a/ok.py b/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n+ok\n")
    assert [f.filename for f in diff.parse_unified_diff(text)] == ["ok.py"]
