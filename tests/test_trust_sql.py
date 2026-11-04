"""Trust rules agree in Python and SQLite without changing existing queries."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from argus.forge import trust_sql
from argus.repo_policy import TRUSTED_ASSOCIATIONS


_ASSOCIATIONS = ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR",
                 "FIRST_TIMER", "NONE", "MANNEQUIN", None, "", "owner"]


@pytest.mark.parametrize("repo", ["gitlab:gitlab.example#42", "owner/repo", "GITLAB:owner/repo"])
@pytest.mark.parametrize("author_trusted", [None, 0, 1, 2, -1])
@pytest.mark.parametrize("association", _ASSOCIATIONS)
def test_sql_and_python_trust_agree(repo, author_trusted, association):
    expected = author_trusted == 1 if repo.startswith("gitlab:") else association in TRUSTED_ASSOCIATIONS
    assert trust_sql.is_trusted(repo, author_trusted, association) is expected
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE authors (repo TEXT, author_trusted INTEGER, author_association TEXT)")
        conn.execute("INSERT INTO authors VALUES (?, ?, ?)", (repo, author_trusted, association))
        value = conn.execute(f"SELECT {trust_sql.trusted_sql()} FROM authors").fetchone()[0]
        assert value == int(expected)
        aliased = trust_sql.trusted_sql("a.repo", "a.author_trusted", "a.author_association")
        assert conn.execute(f"SELECT {aliased} FROM authors a").fetchone()[0] == int(expected)


def test_identity_inserts_always_name_columns():
    pattern = re.compile(
        r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+"
        + r'["`\[]?publishing_identities["`\]]?'
        + r"(?:\s|[\"'])*VALUES\b",
        re.IGNORECASE,
    )
    root = Path(__file__).resolve().parents[1]
    violations = []
    for directory in ("src", "scripts", "tests"):
        for path in (root / directory).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                contents = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for match in pattern.finditer(contents):
                line = contents.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(root)}:{line}")
    assert not violations, "Identity inserts need column lists: " + ", ".join(violations)
