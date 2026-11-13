"""Read GitLab changes without overstating revision stability or diff coverage."""

import re

from argus.diff import FileList, FilePatch, _safe_path
from argus.errors import HeadMoved
from argus.forge.errors import ForgeDenied
from argus.forge.keys import canonical_host, parse_repo_key
from argus.forge.types import ChangeMeta, DiffRefs, RepoRef
from argus.gitlab import client as client_module, config
from argus.gitlab.client import GitLabDenied, GitLabHTTPError

HOUSE_RULE_FILES = ("CLAUDE.md",)


def _payload(response, kind):
    try:
        data = response.json()
    except ValueError:
        raise GitLabHTTPError("Invalid GitLab reader response", reason="http") from None
    if not isinstance(data, kind):
        raise GitLabHTTPError("Invalid GitLab reader response", reason="http")
    return data


def _head(data):
    sha = data.get("sha")
    if not isinstance(sha, str) or not sha:
        raise HeadMoved("GitLab merge request head is unavailable")
    return sha


def _count_unseen(count, listed):
    if type(count) not in (str, int) or not re.fullmatch(r"[0-9]+", str(count)):
        return True
    try:
        return listed < int(count)
    except ValueError:
        return True


def map_diff_entries(entries) -> tuple[list[FilePatch], bool, bool]:
    files = []
    unseen = hidden = False
    for item in entries:
        path = item.get("new_path") or item.get("old_path")
        previous = item.get("old_path") if item.get("renamed_file") else None
        if (not isinstance(path, str) or not _safe_path(path)
                or (previous and not _safe_path(previous))):
            unseen = hidden = True
            continue
        patch = item.get("diff") or ""
        while patch.startswith(("--- ", "+++ ")):
            patch = patch.partition("\n")[2]
        binary = not re.search(r"(?m)^@@", patch) and re.search(r"(?m)^Binary files .+ differ\r?$", patch)
        pure_mode_change = (not item.get("new_file") and not item.get("deleted_file")
                            and item.get("a_mode") != "0" and item.get("b_mode") != "0"
                            and item.get("a_mode") != item.get("b_mode"))
        legacy_empty = ("collapsed" not in item and "too_large" not in item and not patch
                        and not item.get("renamed_file") and not pure_mode_change)
        omitted = any(item.get(flag) for flag in ("too_large", "collapsed", "generated_file"))
        if omitted or legacy_empty:
            hidden = True
        if omitted or binary or legacy_empty:
            unseen = True
            patch = None
        status = ("added" if item.get("new_file") else "removed" if item.get("deleted_file")
                  else "renamed" if item.get("renamed_file") else "modified")
        files.append(FilePatch(path, status, patch or None, previous_filename=previous))
    return files, unseen, hidden


class GitLabForge:
    forge = "gitlab"
    supports_review_submit = False
    supports_auto_invite = False
    supports_outdated_flag = False
    supports_html_markers = False
    supports_drafts = False

    def __init__(self, client=None):
        self._client = client

    def _connection(self, repo):
        ref = parse_repo_key(repo)
        try:
            base = config.base_url()
        except config.GitLabConfigError:
            raise ForgeDenied("GitLab is not configured") from None
        if base is None:
            raise ForgeDenied("GitLab is not configured")
        if ref.forge != self.forge or ref.host != canonical_host(base.host, base.port):
            raise ForgeDenied("GitLab repository host does not match configuration")
        client = self._client if self._client is not None else client_module.client()
        if client is None:
            raise ForgeDenied("GitLab is not configured")
        return client, ref

    def _mr(self, repo, number):
        client, ref = self._connection(repo)
        return _payload(client.get("mr", project_id=ref.project_id, iid=number), dict)

    def change_meta(self, repo: str, number: int) -> ChangeMeta:
        data = self._mr(repo, number)
        state = data.get("state")
        if state not in ("opened", "locked", "closed", "merged"):
            raise GitLabHTTPError("Invalid GitLab merge request state", reason="http")
        author = data.get("author") or {}
        source_id = data.get("source_project_id")
        if source_id is not None and (type(source_id) is not int or not 1 <= source_id < 10**18):
            raise GitLabHTTPError("Invalid GitLab merge request source project", reason="http")
        return ChangeMeta(
            head_sha=data.get("sha"), base_sha=(data.get("diff_refs") or {}).get("base_sha"),
            state="open" if state in ("opened", "locked") else "closed", merged=state == "merged",
            draft=bool(data.get("draft", data.get("work_in_progress", False))),
            author_id=str(author["id"]) if author.get("id") is not None else None,
            author_login=author.get("username"), author_is_bot=author.get("bot") is True,
            title=data.get("title") or "", updated_at=data.get("updated_at"),
            source_repo=RepoRef("gitlab", parse_repo_key(repo).host, None, source_id)
            if source_id is not None else None,
        )

    def pr_meta(self, repo: str, number: int) -> dict:
        data = self._mr(repo, number)
        return {
            "title": data.get("title") or "", "body": data.get("description") or "",
            "commits": [], "head_sha": data.get("sha"), "head_committed_at": None,
            "author_association": None, "branch": data.get("source_branch"),
            "author": (data.get("author") or {}).get("username"),
        }

    def outcome_meta(self, repo: str, number: int) -> dict:
        data = self._mr(repo, number)
        state = data.get("state")
        if state not in ("opened", "locked", "closed", "merged") or not isinstance(data.get("sha"), str):
            raise GitLabHTTPError("Invalid GitLab merge request outcome metadata", reason="http")
        return {
            "state": "open" if state in ("opened", "locked") else "closed",
            "merged": state == "merged", "closed_at": data.get("closed_at") or data.get("merged_at"),
            "head_sha": data["sha"],
        }

    def head_sha(self, repo: str, number: int) -> str:
        return _head(self._mr(repo, number))

    def _version(self, repo, number, sha):
        client, ref = self._connection(repo)
        response = client.get("mr_versions", project_id=ref.project_id, iid=number,
                              query={"per_page": 100})
        versions = _payload(response, list)
        for version in versions:
            if (not isinstance(version, dict) or type(version.get("id")) is not int
                    or (version.get("head_commit_sha") is not None
                        and not isinstance(version["head_commit_sha"], str))):
                raise GitLabHTTPError("Invalid GitLab reader response", reason="http")
        # Diffs always serve the newest version, so an older matching head is unsafe.
        newest = max(versions, key=lambda v: v["id"], default=None)
        if newest is not None and newest.get("head_commit_sha") == sha:
            if any(not isinstance(newest.get(field), str) or not newest[field]
                   for field in ("base_commit_sha", "start_commit_sha", "head_commit_sha")):
                raise GitLabHTTPError("Invalid GitLab reader response", reason="http")
            return newest
        raise HeadMoved("GitLab merge request version is unavailable for the head")

    def diff_refs(self, repo: str, number: int) -> DiffRefs:
        version = self._version(repo, number, self.head_sha(repo, number))
        return DiffRefs(base_sha=version["base_commit_sha"], start_sha=version["start_commit_sha"],
                        head_sha=version["head_commit_sha"])

    def change_files(self, repo: str, number: int) -> FileList:
        data = self._mr(repo, number)
        sha = _head(data)
        version = self._version(repo, number, sha)
        client, ref = self._connection(repo)
        entries = []
        for response in client.pages("mr_diffs", project_id=ref.project_id, iid=number,
                                     query={"per_page": 100}):
            page = _payload(response, list)
            if any(not isinstance(item, dict) for item in page):
                raise GitLabHTTPError("Invalid GitLab reader response", reason="http")
            entries.extend(page)
        # This endpoint cannot select a version, so reject a head that moved during pagination.
        if self.head_sha(repo, number) != sha:
            raise HeadMoved("GitLab merge request head changed while reading diffs")
        # The target can move without changing the head, so the version must also stay stable.
        latest = self._version(repo, number, sha)
        if any(latest[field] != version[field]
               for field in ("id", "base_commit_sha", "start_commit_sha", "head_commit_sha")):
            raise HeadMoved("GitLab merge request version changed while reading diffs")
        files, unseen, _ = map_diff_entries(entries)
        return FileList(files, unseen=unseen or _count_unseen(data.get("changes_count"), len(entries)))

    def compare(self, repo, old, head):
        client, ref = self._connection(repo)
        from argus.gitlab import compare

        return compare.compare(client, ref, old, head)

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        client, project = self._connection(repo)
        try:
            response = client.get("file_raw", project_id=project.project_id, file_path=path,
                                  query={"ref": ref})
        except GitLabDenied as error:
            if error.reason in {"not_found", "guard"}:
                return None
            raise
        return response.content.decode("utf-8", errors="replace")

    def house_rules(self, repo: str) -> str | None:
        import argus.review as _review

        client, ref = self._connection(repo)
        data = _payload(client.get("project", project_id=ref.project_id), dict)
        branch = data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            return None
        for name in HOUSE_RULE_FILES:
            content = self.read_file(repo, name, branch)
            if content is not None:
                return content[:_review.HOUSE_RULES_MAX_CHARS]
        return None

    def file_reader(self, repo: str, head_sha: str | None, file_loader=None):
        import argus.review as _review

        if file_loader is None:
            return _review._file_reader(repo, head_sha)
        return _review._file_reader(repo, head_sha, file_loader)

    def classify(self, exc: Exception) -> str:
        import argus.errors as _errors

        return _errors.classify_error(exc)

    def publish(self, repo, pr, head_sha, review_id, document, files, **kwargs):
        from argus.gitlab import publisher

        if self._client is not None:
            kwargs.setdefault("client", self._client)
        return publisher.publish(repo, pr, head_sha, review_id, document, files, **kwargs)

    def bot_identity(self, *args, **kwargs):
        from argus.gitlab import publisher

        if self._client is not None:
            kwargs.setdefault("client", self._client)
        return publisher.bot_identity(*args, **kwargs)

    def check_access(self, repo, **kwargs):
        from argus.gitlab import publisher

        if self._client is not None:
            kwargs.setdefault("client", self._client)
        return publisher.check_access(repo, **kwargs)


gitlab_forge = GitLabForge()
