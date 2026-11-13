"""demand without GitHub: the pure functions on recorded GitHub answers, the reading functions against the fake GitHub."""

import json
import unittest
from pathlib import Path

from demand import (
    Demand,
    WaitingJob,
    demand_from_jobs,
    read_latest_jobs,
    run_ids_to_watch,
    size_label_of_job,
    stop_watching_finished_runs,
    watch_new_runs,
)
from config import NEWEST_RUNS_PAGE_SIZE
from github_client import GitHubClient
from test_github_client import FakeGitHub, Reply, answer_304_to

# Saved by gha-queue-wait whenever a run with a waiting job was missing from runs?status=queued:
# each GitHub answer about that run, cut to a few fields
RECORDED = sorted((Path(__file__).parent / "recorded").glob("*.json"))
REPO = "/repos/WATonomous/infra-config"
NEWEST_RUNS = (
    f"{REPO}/actions/runs?per_page={NEWEST_RUNS_PAGE_SIZE}&exclude_pull_requests=true"
)
QUEUED_RUNS = (
    f"{REPO}/actions/runs?status=queued&exclude_pull_requests=true&per_page=100"
)
IN_PROGRESS_RUNS = (
    f"{REPO}/actions/runs?status=in_progress&exclude_pull_requests=true&per_page=100"
)


def jobs_path(run_id):
    return f"{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"


def run_path(run_id):
    return f"{REPO}/actions/runs/{run_id}?exclude_pull_requests=true"


def recorded(path):
    """One capture: the run it is about, its answers, and its jobs with the fields the capture left out."""
    capture = json.loads(path.read_text())
    run = capture["direct_run"]["response"]
    jobs = capture["jobs"]["response"]["jobs"]
    for job in jobs:
        job.update(run_id=run["id"], workflow_name=run["name"])
    return run, capture, jobs


def runs_of(capture, *answers):
    return [
        run
        for answer in answers
        for run in capture[answer]["response"]["workflow_runs"]
    ]


def job(
    job_id, status, labels=("slurm-runner-small",), runner_name=None, run_attempt=1
):
    return {
        "id": job_id,
        "status": status,
        "labels": list(labels),
        "runner_name": runner_name,
        "workflow_name": "Provision",
        "name": f"job-{job_id}",
        "run_attempt": run_attempt,
    }


def run(run_id, status, run_attempt=1):
    return {"id": run_id, "status": status, "run_attempt": run_attempt}


def runs_answer(*runs):
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def jobs_answer(*jobs):
    return {"total_count": len(jobs), "jobs": list(jobs)}


class RecordedAnswersTest(unittest.TestCase):
    def test_the_captures_are_the_cases_the_status_lists_missed(self):
        self.assertEqual(len(RECORDED), 5)
        for path in RECORDED:
            with self.subTest(capture=path.name):
                run, capture, _ = recorded(path)
                listed = runs_of(
                    capture, "list_status_queued", "list_status_in_progress"
                )
                self.assertNotIn(run["id"], {listed_run["id"] for listed_run in listed})

    def test_each_missed_run_is_watched_from_the_newest_runs(self):
        for path in RECORDED:
            with self.subTest(capture=path.name):
                run, capture, _ = recorded(path)
                to_watch = run_ids_to_watch(
                    runs_of(capture, "list_unfiltered"),
                    runs_of(capture, "list_status_queued", "list_status_in_progress"),
                )
                self.assertIn(run["id"], to_watch)

    def test_each_queued_job_waits_whatever_its_run_says(self):
        run_statuses = set()
        for path in RECORDED:
            with self.subTest(capture=path.name):
                run, _, jobs = recorded(path)
                run_statuses.add(run["status"])
                demand = demand_from_jobs({run["id"]: jobs})
                queued = [job["id"] for job in jobs if job["status"] == "queued"]
                self.assertTrue(queued)
                self.assertEqual([job.job_id for job in demand.waiting_jobs], queued)
        self.assertEqual(run_statuses, {"pending", "queued"})

    def test_the_pending_run_with_a_queued_job(self):
        path = next(path for path in RECORDED if "run43326533215" in path.name)
        run, _, jobs = recorded(path)
        self.assertEqual(run["status"], "pending")
        self.assertEqual(
            demand_from_jobs({run["id"]: jobs}).waiting_jobs,
            [
                WaitingJob(
                    job_id=131560293044,
                    run_id=43326533215,
                    size_label="slurm-runner-small",
                    labels=("slurm-runner-small",),
                    workflow_name="Check pull request task list completion",
                    job_name="task-list-checker",
                )
            ],
        )

    def test_runner_names_come_from_jobs_of_every_status(self):
        path = next(path for path in RECORDED if "run43368862162" in path.name)
        run, _, jobs = recorded(path)
        names = demand_from_jobs({run["id"]: jobs}).runner_names_on_jobs
        expected = {job["runner_name"] for job in jobs if job["runner_name"]}
        self.assertEqual(names, expected)
        self.assertGreater(len(names), 50)


class PureFunctionsTest(unittest.TestCase):
    def test_completed_runs_on_the_newest_page_are_not_watched_but_listed_runs_are(
        self,
    ):
        newest = [run(1, "completed"), run(2, "queued"), run(3, "pending")]
        newest += [run(4, "in_progress"), run(5, "waiting")]
        listed = [run(1, "queued"), run(6, "in_progress")]
        self.assertEqual(run_ids_to_watch(newest, listed), {1, 2, 3, 4, 5, 6})
        self.assertEqual(run_ids_to_watch([], []), set())

    def test_only_queued_jobs_wait(self):
        statuses = [
            "queued",
            "in_progress",
            "completed",
            "waiting",
            "requested",
            "pending",
        ]
        jobs = [job(index, status) for index, status in enumerate(statuses)]
        waiting = demand_from_jobs({1: jobs}).waiting_jobs
        self.assertEqual([waiting_job.job_id for waiting_job in waiting], [0])

    def test_a_job_waits_for_us_only_if_its_first_label_names_a_slurm_runner(self):
        rows = [
            ([], None),
            (["ubuntu-latest"], None),
            (["self-hosted", "slurm-runner-medium"], None),
            (["slurm-runner-medium"], "slurm-runner-medium"),
            (["slurm-runner-4cpu-2mempercpu"], "slurm-runner-4cpu-2mempercpu"),
        ]
        for labels, size_label in rows:
            with self.subTest(labels=labels):
                self.assertEqual(size_label_of_job(labels), size_label)
                waiting = demand_from_jobs({1: [job(7, "queued", labels)]}).waiting_jobs
                self.assertEqual(
                    [j.size_label for j in waiting], [size_label] if size_label else []
                )

    def test_a_waiting_job_keeps_all_its_labels(self):
        labels = ("slurm-runner-medium", "gpu")
        waiting = demand_from_jobs({9: [job(7, "queued", labels)]}).waiting_jobs
        self.assertEqual(
            waiting,
            [WaitingJob(7, 9, "slurm-runner-medium", labels, "Provision", "job-7")],
        )

    def test_runner_names_skip_jobs_without_a_runner(self):
        jobs = [
            job(1, "queued"),
            job(2, "in_progress", runner_name="slurm-a-1"),
            job(3, "completed", runner_name="slurm-b-2"),
            job(4, "completed", runner_name=""),  # skipped jobs report an empty name
        ]
        self.assertEqual(
            demand_from_jobs({1: jobs}).runner_names_on_jobs, {"slurm-a-1", "slurm-b-2"}
        )

    def test_no_runs_no_demand(self):
        self.assertEqual(demand_from_jobs({}), Demand([], set()))
        self.assertEqual(demand_from_jobs({1: []}), Demand([], set()))


class ReadingTest(unittest.TestCase):
    """The reading functions against the fake GitHub, one GitHubClient cycle per demand cycle."""

    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.github = GitHubClient("test-token")
        self.repo = self.fake.base_url + REPO
        for path in (NEWEST_RUNS, QUEUED_RUNS, IN_PROGRESS_RUNS):
            self.fake.script(path, Reply(200, body=runs_answer()))

    def cycle(self, watched_run_ids):
        """main's order: watch; read each run, newest first, and take its waiting jobs; stop watching.
        The waiting job ids and the runs still watched."""
        self.github.start_cycle()
        watched_run_ids = watch_new_runs(self.github, self.repo, watched_run_ids)
        jobs_by_run, waiting = {}, []
        for run_id in sorted(watched_run_ids, reverse=True):
            jobs_of_run = read_latest_jobs(self.github, self.repo, {run_id})
            jobs_by_run.update(jobs_of_run)
            waiting += [
                job.job_id for job in demand_from_jobs(jobs_of_run).waiting_jobs
            ]
        still_watched = stop_watching_finished_runs(
            self.github, self.repo, watched_run_ids, jobs_by_run
        )
        return waiting, still_watched

    def test_runs_are_added_from_all_three_lists_and_never_removed_by_them(self):
        self.fake.script(
            NEWEST_RUNS,
            Reply(200, body=runs_answer(run(1, "pending"), run(2, "completed"))),
        )
        self.fake.script(QUEUED_RUNS, Reply(200, body=runs_answer(run(3, "queued"))))
        self.fake.script(
            IN_PROGRESS_RUNS, Reply(200, body=runs_answer(run(4, "in_progress")))
        )
        self.assertEqual(watch_new_runs(self.github, self.repo, {9}), {1, 3, 4, 9})

    def test_a_list_that_cannot_be_read_adds_nothing(self):
        self.fake.script(NEWEST_RUNS, Reply(502, body={"message": "Server Error"}))
        self.fake.script(QUEUED_RUNS, Reply(200, body=runs_answer(run(3, "queued"))))
        self.fake.script(IN_PROGRESS_RUNS, Reply(200, body={"message": "no list"}))
        with self.assertLogs("demand", level="WARNING"):
            self.assertEqual(watch_new_runs(self.github, self.repo, {9}), {3, 9})

    def test_jobs_are_read_from_the_latest_attempt_and_a_failed_run_is_left_out(self):
        self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(job(11, "queued"))))
        self.fake.script(jobs_path(2), Reply(502, body={"message": "Server Error"}))
        self.fake.script(jobs_path(3), Reply(200, body=jobs_answer()))
        with self.assertLogs("demand", level="WARNING") as logs:
            jobs_by_run = read_latest_jobs(self.github, self.repo, {1, 2, 3})
        self.assertEqual(sorted(jobs_by_run), [1, 3])
        self.assertEqual(jobs_by_run[1][0]["id"], 11)
        self.assertIn("Left run 2 out of this cycle", logs.output[0])

    def test_a_run_whose_jobs_cannot_be_read_stays_watched_without_asking(self):
        self.assertEqual(
            stop_watching_finished_runs(self.github, self.repo, {2}, {}), {2}
        )
        self.assertEqual(self.fake.received, [])

    def test_a_run_with_an_unfinished_job_stays_watched_without_asking(self):
        jobs_by_run = {1: [job(11, "completed"), job(12, "waiting")]}
        self.assertEqual(
            stop_watching_finished_runs(self.github, self.repo, {1}, jobs_by_run), {1}
        )
        self.assertEqual(self.fake.received, [])

    def test_a_run_whose_own_read_fails_stays_watched(self):
        self.fake.script(run_path(1), Reply(502, body={"message": "Server Error"}))
        with self.assertLogs("demand", level="WARNING"):
            still_watched = stop_watching_finished_runs(
                self.github, self.repo, {1}, {1: [job(11, "completed")]}
            )
        self.assertEqual(still_watched, {1})

    def test_a_run_with_no_jobs_stays_watched_until_it_completes(self):
        self.fake.script(NEWEST_RUNS, Reply(200, body=runs_answer(run(1, "queued"))))
        self.fake.script(jobs_path(1), answer_304_to('"none"', jobs_answer()))
        self.fake.script(run_path(1), Reply(200, body=run(1, "queued")))
        for _ in range(3):
            self.assertEqual(self.cycle(set()), ([], {1}))

        # Cancelled before any job was created, as a run replaced in its concurrency group is
        self.fake.script(NEWEST_RUNS, Reply(200, body=runs_answer(run(1, "completed"))))
        self.fake.script(run_path(1), Reply(200, body=run(1, "completed")))
        self.assertEqual(self.cycle({1}), ([], set()))
        self.assertEqual(self.cycle(set()), ([], set()))

    def test_a_run_between_two_waves_stays_watched_and_its_next_wave_waits(self):
        # The run is no longer on any list: only the watched set keeps it in view
        self.fake.script(
            jobs_path(1), Reply(200, body=jobs_answer(job(11, "completed")))
        )
        self.fake.script(run_path(1), Reply(200, body=run(1, "in_progress")))
        self.assertEqual(self.cycle({1}), ([], {1}))

        next_wave = jobs_answer(
            job(11, "completed"), job(12, "queued"), job(13, "queued")
        )
        self.fake.script(jobs_path(1), Reply(200, body=next_wave))
        self.assertEqual(self.cycle({1}), ([12, 13], {1}))
        self.assertEqual(len(self.fake.requests_to(run_path(1))), 1)

    def test_a_job_waits_through_its_runs_whole_life(self):
        """Blocked behind its concurrency group, released with a queued job while still saying pending
        (#39), between waves, then completed."""
        steps = [
            # (run status, jobs, expected waiting, expected still watched)
            ("pending", [], [], {1}),
            ("pending", [job(11, "queued")], [11], {1}),
            ("in_progress", [job(11, "in_progress", runner_name="r1")], [], {1}),
            ("in_progress", [job(11, "completed", runner_name="r1")], [], {1}),
            ("queued", [job(11, "completed"), job(12, "queued")], [12], {1}),
            ("completed", [job(11, "completed"), job(12, "completed")], [], set()),
        ]
        watched = set()
        for run_status, jobs, waiting, still_watched in steps:
            with self.subTest(run_status=run_status, jobs=jobs):
                newest = runs_answer(run(1, run_status))
                self.fake.script(NEWEST_RUNS, Reply(200, body=newest))
                self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(*jobs)))
                self.fake.script(run_path(1), Reply(200, body=run(1, run_status)))
                found_waiting, watched = self.cycle(watched)
                self.assertEqual((found_waiting, watched), (waiting, still_watched))

    def test_a_failed_read_of_one_run_loses_only_that_run_for_one_cycle(self):
        self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(job(11, "queued"))))
        self.fake.script(
            jobs_path(2),
            Reply(502, body={"message": "Server Error"}),
            Reply(200, body=jobs_answer(job(21, "queued"))),
        )
        with self.assertLogs("demand", level="WARNING"):
            self.assertEqual(self.cycle({1, 2}), ([11], {1, 2}))
        self.assertEqual(self.cycle({1, 2}), ([21, 11], {1, 2}))

    def test_a_rerun_of_a_finished_run_is_watched_again_from_the_status_lists(self):
        old_run_jobs = jobs_answer(job(11, "completed"))
        self.fake.script(jobs_path(1), Reply(200, body=old_run_jobs))
        self.fake.script(run_path(1), Reply(200, body=run(1, "completed")))
        self.assertEqual(self.cycle({1}), ([], set()))

        # Re-run: same run id, too old for the newest page, latest attempt's jobs only
        self.fake.script(QUEUED_RUNS, Reply(200, body=runs_answer(run(1, "queued"))))
        self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(job(31, "queued"))))
        self.fake.script(run_path(1), Reply(200, body=run(1, "queued")))
        self.assertEqual(self.cycle(set()), ([31], {1}))

    def test_a_rerun_requested_after_the_jobs_were_read_keeps_the_run(self):
        """Attempt 1's jobs, all finished, are read; then a re-run starts, and the run's own read
        still says completed (run status lags its jobs, #39) but already names attempt 2."""
        self.fake.script(
            jobs_path(1), Reply(200, body=jobs_answer(job(11, "completed")))
        )
        self.fake.script(
            run_path(1), Reply(200, body=run(1, "completed", run_attempt=2))
        )
        self.assertEqual(self.cycle({1}), ([], {1}))

        rerun_jobs = jobs_answer(job(31, "queued", run_attempt=2))
        self.fake.script(jobs_path(1), Reply(200, body=rerun_jobs))
        self.assertEqual(self.cycle({1}), ([31], {1}))

    def test_a_run_leaves_when_its_newest_jobs_are_from_its_current_attempt(self):
        # Jobs kept from attempt 1 by "re-run failed jobs" do not hold the run back
        jobs = jobs_answer(job(11, "completed"), job(31, "completed", run_attempt=2))
        self.fake.script(jobs_path(1), Reply(200, body=jobs))
        self.fake.script(
            run_path(1), Reply(200, body=run(1, "completed", run_attempt=2))
        )
        self.assertEqual(self.cycle({1}), ([], set()))

    def test_a_deleted_run_leaves_when_its_jobs_and_itself_answer_404(self):
        not_found = Reply(404, body={"message": "Not Found"})
        self.fake.script(jobs_path(1), not_found)
        self.fake.script(run_path(1), not_found)
        self.assertEqual(self.cycle({1}), ([], set()))
        self.assertEqual(self.cycle(set()), ([], set()))
        self.assertEqual(len(self.fake.requests_to(jobs_path(1))), 1)

    def test_one_404_does_not_drop_a_run(self):
        not_found = Reply(404, body={"message": "Not Found"})
        # Its jobs answer 404 once, its own read says it is between waves
        self.fake.script(jobs_path(1), not_found)
        self.fake.script(run_path(1), Reply(200, body=run(1, "in_progress")))
        self.assertEqual(self.cycle({1}), ([], {1}))
        # Its jobs are read, all finished, and its own read answers 404 once
        self.fake.script(
            jobs_path(1), Reply(200, body=jobs_answer(job(11, "completed")))
        )
        self.fake.script(run_path(1), not_found)
        with self.assertLogs("demand", level="WARNING"):
            self.assertEqual(self.cycle({1}), ([], {1}))

    def test_a_run_dropped_on_two_stray_404s_comes_back_from_the_newest_page(self):
        not_found = Reply(404, body={"message": "Not Found"})
        self.fake.script(
            jobs_path(1), not_found, Reply(200, body=jobs_answer(job(11, "queued")))
        )
        self.fake.script(run_path(1), not_found)
        self.assertEqual(self.cycle({1}), ([], set()))
        self.fake.script(NEWEST_RUNS, Reply(200, body=runs_answer(run(1, "queued"))))
        self.assertEqual(self.cycle(set()), ([11], {1}))

    def test_an_idle_cycle_is_answered_by_304s(self):
        self.fake.script(
            NEWEST_RUNS, answer_304_to('"n"', runs_answer(run(1, "queued")))
        )
        self.fake.script(
            QUEUED_RUNS, answer_304_to('"q"', runs_answer(run(1, "queued")))
        )
        self.fake.script(IN_PROGRESS_RUNS, answer_304_to('"i"', runs_answer()))
        self.fake.script(jobs_path(1), answer_304_to('"j"', jobs_answer()))
        self.fake.script(run_path(1), answer_304_to('"r"', run(1, "queued")))
        watched = set()
        for _ in range(2):
            waiting, watched = self.cycle(watched)
        second_cycle = self.fake.received[5:]
        self.assertEqual(len(second_cycle), 5)
        self.assertTrue(
            all("If-None-Match" in headers for _, _, headers in second_cycle)
        )


if __name__ == "__main__":
    unittest.main()
