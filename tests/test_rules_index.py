"""The generic rule baseline loads, and the resolver finds the index via the
env override / source / /app candidates."""
from pathlib import Path

from argus import rules


def test_index_loads_full_ruleset():
    rules.reset_cache()
    assert len(rules.index()) >= 18


def test_resolver_prefers_env_override(tmp_path, monkeypatch):
    f = tmp_path / "index.json"
    f.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(rules.ENV_RULES_PATH, str(f))
    assert rules._resolve_rules_path() == f


def test_resolver_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(rules.ENV_RULES_PATH, raising=False)
    monkeypatch.setattr(rules, "_DEFAULT_RULES", tmp_path / "missing.json")
    monkeypatch.chdir(tmp_path)  # cwd/data/rules absent here
    # /app/data/rules/index.json is absent in the test env
    if not Path("/app/data/rules/index.json").exists():
        assert rules._resolve_rules_path() is None
