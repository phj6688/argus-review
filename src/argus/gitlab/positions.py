"""Build GitLab text positions from the new side of a file patch."""

import hashlib
import re

from argus.diff import FilePatch, split_lines


MULTI_LINE_ENABLED = False
_REF_KEYS = ("base_sha", "start_sha", "head_sha")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _positive_line(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _new_lines(patch: str) -> dict[int, tuple[int, int | None]]:
    lines = {}
    hunk = 0
    old = new = old_left = new_left = 0
    for line in split_lines(patch):
        header = _HUNK_HEADER.match(line)
        if header:
            hunk += 1
            old, new = int(header[1]), int(header[3])
            old_left = int(header[2]) if header[2] is not None else 1
            new_left = int(header[4]) if header[4] is not None else 1
            continue
        if not (old_left or new_left) or line.startswith("\\"):
            continue
        # Counts distinguish file headers from hunk content beginning with -- or ++.
        if line.startswith("+") and new_left:
            lines[new] = (hunk, None)
            new += 1
            new_left -= 1
        elif line.startswith("-") and old_left:
            old += 1
            old_left -= 1
        elif (not line or line.startswith(" ")) and old_left and new_left:
            lines[new] = (hunk, old)
            old += 1
            new += 1
            old_left -= 1
            new_left -= 1
    return lines


def _line_fields(new: int, old: int | None) -> dict:
    return {"new_line": new} if old is None else {"old_line": old, "new_line": new}


def position_for(finding: dict, file_patch: FilePatch, diff_refs: dict) -> dict | None:
    """Return an anchorable new-side position, preserving the caller's diff refs."""
    for key in _REF_KEYS:
        if not diff_refs.get(key):
            raise ValueError(f"Missing diff ref: {key}")

    target = finding.get("end_line")
    if target is None:
        target = finding.get("line")
    if not _positive_line(target) or file_patch.status == "removed" or not file_patch.patch:
        return None

    lines = _new_lines(file_patch.patch)
    if target not in lines:
        return None
    hunk, old = lines[target]
    position = {
        "position_type": "text",
        **{key: diff_refs[key] for key in _REF_KEYS},
        "new_path": file_patch.filename,
        "old_path": file_patch.previous_filename or file_patch.filename,
        **_line_fields(target, old),
    }
    start = finding.get("line")
    if (MULTI_LINE_ENABLED and _positive_line(start) and start < target
            and start in lines and lines[start][0] == hunk):
        path_hash = hashlib.sha1(file_patch.filename.encode(), usedforsecurity=False).hexdigest()  # GitLab's line_code format, not a security use
        position["line_range"] = {
            endpoint: {
                "line_code": f"{path_hash}_{lines[number][1] or 0}_{number}",
                "type": "new",
                **_line_fields(number, lines[number][1]),
            }
            for endpoint, number in (("start", start), ("end", target))
        }
    return position
