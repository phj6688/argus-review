"""Report configured GitLab status without exposing credentials or server errors."""

import hashlib
import re
import threading
import time
from pathlib import Path

from argus.api import db, watch_store
from argus.forge.keys import canonical_host
from argus.gitlab import client as client_module, config
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
        path = db.db_path()
        trust_unknown = watch_store.unknown_trust_count(path) if Path(path).is_file() else 0
    except Exception:
        trust_unknown = 0
    return {
        "host": canonical_host(base.host, base.port),
        "baseUrl": base.url,
        "token": "configured" if token else "missing",
        "identity": _identity(base, token),
        "publish": {"mode": mode, "projectCount": project_count},
        "allowCount": len(config.allow()),
        "refusal": gitlab_policy().refusal,
        "trustUnknown": trust_unknown,
    }
