import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import github, review
from argus.jarvis import live
from argus.diff import FilePatch
from argus.evalkit import replay
from argus.evalkit.snapshot import Snapshot, patch_digest
from argus.schema import Finding, JudgeOpinion, Verdict, VerifiedFinding
from test_evalkit_cases import case_data, write_cases


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "deploy/cron/argus_replay.sh"
REAL_SNAPSHOT_GET = replay.SnapshotStore.get
REAL_CODE_VERSION = replay.code_version


def stub_snapshot(self, case):
    files = [FilePatch("a.py", "modified", "@@ -1 +1 @@\n-old\n+new")]
    return Snapshot(case.repo, case.base_sha, case.head_sha, files, "mirror", patch_digest(files))


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "get", lambda *a, **k: pytest.fail("network used"))
    monkeypatch.setattr(review, "complete_tool", lambda *a, **k: pytest.fail("model used"))
    monkeypatch.setattr(replay.SnapshotStore, "get", stub_snapshot)
    # The default roots live under the home directory, so no test may read the operator's data.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("ARGUS_MIRROR_ROOT", "ARGUS_EVAL_CACHE", "ARGUS_EVAL_AUTH_TOKEN",
                 "ARGUS_GIT_SHA", "ARGUS_EVAL_CODE_SHA"):
        monkeypatch.delenv(name, raising=False)
    # Replay may swap the model credential in its own environment; monkeypatch restores it.
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "production-token")


def git(path, *args):
    # Like replay._git_output: an outer GIT_DIR must not redirect fixture repositories.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.check_output(["git", "-C", str(path), *args], text=True,
                                   env=environment).strip()


def make_checkout(path, files):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", *files)
    git(path, "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


def item(real=True, title="Bad branch"):
    return VerifiedFinding(
        finding=Finding(file="a.py", line=1, severity="major", confidence=0.8,
                        title=title, description="Wrong result", proposed_fix="Change branch"),
        verdict=Verdict(is_real=real, confidence=0.9),
        lane="llm", judge=JudgeOpinion(verdict="real", confidence=0.85, model="judge"),
        fingerprint="fp:sample",
    )


def test_replay_resumes_and_captures_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay, "code_version", lambda: ("test-sha", False))
    cases = write_cases(tmp_path / "cases", [case_data("good"), case_data("bad")])
    out = tmp_path / "results"
    calls = []
    def run(files, repo, head_sha, model, bug_cap, fast=False, pr_title=None):
        assert files[0].filename == "a.py"
        assert repo == "owner/project" and head_sha == "def"
        assert model == "test" and bug_cap == 10 and fast is False
        calls.append(pr_title)
        if len(calls) % 2 == 0:
            raise RuntimeError("review failed")
        return review.ReviewResult(raw_count=2, bugs=[item()], all_findings=[item(), item(False)],
                                   coverage=[{"lane": "llm", "status": "ran", "files_omitted": 0}])
    monkeypatch.setattr(review, "run_diff_review", run)
    args = ["--cases", str(cases), "--out", str(out), "--variant", "baseline", "--model", "test"]
    assert replay.main(args) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2
    good, bad = sorted(rows, key=lambda r: r["case_id"], reverse=True)
    assert good["case_id"] == "good"
    assert good["reported"][0]["judge"] == {"verdict": "real", "confidence": 0.85, "model": "judge"}
    assert good["reported"][0]["verdict_is_real"] is True
    assert good["reported"][0]["fingerprint"] == "fp:sample"
    assert good["raw"][1]["verdict_is_real"] is False
    assert good["coverage"][0]["lane"] == "llm"
    assert good["duration_s"] >= 0
    assert good["code_sha"] == "test-sha"
    assert good["code_dirty"] is False
    assert good["github_mode"] == "api"
    assert good["snapshot"]["files"][0]["patch"].startswith("@@")
    assert set(good["snapshot"]) == {"repo", "base_sha", "head_sha", "source", "patch_sha256",
                                     "truncated", "files"}
    assert bad["error"] == {"class": "RuntimeError", "message": "review failed"}
    assert bad["reported"] == []
    before = out.read_bytes()
    assert replay.main(args) == 0
    assert out.read_bytes() == before and len(calls) == 2
    assert "good" in capsys.readouterr().err


def test_replay_optional_context_and_future_fields(tmp_path, monkeypatch):
    data = {**case_data(), "pr_title": "title", "pr_body": "body"}
    cases = write_cases(tmp_path / "cases", [data])
    def run(files, repo, head_sha, model, bug_cap, *, pr_title, pr_body, base_sha, file_loader):
        assert (pr_title, pr_body, base_sha) == ("title", "body", "abc")
        assert callable(file_loader)
        class FutureFinding(Finding):
            future_explanation: str = "source text"
        finding = FutureFinding(**{**item().finding.model_dump(), "evidence": "source text"})
        return SimpleNamespace(bugs=[VerifiedFinding(finding=finding, verdict=item().verdict)],
                               nits=[], all_findings=[], coverage=[])
    monkeypatch.setattr(review, "run_diff_review", run)
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "future"])
    assert json.loads(out.read_text())["reported"][0]["evidence"] == "source text"
    assert json.loads(out.read_text())["reported"][0]["future_explanation"] == "source text"


def test_replay_filters_runs_concurrency_and_partial_tail(tmp_path, monkeypatch):
    cases = write_cases(tmp_path / "cases", [case_data("one"), {**case_data("two"), "split": "dev",
                                                             "expected": []}])
    monkeypatch.setattr(review, "run_diff_review", lambda files, **kw: review.ReviewResult(raw_count=0))
    out = tmp_path / "results"
    out.write_text('{"variant":"x","case_id":"one","run":1}\n{"partial":')
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x", "--split", "heldout",
                 "--case-ids", "one,two", "--runs", "3", "--concurrency", "2", "--limit", "1"])
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert {r["run"] for r in rows} == {1, 2, 3}
    assert {r["case_id"] for r in rows} == {"one"}
    assert all(r.get("error") is None for r in rows)


def test_replay_snapshot_failure_is_recorded(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("missing commit")
    monkeypatch.setattr(replay.SnapshotStore, "get", fail)
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x"])
    assert json.loads(out.read_text())["error"]["class"] == "OSError"


def test_replay_defaults_to_mirror_mode_with_mirror_root(tmp_path, monkeypatch):
    cases = write_cases(tmp_path / "cases", [case_data()])
    installed = []
    monkeypatch.setattr(replay, "install_mirror_mode", lambda root: installed.append(root) or (lambda: None))
    monkeypatch.setattr(review, "run_diff_review", lambda files, **kw: review.ReviewResult(raw_count=0))
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x",
                 "--mirror-root", str(tmp_path / "mirrors")])
    assert installed == [str(tmp_path / "mirrors")]
    assert json.loads(out.read_text())["github_mode"] == "mirror"


def test_code_version_reads_checkout_head_and_tracked_edits(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    sha = make_checkout(checkout, {"module.py": "value = 1\n"})
    monkeypatch.setenv("ARGUS_EVAL_CODE_SHA", "caller-label")
    monkeypatch.setenv("ARGUS_GIT_SHA", "image-build")
    assert replay.code_version(checkout) == (sha, False)
    (checkout / "module.py").write_text("value = 2\n")
    assert replay.code_version(checkout) == (sha, True)
    git(checkout, "add", "module.py")
    assert replay.code_version(checkout) == (sha, True)


def test_code_version_ignores_untracked_files(tmp_path):
    checkout = tmp_path / "checkout"
    sha = make_checkout(checkout, {"module.py": "value = 1\n"})
    (checkout / "notes.txt").write_text("scratch\n")
    assert replay.code_version(checkout) == (sha, False)


def test_code_version_ignores_repository_selection_variables(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    sha = make_checkout(checkout, {"module.py": "value = 1\n"})
    other = tmp_path / "other"
    other_sha = make_checkout(other, {"module.py": "value = 2\n"})
    (other / "module.py").write_text("value = 3\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    assert sha != other_sha
    assert replay.code_version(checkout) == (sha, False)


def test_code_version_reads_a_worktree_checkout(tmp_path):
    main = tmp_path / "main"
    make_checkout(main, {"module.py": "value = 1\n"})
    worktree = tmp_path / "worktree"
    git(main, "worktree", "add", "-q", "-b", "side", str(worktree))
    (worktree / "module.py").write_text("value = 3\n")
    git(worktree, "commit", "-qam", "side change")
    assert (worktree / ".git").is_file()
    assert replay.code_version(worktree) == (git(worktree, "rev-parse", "HEAD"), False)


def test_code_version_without_git_uses_the_image_build_sha(tmp_path, monkeypatch):
    installed = tmp_path / "site-packages"
    installed.mkdir()
    monkeypatch.setenv("ARGUS_EVAL_CODE_SHA", "caller-label")
    monkeypatch.setenv("ARGUS_GIT_SHA", "image-build")
    assert replay.code_version(installed) == ("image-build", False)


def test_code_version_is_unknown_without_git_or_build_sha(tmp_path, monkeypatch):
    installed = tmp_path / "site-packages"
    installed.mkdir()
    monkeypatch.setenv("ARGUS_EVAL_CODE_SHA", "caller-label")
    assert replay.code_version(installed) == ("unknown", True)
    monkeypatch.setenv("ARGUS_GIT_SHA", "")
    assert replay.code_version(installed) == ("unknown", True)


def test_code_version_ignores_an_enclosing_repository(tmp_path):
    checkout = tmp_path / "checkout"
    make_checkout(checkout, {"module.py": "value = 1\n"})
    installed = checkout / ".venv/lib"
    installed.mkdir(parents=True)
    assert replay.code_version(installed) == ("unknown", True)


@pytest.mark.parametrize("failure", [OSError("no git"),
                                     subprocess.TimeoutExpired(["git"], 30),
                                     subprocess.CalledProcessError(128, ["git"])])
def test_code_version_is_unknown_when_git_fails_in_a_checkout(tmp_path, monkeypatch, failure):
    checkout = tmp_path / "checkout"
    make_checkout(checkout, {"module.py": "value = 1\n"})
    monkeypatch.setenv("ARGUS_GIT_SHA", "image-build")

    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(replay.subprocess, "run", fail)
    assert replay.code_version(checkout) == ("unknown", True)


def test_code_version_is_unknown_for_a_dangling_git_symlink(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").symlink_to(tmp_path / "missing.git")
    monkeypatch.setenv("ARGUS_GIT_SHA", "image-build")
    assert replay.code_version(checkout) == ("unknown", True)


def test_code_version_is_unknown_for_a_broken_worktree_link(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /missing/worktree\n")
    monkeypatch.setenv("ARGUS_GIT_SHA", "image-build")
    assert replay.code_version(checkout) == ("unknown", True)


def test_code_version_defaults_to_the_running_checkout():
    running = replay.RUNNING_CHECKOUT
    if (running / ".git").exists():
        assert replay.code_version()[0] == git(running, "rev-parse", "HEAD")
    else:
        assert replay.code_version() == ("unknown", True)


def test_mirror_mode_reads_git_content_and_falls_back(tmp_path, monkeypatch):
    work, mirrors = tmp_path / "work", tmp_path / "mirrors"
    work.mkdir()
    mirrors.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(work), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (work / "CLAUDE.md").write_text("rules\n")
    (work / "text.txt").write_text("mirror text\n")
    (work / "binary.bin").write_bytes(b"\0binary")
    git("add", ".")
    git("commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(mirrors / "project.git")], check=True)
    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: "api file")
    monkeypatch.setattr(review, "fetch_house_rules", lambda *args: "api rules")
    restore = replay.install_mirror_mode(mirrors)
    try:
        assert live._fetch_file_at("owner/project", "text.txt", sha) == "mirror text\n"
        assert live._fetch_file_at("owner/project", "missing.txt", sha) is None
        assert live._fetch_file_at("owner/project", "binary.bin", sha) == "\0binary"
        assert review.fetch_house_rules("owner/project") == "rules\n"
        assert live._fetch_file_at("owner/absent", "text.txt", sha) == "api file"
        assert review.fetch_house_rules("owner/absent") == "api rules"
    finally:
        restore()
    assert live._fetch_file_at("owner/absent", "text.txt", sha) == "api file"
    assert review.fetch_house_rules("owner/absent") == "api rules"


def test_flat_finding_keeps_verifier_fields():
    class FlatFinding(Finding):
        verdict_is_real: bool = True
        verdict_confidence: float = 0.75
    data = replay.serialize_finding(FlatFinding(**item().finding.model_dump()))
    assert data["verdict_is_real"] is True
    assert data["verdict_confidence"] == 0.75


def test_replay_positional_files_and_score_end_to_end(tmp_path, monkeypatch):
    from argus.evalkit import score
    data = case_data("c")
    data["expected"][0].update(start_line=1, end_line=1)
    cases = write_cases(tmp_path / "cases", [data])
    def run(files, /, repo, head_sha, model, bug_cap):
        return review.ReviewResult(raw_count=1, bugs=[item()], all_findings=[item()])
    monkeypatch.setattr(review, "run_diff_review", run)
    results, report, markdown = tmp_path / "results", tmp_path / "report", tmp_path / "markdown"
    replay.main(["--cases", str(cases), "--out", str(results), "--variant", "v"])
    score.main(["--cases", str(cases), "--results", str(results), "--out-json", str(report),
                "--out-md", str(markdown)])
    overall = json.loads(report.read_text())["variants"]["v"]["overall"]
    assert overall["recall"]["0"]["mean"]["recall"] == 1.0
    assert overall["changed_line_share"] == 1.0
    assert overall["coverage_levels"] == {"unknown": 1}


def test_replay_continues_after_truncated_snapshot(tmp_path, monkeypatch):
    def snapshot(store, case):
        if case.case_id == "bad":
            raise ValueError("bad: diff may be truncated; a local mirror is needed")
        return stub_snapshot(store, case)
    monkeypatch.setattr(replay.SnapshotStore, "get", snapshot)
    monkeypatch.setattr(review, "run_diff_review", lambda *a, **k: review.ReviewResult(0))
    cases = write_cases(tmp_path / "cases", [case_data("bad"), case_data("good")])
    out = tmp_path / "results"
    assert replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x"]) == 0
    rows = {row["case_id"]: row for row in map(json.loads, out.read_text().splitlines())}
    assert rows["bad"]["error"] == {
        "class": "ValueError", "message": "bad: diff may be truncated; a local mirror is needed"}
    assert rows["bad"]["reported"] == []
    assert rows["good"]["error"] is None


def mirror_case(tmp_path):
    work = tmp_path / "work"
    base = make_checkout(work, {"CLAUDE.md": "rules\n", "a.py": "value = 1\n"})
    (work / "a.py").write_text("value = 2\n")
    git(work, "commit", "-qam", "change")
    head = git(work, "rev-parse", "HEAD")
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(mirrors / "project.git")],
                   check=True)
    data = {**case_data("mirrored"), "base_sha": base, "head_sha": head}
    return write_cases(tmp_path / "cases", [data]), mirrors


def no_findings(model, system, user, tool):
    assert tool.name == "report_findings"
    return {"findings": []}


def test_replay_defaults_roots_from_settings_and_records_provenance(tmp_path, monkeypatch):
    cases, mirrors = mirror_case(tmp_path)
    cache = tmp_path / "cache"
    checkout = tmp_path / "argus-checkout"
    sha = make_checkout(checkout, {"module.py": "value = 1\n"})
    monkeypatch.setenv("ARGUS_MIRROR_ROOT", str(mirrors))
    monkeypatch.setenv("ARGUS_EVAL_CACHE", str(cache))
    monkeypatch.setattr(replay.SnapshotStore, "get", REAL_SNAPSHOT_GET)
    monkeypatch.setattr(replay, "code_version", functools.partial(REAL_CODE_VERSION, checkout))
    monkeypatch.setattr(review, "complete_tool", no_findings)
    stores = []
    real_init = replay.SnapshotStore.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        stores.append(self)
    monkeypatch.setattr(replay.SnapshotStore, "__init__", init)
    out = tmp_path / "results"
    assert replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x",
                        "--model", "test"]) == 0
    row = json.loads(out.read_text())
    assert row["error"] is None
    assert (stores[0].mirror_root, stores[0].cache_dir) == (mirrors, cache)
    assert row["snapshot"]["source"] == "mirror" and row["github_mode"] == "mirror"
    assert len(list(cache.glob("*.json"))) == 1
    assert (row["code_sha"], row["code_dirty"]) == (sha, False)
    assert row["credential_env"] == "ANTHROPIC_AUTH_TOKEN"


def test_replay_falls_back_to_the_eval_data_roots(tmp_path, monkeypatch):
    stores = []
    real_init = replay.SnapshotStore.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        stores.append(self)
    monkeypatch.setattr(replay.SnapshotStore, "__init__", init)
    monkeypatch.setattr(review, "run_diff_review", lambda files, **kw: review.ReviewResult(0))
    data_root = Path(os.environ["HOME"]) / "data/argus-eval"
    (data_root / "mirrors").mkdir(parents=True)
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x"])
    assert (stores[0].mirror_root, stores[0].cache_dir) == (data_root / "mirrors",
                                                            data_root / "cache")
    assert json.loads(out.read_text())["github_mode"] == "mirror"


def test_replay_without_mirrors_keeps_api_mode(tmp_path, monkeypatch):
    stores = []
    real_init = replay.SnapshotStore.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        stores.append(self)
    monkeypatch.setattr(replay.SnapshotStore, "__init__", init)
    monkeypatch.setattr(review, "run_diff_review", lambda files, **kw: review.ReviewResult(0))
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x"])
    assert stores[0].mirror_root is None
    assert json.loads(out.read_text())["github_mode"] == "api"


def test_replay_scopes_the_eval_credential_to_one_run(tmp_path, monkeypatch):
    cases, mirrors = mirror_case(tmp_path)
    monkeypatch.setenv("ARGUS_MIRROR_ROOT", str(mirrors))
    monkeypatch.setenv("ARGUS_EVAL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(replay.SnapshotStore, "get", REAL_SNAPSHOT_GET)
    seen = []

    def record_token(model, system, user, tool):
        seen.append(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return no_findings(model, system, user, tool)
    monkeypatch.setattr(review, "complete_tool", record_token)

    def replay_once(variant):
        out = tmp_path / f"{variant}.jsonl"
        assert replay.main(["--cases", str(cases), "--out", str(out), "--variant", variant,
                            "--model", "test"]) == 0
        row = json.loads(out.read_text())
        assert row["error"] is None
        return row["credential_env"]

    monkeypatch.setenv("ARGUS_EVAL_AUTH_TOKEN", "eval-token")
    assert replay_once("first") == "ARGUS_EVAL_AUTH_TOKEN"
    assert set(seen) == {"eval-token"}
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "production-token"
    seen.clear()
    monkeypatch.delenv("ARGUS_EVAL_AUTH_TOKEN")
    assert replay_once("second") == "ANTHROPIC_AUTH_TOKEN"
    assert set(seen) == {"production-token"}


def test_replay_removes_the_eval_credential_it_added(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    monkeypatch.setenv("ARGUS_EVAL_AUTH_TOKEN", "eval-token")
    seen = []

    def run(files, **kwargs):
        seen.append(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        raise RuntimeError("review failed")
    monkeypatch.setattr(review, "run_diff_review", run)
    cases = write_cases(tmp_path / "cases", [case_data()])
    replay.main(["--cases", str(cases), "--out", str(tmp_path / "results"), "--variant", "x"])
    assert seen == ["eval-token"]
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_replay_uses_the_eval_credential_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_EVAL_AUTH_TOKEN", "eval-token")
    seen = []

    def run(files, **kwargs):
        seen.append(os.environ["ANTHROPIC_AUTH_TOKEN"])
        return review.ReviewResult(0)
    monkeypatch.setattr(review, "run_diff_review", run)
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x", "--model", "test"])
    row = json.loads(out.read_text())
    assert row["credential_env"] == "ARGUS_EVAL_AUTH_TOKEN"
    assert seen == ["eval-token"]
    assert "eval-token" not in out.read_text()


def test_replay_runs_with_the_production_credential_without_an_eval_token(tmp_path, monkeypatch):
    seen = []

    def run(files, **kwargs):
        seen.append(os.environ["ANTHROPIC_AUTH_TOKEN"])
        return review.ReviewResult(0)
    monkeypatch.setattr(review, "run_diff_review", run)
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    assert replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x",
                        "--model", "test"]) == 0
    row = json.loads(out.read_text())
    assert row["error"] is None and row["credential_env"] == "ANTHROPIC_AUTH_TOKEN"
    assert seen == ["production-token"]


def test_replay_records_the_router_key_for_router_models(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_EVAL_AUTH_TOKEN", "eval-token")
    monkeypatch.setattr(review, "run_diff_review", lambda files, **kw: review.ReviewResult(0))
    cases = write_cases(tmp_path / "cases", [case_data()])
    out = tmp_path / "results"
    replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x",
                 "--model", "vendor/model"])
    assert json.loads(out.read_text())["credential_env"] == "OPENROUTER_API_KEY"


@pytest.mark.parametrize("provider, model, env, expected", [
    ("gateway", "model", {"ANTHROPIC_BASE_URL": "https://gateway.test", "ANTHROPIC_AUTH_TOKEN": "k"},
     "ANTHROPIC_AUTH_TOKEN"),
    ("anthropic", "model", {"ANTHROPIC_API_KEY": "k"}, "ANTHROPIC_API_KEY"),
    ("openrouter", "vendor/model", {"OPENROUTER_API_KEY": "k"}, "OPENROUTER_API_KEY"),
    ("openai", "vendor/model", {"OPENAI_API_KEY": "k"}, "OPENAI_API_KEY"),
    ("openai", "model", {"OPENAI_BASE_URL": "http://localhost:11434/v1"}, "OPENAI_API_KEY"),
    ("claude-cli", "model", {}, "claude login"),
    ("claude-cli", "model", {"CLAUDE_CODE_OAUTH_TOKEN": "token"}, "CLAUDE_CODE_OAUTH_TOKEN"),
    ("codex-cli", "model", {"CODEX_HOME": "/test/home"}, "codex login"),
    ("claude-cli", "vendor/model", {"OPENROUTER_API_KEY": "k"}, "OPENROUTER_API_KEY"),
    ("codex-cli", "vendor/model", {"OPENROUTER_API_KEY": "k"}, "OPENROUTER_API_KEY"),
])
def test_model_credential_tracks_the_resolved_provider(monkeypatch, provider, model, env, expected):
    for name in (
        "ARGUS_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "ARGUS_EVAL_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    with replay.model_credential(model) as credential:
        assert credential == expected
    assert dict(os.environ) == before


def test_replay_wrapper_lives_in_the_repository():
    text = WRAPPER.read_text()
    assert "/.claude/jobs/" not in text
    assert "-m argus.evalkit.replay" in text
    assert os.access(WRAPPER, os.X_OK)
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def fake_tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\n"
                      f"echo \"docker $*\" >> {log}\n"
                      "[ \"$4\" = ANTHROPIC_AUTH_TOKEN ] && echo copied-model-token\n"
                      "[ \"$4\" = GITHUB_TOKEN ] && echo copied-github-token\n"
                      "exit 0\n")
    python = bin_dir / "python"
    python.write_text("#!/usr/bin/env bash\n"
                      "if [ \"$1\" = -c ]; then\n"
                      f"  echo \"import check PYTHONPATH=${{PYTHONPATH:-}}\" >> {log}\n"
                      "  printf %s \"$FAKE_ARGUS_FILE\"\n"
                      "  exit 0\n"
                      "fi\n"
                      f"echo \"python $*\" >> {log}\n"
                      f"echo \"replay PYTHONPATH=${{PYTHONPATH:-}}\" >> {log}\n"
                      f"echo \"model=$ANTHROPIC_AUTH_TOKEN eval=${{ARGUS_EVAL_AUTH_TOKEN:-}}\" >> {log}\n"
                      f"echo \"mirrors=${{ARGUS_MIRROR_ROOT:-}} cache=${{ARGUS_EVAL_CACHE:-}}\" >> {log}\n")
    for tool in (docker, python):
        tool.chmod(0o755)
    return bin_dir, python, log


CHECKOUT_SOURCE = REPO_ROOT.resolve() / "src"


def run_wrapper(tmp_path, extra_env, check=True):
    bin_dir, python, log = fake_tools(tmp_path)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
           "ARGUS_REPLAY_PY": str(python), "ARGUS_MIRROR_ROOT": "/durable/mirrors",
           "ARGUS_EVAL_CACHE": "/durable/cache",
           "FAKE_ARGUS_FILE": str(CHECKOUT_SOURCE / "argus/__init__.py"), **extra_env}
    result = subprocess.run(["bash", str(WRAPPER), "--cases", "c.jsonl", "--out", "o.jsonl",
                             "--variant", "v"], env=env, capture_output=True, text=True,
                            check=check)
    return result, log.read_text() if log.exists() else ""


def test_replay_wrapper_runs_the_source_of_its_checkout(tmp_path):
    _, calls = run_wrapper(tmp_path, {"ARGUS_EVAL_AUTH_TOKEN": "eval-token",
                                      "GITHUB_TOKEN": "github-token",
                                      "PYTHONPATH": "/caller/path"})
    assert f"import check PYTHONPATH={CHECKOUT_SOURCE}:/caller/path" in calls
    assert f"replay PYTHONPATH={CHECKOUT_SOURCE}:/caller/path" in calls


def test_replay_wrapper_refuses_a_venv_that_loads_other_code(tmp_path):
    other = tmp_path / "site-packages/argus/__init__.py"
    result, calls = run_wrapper(tmp_path, {"ARGUS_EVAL_AUTH_TOKEN": "eval-token",
                                           "FAKE_ARGUS_FILE": str(other)}, check=False)
    assert result.returncode == 1
    assert str(other) in result.stderr and str(CHECKOUT_SOURCE) in result.stderr
    assert "python -m argus.evalkit.replay" not in calls
    assert "docker" not in calls


def test_replay_wrapper_refuses_a_sibling_source_directory(tmp_path):
    sibling = Path(str(CHECKOUT_SOURCE) + "-old") / "argus/__init__.py"
    result, calls = run_wrapper(tmp_path, {"ARGUS_EVAL_AUTH_TOKEN": "eval-token",
                                           "FAKE_ARGUS_FILE": str(sibling)}, check=False)
    assert result.returncode == 1
    assert "python -m argus.evalkit.replay" not in calls


def test_replay_wrapper_prefers_the_eval_credential(tmp_path):
    result, calls = run_wrapper(tmp_path, {"ARGUS_EVAL_AUTH_TOKEN": "eval-token",
                                           "GITHUB_TOKEN": "github-token"})
    assert "docker" not in calls
    assert "python -m argus.evalkit.replay --cases c.jsonl --out o.jsonl --variant v" in calls
    assert "eval=eval-token" in calls
    assert "mirrors=/durable/mirrors cache=/durable/cache" in calls
    assert "ARGUS_EVAL_AUTH_TOKEN" in result.stderr and "eval-token" not in result.stderr


def test_replay_wrapper_never_traces_the_token(tmp_path):
    result, calls = run_wrapper(tmp_path, {"SHELLOPTS": "xtrace"})
    assert "model=copied-model-token" in calls
    assert "copied-" not in result.stderr + result.stdout


def test_replay_wrapper_copies_the_production_credential_into_the_environment(tmp_path):
    result, calls = run_wrapper(tmp_path, {})
    assert "docker exec argus-api printenv ANTHROPIC_AUTH_TOKEN" in calls
    assert "model=copied-model-token eval=" in calls
    python_line = next(line for line in calls.splitlines() if line.startswith("python "))
    assert "copied-" not in python_line
    assert "ANTHROPIC_AUTH_TOKEN" in result.stderr
    assert "copied-" not in result.stderr + result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_replay_wrapper_passes_shellcheck():
    subprocess.run(["shellcheck", str(WRAPPER)], check=True)


_REAL_SNAPSHOT_GET = replay.SnapshotStore.get


def _pr_mirror(tmp_path):
    """A PR of two commits, then house rules rewritten on the default branch after it."""
    work, mirrors = tmp_path / "work", tmp_path / "mirrors"
    work.mkdir()
    mirrors.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(work), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (work / "CLAUDE.md").write_text("rules at the pull request base\n")
    (work / "a.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "pr")
    for text, message in (("two\n", "feat: first change\n\nbody line"), ("three\n", "fix: second change")):
        (work / "a.txt").write_text(text)
        git("commit", "-qam", message)
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (work / "CLAUDE.md").write_text("rules written after the pull request\n")
    git("commit", "-qam", "docs: later rules")
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(mirrors / "project.git")], check=True)
    return mirrors, base, head


def _replay_prompts(tmp_path, monkeypatch):
    mirrors, base, head = _pr_mirror(tmp_path)
    monkeypatch.setattr(replay.SnapshotStore, "get", _REAL_SNAPSHOT_GET)
    prompts = []
    def complete(model, system, user, tool):
        prompts.append(user)
        return {"findings": []}
    monkeypatch.setattr(review, "complete_tool", complete)
    data = {**case_data(), "base_sha": base, "head_sha": head, "pr_title": "title", "pr_body": "body"}
    cases = write_cases(tmp_path / "cases", [data])
    out = tmp_path / "results"
    assert replay.main(["--cases", str(cases), "--out", str(out), "--variant", "x",
                        "--mirror-root", str(mirrors), "--cache-dir", str(tmp_path / "cache")]) == 0
    assert json.loads(out.read_text())["error"] is None
    return prompts


def test_mirror_replay_reads_house_rules_at_the_pull_request_base(tmp_path, monkeypatch):
    prompts = _replay_prompts(tmp_path, monkeypatch)
    assert len(prompts) == 1
    assert "rules at the pull request base" in prompts[0]
    assert "rules written after the pull request" not in prompts[0]


def test_mirror_replay_passes_pull_request_commit_subjects_oldest_first(tmp_path, monkeypatch):
    prompts = _replay_prompts(tmp_path, monkeypatch)
    assert "Commits:\n- feat: first change\n- fix: second change" in prompts[0]
    assert "body line" not in prompts[0]
    assert "docs: later rules" not in prompts[0]


def test_replay_review_call_receives_the_range_commit_messages(tmp_path, monkeypatch):
    mirrors, base, head = _pr_mirror(tmp_path)
    monkeypatch.setattr(replay.SnapshotStore, "get", _REAL_SNAPSHOT_GET)
    received = []
    def run(files, repo, head_sha, model, bug_cap, *, commits):
        received.append(commits)
        return review.ReviewResult(raw_count=0)
    monkeypatch.setattr(review, "run_diff_review", run)
    cases = write_cases(tmp_path / "cases", [{**case_data(), "base_sha": base, "head_sha": head}])
    replay.main(["--cases", str(cases), "--out", str(tmp_path / "results"), "--variant", "x",
                 "--mirror-root", str(mirrors), "--cache-dir", str(tmp_path / "cache")])
    assert received == [["feat: first change\n\nbody line", "fix: second change"]]


def test_house_rules_outside_a_replayed_case_still_read_the_mirror_head(tmp_path):
    mirrors, _, _ = _pr_mirror(tmp_path)
    restore = replay.install_mirror_mode(mirrors)
    try:
        assert review.fetch_house_rules("owner/project") == "rules written after the pull request\n"
    finally:
        restore()


def test_replay_of_a_change_without_a_pull_request_passes_no_commits(tmp_path, monkeypatch):
    mirrors, base, head = _pr_mirror(tmp_path)
    monkeypatch.setattr(replay.SnapshotStore, "get", _REAL_SNAPSHOT_GET)
    received = []
    def run(files, repo, head_sha, model, bug_cap, *, commits):
        received.append(commits)
        return review.ReviewResult(raw_count=0)
    monkeypatch.setattr(review, "run_diff_review", run)
    data = {**case_data(), "pr": 0, "base_sha": base, "head_sha": head}
    cases = write_cases(tmp_path / "cases", [data])
    replay.main(["--cases", str(cases), "--out", str(tmp_path / "results"), "--variant", "x",
                 "--mirror-root", str(mirrors), "--cache-dir", str(tmp_path / "cache")])
    assert received == [None]


def test_mirror_file_hook_decodes_like_the_live_reader(tmp_path):
    work, mirrors = tmp_path / "work", tmp_path / "mirrors"
    work.mkdir()
    mirrors.mkdir()
    content = b"\0head\xffbytes\n"
    (work / "data.bin").write_bytes(content)
    subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(work), "add", "data.bin"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=test@example.invalid",
                    "-c", "user.name=Test", "commit", "-qm", "data"], check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(mirrors / "project.git")], check=True)
    restore = replay.install_mirror_mode(mirrors)
    try:
        assert live._fetch_file_at("owner/project", "data.bin", "HEAD") == live.decode_file_text(content)
    finally:
        restore()
