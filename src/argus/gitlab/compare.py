"""Normalize GitLab comparisons while preserving ancestry and coverage limits."""

from argus.gitlab.client import GitLabDenied, GitLabHTTPError
from argus.gitlab.reader import _payload, map_diff_entries


def compare(client, ref, old, head) -> dict:
    if old == head:
        return {"status": "identical", "files": [], "complete": True}
    try:
        data = _payload(client.get("compare", project_id=ref.project_id,
                                   query={"from": old, "to": head}), dict)
        diffs = data.get("diffs")
        if not isinstance(diffs, list) or any(not isinstance(item, dict) for item in diffs):
            raise GitLabHTTPError("Invalid GitLab compare response", reason="http")
        base = _payload(client.get("merge_base", project_id=ref.project_id,
                                   query={"refs[]": [old, head]}), dict)
        if not isinstance(base.get("id"), str):
            raise GitLabHTTPError("Invalid GitLab merge base response", reason="http")
    except GitLabDenied as error:
        if error.reason == "not_found":
            return {"status": "diverged", "files": [], "complete": False}
        raise
    mapped, _, hidden = map_diff_entries(diffs)
    files = []
    for file in mapped:
        lines = (file.patch or "").split("\n")
        files.append({
            "filename": file.filename, "previous_filename": file.previous_filename,
            "status": file.status, "patch": file.patch,
            "additions": sum(line.startswith("+") for line in lines),
            "deletions": sum(line.startswith("-") for line in lines),
        })
    return {
        "status": "ahead" if base["id"] == old else "diverged", "files": files,
        "complete": not (data.get("compare_timeout") or len(diffs) >= 1000 or hidden),
    }
