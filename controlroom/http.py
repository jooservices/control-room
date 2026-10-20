"""Read-only HTTP helpers with bounded timeouts and retry backoff."""

import json
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.25,
) -> bytes:
    """Fetch bytes with GET only, retrying transient failures with backoff.

    ``retries`` is the number of attempts after the initial request. Retryable
    HTTP statuses are 429 and 5xx; URL and timeout errors are also retried.
    The final failure is propagated to the caller.
    """
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if retries < 0:
        raise ValueError("retries cannot be negative")
    if backoff < 0:
        raise ValueError("backoff cannot be negative")

    request = Request(url, headers=dict(headers or {}), method="GET")
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
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
