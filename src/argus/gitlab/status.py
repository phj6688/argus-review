"""Report configured GitLab status without exposing credentials or server errors."""

import hashlib
import os
import re
import threading
import time
from pathlib import Path

from argus.api import db, watch_store
from argus.forge.keys import canonical_host
from argus.gitlab import client as client_module, config, threads
from argus.gitlab.entry import _classification
from argus.repo_policy import gitlab_policy

_clock = time.monotonic
_identity_cache: tuple[tuple[str, str], float, dict] | None = None
_identity_lock = threading.Lock()


def _identity(base, token):
    global _identity_cache
    if not token:
        return {"status": "skipped", "reason": "token_missing"}
    # A credential change must invalidate status without retaining the credential.
    key = (base.url, hashlib.sha256(token.encode()).hexdigest())
    with _identity_lock:
        if _identity_cache is not None:
            cached_key, expires, result = _identity_cache
            if key == cached_key and _clock() < expires:
                return dict(result)
        try:
            client = client_module.client()
            if client is None:
                raise ValueError
            user = client.get("user").json()
            username = user.get("username") if isinstance(user, dict) else None
            if (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", username)
                    or token in username or type(user.get("bot")) is not bool):
                raise ValueError
            result = {"status": "ok", "username": username, "bot": user["bot"]}
            ttl = 300
        except Exception as error:
            result = {"status": "failed", "reason": _classification(error)}
            ttl = 60
        _identity_cache = (key, _clock() + ttl, result)
        return dict(result)


def integration_status() -> dict:
    from argus.api import watcher

    base = config.base_url()
    token = config.token()
    try:
        mode = config.publish_mode()
    except config.GitLabConfigError:
        mode = "invalid"
    try:
        project_count = len(config.publish_projects())
    except config.GitLabConfigError:
        project_count = 0
    try:
        enabled = config.watch_enabled()
    except config.GitLabConfigError:
        enabled = False
    try:
        replies_enabled = (config.reply_loop() and os.environ.get("ARGUS_REPLY_LOOP", "1") != "0"
                           and config.effective_publish_mode() == "1")
    except config.GitLabConfigError:
        replies_enabled = False
    watch = {"enabled": enabled, "projectCount": 0, "lastPolledAt": None,
             "lastError": None, "trustUnknown": 0, "forkSourceDenied": 0}
    try:
        path = db.db_path()
        watch["lastError"] = watcher.gitlab_pass_error(path)
        if Path(path).is_file():
            with watch_store.connection(path) as conn:
                count, polled = conn.execute(
                    "SELECT COUNT(*), MAX(last_polled_at) FROM gitlab_poll_cursors"
                ).fetchone()
                error = conn.execute(
                    "SELECT last_error FROM gitlab_poll_cursors WHERE last_error IS NOT NULL "
                    "ORDER BY last_polled_at DESC LIMIT 1"
                ).fetchone()
            watch.update(projectCount=count, lastPolledAt=polled,
                         lastError=error[0] if error else watch["lastError"],
                         trustUnknown=watch_store.unknown_trust_count(path),
                         forkSourceDenied=watch_store.fork_denied_count(path))
    except Exception:
        pass
    if watch["lastError"] not in {None, "gitlab_auth", "gitlab_denied", "gitlab_rate_limited", "gitlab_http"}:
        watch["lastError"] = "gitlab_http"
    return {
        "host": canonical_host(base.host, base.port),
        "baseUrl": base.url,
        "token": "configured" if token else "missing",
        "identity": _identity(base, token),
        "publish": {"mode": mode, "projectCount": project_count},
        "allowCount": len(config.allow()),
        "refusal": gitlab_policy().refusal,
        "trustUnknown": watch["trustUnknown"],
        "watch": watch,
        "replyLoop": {"enabled": replies_enabled, "resolveForbidden": threads.COUNTERS["resolve_forbidden"]},
    }
