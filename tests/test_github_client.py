"""github_client without GitHub: the pure functions by table, the client against a local fake server."""

import json
import socket
import threading
import time
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import github_client
from config import (
    FIRST_WAIT_WITHOUT_RETRY_AFTER_S,
    LONGEST_WAIT_WITHOUT_RETRY_AFTER_S,
    MIN_SECONDS_BETWEEN_CYCLES,
    MIN_SECONDS_BETWEEN_WRITES,
)
from github_client import (
    GitHubClient,
    GitHubNotFound,
    GitHubUnavailable,
    RateLimitWindow,
    classify_response,
    next_cycle_delay_s,
    stop_duration_s,
    wait_github_asks_for_s,
    update_rate_limit_window,
)

NOW = 1_800_000_000.0
SECONDARY_LIMIT = {"message": "You have exceeded a secondary rate limit."}


def rate_limit_headers(remaining, resets_at):
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(resets_at)),
        "x-ratelimit-resource": "core",
    }


class ClassifyResponseTest(unittest.TestCase):
    def test_every_status_class(self):
        expected = {
            200: "ok",
            201: "ok",
            204: "ok",
            304: "not_modified",
            401: "stop",
            403: "stop",
            429: "stop",
            301: "failed",
            400: "failed",
            404: "not_found",
            422: "failed",
            500: "failed",
            502: "failed",
            504: "failed",
        }
        for status, outcome in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_response(status), outcome)


class StopDurationTest(unittest.TestCase):
    def test_primary_limit_waits_until_just_after_the_reset(self):
        headers = rate_limit_headers(0, NOW + 300)
        self.assertEqual(stop_duration_s(headers, NOW, 0), 301)

    def test_retry_after_is_obeyed(self):
        headers = {"retry-after": "30", **rate_limit_headers(4000, NOW + 300)}
        self.assertEqual(stop_duration_s(headers, NOW, 0), 30)

    def test_retry_after_and_exhausted_limit_take_the_longer(self):
        headers = {"retry-after": "30", **rate_limit_headers(0, NOW + 300)}
        self.assertEqual(stop_duration_s(headers, NOW, 0), 301)
        headers = {"retry-after": "900", **rate_limit_headers(0, NOW + 300)}
        self.assertEqual(stop_duration_s(headers, NOW, 0), 900)

    def test_no_signal_waits_a_minute_then_doubles_up_to_the_cap(self):
        rows = [
            (0, FIRST_WAIT_WITHOUT_RETRY_AFTER_S),
            (60, 120),
            (120, 240),
            (480, LONGEST_WAIT_WITHOUT_RETRY_AFTER_S),
            (LONGEST_WAIT_WITHOUT_RETRY_AFTER_S, LONGEST_WAIT_WITHOUT_RETRY_AFTER_S),
            (5, FIRST_WAIT_WITHOUT_RETRY_AFTER_S),
        ]
        for previous_stop_s, stop_s in rows:
            with self.subTest(previous_stop_s=previous_stop_s):
                self.assertEqual(stop_duration_s({}, NOW, previous_stop_s), stop_s)

    def test_a_reset_already_past_or_unreadable_falls_back_to_a_minute(self):
        for headers in (
            rate_limit_headers(0, NOW - 5),
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"},
            {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(
                    stop_duration_s(headers, NOW, 0), FIRST_WAIT_WITHOUT_RETRY_AFTER_S
                )


class WaitGitHubAsksForTest(unittest.TestCase):
    def test_only_a_reset_with_nothing_left_or_a_retry_after_is_a_signal(self):
        rows = [
            ({}, None),
            (rate_limit_headers(4000, NOW + 300), None),
            (rate_limit_headers(0, NOW + 300), 301),
            ({"retry-after": "30"}, 30),
        ]
        for headers, wait_s in rows:
            with self.subTest(headers=headers):
                self.assertEqual(wait_github_asks_for_s(headers, NOW), wait_s)


class UpdateRateLimitWindowTest(unittest.TestCase):
    def test_first_reading_opens_a_window(self):
        window = update_rate_limit_window(
            None, rate_limit_headers(4000, NOW + 600), NOW
        )
        self.assertEqual(window, RateLimitWindow(NOW + 600, 4000))

    def test_a_higher_reading_from_another_replica_does_not_raise_the_window(self):
        window = RateLimitWindow(NOW + 600, 750)
        headers = rate_limit_headers(5000, NOW + 600)
        self.assertEqual(update_rate_limit_window(window, headers, NOW), window)

    def test_replicas_a_second_apart_stay_in_one_window(self):
        window = RateLimitWindow(NOW + 600, 750)
        headers = rate_limit_headers(740, NOW + 601)
        updated = update_rate_limit_window(window, headers, NOW)
        self.assertEqual(updated, RateLimitWindow(NOW + 600, 740))

    def test_a_window_that_has_reset_is_replaced(self):
        window = RateLimitWindow(NOW - 1, 3)
        headers = rate_limit_headers(4999, NOW + 3599)
        updated = update_rate_limit_window(window, headers, NOW)
        self.assertEqual(updated, RateLimitWindow(NOW + 3599, 4999))

    def test_missing_headers_are_ignored(self):
        window = RateLimitWindow(NOW + 600, 750)
        self.assertEqual(update_rate_limit_window(window, {}, NOW), window)


class NextCycleDelayTest(unittest.TestCase):
    def test_minimum_gap_without_a_window_or_without_charged_requests(self):
        self.assertEqual(
            next_cycle_delay_s(10, None, 0, NOW), MIN_SECONDS_BETWEEN_CYCLES
        )
        window = RateLimitWindow(NOW + 600, 10)
        self.assertEqual(
            next_cycle_delay_s(0, window, 0, NOW), MIN_SECONDS_BETWEEN_CYCLES
        )

    def test_spreads_the_remaining_budget_until_the_reset(self):
        window = RateLimitWindow(NOW + 2400, 1000)
        self.assertAlmostEqual(next_cycle_delay_s(6, window, 0, NOW), 14.4)

    def test_never_shorter_than_the_minimum_gap(self):
        window = RateLimitWindow(NOW + 600, 5000)
        self.assertEqual(
            next_cycle_delay_s(1, window, 0, NOW), MIN_SECONDS_BETWEEN_CYCLES
        )

    def test_never_longer_than_just_past_the_reset(self):
        rows = [
            (0, 0, 300),  # no budget left
            (30, 10, 600),  # a cycle costs more than what remains
            (26, 5, 3000),
            (60, 1, 300),
        ]
        for charged, remaining, seconds_to_reset in rows:
            with self.subTest(charged=charged, remaining=remaining):
                window = RateLimitWindow(NOW + seconds_to_reset, remaining)
                delay_s = next_cycle_delay_s(charged, window, 0, NOW)
                self.assertEqual(delay_s, seconds_to_reset + 1)

    def test_a_window_that_has_reset_no_longer_paces(self):
        window = RateLimitWindow(NOW - 1, 0)
        self.assertEqual(
            next_cycle_delay_s(50, window, 0, NOW), MIN_SECONDS_BETWEEN_CYCLES
        )

    def test_a_stop_outlasts_the_pacing(self):
        window = RateLimitWindow(NOW + 2400, 1000)
        self.assertEqual(next_cycle_delay_s(6, window, NOW + 500, NOW), 500)

    def test_cycles_paced_this_way_never_overspend_before_the_reset(self):
        for remaining, cost in ((1000, 6), (1000, 7), (50, 49), (12, 10)):
            with self.subTest(remaining=remaining, cost=cost):
                now, resets_at = NOW, NOW + 3600
                while now < resets_at:
                    remaining -= cost
                    self.assertGreaterEqual(remaining, 0)
                    window = RateLimitWindow(resets_at, remaining)
                    now += next_cycle_delay_s(cost, window, 0, now)


@dataclass
class Reply:
    status: int
    headers: dict = field(default_factory=dict)
    body: object = None  # JSON-encoded, or sent as is when bytes
    delay_s: float = 0


class FakeGitHub:
    """A local HTTP server that answers each path from a script and records every request it receives."""

    def __init__(self):
        self.scripts = {}
        self.received = []  # (method, path, headers)
        self.received_bodies = []  # (method, path, body as sent)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.answer(self)

            def do_POST(self):
                fake.answer(self)

            def do_DELETE(self):
                fake.answer(self)

            def log_message(self, *args):
                pass

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # the client hung up first, as in the timeout test

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def script(self, path, *replies):
        """Answer path with these replies in turn, repeating the last; a reply may be a function of the request headers."""
        self.scripts[path] = list(replies)

    def requests_to(self, path):
        return [request for request in self.received if request[1] == path]

    def answer(self, handler):
        request_body = handler.rfile.read(
            int(handler.headers.get("Content-Length") or 0)
        )
        self.received.append((handler.command, handler.path, dict(handler.headers)))
        self.received_bodies.append((handler.command, handler.path, request_body))
        replies = self.scripts.get(handler.path) or [
            Reply(404, body={"message": "Not Found"})
        ]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if callable(reply):
            reply = reply(handler.headers)
        time.sleep(reply.delay_s)  # the real clock; only github_client's is faked
        if isinstance(reply.body, bytes):
            body = reply.body
        else:
            body = b"" if reply.body is None else json.dumps(reply.body).encode()
        handler.send_response(reply.status)
        for name, value in reply.headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def answer_like_a_jit_config_post(remaining, resets_at):
    """Replies as GitHub answers generate-jitconfig: it says "core", but counts on a separate counter of its own."""

    def reply(request_headers):
        nonlocal remaining
        remaining -= 1
        headers = rate_limit_headers(remaining, resets_at)
        return Reply(
            201,
            headers,
            {"runner": {"id": 9}, "encoded_jit_config": "jit-config-value"},
        )

    return reply


def answer_304_to(etag, body, extra_headers=None):
    """A reply that is a 304 when the request carries etag, else a 200 with body and etag."""
    headers = {"ETag": etag, **(extra_headers or {})}

    def reply(request_headers):
        if request_headers.get("If-None-Match") == etag:
            return Reply(304, headers)
        return Reply(200, headers, body)

    return reply


class FakeClock:
    """Stands in for github_client's time module: time passes only when the client sleeps or a test moves it."""

    def __init__(self, now):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.clock = FakeClock(NOW)
        patcher = mock.patch.object(github_client, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = GitHubClient("test-token")
        self.github.start_cycle()

    def url(self, path):
        return self.fake.base_url + path

    def wait_out_the_stop(self):
        self.clock.now = max(self.clock.now, self.github.no_requests_before)

    def test_a_304_returns_the_stored_body_and_is_not_charged(self):
        runs = {"workflow_runs": [{"id": 1}]}
        paced = rate_limit_headers(100, NOW + 2400)  # one charged request -> 24 s
        self.fake.script("/runs", answer_304_to('W/"v1"', runs, paced))
        self.assertEqual(self.github._get_object(self.url("/runs")), runs)
        self.assertEqual(self.github.seconds_until_next_cycle(NOW), 24)
        self.github.start_cycle()
        self.assertEqual(self.github._get_object(self.url("/runs")), runs)
        self.assertEqual(
            self.github.seconds_until_next_cycle(NOW), MIN_SECONDS_BETWEEN_CYCLES
        )
        sent = self.fake.requests_to("/runs")
        self.assertNotIn("If-None-Match", sent[0][2])
        self.assertEqual(sent[1][2]["If-None-Match"], 'W/"v1"')

    def test_changing_a_returned_body_does_not_change_the_next_answer(self):
        jobs = {"jobs": [{"id": 1, "labels": ["slurm-runner-small"]}]}
        self.fake.script("/jobs?per_page=100", answer_304_to('"v1"', jobs))
        for _ in range(2):
            items = self.github._get_all_items(self.url("/jobs"), "jobs")
            self.assertEqual(items, jobs["jobs"])
            items[0]["labels"].append("changed by the caller")
            items.clear()
            self.github.start_cycle()
        self.assertEqual(len(self.fake.requests_to("/jobs?per_page=100")), 2)

    def test_a_page_not_requested_for_a_whole_cycle_is_forgotten(self):
        self.fake.script("/job", answer_304_to('"v1"', {"id": 7}))
        self.github._get_object(self.url("/job"))
        self.github.start_cycle()
        self.github.start_cycle()
        self.github._get_object(self.url("/job"))
        self.assertNotIn("If-None-Match", self.fake.requests_to("/job")[1][2])

    def test_a_200_without_an_etag_drops_the_stored_page(self):
        self.fake.script(
            "/job",
            Reply(200, {"ETag": '"v1"'}, {"n": 1}),
            Reply(200, {}, {"n": 2}),
            Reply(200, {}, {"n": 3}),
        )
        answers = [self.github._get_object(self.url("/job")) for _ in range(3)]
        self.assertEqual(answers, [{"n": 1}, {"n": 2}, {"n": 3}])
        sent = self.fake.requests_to("/job")
        self.assertEqual(sent[1][2]["If-None-Match"], '"v1"')
        self.assertNotIn("If-None-Match", sent[2][2])

    def test_every_request_carries_the_same_fixed_headers(self):
        self.fake.script("/a", Reply(200, body={}))
        self.fake.script("/b", Reply(201, body={"token": "x"}))
        self.github._get_object(self.url("/a"))
        self.github._post_object(self.url("/b"))
        names = ("Authorization", "Accept", "X-GitHub-Api-Version", "User-Agent")
        first, second = (
            {name: headers.get(name) for name in names}
            for _, _, headers in self.fake.received
        )
        self.assertEqual(first, second)
        self.assertEqual(first["Authorization"], "token test-token")
        self.assertEqual(
            first["X-GitHub-Api-Version"], github_client.GITHUB_API_VERSION
        )

    def test_all_pages_are_followed_and_concatenated(self):
        page_2 = self.url("/runs?status=queued&per_page=100&page=2")
        self.fake.script(
            "/runs?status=queued&per_page=100",
            Reply(
                200,
                {"Link": f'<{page_2}>; rel="next"'},
                {"workflow_runs": [{"id": 1}, {"id": 2}]},
            ),
        )
        self.fake.script(
            "/runs?status=queued&per_page=100&page=2",
            Reply(200, body={"workflow_runs": [{"id": 3}]}),
        )
        runs = self.github._get_all_items(
            self.url("/runs?status=queued"), "workflow_runs"
        )
        self.assertEqual([run["id"] for run in runs], [1, 2, 3])

    def test_a_url_that_already_sets_per_page_is_refused(self):
        with self.assertRaises(ValueError):
            self.github._get_all_items(self.url("/runs?per_page=30"), "workflow_runs")
        self.assertEqual(self.fake.received, [])

    def test_pages_answered_304_are_followed_from_the_stored_links(self):
        page_2 = self.url("/jobs?per_page=100&page=2")
        link = {"Link": f'<{page_2}>; rel="next"'}
        self.fake.script(
            "/jobs?per_page=100", answer_304_to('"p1"', {"jobs": [{"id": 1}]}, link)
        )
        self.fake.script(
            "/jobs?per_page=100&page=2", answer_304_to('"p2"', {"jobs": [{"id": 2}]})
        )
        first = self.github._get_all_items(self.url("/jobs"), "jobs")
        self.github.start_cycle()
        self.assertEqual(self.github._get_all_items(self.url("/jobs"), "jobs"), first)
        self.assertEqual(len(self.fake.requests_to("/jobs?per_page=100&page=2")), 2)

    def test_a_failure_on_a_later_page_raises(self):
        page_2 = self.url("/jobs?per_page=100&page=2")
        link = {"Link": f'<{page_2}>; rel="next"'}
        self.fake.script("/jobs?per_page=100", Reply(200, link, {"jobs": [{"id": 1}]}))
        self.fake.script(
            "/jobs?per_page=100&page=2", Reply(502, body={"message": "Server Error"})
        )
        with self.assertRaises(GitHubUnavailable):
            self.github._get_all_items(self.url("/jobs"), "jobs")

    def test_an_answer_without_the_list_or_without_json_raises(self):
        self.fake.script(
            "/jobs?per_page=100", Reply(200, body={"message": "unexpected"})
        )
        self.fake.script(
            "/html", Reply(200, {"Content-Type": "text/html"}, b"<html>unicorn</html>")
        )
        with self.assertRaises(GitHubUnavailable):
            self.github._get_all_items(self.url("/jobs"), "jobs")
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/html"))

    def test_a_rate_limit_403_stops_requests_until_the_reset(self):
        resets_at = int(NOW) + 2
        self.fake.script(
            "/runs?per_page=100",
            Reply(
                403,
                rate_limit_headers(0, resets_at),
                {"message": "API rate limit exceeded"},
            ),
            Reply(200, body={"workflow_runs": [{"id": 1}]}),
        )
        with self.assertRaises(GitHubUnavailable) as raised:
            self.github._get_all_items(self.url("/runs"), "workflow_runs")
        self.assertIn("API rate limit exceeded", str(raised.exception))
        with self.assertRaises(GitHubUnavailable):
            self.github._get_all_items(self.url("/runs"), "workflow_runs")
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual(self.github.no_requests_before, resets_at + 1)
        self.assertEqual(self.github.seconds_until_next_cycle(NOW), resets_at + 1 - NOW)

        self.wait_out_the_stop()
        runs = self.github._get_all_items(self.url("/runs"), "workflow_runs")
        self.assertEqual(runs, [{"id": 1}])
        self.assertEqual(len(self.fake.received), 2)

    def test_a_secondary_limit_obeys_retry_after(self):
        self.fake.script("/runs", Reply(403, {"Retry-After": "120"}, SECONDARY_LIMIT))
        with self.assertRaises(GitHubUnavailable) as raised:
            self.github._get_object(self.url("/runs"))
        self.assertIn("secondary rate limit", str(raised.exception))
        self.assertEqual(self.github.no_requests_before, NOW + 120)
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        self.assertEqual(len(self.fake.received), 1)

    def test_a_429_obeys_retry_after(self):
        self.fake.script(
            "/runs", Reply(429, {"Retry-After": "30"}, {"message": "Too many requests"})
        )
        with self.assertRaises(GitHubUnavailable) as raised:
            self.github._get_object(self.url("/runs"))
        self.assertIn("HTTP 429 (Too many requests)", str(raised.exception))
        self.assertEqual(self.github.no_requests_before, NOW + 30)

    def test_a_401_stops_requests_and_says_why(self):
        self.fake.script("/runs", Reply(401, body={"message": "Bad credentials"}))
        with self.assertRaises(GitHubUnavailable) as raised:
            self.github._get_object(self.url("/runs"))
        self.assertIn("HTTP 401 (Bad credentials)", str(raised.exception))
        self.assertIn("/runs", str(raised.exception))
        self.assertEqual(
            self.github.no_requests_before, NOW + FIRST_WAIT_WITHOUT_RETRY_AFTER_S
        )
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        self.assertEqual(len(self.fake.received), 1)

    def test_a_post_during_a_stop_raises_without_sending(self):
        self.fake.script("/runs", Reply(403, {"Retry-After": "60"}, SECONDARY_LIMIT))
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        with self.assertRaises(GitHubUnavailable):
            self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(self.fake.requests_to("/generate-jitconfig"), [])

    def test_repeated_secondary_limits_double_across_cycles_despite_successes(self):
        self.fake.script("/runs", Reply(200, body={}))
        self.fake.script("/generate-jitconfig", Reply(403, body=SECONDARY_LIMIT))
        stops = []
        for _ in range(3):
            self.github.start_cycle()
            self.github._get_object(self.url("/runs"))
            with self.assertRaises(GitHubUnavailable):
                self.github._post_object(self.url("/generate-jitconfig"))
            stops.append(self.github.no_requests_before - self.clock.now)
            self.wait_out_the_stop()
        self.assertEqual(stops, [60, 120, 240])

        # A whole cycle without a stop lets the backoff start over
        self.github.start_cycle()
        self.github._get_object(self.url("/runs"))
        self.github.start_cycle()
        with self.assertRaises(GitHubUnavailable):
            self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(self.github.no_requests_before - self.clock.now, 60)

    def test_a_long_primary_stop_does_not_inflate_the_next_backoff(self):
        self.fake.script(
            "/runs",
            Reply(
                403,
                rate_limit_headers(0, NOW + 1800),
                {"message": "API rate limit exceeded"},
            ),
            Reply(403, body={"message": "Must have admin rights to Repository."}),
        )
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        self.assertEqual(self.github.no_requests_before - NOW, 1801)
        self.wait_out_the_stop()
        self.github.start_cycle()
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        self.assertEqual(
            self.github.no_requests_before - self.clock.now,
            FIRST_WAIT_WITHOUT_RETRY_AFTER_S,
        )

    def test_a_cycle_whose_requests_were_refused_keeps_the_backoff(self):
        self.fake.script("/generate-jitconfig", Reply(403, body=SECONDARY_LIMIT))
        with self.assertRaises(GitHubUnavailable):
            self.github._post_object(self.url("/generate-jitconfig"))
        self.github.start_cycle()
        with self.assertRaises(GitHubUnavailable):  # refused: the stop is still active
            self.github._post_object(self.url("/generate-jitconfig"))
        self.github.start_cycle()
        self.wait_out_the_stop()
        with self.assertRaises(GitHubUnavailable):
            self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(self.github.no_requests_before - self.clock.now, 120)
        self.assertEqual(len(self.fake.requests_to("/generate-jitconfig")), 2)

    def test_a_404_raises_not_found_and_stops_nothing(self):
        self.fake.script(
            "/runs/7", Reply(404, body={"message": "Not Found"}), Reply(200, body={})
        )
        with self.assertRaises(GitHubNotFound) as raised:
            self.github._get_object(self.url("/runs/7"))
        self.assertIsInstance(raised.exception, GitHubUnavailable)
        self.assertIn("HTTP 404", str(raised.exception))
        self.assertEqual(self.github.no_requests_before, 0)
        self.assertEqual(self.github._get_object(self.url("/runs/7")), {})

    def test_get_page_names_the_next_page_only_when_github_does(self):
        page_2 = self.url("/runs?page=2")
        # A short page with a next link: GitHub may return fewer than per_page before the end
        short_with_next = {"workflow_runs": [{"id": 1}]}
        self.fake.script(
            "/runs?page=1",
            answer_304_to('"p1"', short_with_next, {"Link": f'<{page_2}>; rel="next"'}),
        )
        self.fake.script("/runs?page=2", Reply(200, body={"workflow_runs": []}))
        for _ in range(2):  # the second answer is a 304, followed from the stored link
            self.assertEqual(
                self.github._get_page(self.url("/runs?page=1")),
                (short_with_next, page_2),
            )
            self.github.start_cycle()
        self.assertEqual(self.github._get_page(page_2), ({"workflow_runs": []}, None))
        self.assertEqual(
            self.fake.requests_to("/runs?page=1")[1][2]["If-None-Match"], '"p1"'
        )

    def test_a_server_error_fails_only_that_request(self):
        self.fake.script(
            "/runs",
            Reply(500, body={"message": "Server Error"}),
            Reply(200, body={"ok": 1}),
        )
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        self.assertEqual(self.github._get_object(self.url("/runs")), {"ok": 1})

    def test_a_timeout_raises_and_leaves_the_next_cycle_free_to_try(self):
        self.fake.script("/slow", Reply(200, body={}, delay_s=0.5))
        with mock.patch.object(github_client, "NETWORK_TIMEOUT", 0.05):
            with self.assertRaises(GitHubUnavailable):
                self.github._get_object(self.url("/slow"))
        self.assertEqual(self.github.no_requests_before, 0)

    def test_an_unreachable_github_raises(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(f"http://127.0.0.1:{port}/runs")

    def test_a_delete_returns_the_status_github_answered_or_raises(self):
        for status in (204, 404, 422):
            with self.subTest(status=status):
                self.fake.script(f"/runners/{status}", Reply(status, body=None))
                self.assertEqual(
                    self.github._delete(self.url(f"/runners/{status}")), status
                )
        self.fake.script("/runners/500", Reply(500, body={"message": "Server Error"}))
        with self.assertRaises(GitHubUnavailable):
            self.github._delete(self.url("/runners/500"))
        self.assertEqual(self.github.no_requests_before, 0)

    def test_a_delete_is_a_write_spaced_from_the_last_and_obeys_stops(self):
        self.fake.script("/write", Reply(201, body={}))
        self.fake.script("/runners/1", Reply(204, body=None))
        self.github._post_object(self.url("/write"))
        self.github._delete(self.url("/runners/1"))
        self.assertEqual(self.clock.slept, [MIN_SECONDS_BETWEEN_WRITES])
        self.fake.script("/runners/2", Reply(403, body=SECONDARY_LIMIT))
        with self.assertRaises(GitHubUnavailable):
            self.github._delete(self.url("/runners/2"))
        self.assertEqual(self.github.no_requests_before - self.clock.now, 60)
        with self.assertRaises(GitHubUnavailable):
            self.github._delete(self.url("/runners/1"))
        self.assertEqual(len(self.fake.requests_to("/runners/1")), 1)

    def test_writes_are_spaced_one_second_apart(self):
        self.fake.script(
            "/generate-jitconfig", answer_like_a_jit_config_post(4000, NOW + 600)
        )
        answer = self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(answer["encoded_jit_config"], "jit-config-value")
        self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(self.clock.slept, [MIN_SECONDS_BETWEEN_WRITES])

    def test_a_wave_of_jit_config_posts_neither_paces_nor_changes_the_window(self):
        self.fake.script("/runs", Reply(200, rate_limit_headers(4000, NOW + 3000), {}))
        self.fake.script(
            "/generate-jitconfig", answer_like_a_jit_config_post(400, NOW + 3000)
        )
        self.github._get_object(self.url("/runs"))
        for _ in range(47):
            self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(
            self.github.rate_limit_window, RateLimitWindow(NOW + 3000, 4000)
        )
        self.assertEqual(
            self.github.seconds_until_next_cycle(self.clock.now),
            MIN_SECONDS_BETWEEN_CYCLES,
        )

    def test_writes_are_spaced_from_start_to_start(self):
        def answer_after_300_ms(request_headers):
            self.clock.now += 0.3  # time passes on the client's clock while it waits
            return Reply(201, body={"token": "x"})

        self.fake.script("/generate-jitconfig", answer_after_300_ms)
        self.github._post_object(self.url("/generate-jitconfig"))
        self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], MIN_SECONDS_BETWEEN_WRITES - 0.3)

    def test_a_refused_write_neither_sleeps_nor_counts_as_a_write(self):
        self.fake.script("/generate-jitconfig", Reply(201, body={"token": "x"}))
        self.fake.script("/runs", Reply(403, {"Retry-After": "60"}, SECONDARY_LIMIT))
        self.github._post_object(self.url("/generate-jitconfig"))
        with self.assertRaises(GitHubUnavailable):
            self.github._get_object(self.url("/runs"))
        for refused_at in (NOW + 0.5, NOW + 59.5):
            self.clock.now = refused_at
            with self.assertRaises(GitHubUnavailable):
                self.github._post_object(self.url("/generate-jitconfig"))
        self.clock.now = NOW + 60
        self.github._post_object(self.url("/generate-jitconfig"))
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(len(self.fake.requests_to("/generate-jitconfig")), 2)

    def test_pacing_uses_this_cycles_charged_requests(self):
        self.fake.script("/a", Reply(200, rate_limit_headers(100, NOW + 2400), {}))
        self.github._get_object(self.url("/a"))
        self.github._get_object(self.url("/a"))
        self.assertEqual(self.github.seconds_until_next_cycle(NOW), 48)
        self.github.start_cycle()
        self.assertEqual(
            self.github.seconds_until_next_cycle(NOW), MIN_SECONDS_BETWEEN_CYCLES
        )

    def test_no_token_value_reaches_a_log_record_or_an_exception(self):
        secrets = ("test-token", "jit-config-value")
        self.fake.script(
            "/generate-jitconfig", answer_like_a_jit_config_post(4000, NOW + 600)
        )
        # remaining 100 makes the client log a line, so the log is not trivially clean
        self.fake.script(
            "/forbidden",
            Reply(
                403,
                rate_limit_headers(100, NOW + 600),
                {"message": "Must have admin rights to Repository."},
            ),
        )
        self.fake.script(
            "/unauthorized", Reply(401, body={"message": "Bad credentials"})
        )
        messages = []
        with self.assertLogs(level="DEBUG") as logs:
            self.github._post_object(self.url("/generate-jitconfig"))
            for path in ("/forbidden", "/unauthorized"):
                self.wait_out_the_stop()
                with self.assertRaises(GitHubUnavailable) as raised:
                    self.github._get_object(self.url(path))
                messages.append(str(raised.exception))
        self.assertIn("Must have admin rights to Repository.", messages[0])
        self.assertTrue(
            any("Rate Limit Remaining: 100" in line for line in logs.output)
        )
        everything_said = "\n".join(logs.output + messages)
        for secret in secrets:
            self.assertNotIn(secret, everything_said)


if __name__ == "__main__":
    unittest.main()
