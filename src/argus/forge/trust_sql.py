"""Keep trust semantics consistent for future queries and Python callers."""

from argus.repo_policy import TRUSTED_ASSOCIATIONS


def trusted_sql(
    repo: str = "repo", author_trusted: str = "author_trusted",
    author_association: str = "author_association",
) -> str:
    """Build a predicate from caller-supplied SQL column names, never row values."""
    associations = ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(TRUSTED_ASSOCIATIONS))
    return (
        f"CASE WHEN substr({repo}, 1, 7) = 'gitlab:' "
        f"THEN COALESCE({author_trusted} = 1, 0) "
        f"ELSE COALESCE({author_association} IN ({associations}), 0) END"
    )


def is_trusted(repo_key: str, author_trusted: int | None, author_association: str | None) -> bool:
    if repo_key.startswith("gitlab:"):
        return author_trusted == 1
    return author_association in TRUSTED_ASSOCIATIONS
