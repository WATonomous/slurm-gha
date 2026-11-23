"""cycle_summary by table, and the totals the GitHub client and a whole cycle add to SPENT."""

import logging
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest import mock

import github_client
import main
from config import CYCLE_SUMMARY_INTERVAL_S
from cycle_summary import (
    SPENT,
    Spent,
    SummaryWindow,
    cycle_line,
    cycle_times,
    summary_line,
    window_with_cycle,
)
from github_client import GitHubClient, GitHubUnavailable, RateLimitWindow
from main import empty_memory, log_cycle
from test_demand import job
from test_github_client import (
    NOW,
    FakeClock,
    FakeGitHub,
    Reply,
    answer_304_to,
)
from test_main import MEDIUM, SMALL, CycleAgainstFakes

SPENT_IN_A_BUSY_CYCLE = Spent(
    github_read_s=6.25,
    github_write_s=3.5,
    write_spacing_s=9.0,
    slurm_s=4.0,
    reads=14,
    charged_reads=11,
    writes=18,
    runners_submitted=18,
)


class CycleTimesTest(unittest.TestCase):
    def test_the_parts_add_up_to_the_duration(self):
        cycle = cycle_times(
            SPENT_IN_A_BUSY_CYCLE, duration_s=26.0, pause_s=2.0, jobs_queued=5
        )
        spent = cycle.spent
        parts = (
            spent.github_read_s,
            spent.github_write_s,
            spent.write_spacing_s,
            spent.slurm_s,
            cycle.pause_s,
            cycle.other_s,
        )
        self.assertAlmostEqual(sum(parts), 26.0)
        self.assertAlmostEqual(cycle.other_s, 1.25)

    def test_what_is_left_is_never_negative(self):
        # Clock granularity can make the measured parts a hair longer than the cycle
        cycle = cycle_times(Spent(github_read_s=2.0001), 2.0, 0.0, 0)
        self.assertEqual(cycle.other_s, 0.0)

    def test_the_debug_line(self):
        cycle = cycle_times(SPENT_IN_A_BUSY_CYCLE, 26.0, 2.0, 5)
        self.assertEqual(
            cycle_line(cycle),
            "Cycle took 26.0 s: GitHub reads 6.2 s, GitHub writes 3.5 s + 9.0 s spacing, Slurm 4.0 s,"
            " pause 2.0 s, other 1.2 s; 14 reads (11 charged), 18 writes; 18 runners submitted;"
            " 5 jobs queued",
        )


class SummaryLineTest(unittest.TestCase):
    def window(self, *cycles):
        window = SummaryWindow(started_at=100.0)
        for cycle in cycles:
            window = window_with_cycle(window, cycle)
        return window

    def test_a_minute_with_work_sums_its_cycles(self):
        busy = cycle_times(SPENT_IN_A_BUSY_CYCLE, 26.0, 2.0, 5)
        quieter = cycle_times(
            Spent(github_read_s=3.0, slurm_s=0.5, reads=12, charged_reads=4),
            duration_s=10.0,
            pause_s=6.0,
            jobs_queued=2,
        )
        line = summary_line(
            self.window(busy, quieter, quieter),
            ended_at=166.0,
            lowest_remaining=4512,
            resets_in_s=2460,
        )
        self.assertEqual(
            line,
            "Last 66 s: 3 cycles, longest 26.0 s; GitHub reads 12.2 s, GitHub writes 3.5 s + 9.0 s"
            " spacing, Slurm 5.0 s, pause 14.0 s, other 2.2 s; 38 reads (19 charged), 18 writes;"
            " 18 runners submitted; 2 jobs queued at the end; rate limit 4512 left at this hour's"
            " lowest reading, resets in 41 min",
        )

    def test_an_outage_alone_logs_nothing_here(self):
        # The repository's last count was 2, but no cycle in the minute completed: no fresh count, no line
        failed = cycle_times(Spent(), 2.0, 2.0, jobs_queued=None)
        self.assertIsNone(summary_line(self.window(failed, failed), 166.0, 4800, 600))

    def test_a_minute_that_ends_in_an_outage_says_the_count_is_unknown(self):
        busy = cycle_times(SPENT_IN_A_BUSY_CYCLE, 26.0, 2.0, 5)
        failed = cycle_times(Spent(), 2.0, 30.0, jobs_queued=None)
        line = summary_line(self.window(busy, failed), 166.0, None, None)
        self.assertTrue(
            line.startswith("Last 66 s: 2 cycles (1 failed), longest 26.0 s;")
        )
        self.assertTrue(
            line.endswith(
                "18 runners submitted; jobs queued at the end unknown: the last cycle did not complete"
            )
        )
        self.assertIn(
            "jobs queued unknown: the cycle did not complete", cycle_line(failed)
        )

    def test_a_minute_without_work_has_no_line(self):
        idle = cycle_times(Spent(github_read_s=1.0, reads=4), 3.0, 2.0, 0)
        self.assertIsNone(summary_line(self.window(idle, idle), 166.0, 5000, 600))

    def test_a_job_that_waited_or_a_runner_submitted_is_work(self):
        waited = cycle_times(Spent(reads=4), 3.0, 2.0, jobs_queued=1)
        served = cycle_times(Spent(reads=4), 3.0, 2.0, jobs_queued=0)
        self.assertIsNotNone(
            summary_line(self.window(waited, served), 166.0, None, None)
        )
        submitted = cycle_times(Spent(runners_submitted=1), 3.0, 2.0, jobs_queued=0)
        line = summary_line(self.window(submitted), 166.0, None, None)
        self.assertTrue(line.endswith("1 runner submitted; 0 jobs queued at the end"))


class LogCycleTest(unittest.TestCase):
    def setUp(self):
        self.github = GitHubClient("test-token")
        self.github.rate_limit_window = RateLimitWindow(NOW + 600, 4800)

    def log(self, window, ended_at, jobs_queued):
        return log_cycle(
            window,
            spent=Spent(reads=4, runners_submitted=1 if jobs_queued else 0),
            duration_s=5.0,
            pause_s=2.0,
            jobs_queued=jobs_queued,
            ended_at=ended_at,
            github=self.github,
        )

    def test_one_info_line_per_interval_and_only_with_work(self):
        window = SummaryWindow(started_at=0.0)
        with self.assertLogs(level=logging.DEBUG) as logs:
            window = self.log(window, 30.0, jobs_queued=3)
            window = self.log(window, CYCLE_SUMMARY_INTERVAL_S + 1.0, jobs_queued=0)
            # A quiet interval: its cycles still log at DEBUG, the summary is skipped
            window = self.log(window, 2 * CYCLE_SUMMARY_INTERVAL_S + 5.0, jobs_queued=0)
        info = [r.getMessage() for r in logs.records if r.levelno == logging.INFO]
        debug = [r.getMessage() for r in logs.records if r.levelno == logging.DEBUG]
        self.assertEqual(len(debug), 3)
        self.assertEqual(len(info), 1)
        self.assertTrue(info[0].startswith("Last 61 s: 2 cycles, longest 5.0 s;"))
        self.assertEqual(
            window, SummaryWindow(started_at=2 * CYCLE_SUMMARY_INTERVAL_S + 5.0)
        )

    def test_an_error_while_logging_is_logged_and_never_stops_the_cycles(self):
        class BrokenClient:
            @property
            def rate_limit_window(self):
                raise RuntimeError("broken")

        with self.assertLogs(level=logging.ERROR) as logs:
            window = log_cycle(
                SummaryWindow(started_at=0.0),
                spent=Spent(runners_submitted=1),
                duration_s=5.0,
                pause_s=2.0,
                jobs_queued=1,
                ended_at=CYCLE_SUMMARY_INTERVAL_S + 1.0,
                github=BrokenClient(),
            )
        self.assertEqual(
            window, SummaryWindow(started_at=CYCLE_SUMMARY_INTERVAL_S + 1.0)
        )
        self.assertIn("Could not log the cycle summary", logs.output[0])
        self.assertIn("RuntimeError: broken", logs.output[0])


class StopTheLoop(BaseException):
    """Ends main()'s endless loop from inside a test."""


class MainLoopCountsTest(unittest.TestCase):
    def test_only_a_completed_cycle_gives_a_queued_count(self):
        # The repository's second cycle fails: its memory keeps the first cycle's 3, which must not be reported
        answers = iter(
            [
                lambda memory: replace(memory, jobs_queued=3),
                GitHubUnavailable("HTTP 502"),
                lambda memory: replace(memory, jobs_queued=0),
            ]
        )

        def run_cycle(github, repo, memory, cycle_started_at):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer(memory)

        reported = []

        def log_cycle(
            window, spent, duration_s, pause_s, jobs_queued, ended_at, github
        ):
            reported.append(jobs_queued)
            if len(reported) == 3:
                raise StopTheLoop
            return window

        with ExitStack() as stack:
            for patcher in (
                mock.patch.object(main, "run_cycle", run_cycle),
                mock.patch.object(main, "log_cycle", log_cycle),
                mock.patch.object(main.time, "sleep"),
                mock.patch.object(
                    GitHubClient, "seconds_until_next_cycle", return_value=0
                ),
                mock.patch.object(main, "configure_logging"),
                mock.patch.object(main, "load_dotenv"),
                mock.patch.dict(main.os.environ, {"GITHUB_ACCESS_TOKEN": "test-token"}),
            ):
                stack.enter_context(patcher)
            stack.enter_context(self.assertLogs(level=logging.INFO))
            with self.assertRaises(StopTheLoop):
                main.main()
        self.assertEqual(reported, [3, None, 0])


class ClientCountsTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.clock = FakeClock(NOW)
        patcher = mock.patch.object(github_client, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = GitHubClient("test-token")
        self.github.start_cycle()

    def test_a_304_is_a_read_but_not_charged(self):
        self.fake.script("/runs", answer_304_to('"e1"', {"workflow_runs": []}))
        before = SPENT.snapshot()
        self.github._get_object(self.fake.base_url + "/runs")
        self.github._get_object(self.fake.base_url + "/runs")
        spent = SPENT.minus(before)
        self.assertEqual((spent.reads, spent.charged_reads, spent.writes), (2, 1, 0))

    def test_writes_and_their_spacing_are_counted_apart_from_reads(self):
        self.fake.script("/token", Reply(201, body={"token": "x"}))
        before = SPENT.snapshot()
        self.github._post_object(self.fake.base_url + "/token")
        self.github._post_object(self.fake.base_url + "/token")
        spent = SPENT.minus(before)
        self.assertEqual((spent.reads, spent.writes), (0, 2))
        # The fake clock moves only while the client sleeps: the second write waited the full spacing
        self.assertEqual(
            spent.write_spacing_s, github_client.MIN_SECONDS_BETWEEN_WRITES
        )
        self.assertEqual(self.clock.slept, [github_client.MIN_SECONDS_BETWEEN_WRITES])


class CycleCountsTest(CycleAgainstFakes):
    def test_a_cycle_counts_every_request_it_sends_and_every_runner_it_submits(self):
        self.jobs(
            *(job(i, "queued", [MEDIUM]) for i in range(3)), job(9, "queued", [SMALL])
        )
        before = SPENT.snapshot()
        memory = self.cycle(empty_memory())
        spent = SPENT.minus(before)
        methods = [method for method, _, _ in self.fake.received]
        self.assertEqual(spent.reads, methods.count("GET"))
        # The fake answers every read 200, without an ETag: each is charged
        self.assertEqual(spent.charged_reads, spent.reads)
        self.assertEqual(spent.writes, len(methods) - methods.count("GET"))
        self.assertEqual(spent.runners_submitted, len(self.slurm.commands("sbatch")))
        self.assertEqual(memory.jobs_queued, 4)


if __name__ == "__main__":
    unittest.main()
