"""main without Slurm or GitHub: one repository's cycle against the fake Slurm and the fake GitHub."""

import logging
import unittest
from unittest import mock

import github_client
import main
import supply
from config import (
    BUSY_COVER_S as T,
    HISTORY_PAGE_INTERVAL_S,
    IDLE_ALLOWANCE_S as W,
    UNUSED_REGISTRATION_LIFETIME_S as A_DAY,
    Repository,
)
from github_client import GitHubClient, GitHubUnavailable
from main import (
    RepositoryMemory,
    empty_memory,
    outage_after_cycle,
    run_cycle,
    unsafe_settings,
)
from plan import NO_SIGHTINGS, LabelBreaker, is_proven
from supply import JIT_CONFIG_VARIABLE, SlurmUnavailable
from test_demand import (
    IN_PROGRESS_RUNS,
    NEWEST_RUNS,
    QUEUED_RUNS,
    REPO,
    history_path,
    job,
    jobs_answer,
    jobs_path,
    run,
    run_path,
    runs_answer,
)
from test_github_client import FakeGitHub, Reply
from test_supply import JIT_CONFIG, FakeSlurm, runner

NOW = 1_800_000_000
PREFIX = "gha-test-"
MEDIUM = "slurm-runner-medium"
SMALL = "slurm-runner-small"
RUNNERS = f"{REPO}/actions/runners?per_page=100"
GENERATE_JIT_CONFIG = f"{REPO}/actions/runners/generate-jitconfig"


def memory_with(breakers):
    """A daemon that has run for a while: these breakers, and the history page not due."""
    return RepositoryMemory(
        set(), 1, NOW + HISTORY_PAGE_INTERVAL_S, breakers, NO_SIGHTINGS
    )


class CycleAgainstFakes(unittest.TestCase):
    """One repository, the fake Slurm and the fake GitHub, and a cycle at a time; no tests of its own."""

    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.github = GitHubClient("test-token")
        self.repo = Repository(
            name="WATonomous/infra-config",
            api_base_url=self.fake.base_url + REPO,
            runner_name_prefix=PREFIX,
        )
        self.slurm = FakeSlurm(NOW)
        for patcher in (
            mock.patch.object(supply.subprocess, "run", self.slurm.run),
            mock.patch.object(github_client, "MIN_SECONDS_BETWEEN_WRITES", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.fake.script(
            NEWEST_RUNS, Reply(200, body=runs_answer(run(1, "in_progress")))
        )
        self.fake.script(QUEUED_RUNS, Reply(200, body=runs_answer()))
        self.fake.script(IN_PROGRESS_RUNS, Reply(200, body=runs_answer()))
        self.fake.script(run_path(1), Reply(200, body=run(1, "in_progress")))
        self.fake.script(history_path(1), Reply(200, body=runs_answer()))
        self.runners = []  # the runners list as GitHub answers it, changed by the tests
        self.fake.script(
            RUNNERS, lambda headers: Reply(200, body={"runners": self.runners})
        )
        self.jit_config_statuses = []  # statuses of the next generate-jitconfig answers; 201 after
        self.fake.script(GENERATE_JIT_CONFIG, self.answer_generate_jit_config)

    def answer_generate_jit_config(self, request_headers):
        status = self.jit_config_statuses.pop(0) if self.jit_config_statuses else 201
        if status != 201:
            return Reply(status, body={"message": "Server Error"})
        return Reply(201, body={"runner": {"id": 9}, "encoded_jit_config": JIT_CONFIG})

    def jobs(self, *jobs_of_run_1):
        self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(*jobs_of_run_1)))

    def cycle(self, memory, now=NOW):
        self.github.start_cycle()
        return run_cycle(self.github, self.repo, memory, now)

    def submitted_size_labels(self):
        return [
            next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--comment="))
            for argv in self.slurm.commands("sbatch")
        ]

    def add_runner(self, state, status="online", busy=False, started_ago_s=30):
        """One of our allocations in Slurm and, with status, its registration."""
        name = f"{PREFIX}{len(self.slurm.jobs):012x}"
        started_at = "N/A" if state == "PENDING" else NOW - started_ago_s
        self.slurm.add_job(
            name, state, submitted_at=NOW - started_ago_s - 5, started_at=started_at
        )
        if status is not None:
            self.runners.append(
                {**runner(name, status, busy), "id": len(self.slurm.jobs)}
            )
        return name


class RunCycleTest(CycleAgainstFakes):
    def test_a_first_cycle_sends_one_runner_per_unproven_label(self):
        self.jobs(
            *(job(i, "queued", [MEDIUM]) for i in range(5)), job(9, "queued", [SMALL])
        )
        memory = self.cycle(empty_memory())
        self.assertEqual(self.submitted_size_labels(), [MEDIUM, SMALL])
        self.assertEqual(
            memory,
            RepositoryMemory({1}, 1, NOW + HISTORY_PAGE_INTERVAL_S, {}, NO_SIGHTINGS),
        )
        # Supply before demand: a runner that took a job after the jobs were read would look free
        paths = [path for _, path, _ in self.fake.received]
        self.assertLess(paths.index(RUNNERS), paths.index(jobs_path(1)))

    def test_the_history_is_read_one_page_per_interval(self):
        self.jobs()
        memory = empty_memory()
        for now in (NOW, NOW + 1, NOW + HISTORY_PAGE_INTERVAL_S - 1):
            memory = self.cycle(memory, now)
        self.assertEqual(len(self.fake.requests_to(history_path(1))), 1)
        memory = self.cycle(memory, NOW + HISTORY_PAGE_INTERVAL_S)
        self.assertEqual(len(self.fake.requests_to(history_path(1))), 2)
        self.assertEqual(memory.next_history_page_at, NOW + 2 * HISTORY_PAGE_INTERVAL_S)

    def test_a_restart_in_the_middle_of_a_wave(self):
        busy = [self.add_runner("RUNNING", busy=True) for _ in range(6)]
        for _ in range(2):
            # Online and idle, and registered and waiting for a node: both can take a job
            self.add_runner("RUNNING")
            self.add_runner("PENDING", "offline")
        # Its registration is not listed yet: still on its way
        self.add_runner("PENDING", status=None)
        never_registered = self.add_runner("PENDING", status=None, started_ago_s=A_DAY)
        self.runners.append(runner(f"{PREFIX}leftover", "offline"))  # its sbatch failed
        self.runners.append(runner("slurm-thor-slurm1-747004"))  # the old daemon's
        self.jobs(
            *(
                job(i, "in_progress", [MEDIUM], runner_name=name)
                for i, name in enumerate(busy)
            ),
            *(job(100 + i, "queued", [MEDIUM]) for i in range(15)),
        )

        memory = self.cycle(empty_memory())  # memory lost: every label starts unproven

        # The busy runners prove the label in this first cycle, so the whole gap goes out: 15 - 5
        self.assertTrue(is_proven(memory.breakers[MEDIUM]))
        self.assertEqual(self.submitted_size_labels(), [MEDIUM] * 10)
        never_registered_id = next(
            slurm_job_id
            for slurm_job_id, each in self.slurm.jobs.items()
            if each["name"] == never_registered
        )
        self.assertEqual(
            self.slurm.commands("scancel"),
            [["scancel", "--ctld", "--state=PENDING", str(never_registered_id)]],
        )
        # No Slurm job has ended, so nothing on GitHub is deleted: only JIT configs are created
        self.assertEqual(
            {method for method, _, _ in self.fake.received}, {"GET", "POST"}
        )
        self.assertEqual(
            {path for method, path, _ in self.fake.received if method == "POST"},
            {GENERATE_JIT_CONFIG},
        )

    def test_supply_that_cannot_be_read_changes_nothing(self):
        # Would be cancelled
        self.add_runner("PENDING", status=None, started_ago_s=A_DAY)
        self.jobs(job(1, "queued", [MEDIUM]))

        self.slurm.failing_commands.add("squeue")
        with self.assertRaises(SlurmUnavailable):
            self.cycle(empty_memory())
        self.slurm.failing_commands.clear()

        self.fake.script(RUNNERS, Reply(502, body={"message": "Server Error"}))
        with self.assertRaises(GitHubUnavailable):
            self.cycle(empty_memory())

        self.assertEqual(self.slurm.commands("scancel"), [])
        self.assertEqual(self.slurm.commands("sbatch"), [])
        self.assertEqual(self.fake.requests_to(GENERATE_JIT_CONFIG), [])

    def test_submissions_alternate_between_labels_and_a_failed_one_is_skipped(self):
        proven = LabelBreaker(NOW - 3600, ())
        memory = memory_with({MEDIUM: proven, SMALL: proven})
        self.jobs(
            *(job(i, "queued", [MEDIUM]) for i in range(3)),
            *(job(10 + i, "queued", [SMALL]) for i in range(2)),
        )
        self.jit_config_statuses = [201, 500]
        with self.assertLogs(level=logging.WARNING) as logs:
            self.cycle(memory)
        # Planned: medium, small, medium, small, medium; the first small failed at GitHub
        self.assertEqual(self.submitted_size_labels(), [MEDIUM, MEDIUM, SMALL, MEDIUM])
        self.assertEqual(len(self.fake.requests_to(GENERATE_JIT_CONFIG)), 5)
        self.assertIn(f"could not submit a runner for {SMALL}", logs.output[0])

    def test_a_failed_sbatch_is_skipped_too_and_the_cycle_still_returns(self):
        proven = LabelBreaker(NOW - 3600, ())
        memory = memory_with({MEDIUM: proven})
        self.jobs(*(job(i, "queued", [MEDIUM]) for i in range(2)))
        self.slurm.failing_commands.add("sbatch")
        with self.assertLogs(level=logging.WARNING) as logs:
            memory = self.cycle(memory)
        self.assertEqual(len(self.slurm.commands("sbatch")), 2)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(memory.watched_run_ids, {1})

    def test_the_jit_config_reaches_sbatch_only_through_the_environment(self):
        self.jobs(job(1, "queued", [MEDIUM]))
        with self.assertLogs(level=logging.DEBUG) as logs:
            self.cycle(empty_memory())
        ((argv, added_environment),) = [
            call for call in self.slurm.calls if call[0][0] == "sbatch"
        ]
        self.assertEqual(added_environment[JIT_CONFIG_VARIABLE], JIT_CONFIG)
        self.assertFalse(any(JIT_CONFIG in arg for arg in argv))
        self.assertFalse(any(JIT_CONFIG in line for line in logs.output))

    def test_a_label_whose_runners_keep_failing_gets_one_runner_at_a_time(self):
        self.jobs(*(job(i, "queued", [MEDIUM]) for i in range(20)))
        memory = empty_memory()
        t = NOW
        submitted = []
        for _ in range(8):
            for slurm_job_id, each in self.slurm.jobs.items():
                # Every runner fails within the second it starts, so Slurm stamps start = end
                if each["state"] == "PENDING":
                    each.update(state="FAILED", started_at=t - 5, ended_at=t - 5)
            before = len(self.slurm.commands("sbatch"))
            memory = self.cycle(memory, now=t)
            submitted.append(len(self.slurm.commands("sbatch")) - before)
            t += 30
        # Two failures in a row hold it to one runner per wait: 60 s, then 120 s, from the newest failure
        self.assertEqual(submitted, [1, 1, 0, 0, 1, 0, 0, 0])
        self.assertFalse(is_proven(memory.breakers[MEDIUM]))


class DeleteEndedRunnersTest(CycleAgainstFakes):
    """A stopped runner never signs off, and GitHub offers its session jobs until its registration goes."""

    def ended_runner(self, status="offline", busy=False):
        name = self.add_runner("COMPLETED", status, busy)
        slurm_job_id = next(
            slurm_job_id
            for slurm_job_id, each in self.slurm.jobs.items()
            if each["name"] == name
        )
        self.slurm.jobs[slurm_job_id]["ended_at"] = NOW - 5
        return name, f"{REPO}/actions/runners/{len(self.slurm.jobs)}"

    def deletes(self):
        return [path for method, path, _ in self.fake.received if method == "DELETE"]

    def test_an_ended_runners_idle_registration_is_deleted_once(self):
        _, path = self.ended_runner()
        # The runners list may still show it a cycle later
        self.fake.script(path, Reply(204, body=None))
        self.jobs()
        memory = self.cycle(empty_memory())
        self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path])

    def test_never_a_busy_pending_or_running_runners_nor_one_absent_from_squeue(self):
        self.ended_runner(busy=True)
        self.add_runner("PENDING", "offline")
        self.add_runner("RUNNING")
        # Its sbatch has not happened yet, or another daemon's
        self.runners.append({**runner(f"{PREFIX}not-in-squeue", "offline"), "id": 99})
        self.jobs()
        self.cycle(empty_memory())
        self.assertEqual(self.deletes(), [])

    def test_a_refused_delete_is_left_alone_and_logged_once(self):
        name, path = self.ended_runner()
        self.fake.script(path, Reply(422, body={"message": "Runner is busy"}))
        self.jobs()
        with self.assertLogs(level=logging.WARNING) as logs:
            memory = self.cycle(empty_memory())
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path])
        self.assertEqual(len([line for line in logs.output if name in line]), 1)

    def test_a_failed_delete_does_not_stop_the_cycle_and_is_tried_again(self):
        _, path = self.ended_runner()
        self.fake.script(path, Reply(500, body={"message": "Server Error"}))
        self.jobs(job(1, "queued", [MEDIUM]))
        memory = memory_with({MEDIUM: LabelBreaker(NOW - 3600, ())})
        with self.assertLogs(level=logging.WARNING):
            memory = self.cycle(memory)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])
        with self.assertLogs(level=logging.WARNING):
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path, path])


class SubmitAsRunsAreReadTest(CycleAgainstFakes):
    """Runners go out after each run's jobs are read, newest run first, without counting a job twice."""

    def setUp(self):
        super().setUp()
        self.fake.script(
            NEWEST_RUNS,
            Reply(200, body=runs_answer(run(2, "in_progress"), run(1, "in_progress"))),
        )
        self.fake.script(run_path(2), Reply(200, body=run(2, "in_progress")))

    def jobs_of(self, run_id, *jobs):
        self.fake.script(jobs_path(run_id), Reply(200, body=jobs_answer(*jobs)))

    def test_the_newest_runs_jobs_get_runners_before_the_next_run_is_read(self):
        self.jobs_of(2, *(job(20 + i, "queued", [MEDIUM]) for i in range(3)))
        self.jobs_of(1, *(job(10 + i, "queued", [MEDIUM]) for i in range(2)))
        self.cycle(memory_with({MEDIUM: LabelBreaker(NOW - 3600, ())}))
        paths = [path for _, path, _ in self.fake.received]
        first_jit_config = paths.index(GENERATE_JIT_CONFIG)
        self.assertLess(paths.index(RUNNERS), paths.index(jobs_path(2)))
        self.assertLess(paths.index(jobs_path(2)), first_jit_config)
        self.assertLess(first_jit_config, paths.index(jobs_path(1)))
        self.assertEqual(self.submitted_size_labels(), [MEDIUM] * 5)

    def test_launch_evidence_is_weighed_even_with_no_run_to_read(self):
        self.fake.script(NEWEST_RUNS, Reply(200, body=runs_answer()))
        failed = self.slurm.add_job(f"{PREFIX}a", "FAILED", started_at=NOW - 20)
        self.slurm.jobs[failed]["ended_at"] = NOW - 10
        memory = self.cycle(memory_with({MEDIUM: LabelBreaker(NOW - 3600, ())}))
        self.assertEqual(len(memory.breakers[MEDIUM].failures_since_success), 1)

    def test_an_unproven_label_still_gets_one_runner_in_the_whole_cycle(self):
        self.jobs_of(2, job(20, "queued", [MEDIUM]))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.cycle(empty_memory())
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])


class BusyRunnerCoversItsJobTest(CycleAgainstFakes):
    """A runner GitHub has offered a job covers it until the job names it, for at most T."""

    def test_a_cycle_inside_the_gap_submits_nothing_extra_until_t(self):
        self.add_runner("RUNNING", busy=True)
        # Its job, still queued and naming no runner
        self.jobs(job(1, "queued", [MEDIUM]))
        memory = memory_with({MEDIUM: LabelBreaker(NOW - 3600, ())})
        # The first cycle after a restart sees it busy for the first time, and counts on it from then
        memory = self.cycle(memory, NOW)
        memory = self.cycle(memory, NOW + T - 1)
        self.assertEqual(self.submitted_size_labels(), [])
        self.cycle(memory, NOW + T)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])

    def test_a_runner_once_named_does_not_cover_when_its_runs_read_fails(self):
        named = self.add_runner("RUNNING", busy=True)
        self.fake.script(
            NEWEST_RUNS,
            Reply(200, body=runs_answer(run(2, "in_progress"), run(1, "in_progress"))),
        )
        self.fake.script(run_path(2), Reply(200, body=run(2, "in_progress")))
        self.jobs(job(1, "in_progress", [MEDIUM], runner_name=named))
        self.fake.script(jobs_path(2), Reply(200, body=jobs_answer()))
        memory = self.cycle(memory_with({MEDIUM: LabelBreaker(NOW - 3600, ())}))
        self.fake.script(jobs_path(1), Reply(502, body={"message": "Server Error"}))
        self.fake.script(
            jobs_path(2), Reply(200, body=jobs_answer(job(2, "queued", [MEDIUM])))
        )
        with self.assertLogs(level=logging.WARNING):
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])


class OutageLogTest(unittest.TestCase):
    def test_lines_when_cycles_stop_change_cause_remind_and_recover(self):
        rate_limited = GitHubUnavailable(
            "GET runners: HTTP 403 (API rate limit exceeded)"
        )
        bad_token = GitHubUnavailable("GET runners: HTTP 401 (Bad credentials)")
        slurm_down = SlurmUnavailable("squeue: exit status 1")
        self.assertEqual(outage_after_cycle(None, None, NOW), (None, None))
        # A cycle every 2 s: rate limited, then a bad token from 300 s, then Slurm down from 1,200 s
        outage, lines = None, []
        for now in range(NOW, NOW + 1802, 2):
            error = rate_limited if now < NOW + 300 else bad_token
            error = slurm_down if now >= NOW + 1200 else error
            outage, line = outage_after_cycle(outage, error, now)
            if line is not None:
                lines.append((now - NOW, line))
        outage, line = outage_after_cycle(outage, None, NOW + 1802)
        self.assertIsNone(outage)
        lines.append((1802, line))
        self.assertEqual(
            lines,
            [
                (0, f"cycles stopped, GitHub cannot be read: {rate_limited}"),
                # The change of cause within GitHub shows at the next reminder
                (600, f"cycles still stopped after 600 s and 301 cycles: {bad_token}"),
                (1200, f"cycles still stopped, now Slurm cannot be read: {slurm_down}"),
                (
                    1800,
                    f"cycles still stopped after 1800 s and 901 cycles: {slurm_down}",
                ),
                (
                    1802,
                    f"cycles recovered after 1802 s and 901 failed cycles; the last: {slurm_down}",
                ),
            ],
        )


class UnsafeSettingsTest(unittest.TestCase):
    def repos(self, *prefixes):
        return [
            Repository(
                f"o/r{index}", f"https://api.github.com/repos/o/r{index}", prefix
            )
            for index, prefix in enumerate(prefixes)
        ]

    def test_b_must_stay_below_w(self):
        self.assertEqual(unsafe_settings(120, 600, self.repos("gha-a-")), [])
        for boot_allowance_s in (600, 900):
            with self.subTest(boot_allowance_s=boot_allowance_s):
                self.assertEqual(
                    len(unsafe_settings(boot_allowance_s, 600, self.repos("gha-a-"))), 1
                )

    def test_no_repository_may_call_another_ones_runners_its_own(self):
        self.assertEqual(unsafe_settings(120, 600, self.repos("gha-a-", "gha-b-")), [])
        for prefixes in (("gha-a-", "gha-a-"), ("gha-a", "gha-a-b-"), ("", "gha-b-")):
            with self.subTest(prefixes=prefixes):
                self.assertTrue(unsafe_settings(120, 600, self.repos(*prefixes)))

    def test_the_daemon_refuses_to_start_with_b_at_or_above_w(self):
        with (
            mock.patch.object(main, "BOOT_ALLOWANCE_S", W),
            mock.patch.object(main, "configure_logging"),
            mock.patch.object(main, "GitHubClient") as client,
        ):
            with self.assertLogs(level=logging.ERROR), self.assertRaises(SystemExit):
                main.main()
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
