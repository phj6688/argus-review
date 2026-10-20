from pathlib import Path

import yaml


def test_compose_passes_every_watch_setting():
    env = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]["argus-api"][
        "environment"
    ]
    expected = {
        "ARGUS_WATCH_OWNERS": "",
        "ARGUS_WATCH_ACTIVE_DAYS": "90",
        "ARGUS_WATCH_DISCOVERY_TICKS": "1",
        "ARGUS_WATCH_INCLUDE_DRAFTS": "",
        "ARGUS_WATCH_INCLUDE_BOTS": "",
        "ARGUS_WATCH_FAST_MERGES": "1",
        "ARGUS_WATCH_MAX_ATTEMPTS": "4",
        "ARGUS_REVIEW_WORKERS": "2",
        "ARGUS_REVIEW_TIMEOUT_S": "1800",
        "ARGUS_LLM_MAX_CONCURRENCY": "3",
        "ARGUS_GITHUB_MIN_REMAINING": "500",
        "ARGUS_WATCH_INTERVAL": "60",
    }
    example = Path(".env.example").read_text().splitlines()
    for name, default in expected.items():
        assert env[name] == "${" + name + ":-" + default + "}"
        example_default = "" if name == "ARGUS_WATCH_OWNERS" else default
        assert f"{name}={example_default}" in example


def test_no_webhook_route():
    from argus.api.main import app

    assert not any("webhook" in route.path for route in app.routes)
