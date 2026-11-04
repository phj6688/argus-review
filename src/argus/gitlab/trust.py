"""Resolve membership conservatively and retry unknown heads on later ticks."""

import logging
import re

from argus.api import watch_store
from argus.forge.keys import canonical_host, parse_repo_key
from argus.gitlab import config
from argus.gitlab.client import GitLabDenied, client as shared_client

log = logging.getLogger(__name__)


def is_bot(author: dict) -> bool:
    if not isinstance(author, dict):
        return False
    username = author.get("username")
    return author.get("bot") is True or (
        isinstance(username, str) and re.match(r"^(project|group)_\d+_bot(_|$)", username) is not None
    )


class TrustCache:
    def __init__(self):
        self.results: dict[tuple[int, int], bool | None] = {}


def author_trusted(repo: str, user_id, *, client=None, cache: TrustCache | None = None) -> bool | None:
    if type(user_id) is not int or not 1 <= user_id < 10**18:
        return None
    result = None
    key = None
    try:
        ref = parse_repo_key(repo)
        base = config.base_url()
        if ref.forge != "gitlab" or base is None or ref.host != canonical_host(base.host, base.port):
            return None
        key = (ref.project_id, user_id)
        if cache is not None and key in cache.results:
            return cache.results[key]
        selected = client or shared_client()
        if selected is not None:
            response = selected.get("members_all", project_id=ref.project_id, user_id=user_id)
            data = response.json()
            if response.status_code == 200 and isinstance(data, dict) and type(data.get("access_level")) is int:
                result = data["access_level"] >= config.trusted_level()
    except Exception as error:
        if isinstance(error, GitLabDenied) and error.reason == "not_found":
            result = False
        else:
            log.debug("%s", type(error).__name__)
    if cache is not None and key is not None:
        cache.results[key] = result
    return result


def sweep_unknown(path, limit, *, client=None, cache=None) -> dict:
    cache = cache if cache is not None else TrustCache()
    counts = {"checked": 0, "requeued": 0, "untrusted": 0, "unknown": 0}
    for head in watch_store.unknown_trust_heads(path, limit):
        counts["checked"] += 1
        trusted = author_trusted(head["repo"], head["author_id"], client=client, cache=cache)
        if trusted is None:
            watch_store.defer_unknown_trust(path, head["id"])
            counts["unknown"] += 1
            continue
        status = watch_store.apply_trust(path, head, trusted)
        if status is None:
            continue
        counts["requeued"] += int(status == "queued")
        counts["untrusted"] += int(trusted is False)
    return counts
