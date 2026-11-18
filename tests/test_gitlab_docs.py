"""Keep the GitLab guide aligned with the configured settings."""

import ast
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ast.parse((ROOT / "src/argus/gitlab/config.py").read_text(encoding="utf-8"))
GITLAB_VARIABLES = sorted({
    node.value
    for node in ast.walk(CONFIG)
    if isinstance(node, ast.Constant)
    and isinstance(node.value, str)
    and re.fullmatch(r"ARGUS_GITLAB_[A-Z_]+", node.value)
})
HEADINGS = (
    "## Setup",
    "## Token choice",
    "## Scopes and roles",
    "## Minimum GitLab version",
    "## Settings",
    "## Limits and pacing",
    "## Safety rules",
    "## Known limits",
)


@pytest.mark.parametrize("variable", GITLAB_VARIABLES)
def test_gitlab_setting_has_operator_table_row(variable):
    lines = (ROOT / "docs/gitlab.md").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("| ") and f"`{variable}`" in line for line in lines), (
        f"{variable} is missing a table row in docs/gitlab.md"
    )


@pytest.mark.parametrize("variable", GITLAB_VARIABLES)
def test_gitlab_setting_has_example_assignment(variable):
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^{variable}=", example, re.MULTILINE), (
        f"{variable} is missing from .env.example"
    )


@pytest.mark.parametrize("variable", GITLAB_VARIABLES)
def test_gitlab_setting_reaches_api_container(variable):
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["argus-api"]["environment"]
    assert variable in environment, (
        f"{variable} is missing from the argus-api environment block in docker-compose.yml"
    )


def test_readme_links_gitlab_guide():
    assert "docs/gitlab.md" in (ROOT / "README.md").read_text(encoding="utf-8"), (
        "README.md is missing the docs/gitlab.md link"
    )


@pytest.mark.parametrize("heading", HEADINGS)
def test_gitlab_guide_sections_exist(heading):
    lines = (ROOT / "docs/gitlab.md").read_text(encoding="utf-8").splitlines()
    assert heading in lines, f"{heading} is missing from docs/gitlab.md"
