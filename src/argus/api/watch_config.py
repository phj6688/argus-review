"""Effective watch policy over the canonical owner listing."""

from datetime import datetime, timedelta, timezone

from ..repo_policy import env_int, hard_deny_owners, is_denied, validate_repo, valid_catalogue_name, watch_owners
from . import db


def auto_decision(repo, now=None):
    if repo.get("archived") or not repo.get("pushed_at"):
        return False
    now = now or datetime.now(timezone.utc)
    try:
        return db._parse_created_at(repo["pushed_at"]) >= now - timedelta(
            days=env_int("ARGUS_WATCH_ACTIVE_DAYS", 90)
        )
    except ValueError:
        return False


def allowed(name, deny, *, explicit=False):
    try:
        validate_repo(name, deny)
    except ValueError:
        return False
    return (explicit or name.split("/")[0].casefold() in watch_owners()) and not is_denied(name, deny)


def effective(config, now=None):
    if not config:
        return []
    overrides = {r["name"].casefold(): r.get("watched") for r in config["repos"]}
    result = []
    for repo in config.get("owned_repos", []):
        name = repo["full_name"]
        value = overrides.get(name.casefold())
        if not allowed(name, config.get("deny", []), explicit=value is True) or repo.get("archived"):
            continue
        if value is True or (
            value is None and config.get("watch_mode") != "manual" and auto_decision(repo, now)
        ):
            result.append(name)
    return result


def public_repos(config):
    watched = {name.casefold() for name in effective(config)}
    return [
        {
            **r,
            "watched": r.get("watched")
            if r.get("watched") is not None
            else r["name"].casefold() in watched,
            "stored_watched": r.get("watched"),
            "effective_watched": r["name"].casefold() in watched,
            "watched_source": "auto" if r.get("watched") is None else "operator",
        }
        for r in config["repos"]
        if valid_catalogue_name(r["name"])
    ]


def console_update(config, submitted):
    old = {r["name"].casefold(): r for r in (config or {}).get("repos", [])}
    listing = {r["full_name"].casefold(): r for r in (config or {}).get("owned_repos", [])}
    result = []
    for repo in submitted:
        value = repo["watched"]
        previous = old.get(repo["name"].casefold())
        record = listing.get(repo["name"].casefold(), {})
        if previous and previous.get("watched") is None:
            decision = repo["name"].casefold() in {name.casefold() for name in effective(config)}
            if value == decision:
                value = None
        result.append({"name": record.get("full_name", repo["name"]), "watched": value})
    return result


def policy_payload(config):
    return {
        "deny": config.get("deny", []),
        "hard_denied_owners": sorted(hard_deny_owners()),
        "watch_mode": config.get("watch_mode", "auto"),
    }
