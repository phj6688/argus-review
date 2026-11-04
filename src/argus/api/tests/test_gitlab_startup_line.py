"""Startup reports GitLab's configured state without exposing settings."""

import logging

import pytest

from argus.api import main


@pytest.mark.parametrize(
    ("url", "level", "message"),
    [
        (None, logging.INFO, "GitLab disabled: ARGUS_GITLAB_URL not set"),
        ("https://gitlab.example", logging.INFO,
         "GitLab enabled: forge adapter active"),
        ("http://not-valid.example", logging.WARNING,
         "GitLab disabled: ARGUS_GITLAB_URL is invalid"),
    ],
)
def test_gitlab_startup_line_is_exact_and_never_includes_url(monkeypatch, caplog, url, level, message):
    if url is None:
        monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    else:
        monkeypatch.setenv("ARGUS_GITLAB_URL", url)

    with caplog.at_level(logging.INFO, logger="argus.api"):
        main._log_gitlab_state()

    records = [record for record in caplog.records if record.name == "argus.api"]
    assert [(record.levelno, record.getMessage()) for record in records] == [(level, message)]
    assert url is None or url not in caplog.text
