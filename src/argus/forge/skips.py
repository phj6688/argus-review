"""Keep skip logs visible without changing unconfigured report shapes."""

from argus.forge.keys import is_gitlab_key as is_gitlab_key

SKIPPED_KEY = "skipped_gitlab"


def gitlab_configured() -> bool:
    from argus.gitlab import config

    try:
        return config.base_url() is not None
    except config.GitLabConfigError:
        return False


def count_skip(stats: dict | None, n: int = 1) -> None:
    if isinstance(stats, dict) and gitlab_configured():
        stats[SKIPPED_KEY] = stats.get(SKIPPED_KEY, 0) + n


def log_skips(logger, consumer: str, count: int) -> None:
    if count > 0:
        logger.info("%s: skipped_gitlab=%d", consumer, count)
