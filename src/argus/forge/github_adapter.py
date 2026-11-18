"""Preserve the existing GitHub readers, writers and their runtime patch points."""


class GitHubForge:
    forge = "github"
    supports_review_submit = True
    supports_auto_invite = True
    supports_outdated_flag = False
    supports_html_markers = False
    supports_drafts = False

    def change_files(self, repo: str, number: int):
        import argus.diff as _diff

        return _diff.fetch_pr_files(repo, number)

    def compare(self, repo, old, head):
        import argus.github as _github

        response = _github.get(f"/repos/{repo}/compare/{old}...{head}")
        response.raise_for_status()
        return response.json()

    def publish(self, repo, pr, head_sha, review_id, document, files, **kwargs):
        import argus.publish as _publish

        return _publish.publish_review(repo, pr, head_sha, review_id, document, files, **kwargs)

    def bot_identity(self, *args, **kwargs):
        import argus.publish as _publish

        return _publish._bot_login(*args, **kwargs)

    def check_access(self, repo, **kwargs):
        import argus.publish_access as _access

        return _access.check_access(repo, **kwargs)

    def pr_meta(self, repo: str, number: int) -> dict:
        """Return the pipeline's existing metadata dict.

        The typed ChangeMeta arrives with the GitLab reader.
        """
        import argus.live as _live

        return _live._pr_meta(repo, number)

    def head_sha(self, repo: str, number: int) -> str:
        import argus.live as _live

        return _live._pr_head_sha(repo, number)

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        import argus.live as _live

        return _live._fetch_file_at(repo, path, ref)

    def house_rules(self, repo: str) -> str | None:
        import argus.review as _review

        return _review.fetch_house_rules(repo)

    def file_reader(self, repo: str, head_sha: str | None, file_loader=None):
        import argus.review as _review

        # A runtime replacement of _file_reader may take only (repo, head_sha).
        if file_loader is None:
            return _review._file_reader(repo, head_sha)
        return _review._file_reader(repo, head_sha, file_loader)

    def diff_refs(self, repo: str, number: int) -> None:
        return None

    def classify(self, exc: Exception) -> str:
        import argus.errors as _errors

        return _errors.classify_error(exc)


github_forge = GitHubForge()
