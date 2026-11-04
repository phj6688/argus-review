"""Finding selection and automatic marks through the public route handlers."""

import base64
import json

from argus.api import db, main
from argus.jarvis import automark

from .test_golden_acceptance import HEAD, REPO, seed_review


PROBE_PATH = "app/api/checkout/place/route.ts"
PROBE_CONTENT = (
    "const s = z.object({ unitPriceToman: z.number().int() });\n"
    "export async function POST(req) { return createOrder(s); }\n"
)


def _source(**filters):
    summaries = main.list_reviews()["reviews"]
    chosen = automark._select_reviews(summaries, **filters)
    return [finding for row in chosen
            for finding in automark._findings_from_detail(main.get_review(row["id"]))]


def _judge(golden, outputs):
    def judge(payload):
        golden.llm_calls.append({
            "lane": "judge", "model": None, "system": None,
            "messages": payload, "tools": None, "tool_choice": None,
            "max_tokens": None, "extra": {},
        })
        return outputs[payload["title"]]
    return judge


def _contents(transport, contents):
    transport.add("GET", f"/repos/{REPO}/pulls/3", (200, {"head": {"sha": HEAD}}, {}))
    for path, content in contents.items():
        body = ({"message": "Not Found"} if content is None else {
            "encoding": "base64", "content": base64.b64encode(content.encode()).decode(),
        })
        transport.add("GET", f"/repos/{REPO}/contents/{path}",
                      (404 if content is None else 200, body, {}), query={"ref": HEAD})


def test_select_and_mark(golden, transport, db_path):
    review_id, _ = seed_review(db_path, [
        {"file": PROBE_PATH, "line": 1, "title": "Client price is trusted"},
        {"file": "missing.py", "title": "Missing file finding"},
        {"file": "short.txt", "line": 99, "end_line": None, "title": "Stale line finding"},
        {"file": "app.py", "title": "Missing input check"},
        {"file": "uncertain.py", "title": "Uncertain input check"},
        {"file": "marked.py", "title": "Already classified"},
    ])
    seed_review(db_path, repo="acme-org/other", pr=8)
    db.add_mark(db_path, db.get_findings(db_path, review_id)[-1]["id"], "fp",
                marker="maintainer", note="The caller validates input.")
    _contents(transport, {
        PROBE_PATH: PROBE_CONTENT, "missing.py": None, "short.txt": "one\ntwo\n",
        "app.py": "def parse_input(value):\n    return parse(value)\n",
        "uncertain.py": "def parse_input(value):\n    return helper(value)\n",
    })
    writes = []

    def write_mark(finding_id, mark, *, marker, note):
        response = main.mark_finding(finding_id, main.MarkRequest(mark=mark, marker=marker, note=note))
        writes.append(response)
        return response["id"]

    report = automark.run(
        repo=REPO, pr=3, since=1, commit=True, list_findings=_source,
        judge=_judge(golden, {
            "Missing input check": {"verdict": "real", "confidence": 0.92,
                                    "evidence": "parse(value) receives unchecked input."},
            "Uncertain input check": {"verdict": "real", "confidence": 0.5,
                                      "evidence": "The helper implementation is unavailable."},
        }), write_mark=write_mark,
    )
    transport.assert_all_used()
    golden.check("automark", "select-and-mark", {"report": report, "mark_responses": writes})


def test_needs_human_queue(golden, transport, db_path, tmp_path):
    seed_review(db_path, [
        {"file": "app.py", "title": "Uncertain input check"},
        {"file": "worker.py", "title": "Unknown caller contract"},
    ])
    _contents(transport, {
        "app.py": "def parse_input(value):\n    return helper(value)\n",
        "worker.py": "def work(value):\n    return consume(value)\n",
    })
    report = automark.run(
        repo=REPO, pr=3, since=1, commit=False, list_findings=_source,
        judge=_judge(golden, {
            "Uncertain input check": {"verdict": "real", "confidence": 0.6,
                                      "evidence": "The helper implementation is unavailable."},
            "Unknown caller contract": {"verdict": "unsure", "confidence": 0.99,
                                        "evidence": "The caller contract is not visible."},
        }),
    )
    queue_path = automark.write_needs_human_queue(report, str(tmp_path / "needs-human.jsonl"))
    with open(queue_path, encoding="utf-8") as handle:
        queue_text = handle.read()
    transport.assert_all_used()
    golden.check("automark", "needs-human-queue", {
        "report": report, "queue_path": queue_path, "queue_text": queue_text,
        "queue_rows": [json.loads(line) for line in queue_text.splitlines()],
    })
