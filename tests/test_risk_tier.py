import json
import re
from dataclasses import FrozenInstanceError

import pytest

from argus import risk_tier
from argus.diff import FileList, FilePatch


@pytest.fixture(autouse=True)
def default_environment(monkeypatch):
    monkeypatch.delenv("ARGUS_RISK_TRIPWIRES", raising=False)
    monkeypatch.delenv("ARGUS_RISK_MAX_LINES", raising=False)


def test_defaults_match_the_documented_rule():
    assert risk_tier.DEFAULT_TRIPWIRES == (
        "nginx*", "Caddyfile*", "traefik/", "cloudflared/", "docker-compose*",
        "compose.yml", "compose.yaml", "*auth*", "*proxy*",
    )
    assert risk_tier.DEFAULT_MAX_LINES == 300
    assert risk_tier.ENV_TRIPWIRES == "ARGUS_RISK_TRIPWIRES"
    assert risk_tier.ENV_MAX_LINES == "ARGUS_RISK_MAX_LINES"


@pytest.mark.parametrize("path,pattern", [
    ("deploy/docker-compose.prod.yml", "docker-compose*"),
    ("traefik/dynamic/apps.yml", "traefik/"),
    ("infra/nginx/conf.d/x.conf", "nginx*"),
    ("Caddyfile", "Caddyfile*"),
    ("Caddyfile.dev", "Caddyfile*"),
    ("cloudflared/config.yml", "cloudflared/"),
    ("compose.yml", "compose.yml"),
    ("compose.yaml", "compose.yaml"),
    ("src/auth/login.py", "*auth*"),
    ("api/proxy.ts", "*proxy*"),
    ("NGINX.conf", "nginx*"),
    ("Traefik/x.yml", "traefik/"),
    ("nested/CLOUDFLARED/config.yml", "cloudflared/"),
    ("nested/COMPOSE.YAML", "compose.yaml"),
    ("src/authors.md", "*auth*"),
    ("deploy/docker-compose/config.yml", "docker-compose*"),
    ("src/reverse-proxy/settings.py", "*proxy*"),
])
def test_default_tripwires(path, pattern):
    result = risk_tier.classify([path], 1).to_dict()
    assert result["tier"] == "risky"
    assert result["matches"] == [{"path": path, "pattern": pattern}]
    assert result["reasons"] == [f"tripwire path {path} ({pattern})"]


@pytest.mark.parametrize("path", [
    "src/compose_utils.py", "docs/traefik.md", "traefik", "cloudflared",
    "docs/cloudflared.md", "src/main.py", "Dockerfile", ".github/workflows/ci.yml",
    "deploy/my-nginx.conf", "deploy/my-Caddyfile", "src/my-compose.yml",
])
def test_safe_paths(path):
    result = risk_tier.classify([path], 1).to_dict()
    assert result["tier"] == "safe"
    assert result["matches"] == []
    assert result["reasons"] == []


@pytest.mark.parametrize("lines,tier", [(0, "safe"), (299, "safe"), (300, "safe"), (301, "risky")])
def test_size_boundary(lines, tier):
    assert risk_tier.classify(["src/main.py"], lines).tier == tier


def test_empty_change_and_frozen_result():
    result = risk_tier.classify([], 0)
    assert result.to_dict() == {
        "tier": "safe", "changed_lines": 0, "max_lines": 300, "files": 0,
        "matches": [], "matches_total": 0, "incomplete": False, "reasons": [],
    }
    with pytest.raises(FrozenInstanceError):
        result.tier = "risky"


def test_serialized_keys_and_fact_reasons():
    result = risk_tier.classify(["deploy/docker-compose.prod.yml"], 412, incomplete=True).to_dict()
    assert set(result) == {
        "tier", "changed_lines", "max_lines", "files", "matches", "matches_total",
        "incomplete", "reasons",
    }
    assert result["reasons"] == [
        "tripwire path deploy/docker-compose.prod.yml (docker-compose*)",
        "412 changed lines exceed the 300 line limit",
        "file list incomplete, an unseen path might match",
    ]


def test_incomplete_empty_change_is_risky():
    result = risk_tier.classify([], 0, incomplete=True).to_dict()
    assert result["tier"] == "risky"
    assert result["reasons"] == ["file list incomplete, an unseen path might match"]


def test_matches_and_path_reasons_are_capped_without_losing_totals():
    paths = [f"infra/auth-{index}/config.yml" for index in range(25)]
    result = risk_tier.classify(paths, 0).to_dict()
    assert len(result["matches"]) == 20
    assert result["matches_total"] == 25
    assert result["reasons"] == [
        f"tripwire path {path} (*auth*)" for path in paths[:5]
    ] + ["and 20 more tripwire paths"]


def test_one_path_reason_even_when_multiple_patterns_match():
    result = risk_tier.classify(["auth/proxy.py", "auth/proxy.py"], 0).to_dict()
    assert result["matches"] == [
        {"path": "auth/proxy.py", "pattern": "*auth*"},
        {"path": "auth/proxy.py", "pattern": "*proxy*"},
    ]
    assert result["matches_total"] == 2
    assert result["reasons"] == ["tripwire path auth/proxy.py (*auth*)"]


def test_tripwire_override_replaces_defaults_at_call_time(monkeypatch):
    assert risk_tier.tripwires() == risk_tier.DEFAULT_TRIPWIRES
    monkeypatch.setenv("ARGUS_RISK_TRIPWIRES", " *.sql, ,migrations/ , ")
    assert risk_tier.tripwires() == ("*.sql", "migrations/")
    assert risk_tier.classify(["NGINX.conf"], 0).tier == "safe"
    assert risk_tier.classify(["src/SCHEMA.SQL"], 0).tier == "risky"
    assert risk_tier.classify(["migrations/a.py"], 0).tier == "risky"
    assert risk_tier.classify(["migrations"], 0).tier == "safe"


@pytest.mark.parametrize("value", ["", "   ", ", , ,"])
def test_blank_override_falls_back(monkeypatch, value):
    monkeypatch.setenv("ARGUS_RISK_TRIPWIRES", value)
    assert risk_tier.tripwires() == risk_tier.DEFAULT_TRIPWIRES


def test_limit_override_at_call_time(monkeypatch):
    assert risk_tier.max_lines() == 300
    monkeypatch.setenv("ARGUS_RISK_MAX_LINES", " 12 ")
    assert risk_tier.max_lines() == 12
    assert risk_tier.classify([], 12).tier == "safe"
    assert risk_tier.classify([], 13).tier == "risky"


@pytest.mark.parametrize("value", ["", "zero", "0", "-1", "3.5"])
def test_invalid_limit_falls_back_with_warning(monkeypatch, caplog, value):
    monkeypatch.setenv("ARGUS_RISK_MAX_LINES", value)
    assert risk_tier.max_lines() == 300
    assert "ARGUS_RISK_MAX_LINES" in caplog.text


def test_explicit_parameters_override_environment(monkeypatch):
    monkeypatch.setenv("ARGUS_RISK_TRIPWIRES", "*.py")
    monkeypatch.setenv("ARGUS_RISK_MAX_LINES", "1")
    assert risk_tier.classify(["main.py"], 2, patterns=(), limit=2).tier == "safe"
    assert risk_tier.classify(["db.sql"], 0, patterns=("*.SQL",), limit=2).tier == "risky"


@pytest.mark.parametrize("old,new", [
    ("deploy/docker-compose.yml", "src/app.py"),
    ("src/app.py", "deploy/docker-compose.yml"),
])
def test_renames_classify_both_paths_once_per_file(old, new):
    files = FileList([FilePatch(new, "renamed", None, 5, 3, old)])
    result = risk_tier.classify_files(files).to_dict()
    assert result["tier"] == "risky"
    assert result["files"] == 1
    assert result["changed_lines"] == 8
    assert result["matches"][0]["path"] == "deploy/docker-compose.yml"


def test_deleted_file_still_matches_and_counts_hunk_deletions():
    files = [FilePatch("traefik/dynamic/apps.yml", "removed", "@@ -1,2 +0,0 @@\n-one\n-two")]
    result = risk_tier.classify_files(files).to_dict()
    assert result["tier"] == "risky"
    assert result["changed_lines"] == 2


def test_classify_files_reuses_change_stats():
    files = [FilePatch("main.py", "modified", "@@ -1 +1 @@\n-old\n+new", 250, 51)]
    assert risk_tier.classify_files(files).tier == "risky"
    assert risk_tier.classify_files(files).to_dict()["changed_lines"] == 301


def test_file_list_completeness_and_explicit_override():
    files = FileList([], unseen=True)
    assert files == []
    assert risk_tier.classify_files(files).tier == "risky"
    assert risk_tier.classify_files(files, incomplete=False).tier == "safe"
    assert risk_tier.classify_files([], incomplete=True).tier == "risky"
    assert risk_tier.classify_files([]).tier == "safe"


@pytest.mark.parametrize("tier,expected", [
    ("safe", "Risk tier: safe (path and size rule only; not a correctness verdict)"),
    ("risky", "Risk tier: risky (path and size rule only; needs the operator checkpoint; "
     "not a correctness verdict)"),
])
def test_labels_are_categories(tier, expected):
    assert risk_tier.label(tier) == expected
    assert getattr(risk_tier, f"LABEL_{tier.upper()}") == expected
    assert not re.search(r"no defect|clean|approved", expected, re.I)


@pytest.mark.parametrize("row", [{}, {"risk_tier": None}, {"risk_tier": "bad"}, {"risk_tier": "SAFE"}])
def test_from_row_rejects_missing_or_unknown_tier(row):
    assert risk_tier.from_row(row) is None


def test_from_row_full_and_compact_column_wins():
    stored = risk_tier.classify(["nginx.conf"], 3).to_dict()
    row = {"risk_tier": "safe", "risk_json": json.dumps(stored)}
    expected = {**stored, "tier": "safe", "label": risk_tier.LABEL_SAFE}
    assert risk_tier.from_row(row) == expected
    assert risk_tier.from_row(row, compact=True) == {
        "tier": "safe", "label": risk_tier.LABEL_SAFE, "reasons": stored["reasons"],
    }


@pytest.mark.parametrize("raw", [None, "", "{broken", "null", "[]", "7", '"safe"', "{}"])
def test_from_row_tolerates_missing_or_corrupt_payload(raw):
    row = {"risk_tier": "risky", "risk_json": raw}
    assert risk_tier.from_row(row) == {
        "tier": "risky", "reasons": [], "label": risk_tier.LABEL_RISKY,
    }


@pytest.mark.parametrize("reasons", [None, "a reason", 7, {}, [None], ["a reason", 3]])
def test_from_row_normalizes_malformed_reasons(reasons):
    row = {"risk_tier": "risky", "risk_json": json.dumps({"changed_lines": 1, "reasons": reasons})}
    assert risk_tier.from_row(row) == {
        "tier": "risky", "label": risk_tier.LABEL_RISKY, "changed_lines": 1, "reasons": [],
    }
    assert risk_tier.from_row(row, compact=True)["reasons"] == []


def test_research_regex_parity_on_realistic_paths():
    strict = re.compile(
        r"(^|/)(nginx[^/]*|Caddyfile[^/]*|traefik|cloudflared|docker-compose[^/]*|compose\.ya?ml)(/|$)",
        re.I,
    )
    broad_extra = re.compile(r"(^|/)[^/]*(auth|proxy)[^/]*", re.I)
    paths = [
        "deploy/docker-compose.prod.yml", "docker-compose.yml", "docker-compose.yaml",
        "Docker-Compose.dev.yml", "infra/docker-compose/config.yml", "docker-compose",
        "deploy/compose.yml", "compose.yaml", "COMPOSE.YML", "infra/Compose.Yaml",
        "nginx.conf", "NGINX.conf", "infra/nginx/conf.d/x.conf", "nginx/sites/default",
        "deploy/nginx-prod.conf", "Caddyfile", "Caddyfile.dev", "infra/CADDYFILE",
        "caddyfile.d/site", "traefik/dynamic/apps.yml", "Traefik/x.yml",
        "infra/TRAEFIK/routes.yml", "cloudflared/config.yml", "CloudFlared/tunnel.yml",
        "deploy/cloudflared/config.yml", "src/auth/login.py", "api/proxy.ts",
        "src/authors.md", "docs/authorization.md", "src/oauth/callback.ts",
        "lib/AUTH/session.py", "api/ProxyHandler.ts", "deploy/reverse-proxy/config.yml",
        "tests/test_auth.py", "tests/test_proxy.py", "infra/auth-service/Dockerfile",
        "src/compose_utils.py", "docs/traefik.md", "docs/cloudflared.md", "Dockerfile",
        ".github/workflows/ci.yml", "src/main.py", "tests/test_main.py", "README.md",
        "deploy/my-nginx.conf", "infra/my-Caddyfile", "deploy/my-compose.yml",
        "compose.yml.bak", "compose.yaml.old", "docs/docker_compose.md",
        "traefik.toml", "cloudflared.yaml", "src/traefik_client.py", "src/ngin.py",
        "deploy/kubernetes/app.yml", "web/app/page.tsx", "web/package.json",
        "pyproject.toml", "src/db/migrations.py", "tests/fixtures/sample.json",
        "infra/redis/config.conf", "config/caddy.json", "scripts/release.sh",
        "docs/architecture.md", "src/proximal.py", "src/authority.py",
        "nginx_backup/settings.yml", "infra/docker-compose.d/app.yml",
        "deep/nested/auth/config.yml", "deep/nested/compose.yaml",
    ]
    assert len(paths) >= 60
    for path in paths:
        expected = "risky" if strict.search(path) or broad_extra.search(path) else "safe"
        assert risk_tier.classify([path], 0).tier == expected, path


@pytest.mark.parametrize("path", ["traefik", "infra/traefik", "cloudflared", "infra/cloudflared"])
def test_documented_directory_only_difference(path):
    assert risk_tier.classify([path], 0).tier == "safe"
