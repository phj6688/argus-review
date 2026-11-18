"""Which model ids the gateway no longer serves, and for how long to believe it.

The upstream gateway retires model ids without notice (2026-09-30, 2026-10-08,
2026-11-18). Each time every review failed until someone edited the stored pin,
and every review paid the dead call first. A retired id answers HTTP 400 or 404
with a body that names the model, which is different from a bad payload: a bad
payload is our bug and another model would not fix it, so it is never treated
as unavailable here.
"""

import logging
import os
import re
import threading
import time

log = logging.getLogger("argus.availability")

ENV_TTL = "ARGUS_MODEL_UNAVAILABLE_TTL"
DEFAULT_TTL_SECONDS = 1800.0

REVIEW = "review"
JUDGE = "judge"

# Body text seen from the gateway and from the vendor APIs behind it when the id
# itself is not served. Each one names the model, not the payload.
_MARKERS = (
    "unknown provider for model",
    "model_not_found",
    "model not found",
)
_DOES_NOT_EXIST = re.compile(r"\bmodel\b[^\n]{0,120}?\bdoes not exist\b", re.IGNORECASE)
_STATUSES = frozenset({400, 404})


class ModelUnavailable(RuntimeError):
    """The gateway does not serve this model id.

    A RuntimeError with a status_code, like the other gateway failures, so the
    watch error categories and the fallback rule treat it as a gateway error.
    """

    def __init__(self, model: str, message: str, status_code: int | None = None):
        super().__init__(f"model {model} is not available: {message}")
        self.model = model
        self.status_code = status_code


def _status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _body_text(error: Exception) -> str:
    parts = [str(error), str(getattr(error, "body", "") or "")]
    response = getattr(error, "response", None)
    if response is not None:
        try:
            parts.append(response.text)
        except Exception:  # noqa: BLE001 - a streamed or closed body has no text to add
            pass
    return " ".join(parts)


def is_model_unavailable(error: BaseException) -> bool:
    """True only for an HTTP 400/404 whose body says the model id is not served."""
    if isinstance(error, ModelUnavailable):
        return True
    if not isinstance(error, Exception):
        return False
    status = _status(error)
    if status not in _STATUSES:
        return False
    text = _body_text(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _MARKERS):
        return True
    if _DOES_NOT_EXIST.search(text):
        return True
    # The messages API answers an unknown id with 404 not_found_error and a message
    # that names the model. A route 404 has the same type but no model in it.
    return status == 404 and "not_found_error" in lowered and "model" in lowered


def translate(model: str, error: Exception) -> Exception:
    """The ModelUnavailable for an error that means the id is gone, else the error."""
    if isinstance(error, ModelUnavailable) or not is_model_unavailable(error):
        return error
    return ModelUnavailable(model, str(error)[:500], _status(error))


def ttl_seconds() -> float:
    """Seconds a model stays marked. 0 turns the memory off; garbage keeps the default."""
    raw = os.environ.get(ENV_TTL, "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value >= 0 else DEFAULT_TTL_SECONDS


_lock = threading.Lock()
_marked: dict[tuple[str, str], float] = {}


def mark_unavailable(model: str, error: BaseException, scope: str = REVIEW) -> None:
    """Remember that this id is not served, so later runs skip it until the TTL ends.

    Scoped per lane: the judge route and the review gateway use different tokens,
    and a token can lose access to an id that another token still has.
    """
    ttl = ttl_seconds()
    if not model or ttl <= 0:
        return
    now = time.monotonic()
    key = (scope, model)
    with _lock:
        fresh = _marked.get(key, 0.0) <= now
        _marked[key] = now + ttl
    if fresh:
        log.warning("model %s marked unavailable for %ss on the %s lane: %s",
                    model, int(ttl), scope, str(error)[:300])


def is_marked_unavailable(model: str | None, scope: str = REVIEW) -> bool:
    if not model:
        return False
    now = time.monotonic()
    with _lock:
        until = _marked.get((scope, model))
        if until is None:
            return False
        if until <= now:
            del _marked[(scope, model)]
            return False
        return True


def marked_among(models: list[str], scope: str = REVIEW) -> set[str]:
    """The ids in models that are marked, read once under one clock reading, so a
    mark that expires or appears mid-selection cannot drop or repeat a model."""
    now = time.monotonic()
    with _lock:
        return {model for model in models if _marked.get((scope, model), 0.0) > now}


def reset() -> None:
    """Forget every mark. Tests use it; a restart does the same in production."""
    with _lock:
        _marked.clear()
