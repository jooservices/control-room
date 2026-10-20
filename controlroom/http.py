"""Read-only HTTP helpers with bounded timeouts and retry backoff."""

import json
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class _HTTPOnlyRedirectHandler(HTTPRedirectHandler):
    """Follow redirects only when their targets remain on HTTP or HTTPS."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if urlsplit(new_url).scheme not in {"http", "https"}:
            raise HTTPError(
                new_url,
                code,
                "redirect target must use http or https",
                headers,
                file_pointer,
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


_HTTP_OPENER = build_opener(_HTTPOnlyRedirectHandler())


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects for authenticated search requests."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_SEARCH_OPENER = build_opener(_NoRedirectHandler())


def request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.25,
) -> bytes:
    """Fetch bytes with GET only, retrying transient failures with backoff.

    ``retries`` is the number of attempts after the initial request. Retryable
    HTTP statuses are 429 and 5xx; URL and timeout errors are also retried.
    The final failure is propagated to the caller. Other methods are rejected
    before any network request is made.
    """
    if method != "GET":
        raise ValueError("only GET requests are allowed")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if retries < 0:
        raise ValueError("retries cannot be negative")
    if backoff < 0:
        raise ValueError("backoff cannot be negative")
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")

    request = Request(url, headers=dict(headers or {}), method="GET")
    for attempt in range(retries + 1):
        try:
            with _HTTP_OPENER.open(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in _RETRYABLE_STATUS_CODES or attempt == retries:
                error.close()
                raise
            error.close()
        except (URLError, TimeoutError):
            if attempt == retries:
                raise

        if backoff:
            time.sleep(backoff * (2**attempt))

    raise AssertionError("unreachable")


def get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.25,
) -> bytes:
    """Fetch bytes using the read-only GET request boundary."""
    return request(
        url,
        method="GET",
        headers=headers,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
    )


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.25,
) -> object:
    """Fetch and decode a JSON response using the read-only GET helper."""
    response = get(
        url,
        headers=headers,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
    )
    return json.loads(response)


def search_json(
    url: str,
    payload: Mapping[str, object],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.25,
) -> object:
    """POST JSON only to an ``/_search`` endpoint and return its JSON result.

    Search APIs such as OpenObserve use POST for read-only queries. Redirects
    are rejected so an authorization header cannot be forwarded elsewhere.
    """
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if retries < 0:
        raise ValueError("retries cannot be negative")
    if backoff < 0:
        raise ValueError("backoff cannot be negative")
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        raise ValueError("url must use http or https") from None
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed_url.path.rstrip("/").endswith("/_search"):
        raise ValueError("url must target a read-only search endpoint")

    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with _SEARCH_OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code not in _RETRYABLE_STATUS_CODES or attempt == retries:
                error.close()
                raise
            error.close()
        except (URLError, TimeoutError):
            if attempt == retries:
                raise

        if backoff:
            time.sleep(backoff * (2**attempt))

    raise AssertionError("unreachable")
