"""
Which jobs are waiting for a runner. A run's status and the status-filtered run lists lag behind its
jobs (WATonomous/slurm-gha#39), so they only decide which runs to watch; whether a job waits is read
from the job's own status, in the jobs of every watched run, every cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime

from config import NEWEST_RUNS_PAGE_SIZE, RERUN_WINDOW_S
from github_client import PAGE_SIZE, GitHubClient, GitHubNotFound, GitHubUnavailable
from plan import Demand, WaitingJob
from runner_size_config import size_label_of_job

logger = logging.getLogger(__name__)


def watch_new_runs(
    github: GitHubClient, repo_api_base_url: str, watched_run_ids: set[int]
) -> set[int]:
    """watched_run_ids plus not-completed runs on the newest page and every run listed queued/in_progress;
    never removes; a failed list adds nothing."""
    newest_runs = []
    newest_page_url = f"{repo_api_base_url}/actions/runs?per_page={NEWEST_RUNS_PAGE_SIZE}&exclude_pull_requests=true"
    try:
        newest_runs = workflow_runs_in(
            github.get_json(newest_page_url), newest_page_url
        )
    except GitHubUnavailable as e:
        logger.warning(f"Could not read the newest runs: {e}")

    listed_runs = []
    for status in ("queued", "in_progress"):
        try:
            listed_runs += github.get_all_pages(
                f"{repo_api_base_url}/actions/runs?status={status}&exclude_pull_requests=true",
                "workflow_runs",
            )
        except GitHubUnavailable as e:
            logger.warning(f"Could not list the {status} runs: {e}")

    new_run_ids = run_ids_to_watch(newest_runs, listed_runs) - watched_run_ids
    for run_id in sorted(new_run_ids):
        logger.info(f"Watching run {run_id}")
    return watched_run_ids | new_run_ids


def watch_runs_from_history_page(
    github: GitHubClient,
    repo_api_base_url: str,
    watched_run_ids: set[int],
    page: int,
    now: float,
) -> tuple[set[int], int]:
    """watched_run_ids plus the not-completed runs on one page of all runs, newest first, and the page to
    read next; finds a re-run or, after a restart, a run that neither the newest page nor the status
    lists show. A failed read adds nothing and keeps the page."""
    page_url = f"{repo_api_base_url}/actions/runs?per_page={PAGE_SIZE}&page={page}&exclude_pull_requests=true"
    try:
        body, next_page_url = github.get_page(page_url)
        runs = workflow_runs_in(body, page_url)
    except GitHubUnavailable as e:
        logger.warning(f"Could not read page {page} of the run history: {e}")
        return watched_run_ids, page

    new_run_ids = run_ids_to_watch(runs, []) - watched_run_ids
    for run_id in sorted(new_run_ids):
        logger.info(f"Watching run {run_id}, found on page {page} of the run history")
    next_page = next_history_page(page, runs, next_page_url is not None, now)
    return watched_run_ids | new_run_ids, next_page


def next_history_page(
    page: int, runs_on_page: list[dict], has_next_page: bool, now: float
) -> int:
    """Pure: the next page, or 1 again once this page is empty, has no next page, or reaches runs too old
    to be re-run. Never judged by length: GitHub may return a short page with more after it."""
    if not runs_on_page or not has_next_page:
        return 1
    if epoch_seconds(runs_on_page[-1]["created_at"]) < now - RERUN_WINDOW_S:
        return 1
    return page + 1


def read_latest_jobs(
    github: GitHubClient, repo_api_base_url: str, run_ids: set[int]
) -> dict[int, list[dict]]:
    """Each run's latest-attempt jobs (filter=latest, all pages); none for a run GitHub no longer has (404);
    a run whose read failed otherwise is left out."""
    jobs_by_run = {}
    for run_id in sorted(run_ids):
        try:
            jobs_by_run[run_id] = github.get_all_pages(
                f"{repo_api_base_url}/actions/runs/{run_id}/jobs?filter=latest", "jobs"
            )
        except GitHubNotFound:
            jobs_by_run[run_id] = []
        except GitHubUnavailable as e:
            logger.warning(f"Left run {run_id} out of this cycle: {e}")
    return jobs_by_run


def stop_watching_finished_runs(
    github: GitHubClient,
    repo_api_base_url: str,
    watched_run_ids: set[int],
    jobs_by_run: dict[int, list[dict]],
) -> set[int]:
    """watched_run_ids minus runs whose jobs, read this cycle, are all completed and whose own GET then says
    completed, about the same attempt as those jobs; or that answer 404 with no jobs read."""
    still_watched = set(watched_run_ids)
    for run_id in sorted(watched_run_ids):
        jobs = jobs_by_run.get(run_id)
        # Between two waves every job can be completed while the run will still queue more
        if jobs is None or any(job["status"] != "completed" for job in jobs):
            continue
        try:
            run = github.get_json(
                f"{repo_api_base_url}/actions/runs/{run_id}?exclude_pull_requests=true"
            )
        except GitHubNotFound as e:
            # Its jobs answered 404 or nothing too: one stray 404 cannot drop a run between waves
            if not jobs:
                still_watched.discard(run_id)
                logger.info(f"Stopped watching run {run_id}, GitHub no longer has it")
            else:
                logger.warning(f"Still watching run {run_id}, its jobs exist: {e}")
            continue
        except GitHubUnavailable as e:
            logger.warning(f"Still watching run {run_id}, could not read it: {e}")
            continue
        # A re-run requested after the jobs were read has a newer attempt than they do
        attempt_read = max((job["run_attempt"] for job in jobs), default=None)
        if run["status"] == "completed" and attempt_read in (None, run["run_attempt"]):
            still_watched.discard(run_id)
            logger.info(f"Stopped watching run {run_id}, it has completed")
    return still_watched


def run_ids_to_watch(
    newest_runs: list[dict], runs_listed_queued_or_in_progress: list[dict]
) -> set[int]:
    """Pure: the newest page's runs that are not completed, plus every listed run."""
    return {run["id"] for run in newest_runs if run["status"] != "completed"} | {
        run["id"] for run in runs_listed_queued_or_in_progress
    }


def demand_from_jobs(jobs_by_run: dict[int, list[dict]]) -> Demand:
    """Pure: jobs whose status is "queued" with a size label we serve, and every job's runner_name."""
    waiting_jobs = []
    runner_names_on_jobs = set()
    for run_id, jobs in sorted(jobs_by_run.items()):
        for job in jobs:
            if job.get("runner_name"):
                runner_names_on_jobs.add(job["runner_name"])
            size_label = size_label_of_job(job["labels"])
            if job["status"] == "queued" and size_label is not None:
                waiting_jobs.append(
                    WaitingJob(
                        job_id=job["id"],
                        run_id=run_id,
                        size_label=size_label,
                        created_at=epoch_seconds(job["created_at"]),
                        workflow_name=job["workflow_name"],
                        job_name=job["name"],
                    )
                )
    return Demand(waiting_jobs, runner_names_on_jobs)


def epoch_seconds(github_time: str) -> float:
    """Pure: a GitHub timestamp such as "2026-11-13T00:37:59Z" in seconds since the epoch."""
    return datetime.fromisoformat(github_time.replace("Z", "+00:00")).timestamp()


def workflow_runs_in(body: dict, url: str) -> list[dict]:
    """The workflow_runs list of a run-list answer; GitHubUnavailable if the answer has none."""
    runs = body.get("workflow_runs")
    if not isinstance(runs, list):
        raise GitHubUnavailable(f"GET {url}: no 'workflow_runs' list in the answer")
    return runs
