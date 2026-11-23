"""
Every request to GitHub goes through GitHubClient. A call returns what GitHub answered (one page
from get_json, every page from get_all_pages) or raises GitHubUnavailable, never an empty stand-in.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

import requests

from config import (
    FIRST_WAIT_WITHOUT_RETRY_AFTER_S,
    GITHUB_API_VERSION,
    LONGEST_WAIT_WITHOUT_RETRY_AFTER_S,
    MIN_SECONDS_BETWEEN_CYCLES,
    MIN_SECONDS_BETWEEN_WRITES,
    NETWORK_TIMEOUT,
)
from cycle_summary import SPENT

logger = logging.getLogger(__name__)

PAGE_SIZE = 100  # the most items GitHub returns per page of a list


class GitHubUnavailable(Exception):
    """No complete answer: what the caller wanted is unknown, never empty."""


class GitHubNotFound(GitHubUnavailable):
    """GitHub answered 404: the resource is gone, or this token may not see it."""


@dataclass(frozen=True)
class RateLimitWindow:
    """One primary rate-limit window, by its lowest reading: GitHub's replicas disagree and the lowest is safe."""

    resets_at: float
    lowest_remaining: int


@dataclass(frozen=True)
class StoredPage:
    """One page of a GET answer as GitHub sent it, with its ETag, so that a 304 can be answered from it."""

    etag: str
    content: bytes
    next_page_url: str | None


class GitHubClient:
    """Holds the token, the ETag+body cache and GitHub's stop signals; before a stop ends it raises without sending."""

    def __init__(self, token: str) -> None:
        self.rate_limit_window: RateLimitWindow | None = None
        self.no_requests_before = 0.0  # epoch seconds, from GitHub's last stop signal
        # The last stop GitHub gave no wait for; the doubling grows from it
        self._last_backoff_s = 0.0
        self._stop_seen_this_cycle = False
        # Read responses GitHub counted against the limit this cycle; a 304 is not counted
        self._charged_this_cycle = 0
        self._last_write_started_at = 0.0  # time.monotonic()
        self._pages_this_cycle: dict[str, StoredPage] = {}
        self._pages_last_cycle: dict[str, StoredPage] = {}
        self._session = requests.Session()
        # Identical on every request: GitHub answers 304 only to an identical request
        self._session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            }
        )

    def start_cycle(self) -> None:
        """Begin a polling cycle: forget pages not requested in the last one, restart the request count,
        and let the stop backoff start over once a whole cycle passed without a stop."""
        if not self._stop_seen_this_cycle:
            self._last_backoff_s = 0.0
        self._stop_seen_this_cycle = False
        self._charged_this_cycle = 0
        self._pages_last_cycle = self._pages_this_cycle
        self._pages_this_cycle = {}

    def get_json(self, url: str) -> dict:
        """One conditional GET of exactly one page; a 304 returns the stored body. A fresh object every call."""
        body, _ = self.get_page(url)
        return body

    def get_all_pages(self, url: str, items_key: str) -> list[dict]:
        """get_json on every page (Link rel="next"); the objects listed under items_key, concatenated."""
        if "per_page=" in url:
            raise ValueError(f"{url}: get_all_pages sets per_page itself")
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page={PAGE_SIZE}"
        items = []
        while page_url is not None:
            body, page_url = self.get_page(page_url)
            items.extend(extract_list_of_objects(body, items_key, f"GET {url}"))
        return items

    def post_json(self, url: str, body: dict | None = None) -> dict:
        """One write, spaced from the last (_wait_to_write); never retried, body never logged."""
        self._wait_to_write("POST", url)
        response = self._send("POST", url, json_body=body)
        return _json_object_from(response.content, f"POST {url}")

    def delete(self, url: str) -> int:
        """One write, spaced from the last (_wait_to_write); never retried. The status for 204, 404 (already
        gone) and 422 (GitHub refuses); GitHubUnavailable for any other answer."""
        self._wait_to_write("DELETE", url)
        return self._send("DELETE", url, answered_statuses=(404, 422)).status_code

    def seconds_until_next_cycle(self, now: float) -> float:
        """next_cycle_delay_s with this cycle's charged requests, this client's window and its stop signal."""
        return next_cycle_delay_s(
            self._charged_this_cycle,
            self.rate_limit_window,
            self.no_requests_before,
            now,
        )

    def get_page(self, url: str) -> tuple[dict, str | None]:
        """One conditional GET: the body and the next page's URL (None when GitHub names none), from GitHub or,
        on a 304, from the stored page. A page shorter than per_page is not the last one unless this says so."""
        stored = self._pages_this_cycle.get(url) or self._pages_last_cycle.get(url)
        headers = {"If-None-Match": stored.etag} if stored is not None else {}
        response = self._send("GET", url, headers=headers)
        if response.status_code == 304:
            if stored is None:
                raise GitHubUnavailable(f"GET {url}: 304 for a page that is not stored")
            self._pages_this_cycle[url] = stored
            return _json_object_from(stored.content, f"GET {url}"), stored.next_page_url

        body = _json_object_from(response.content, f"GET {url}")
        next_page_url = response.links.get("next", {}).get("url")
        self._pages_last_cycle.pop(url, None)
        etag = response.headers.get("ETag")
        if etag:
            self._pages_this_cycle[url] = StoredPage(
                etag, response.content, next_page_url
            )
        else:
            self._pages_this_cycle.pop(url, None)
        return body, next_page_url

    def _raise_if_stopped(self, method: str, url: str) -> None:
        """GitHubUnavailable, without sending anything, while GitHub's last stop signal lasts."""
        wait_s = self.no_requests_before - time.time()
        if wait_s > 0:
            self._stop_seen_this_cycle = True
            raise GitHubUnavailable(
                f"{method} {url}: not sent, GitHub asked for {wait_s:.0f} s more without requests"
            )

    def _wait_to_write(self, method: str, url: str) -> None:
        """Refused while a stop lasts; else sleeps until MIN_SECONDS_BETWEEN_WRITES after the last write
        started (the client's only sleep)."""
        self._raise_if_stopped(method, url)
        wait_s = (
            self._last_write_started_at + MIN_SECONDS_BETWEEN_WRITES - time.monotonic()
        )
        if wait_s > 0:
            slept_from = time.monotonic()
            time.sleep(wait_s)
            SPENT.write_spacing_s += time.monotonic() - slept_from

    def _send(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        json_body: dict | None = None,
        answered_statuses: tuple[int, ...] = (),
    ) -> requests.Response:
        """The one place a request leaves: refused while a stop lasts; returns a 2xx or 304 response, or one
        with a status in answered_statuses, and raises on the rest."""
        self._raise_if_stopped(method, url)
        sent_at = time.monotonic()
        if method != "GET":
            self._last_write_started_at = sent_at
        try:
            response = self._session.request(
                method, url, headers=headers, json=json_body, timeout=NETWORK_TIMEOUT
            )
        except requests.RequestException as error:
            raise GitHubUnavailable(f"{method} {url}: {error}") from error
        finally:
            count_request(method, time.monotonic() - sent_at)

        now = time.time()
        # Only reads pace: writes report "core" but count separately (measured: token and JIT-config POSTs)
        if method == "GET":
            self.rate_limit_window = update_rate_limit_window(
                self.rate_limit_window, response.headers, now
            )
            if response.status_code != 304:
                self._charged_this_cycle += 1
                SPENT.charged_reads += 1
                remaining = response.headers.get("x-ratelimit-remaining", "")
                if remaining.isdigit() and int(remaining) % 100 == 0:
                    logger.info(f"Rate Limit Remaining: {remaining}")

        if response.status_code in answered_statuses:
            return response
        outcome = classify_response(response.status_code)
        if outcome == "stop":
            message = _github_error_message(response)
            stop_s = stop_duration_s(response.headers, now, self._last_backoff_s)
            if wait_github_asks_for_s(response.headers, now) is None:
                self._last_backoff_s = stop_s
            self._stop_seen_this_cycle = True
            self.no_requests_before = now + stop_s
            raise GitHubUnavailable(
                f"{method} {url}: HTTP {response.status_code} ({message});"
                f" no requests for {stop_s:.0f} s"
            )
        if outcome == "not_found":
            raise GitHubNotFound(
                f"{method} {url}: HTTP 404 {_github_error_message(response)}"
            )
        if outcome == "failed":
            raise GitHubUnavailable(
                f"{method} {url}: HTTP {response.status_code} {_github_error_message(response)}"
            )
        return response


def count_request(method: str, took_s: float) -> None:
    """Add one sent request and how long it took to SPENT, as a read or a write."""
    if method == "GET":
        SPENT.reads += 1
        SPENT.github_read_s += took_s
    else:
        SPENT.writes += 1
        SPENT.github_write_s += took_s


def classify_response(status_code: int) -> str:
    """Pure: "ok" (2xx), "not_modified" (304), "stop" (401, 403, 429: GitHub asks us to wait), "not_found" (404)
    or "failed" (the rest)."""
    if 200 <= status_code < 300:
        return "ok"
    if status_code == 304:
        return "not_modified"
    if status_code == 404:
        return "not_found"
    # 401 too: repeated bad credentials make GitHub refuse the account's valid tokens as well
    if status_code in (401, 403, 429):
        return "stop"
    return "failed"


def stop_duration_s(
    headers: Mapping[str, str], now: float, previous_backoff_s: float
) -> float:
    """Pure: as long as GitHub's headers ask; without such a signal 60 s, doubling from previous_backoff_s."""
    asked_s = wait_github_asks_for_s(headers, now)
    if asked_s is not None:
        return asked_s
    doubled = max(2 * previous_backoff_s, FIRST_WAIT_WITHOUT_RETRY_AFTER_S)
    return min(doubled, LONGEST_WAIT_WITHOUT_RETRY_AFTER_S)


def wait_github_asks_for_s(headers: Mapping[str, str], now: float) -> float | None:
    """Pure: the wait GitHub's headers ask for, until x-ratelimit-reset if remaining is 0 and
    at least retry-after; None if they ask for none."""
    waits = []
    resets_at = _int_header(headers, "x-ratelimit-reset")
    if (
        headers.get("x-ratelimit-remaining") == "0"
        and resets_at is not None
        and resets_at > now
    ):
        waits.append(resets_at + 1 - now)  # the reset is in whole seconds
    retry_after = _int_header(headers, "retry-after")
    if retry_after is not None:
        waits.append(retry_after)
    return max(waits) if waits else None


def update_rate_limit_window(
    window: RateLimitWindow | None, read_headers: Mapping[str, str], now: float
) -> RateLimitWindow | None:
    """Pure: fold one read's rate-limit headers into the window; once the window has reset, start a new one."""
    remaining = _int_header(read_headers, "x-ratelimit-remaining")
    resets_at = _int_header(read_headers, "x-ratelimit-reset")
    if remaining is None or resets_at is None:
        return window
    if window is None or now >= window.resets_at:
        return RateLimitWindow(resets_at, remaining)
    # Replicas disagree on the reset as well; the earliest keeps a low reading from outliving its window
    return RateLimitWindow(
        min(window.resets_at, resets_at), min(window.lowest_remaining, remaining)
    )


def next_cycle_delay_s(
    charged_requests_last_cycle: int,
    window: RateLimitWindow | None,
    no_requests_before: float,
    now: float,
) -> float:
    """Pure: spread the remaining budget over cycles like the last until the reset; never before a stop ends or the minimum gap."""
    delay_s = MIN_SECONDS_BETWEEN_CYCLES
    if window is not None and window.resets_at > now:
        until_reset_s = window.resets_at + 1 - now  # the reset is in whole seconds
        if window.lowest_remaining > 0:
            spread_s = (
                (window.resets_at - now)
                * charged_requests_last_cycle
                / window.lowest_remaining
            )
        else:
            spread_s = until_reset_s
        # After the reset the whole budget is back, so waiting longer gains nothing
        delay_s = max(delay_s, min(spread_s, until_reset_s))
    return max(delay_s, no_requests_before - now)


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    """The header as an integer, or None if it is missing or not an integer; name in lower case."""
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _json_object_from(content: bytes, request: str) -> dict:
    """A body parsed as a JSON object, freshly each time; GitHubUnavailable if it is not one."""
    try:
        body = json.loads(content)
    except ValueError as error:
        raise GitHubUnavailable(f"{request}: the answer is not JSON") from error
    if not isinstance(body, dict):
        raise GitHubUnavailable(f"{request}: the answer is not a JSON object")
    return body


def extract_list_of_objects(body: dict, items_key: str, request: str) -> list[dict]:
    """Pure: body[items_key] if it is a list of JSON objects; GitHubUnavailable otherwise. GitHub has listed a
    null among the jobs of a run it had just created."""
    items = body.get(items_key)
    if not isinstance(items, list):
        raise GitHubUnavailable(f"{request}: no {items_key!r} list in the answer")
    if not all(isinstance(item, dict) for item in items):
        raise GitHubUnavailable(
            f"{request}: an entry of the {items_key!r} list is not an object"
        )
    return items


def _github_error_message(response: requests.Response) -> str:
    """GitHub's explanation in an error response, such as "API rate limit exceeded ..."; "" if there is none."""
    try:
        return str(json.loads(response.content).get("message", ""))
    except (ValueError, AttributeError):
        return ""
