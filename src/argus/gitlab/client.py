"""Provide guarded GitLab HTTP access with bounded responses and isolated quotas."""

import logging
import math
import re
import threading
import time
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from argus.forge.errors import ForgeAuthError, ForgeDenied, ForgeError, ForgeRateLimited
from argus.gitlab import config, guard
from argus.redact import redact


_http_active = ContextVar("gitlab_http_active", default=False)


def _allow_http_log(record: logging.LogRecord) -> bool:
    return not _http_active.get()


# HTTP tracing can expose server-controlled headers before response hooks run.
for _logger in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger).addFilter(_allow_http_log)


@contextmanager
def _without_http_trace() -> Iterator[None]:
    marker = _http_active.set(True)
    try:
        yield
    finally:
        _http_active.reset(marker)


class GitLabAuthError(ForgeAuthError):
    forge = "gitlab"

    def __init__(self, message: str):
        super().__init__(redact(message))


class GitLabDenied(ForgeDenied):
    forge = "gitlab"

    def __init__(self, message: str, reason: str = "forbidden"):
        self.reason = reason
        super().__init__(redact(message))

    def __reduce__(self):
        return type(self), (str(self), self.reason)


class GitLabRateLimited(ForgeRateLimited):
    forge = "gitlab"

    def __init__(self, reset_at: float):
        self.reset_at = reset_at
        ForgeError.__init__(self, redact(f"GitLab rate limited until {reset_at}"))

    def __reduce__(self):
        return type(self), (self.reset_at,)


class GitLabHTTPError(ForgeError):
    forge = "gitlab"

    def __init__(self, message: str, status_code: int | None = None, reason: str = "http"):
        self.status_code = status_code
        self.reason = reason
        super().__init__(redact(message))

    def __reduce__(self):
        return type(self), (str(self), self.status_code, self.reason)


def _integer(value: str | None) -> int | None:
    try:
        if value is not None and re.fullmatch(r"[+-]?[0-9]+", value):
            number = int(value)
            if math.isfinite(number):
                return number
    except (ValueError, OverflowError):
        pass
    return None


def _reset_at(headers: httpx.Headers) -> float:
    now = time.time()
    retry = headers.get("Retry-After", "")
    seconds = _integer(retry)
    if seconds is not None and seconds >= 0:
        return now + seconds
    try:
        date = parsedate_to_datetime(retry)
        reset = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
        if math.isfinite(reset):
            return reset
    except (ValueError, TypeError, OverflowError):
        pass
    reset = _integer(headers.get("RateLimit-Reset"))
    return float(reset) if reset is not None and reset > 0 else now + 60


def _next_page(response: httpx.Response) -> int | None:
    value = response.headers.get("X-Next-Page")
    if value is None:
        try:
            link = response.links.get("next", {}).get("url", "")
            values = parse_qs(urlsplit(link).query).get("page", [])
            value = values[0] if len(values) == 1 else ""
        except ValueError:
            return None
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,5}", value) else None


class GitLabClient:
    __slots__ = ("_base", "_token", "_http", "_max_bytes", "_lock", "_pause", "_quota", "pacing")
    retry_delay = 0.5

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self._base = config.base_url()
        self._token = config.token()
        if not self._token:
            raise GitLabAuthError("GitLab token is not set")
        self._max_bytes = config.max_response_bytes()
        self._lock = threading.Lock()
        self.pacing = None
        self._pause = 0.0
        self._quota: dict[str, int | str] = {}
        self._http = httpx.Client(
            transport=transport, follow_redirects=False, trust_env=False,
            timeout=config.timeout_s(), verify=config.ca_bundle() or True,
            event_hooks={"response": [self._observe]},
        )

    def __repr__(self) -> str:
        origin = self._base.origin if self._base else None
        return f"GitLabClient(origin={origin!r})"

    def quota_snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return dict(self._quota)

    def pause_until(self) -> float:
        with self._lock:
            return self._pause

    def _observe(self, response: httpx.Response) -> None:
        with self._lock:
            for key in ("limit", "remaining", "reset", "observed"):
                value = _integer(response.headers.get("RateLimit-" + key))
                if value is not None and value >= 0:
                    self._quota[key] = value
            if "RateLimit-Name" in response.headers:
                self._quota["name"] = redact(response.headers["RateLimit-Name"])
            if response.status_code == 429:
                self._pause = max(self._pause, _reset_at(response.headers))
                raise GitLabRateLimited(self._pause)
        # HTTPX otherwise reads Location to prepare a redirect even without following it.
        if 300 <= response.status_code < 400 and response.status_code != 304:
            raise GitLabDenied("GitLab redirect refused", "redirect_refused")

    def _read(self, response: httpx.Response) -> httpx.Response:
        if (response.request.method == "HEAD" or response.status_code in {204, 304}
                or 100 <= response.status_code < 200):
            # These headers can describe a representation without a response body.
            result = httpx.Response(response.status_code, content=b"", request=response.request)
            result.headers = response.headers.copy()
            return result
        length = _integer(response.headers.get("Content-Length"))
        if length is not None and length > self._max_bytes:
            raise GitLabHTTPError("GitLab response exceeds size limit", reason="response_too_large")
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise GitLabHTTPError("GitLab response encoding unsupported", reason="decoding")
        decoder = None
        if response.is_stream_consumed:
            chunks = response.iter_bytes()
        else:
            chunks = response.iter_raw()
            if encoding != "identity":
                decoder = zlib.decompressobj(zlib.MAX_WBITS | (16 if encoding == "gzip" else 0))
        body = bytearray()
        raw_bytes = 0
        for chunk in chunks:
            raw_bytes += len(chunk)
            if raw_bytes > self._max_bytes:
                raise GitLabHTTPError("GitLab response exceeds size limit", reason="response_too_large")
            if decoder is not None:
                try:
                    chunk = decoder.decompress(chunk, self._max_bytes - len(body) + 1)
                except zlib.error:
                    raise GitLabHTTPError("GitLab response decoding failed", reason="decoding") from None
            if len(body) + len(chunk) > self._max_bytes:
                raise GitLabHTTPError("GitLab response exceeds size limit", reason="response_too_large")
            body.extend(chunk)
        if decoder is not None and (not decoder.eof or decoder.unused_data):
            raise GitLabHTTPError("GitLab response decoding failed", reason="decoding")
        result = httpx.Response(response.status_code, content=bytes(body), request=response.request)
        # The body is already decoded. Installing headers afterward avoids decoding it twice.
        result.headers = response.headers.copy()
        return result

    def _check_status(self, response: httpx.Response, route: str) -> None:
        status = response.status_code
        if 200 <= status < 300 or status == 304:
            return
        if status == 401:
            raise GitLabAuthError("GitLab rejected the token")
        if status == 403:
            scope = "insufficient_scope" in response.headers.get("WWW-Authenticate", "")
            try:
                scope = scope or "insufficient_scope" in str(self._read(response).json())
            except (GitLabHTTPError, ValueError, RecursionError,
                    httpx.TransportError, httpx.DecodingError):
                pass
            if scope:
                raise GitLabAuthError("GitLab token has insufficient scope")
            raise GitLabDenied("GitLab access forbidden", "forbidden")
        if status == 404 and guard.ROUTES[route].template.startswith("/projects/"):
            raise GitLabDenied("GitLab project not found", "not_found")
        raise GitLabHTTPError(f"GitLab returned HTTP {status}", status)

    def request(self, method: str, route: str, *, query: Mapping[str, object] | None = None,
                json: object = None, **parts: object) -> httpx.Response:
        method = method.upper()
        retried = False
        while True:
            pause = self.pause_until()
            if time.time() < pause:
                raise GitLabRateLimited(pause)
            try:
                url = guard.build(route, query=query, **parts)
                if config.base_url() != self._base:
                    raise guard.GitLabGuardError("GitLab configuration changed")
                url = guard.check(url)
            except (guard.GitLabGuardError, config.GitLabConfigError):
                raise GitLabDenied("GitLab route refused", "guard") from None
            transport_failure = None
            decoding_failure = False
            try:
                with _without_http_trace(), self._http.stream(method, url, json=json, headers={
                    "PRIVATE-TOKEN": self._token, "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                }) as streamed:
                    # A malformed error body must not override the status or GET retry.
                    if 500 <= streamed.status_code < 600:
                        response = streamed
                    elif streamed.status_code >= 400:
                        self._check_status(streamed, route)
                    else:
                        response = self._read(streamed)
            except (httpx.TransportError, httpx.DecodingError) as error:
                transport_failure = type(error).__name__
                decoding_failure = isinstance(error, httpx.DecodingError)
            if decoding_failure:
                raise GitLabHTTPError("GitLab response decoding failed", reason="decoding")
            if transport_failure is None and not 500 <= response.status_code < 600:
                self._check_status(response, route)
                return response
            if retried or method != "GET":
                if transport_failure is not None:
                    raise GitLabHTTPError(f"GitLab transport failed: {transport_failure}", reason="transport")
                self._check_status(response, route)
            retried = True
            time.sleep(self.retry_delay)

    def get(self, route: str, *, query: Mapping[str, object] | None = None,
            **parts: object) -> httpx.Response:
        return self.request("GET", route, query=query, **parts)

    def pages(self, route: str, *, query: Mapping[str, object] | None = None,
              **parts: object) -> Iterator[httpx.Response]:
        query = dict(query or {})
        for _ in range(1000):
            response = self.get(route, query=query, **parts)
            yield response
            page = _next_page(response)
            if page is None or page <= query.get("page", 1):
                return
            query["page"] = page
        raise GitLabHTTPError("GitLab pagination limit exceeded", reason="too_many_pages")

    def close(self) -> None:
        with _without_http_trace():
            self._http.close()


_shared: GitLabClient | None = None
_shared_lock = threading.Lock()


def client() -> GitLabClient | None:
    global _shared
    if config.base_url() is None:
        return None
    with _shared_lock:
        if _shared is None:
            _shared = GitLabClient()
        return _shared


def close_client() -> None:
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.close()
            _shared = None
