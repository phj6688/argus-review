import re

import httpx
import pytest

from argus.gitlab import body, guard
from argus.gitlab.writer import GitLabWriter
from test_gitlab_body import finding
from test_gitlab_client import configured as configured, make_client as make_client
from test_gitlab_writer import DISCUSSION, POSITION


@pytest.mark.parametrize("path", ["/usr/bin/env", "/path/to/file.py"])
def test_path_lines_are_neutralized(path):
    assert body.neutralize_quick_actions(path) == "\\" + path
    rendered = body.render_gitlab_comment(finding(evidence=path, description="Example:\n" + path))
    assert "\\" + path in rendered
    body.assert_body_safe(rendered)
    assert not any(line.lstrip().startswith("/") for line in rendered.splitlines())


@pytest.mark.parametrize("field", ["evidence", "description"])
@pytest.mark.parametrize("fence", ["```", "`````", "~~~", "~~~~~", "   ```", "\t~~~"])
@pytest.mark.parametrize("space", ["", "\u00a0", "\u200b", "\u2060", "\ufeff", "\u2003"])
@pytest.mark.parametrize("command", ["/approve", "/merge"])
def test_fence_breakout_cannot_expose_quick_actions(field, fence, space, command):
    rendered = body.render_gitlab_comment(finding(**{field: "```\ncode\n" + fence + "\n" + space + command}))
    body.assert_body_safe(rendered)
    assert not re.search(r"(?m)^[\s\u200b\u200c\u200d\u2060\ufeff]*/", rendered)
    assert "\\" + command in rendered


def test_discussion_writer_never_uses_silently_dropping_drafts(make_client):
    def handle(request):
        if request.url.path.endswith("/draft_notes/bulk_publish"):
            return httpx.Response(204)
        return (httpx.Response(201, json={"id": DISCUSSION, "notes": [{"id": 123}]})
                if len(requests) == 1 else httpx.Response(400))

    client, requests = make_client(handle)
    writer = GitLabWriter(client, 7)
    assert writer.post_discussion(42, 3, "Finding", POSITION).outcome == "posted"
    assert writer.post_discussion(42, 3, "Finding", POSITION | {"new_line": 999}).outcome == "failed"
    assert len(requests) == 2
    assert all(request.method == "POST" and "draft" not in request.url.path for request in requests)
    assert all("draft" not in route.template for route in guard.ROUTES.values())
