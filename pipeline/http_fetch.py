"""One HTTP door for every source: browser headers, timeouts, retries, typed errors."""
import time

import requests

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DEFAULT_TIMEOUT = 20
RETRIES = 2
RETRY_WAIT = 3.0
NO_RETRY_CODES = {401, 403, 404, 410}


class FetchError(Exception):
    """A source could not be fetched. Callers report it in fetch health, never swallow it."""


def fetch(url: str, *, method: str = "GET", params: dict | None = None,
          json_body: dict | None = None, headers: dict | None = None,
          timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    merged = {"User-Agent": USER_AGENT,
              "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
    if headers:
        merged.update(headers)
    last_error = "unknown"
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.request(method, url, params=params, json=json_body,
                                    headers=merged, timeout=timeout)
            if resp.status_code == 200:
                return resp
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in NO_RETRY_CODES:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT)
    raise FetchError(f"{url}: {last_error}")


def fetch_json(url: str, **kwargs):
    return fetch(url, **kwargs).json()
