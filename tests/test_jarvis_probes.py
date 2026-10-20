"""Behaviour tests for the Jarvis probe engine (glob + regex + decay log)."""

import sqlite3

import pytest

from argus.jarvis import corpus, probes
from argus.jarvis.probes import FileProbe, Probe


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_run_probe_matches_glob_and_pattern_with_line_numbers(tmp_path):
    _write(tmp_path, "deploy/nginx.conf", "server {\n  allow 127.0.0.1;\n}\n")
    probe = Probe("loopback_trust", "**/nginx*.conf", r"allow\s+127\.", "critical")

    hits = probes.run_probe(probe, str(tmp_path))

    assert len(hits) == 1
    assert hits[0].file == "deploy/nginx.conf"
    assert hits[0].line == 2
    assert "127.0.0.1" in hits[0].text


def test_run_probe_no_hit_when_pattern_absent(tmp_path):
    _write(tmp_path, "deploy/nginx.conf", "server { allow 10.8.0.1; }\n")
    probe = Probe("loopback_trust", "**/nginx*.conf", r"allow\s+127\.", "critical")
    assert probes.run_probe(probe, str(tmp_path)) == []


def test_run_probe_respects_glob_scope(tmp_path):
    # The pattern is present, but in a file the glob does not select.
    _write(tmp_path, "notes.txt", "allow 127.0.0.1;\n")
    probe = Probe("loopback_trust", "**/nginx*.conf", r"allow\s+127\.", "critical")
    assert probes.run_probe(probe, str(tmp_path)) == []


def test_run_probe_glob_matches_deeply_nested(tmp_path):
    _write(tmp_path, "app/api/checkout/route.ts", "const p = body.unitPriceToman;\n")
    probe = Probe("client_price", "**/api/**/route.ts", r"body\.\w*[Pp]rice", "critical")
    hits = probes.run_probe(probe, str(tmp_path))
    assert len(hits) == 1
    assert hits[0].file == "app/api/checkout/route.ts"


def test_probe_skips_vendor_dirs(tmp_path):
    # The only match is under node_modules, which must be excluded from scans.
    _write(tmp_path, "node_modules/pkg/api/checkout/route.ts", "const p = body.unitPrice;\n")
    _write(tmp_path, "src/api/checkout/route.ts", "const safe = 1;\n")
    probe = Probe("client_price", "**/route.ts", r"body\.\w*[Pp]rice", "critical")
    assert probes.run_probe(probe, str(tmp_path)) == []


def test_scan_contents_matches_in_memory_like_disk():
    # Live review has file CONTENT (from the API), not a checkout. scan_contents
    # must give the same result the disk engine would, for both probe kinds.
    files = {
        "deploy/nginx.conf": "allow 127.0.0.1;\nset_real_ip_from 192.168.0.0/16;\n",
        "svc/docker-compose.yml": "command: uvicorn app --host 0.0.0.0\n",
    }
    line_probe = Probe("bind", "**/docker-compose*.yml", r"--host\s+0\.0\.0\.0", "major")
    file_probe = FileProbe("gate", "**/nginx*.conf", r"allow\s+127\.", r"set_real_ip_from", "critical")
    hits = probes.scan_contents([line_probe, file_probe], files)
    ids = {h.probe_id for h in hits}
    assert "bind" in ids        # line probe fires on the compose bind
    assert "gate" not in ids    # file probe silent: the fix marker is present


def test_probe_skips_test_fixture_dirs(tmp_path):
    # A vendored fixture is outside the application being reviewed.
    _write(tmp_path, "tests/golden/bundle/docker-compose.yml", 'command: --host 0.0.0.0\n')
    probe = Probe("bind", "**/docker-compose*.yml", r"--host\s+0\.0\.0\.0", "major")
    assert probes.run_probe(probe, str(tmp_path)) == []


@pytest.fixture
def jarvis_path(tmp_path) -> str:
    path = str(tmp_path / "jarvis.db")
    corpus.init_corpus_db(path)
    return path


def test_run_probes_logs_path_matched_and_fired(tmp_path, jarvis_path):
    _write(tmp_path, "deploy/nginx.conf", "allow 127.0.0.1;\n")  # will fire
    _write(tmp_path, "src/app.py", "x = 1\n")                    # glob matches, no fire
    fires = Probe("loopback_trust", "**/nginx*.conf", r"allow\s+127\.", "critical")
    quiet = Probe("debug_print", "**/*.py", r"console\.log", "major")  # matches .py, no hit
    nomatch = Probe("no_files", "**/*.rs", r"unsafe", "major")          # glob matches nothing

    probes.run_probes([fires, quiet, nomatch], str(tmp_path), log_path=jarvis_path)

    conn = sqlite3.connect(jarvis_path)
    conn.row_factory = sqlite3.Row
    rows = {r["probe_id"]: r for r in
            conn.execute("SELECT probe_id, path_matched, fired FROM probe_eval_log")}
    assert (rows["loopback_trust"]["path_matched"], rows["loopback_trust"]["fired"]) == (1, 1)
    assert (rows["debug_print"]["path_matched"], rows["debug_print"]["fired"]) == (1, 0)
    assert (rows["no_files"]["path_matched"], rows["no_files"]["fired"]) == (0, 0)


def test_file_probe_fires_when_present_and_fix_marker_absent(tmp_path):
    _write(tmp_path, "app/admin/page.tsx",
           "export default async function P(){ const o = await db.select(); }\n")
    probe = FileProbe("admin_guard", "**/admin/**/*.tsx", r"db\.select", r"requireAdmin", "critical")
    hits = probes.run_probe(probe, str(tmp_path))
    assert len(hits) == 1
    assert hits[0].file == "app/admin/page.tsx"


def test_file_probe_silent_when_fix_marker_present(tmp_path):
    _write(tmp_path, "app/admin/page.tsx",
           "export default async function P(){ await requireAdmin(); const o = await db.select(); }\n")
    probe = FileProbe("admin_guard", "**/admin/**/*.tsx", r"db\.select", r"requireAdmin", "critical")
    assert probes.run_probe(probe, str(tmp_path)) == []


def test_file_probe_silent_when_present_pattern_absent(tmp_path):
    _write(tmp_path, "app/admin/page.tsx", "export default function P(){ return null; }\n")
    probe = FileProbe("admin_guard", "**/admin/**/*.tsx", r"db\.select", r"requireAdmin", "critical")
    assert probes.run_probe(probe, str(tmp_path)) == []


def test_file_probe_reports_line_of_present_match(tmp_path):
    _write(tmp_path, "pwa/sw.js", "self.addEventListener('fetch', e => {\n  e.respondWith(x);\n});\n")
    probe = FileProbe("sw_nav", "**/sw.js", r"respondWith", r"mode === 'navigate'", "major")
    hits = probes.run_probe(probe, str(tmp_path))
    assert len(hits) == 1
    assert hits[0].line == 2
