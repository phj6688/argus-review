"""Trivial full lines satisfy the full-line quote rule. Fakes only."""
import base64

import httpx
import pytest

from argus import client, github, replies
import test_replies as fixtures
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck
API = "https://api.github.com"


@pytest.mark.parametrize("quote", ["}", "return", "else:", ")"])
def test_trivial_added_line_cannot_establish_fix(live, monkeypatch, quote):
    code = ["x = 1"] * 33 + ["    if (ok) {", "        run(eval(input))", "    " + quote] + ["y = 2"] * 40

    def fake_get(url, params=None):
        content = base64.b64encode("\n".join(code).encode()).decode()
        return httpx.Response(200, json=dict(encoding="base64", content=content), request=httpx.Request("GET", API + url))

    monkeypatch.setattr(github, "get", fake_get)
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    finding = dict(path="app.py", head_sha="a" * 40, line=34, end_line=36, evidence="",
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py",
                       patch="@@ -36 +36 @@\n-original_line\n+    " + quote)]))
    result = REAL_RECHECK("owner/repo", finding, "b" * 40)
    assert result == {"verdict": "unsure", "quote": ""}
