"""Best-effort publishing access checks and collaborator invitations."""

import logging
import os
import re
import threading
import time

import httpx

from . import github, publish, repo_policy

log = logging.getLogger("argus.publish_access")
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_BOT_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")


def settings_url(repo: str) -> str:
    return f"https://github.com/{repo}/settings/access"


def _status(repo: str, state: str, detail: str) -> dict:
    return {"state": state, "detail": detail, "settingsUrl": settings_url(repo), "grant": None}


def _request(method: str, url: str, *, token: str, publish_token: bool = False,
             **kwargs) -> httpx.Response:
    response = httpx.request(method, str(github.guard_url(url)), headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": github.API_VERSION,
    }, timeout=5.0, **kwargs)
    try:
        if publish_token:
            github.observe_response(response, record_quota=False)
        else:
            github.observe_response(response)
    except (github.GitHubAuthError, github.GitHubRateLimited):
        # Callers need the HTTP status to return a fixed, credential-free explanation.
        pass
    return response


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def check_access(repo: str, *, fresh: bool = False) -> dict:
    token = os.environ.get("ARGUS_PUBLISH_TOKEN", "").strip()
    key = repo.casefold()
    if not token:
        status = _status(repo, "unconfigured", "ARGUS_PUBLISH_TOKEN is not set")
    else:
        if not fresh:
            with _cache_lock:
                cached = _cache.get(key)
                if cached is not None and cached[0] > time.monotonic():
                    return dict(cached[1])
        try:
            response = _request("GET", f"{github.GITHUB_API}/repos/{repo}",
                                token=token, publish_token=True)
            code = response.status_code
            if code == 200:
                push = bool((response.json().get("permissions") or {}).get("push"))
                status = _status(repo, "ok" if push else "missing",
                                 "bot can push" if push else "bot has no write access")
            elif code == 404:
                status = _status(repo, "missing", "bot cannot see this repository")
            elif code == 401:
                status = _status(repo, "unknown", "publish token was rejected")
            elif code in (403, 429):
                status = _status(repo, "unknown", f"GitHub refused the check (status {code})")
            else:
                status = _status(repo, "unknown", f"GitHub access check failed (status {code})")
        except github.GitHubRateLimited:
            status = _status(repo, "unknown", "rate limited")
        except github.GitHubAuthError:
            status = _status(repo, "unknown", "GitHub refused the check")
        except Exception:
            # Transport and decoding errors can contain credentials, so never echo them.
            status = _status(repo, "unknown", "GitHub access check failed")
    ttl = 600 if status["state"] == "ok" else 60
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, dict(status))
    return status


def _accept_invitation(repo: str, invitation_id, token: str) -> str | None:
    detail = "invitation sent but the bot could not accept it"
    try:
        if type(invitation_id) is not int:
            response = _request("GET", f"{github.GITHUB_API}/user/repository_invitations?per_page=100",
                                token=token, publish_token=True)
            if response.status_code != 200:
                return f"{detail} (status {response.status_code})"
            invitation_id = next((entry.get("id") for entry in response.json()
                                  if (entry.get("repository") or {}).get("full_name", "").casefold() == repo.casefold()), None)
        if type(invitation_id) is not int:
            return detail
        response = _request("PATCH", f"{github.GITHUB_API}/user/repository_invitations/{invitation_id}",
                            token=token, publish_token=True)
        if response.status_code != 204:
            return f"{detail} (status {response.status_code})"
    except Exception:
        return detail
    return None


def auto_invite_enabled() -> bool:
    """Opt-in: the only place GITHUB_TOKEN is used for a write is this invite."""
    return os.environ.get("ARGUS_PUBLISH_AUTO_INVITE", "").strip() == "1"


def _grant_access(repo: str, status: dict) -> dict:
    if not auto_invite_enabled():
        return {**status, "grant": "skipped",
                "detail": "bot has no write access, auto-invite is off (ARGUS_PUBLISH_AUTO_INVITE), add the bot by hand"}
    if repo.split("/", 1)[0].casefold() not in repo_policy.watch_owners():
        return {**status, "grant": "skipped",
                "detail": "owner is not in ARGUS_WATCH_OWNERS, add the bot by hand"}
    reviewer_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not reviewer_token:
        return {**status, "grant": "failed", "detail": "GITHUB_TOKEN is not set"}
    try:
        bot = publish._bot_login()
        if not isinstance(bot, str) or not _BOT_LOGIN.fullmatch(bot):
            raise ValueError("invalid bot login")
    except Exception:
        return {**status, "grant": "failed", "detail": "bot login could not be resolved"}
    detail = "reviewer token cannot add collaborators here"
    try:
        repo_policy.validate_repo(repo)
    except repo_policy.RepoDenied:
        return {**status, "grant": "skipped", "detail": "repository is denied, add the bot by hand"}
    try:
        response = _request("PUT", f"{github.GITHUB_API}/repos/{repo}/collaborators/{bot}",
                            token=reviewer_token, json={"permission": "push"})
    except Exception:
        return {**status, "grant": "failed", "detail": detail}
    if response.status_code not in (201, 204):
        return {**status, "grant": "failed", "detail": f"{detail} (status {response.status_code})"}
    if response.status_code == 201:
        try:
            invitation_id = response.json().get("id")
        except (ValueError, AttributeError):
            invitation_id = None
        failure = _accept_invitation(repo, invitation_id, os.environ.get("ARGUS_PUBLISH_TOKEN", "").strip())
        if failure:
            return {**status, "grant": "failed", "detail": failure}
    final = check_access(repo, fresh=True)
    if final["state"] == "ok":
        return {**final, "grant": "granted", "detail": "bot added and invitation accepted"}
    return {**final, "grant": "failed"}


def ensure_access(repo: str) -> dict:
    status = _status(repo, "unknown", "GitHub access check failed")
    try:
        status = check_access(repo, fresh=True)
        if status["state"] == "missing":
            status = _grant_access(repo, status)
    except Exception:
        status = {**status, "grant": "failed", "detail": "bot access could not be granted"}
    log.info("publish access %s: state=%s grant=%s", repo, status["state"], status["grant"])
    return status
