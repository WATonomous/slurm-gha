import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from dotenv import load_dotenv

from config import ALLOCATE_RUNNER_SCRIPT_PATH, REPOS_TO_MONITOR
from demand import (
    WaitingJob,
    demand_from_jobs,
    read_latest_jobs,
    stop_watching_finished_runs,
    watch_new_runs,
    watch_runs_from_history_page,
)
from github_client import GitHubClient, GitHubUnavailable
from KubernetesLogFormatter import KubernetesLogFormatter
from runner_size_config import get_runner_resources
from config import HISTORY_PAGE_INTERVAL_S, MIN_SECONDS_BETWEEN_CYCLES
from config import NETWORK_TIMEOUT, SLURM_COMMAND_TIMEOUT
from config import THREAD_SLEEP_TIMEOUT
from RunningJob import RunningJob

logger = logging.getLogger()
log_formatter = KubernetesLogFormatter()

# StreamHandler for stdout (INFO and below)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(log_formatter)

# StreamHandler for stderr (WARNING and above)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
stderr_handler.setFormatter(log_formatter)

# Initialize logger
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Add handlers to logger
logger.addHandler(stdout_handler)
logger.addHandler(stderr_handler)

# Load GitHub access token from .env file
# Only secret required is the GitHub access token
load_dotenv()
# Removed from the environment, which sbatch copies into runner jobs by default
GITHUB_ACCESS_TOKEN = os.environ.pop("GITHUB_ACCESS_TOKEN").strip()
os.environ["PATH"] = "/opt/slurm/bin:" + os.environ["PATH"]


# All ephemeral runner allocations, keyed by (repo_name, job_id).
# e.g. allocated_jobs[("WATonomous/infra-config", 123456789)] = RunningJob(...)
allocated_jobs = {}

# A small flag used for logging "Polling for queued workflows..." only when we don't allocate anything.
POLLED_WITHOUT_ALLOCATING = False

# sacct states from which a job can still run; every other state is final.
# https://slurm.schedmd.com/archive/slurm-24.05.1/sacct.html#SECTION_JOB-STATE-CODES
UNFINISHED_SLURM_STATES = ("PENDING", "RUNNING", "REQUEUED", "RESIZING", "SUSPENDED")

# After a runner cannot be submitted or ends in any state but COMPLETED, its job gets no new
# runner for this long; a launch that always fails would otherwise be resubmitted every poll
SECONDS_BEFORE_REPLACING_A_FAILED_RUNNER = 60

# When each job whose last runner failed may get a new one, keyed like allocated_jobs (epoch seconds)
no_new_runner_before = {}


def hold_job_back_after_failed_runner(job_key, how_it_failed):
    """
    Gives the job (repo_name, job_id) no new runner for SECONDS_BEFORE_REPLACING_A_FAILED_RUNNER.
    """
    no_new_runner_before[job_key] = (
        time.time() + SECONDS_BEFORE_REPLACING_A_FAILED_RUNNER
    )
    logger.warning(
        f"Runner for job {job_key[1]} {how_it_failed}; its next runner may go in "
        f"{SECONDS_BEFORE_REPLACING_A_FAILED_RUNNER} s"
    )


def poll_github_actions_and_allocate_runners(github: GitHubClient) -> None:
    """
    Each cycle, finds the jobs waiting for a runner in each repository in REPOS_TO_MONITOR
    by reading the jobs of every run it watches, and tries to allocate an ephemeral runner
    for each as soon as its run is read. What GitHub cannot answer is left out of that
    cycle; the watched runs stay.
    """
    global POLLED_WITHOUT_ALLOCATING

    logger.info(
        f"Starting GitHub Actions polling thread, at least {MIN_SECONDS_BETWEEN_CYCLES}s between cycles"
    )
    # Demand's only state across cycles; after a restart GitHub's run lists refill it
    watched_run_ids_by_repo = {repo["name"]: set() for repo in REPOS_TO_MONITOR}
    history_page_by_repo = {repo["name"]: 1 for repo in REPOS_TO_MONITOR}
    next_history_page_at = 0.0  # time.monotonic(); 0 reads page 1 in the first cycle

    while True:
        github.start_cycle()
        try:
            something_allocated = False
            read_history_page = time.monotonic() >= next_history_page_at
            if read_history_page:
                next_history_page_at = time.monotonic() + HISTORY_PAGE_INTERVAL_S

            for repo in REPOS_TO_MONITOR:
                repo_api_base_url = repo["api_base_url"]
                watched_run_ids = watch_new_runs(
                    github, repo_api_base_url, watched_run_ids_by_repo[repo["name"]]
                )
                if read_history_page:
                    watched_run_ids, history_page_by_repo[repo["name"]] = (
                        watch_runs_from_history_page(
                            github,
                            repo_api_base_url,
                            watched_run_ids,
                            history_page_by_repo[repo["name"]],
                            time.time(),
                        )
                    )
                watched_run_ids_by_repo[repo["name"]] = watched_run_ids

                jobs_by_run = {}
                # Newest first: runs idle for days, such as those that never got jobs, come last
                for run_id in sorted(watched_run_ids, reverse=True):
                    jobs_of_run = read_latest_jobs(github, repo_api_base_url, {run_id})
                    jobs_by_run.update(jobs_of_run)
                    if allocate_runners_for_waiting_jobs(
                        github, repo, demand_from_jobs(jobs_of_run).waiting_jobs
                    ):
                        something_allocated = True

                watched_run_ids_by_repo[repo["name"]] = stop_watching_finished_runs(
                    github, repo_api_base_url, watched_run_ids, jobs_by_run
                )

            if not something_allocated and not POLLED_WITHOUT_ALLOCATING:
                logger.info("Polling for queued workflows...")
                POLLED_WITHOUT_ALLOCATING = True
        except Exception as e:
            logger.error(f"Exception in poll_github_actions_and_allocate_runners: {e}")

        time.sleep(github.seconds_until_next_cycle(time.time()))


def allocate_runners_for_waiting_jobs(
    github: GitHubClient, repo: dict, waiting_jobs: list[WaitingJob]
) -> bool:
    """
    allocate_actions_runner for each waiting job of one repository; a job GitHub gives no
    answer for is skipped. True if any runner was allocated.
    """
    something_allocated = False
    for job in waiting_jobs:
        # Skipping is safe only because this adds runners: a lower bound on demand is enough
        try:
            if allocate_actions_runner(
                github=github,
                job=job,
                repo_api_base_url=repo["api_base_url"],
                repo_url=repo["repo_url"],
                repo_name=repo["name"],
            ):
                something_allocated = True
        except GitHubUnavailable as e:
            logger.warning(f"Skipped job {job.job_id}: {e}")
    return something_allocated


def allocate_actions_runner(
    github: GitHubClient,
    job: WaitingJob,
    repo_api_base_url: str,
    repo_url: str,
    repo_name: str,
) -> bool:
    """
    Allocates a runner for a job waiting for one. Returns True if successful, False
    otherwise. Raises GitHubUnavailable, after forgetting the job, if GitHub gives no answer.
    """
    global allocated_jobs, POLLED_WITHOUT_ALLOCATING

    job_id = job.job_id
    # If we already allocated a runner for this job in this repo, skip
    if (repo_name, job_id) in allocated_jobs:
        logger.info(f"Runner already allocated for job {job_id} in {repo_name}")
        return False
    if time.time() < no_new_runner_before.get((repo_name, job_id), 0):
        return False

    labels = [job.size_label]
    runner_size_label = job.size_label

    logger.info(f"Allocating runner for job {job_id} in repo {repo_name}")
    logger.info(f"Job {job_id} labels: {labels}")
    allocated_jobs[(repo_name, job_id)] = None
    POLLED_WITHOUT_ALLOCATING = False

    try:
        run_id = job.run_id
        allocated_jobs[(repo_name, job_id)] = RunningJob(
            repo=repo_name,
            job_id=job_id,
            slurm_job_id=None,
            workflow_name=job.workflow_name,
            job_name=job.job_name,
            labels=labels,
        )

        logger.info(f"Using runner size label: {runner_size_label}")
        runner_resources = get_runner_resources(runner_size_label)

        registration_token = github.post_json(
            f"{repo_api_base_url}/actions/runners/registration-token"
        )["token"]
        removal_token = github.post_json(
            f"{repo_api_base_url}/actions/runners/remove-token"
        )["token"]

        # sbatch resource allocation command
        command = [
            "sbatch",
            "--output=/var/log/slurm-ci/slurm-ci-%j.out",
            f"--job-name=slurm-{runner_size_label}-{job_id}",
            f"--mem-per-cpu={runner_resources.mem_per_cpu}",
            f"--cpus-per-task={runner_resources.cpus}",
            f"--gres=tmpdisk:{runner_resources.tmpdisk_mib}",
            f"--time={runner_resources.job_budget_minutes}",
            # Else JobRequeue=1 turns a failed launch into a held job that never ends
            "--no-requeue",
            ALLOCATE_RUNNER_SCRIPT_PATH,  # allocate-ephemeral-runner-from-docker.sh
            repo_url,
            registration_token,
            removal_token,
            ",".join(labels),
            str(run_id),
        ]

        command_without_tokens = " ".join(
            "<token>" if arg in (registration_token, removal_token) else arg
            for arg in command
        )
        logger.info(f"Running command: {command_without_tokens}")
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=SLURM_COMMAND_TIMEOUT
            )
            output = result.stdout.strip()
            error_output = result.stderr.strip()
            logger.info(f"Command stdout: {output}")
            if error_output:
                logger.error(f"Command stderr: {error_output}")

            # Attempt to parse the SLURM job ID from output (e.g. "Submitted batch job 3828")
            if result.returncode == 0:
                try:
                    slurm_job_id = int(output.split()[-1])
                    # Store the SLURM job ID in allocated_jobs
                    allocated_jobs[(repo_name, job_id)] = RunningJob(
                        repo=repo_name,
                        job_id=job_id,
                        slurm_job_id=slurm_job_id,
                        workflow_name=job.workflow_name,
                        job_name=job.job_name,
                        labels=labels,
                    )
                    logger.info(
                        f"Allocated runner for job {job_id} in {repo_name} with SLURM job ID {slurm_job_id}."
                    )
                    return True
                except (IndexError, ValueError) as parse_err:
                    logger.error(
                        f"Failed to parse SLURM job ID from: {output}. Error: {parse_err}"
                    )
            else:
                logger.error(
                    f"sbatch command failed with return code {result.returncode}"
                )

            # If we get here, something failed, so remove from tracking and consider retry
            hold_job_back_after_failed_runner((repo_name, job_id), "was not submitted")
            del allocated_jobs[(repo_name, job_id)]
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                f"SLURM command timed out after {SLURM_COMMAND_TIMEOUT} seconds: {command_without_tokens}"
            )
            hold_job_back_after_failed_runner((repo_name, job_id), "was not submitted")
            del allocated_jobs[(repo_name, job_id)]
            return False
        except subprocess.SubprocessError as e:
            logger.error(f"Subprocess error running SLURM command: {e}")
            hold_job_back_after_failed_runner((repo_name, job_id), "was not submitted")
            del allocated_jobs[(repo_name, job_id)]
            return False

    except GitHubUnavailable:
        allocated_jobs.pop((repo_name, job_id), None)
        raise
    except Exception as e:
        logger.error(f"Exception in allocate_actions_runner for job_id {job_id}: {e}")
        if (repo_name, job_id) in allocated_jobs:
            del allocated_jobs[(repo_name, job_id)]
        return False


def check_slurm_status():
    """
    Checks the status of SLURM jobs and removes completed or failed entries from allocated_jobs.
    """
    for key, allowed_at in list(no_new_runner_before.items()):
        if time.time() >= allowed_at:
            del no_new_runner_before[key]

    if not allocated_jobs:
        return

    to_remove = []
    frozen_jobs = allocated_jobs.copy()
    for job_id, running_job in frozen_jobs.items():
        if not running_job or not running_job.slurm_job_id:
            continue

        sacct_cmd = [
            "sacct",
            "-n",
            "-P",
            "-o",
            "JobID,State,Start,End",
            "--jobs",
            str(running_job.slurm_job_id),
        ]

        try:
            logger.debug(
                f"Checking SLURM job status for job ID: {running_job.slurm_job_id}"
            )
            sacct_result = subprocess.run(
                sacct_cmd, capture_output=True, text=True, timeout=SLURM_COMMAND_TIMEOUT
            )
            if sacct_result.returncode != 0:
                logger.error(
                    f"sacct command failed with return code {sacct_result.returncode}"
                )
                if sacct_result.stderr:
                    logger.error(f"Error output: {sacct_result.stderr}")
                continue

            sacct_output = sacct_result.stdout.strip()
            if not sacct_output:
                continue

            for line in sacct_output.split("\n"):
                parts = line.split("|")
                # Typically lines might look like: 3840|COMPLETED|2025-01-22T10:11:12|2025-01-22T10:16:30
                if len(parts) < 4:
                    continue

                job_component = parts[0]
                status = parts[1]
                start_time_str = parts[2]
                end_time_str = parts[3]

                if ".batch" in job_component or ".extern" in job_component:
                    continue

                # Convert time strings to datetime objects

                if not status.startswith(UNFINISHED_SLURM_STATES):
                    try:
                        start_time = datetime.strptime(
                            start_time_str, "%Y-%m-%dT%H:%M:%S"
                        )
                        end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S")
                    except Exception as e:
                        logger.error(
                            f"Error parsing start/end time for job {job_component}: {e}"
                        )
                        start_time = None
                        end_time = None
                        duration = "[Unknown Duration]"

                    if start_time and end_time:
                        duration = end_time - start_time
                    logger.info(
                        f"Slurm job {job_component} {status} in {duration}. Running Job Info: {str(running_job)}"
                    )
                    if not status.startswith("COMPLETED"):
                        hold_job_back_after_failed_runner(job_id, f"ended {status}")
                    to_remove.append(job_id)

        except subprocess.TimeoutExpired:
            logger.error(
                f"SLURM status check timed out after {SLURM_COMMAND_TIMEOUT} seconds for job ID {running_job.slurm_job_id}"
            )
        except subprocess.SubprocessError as e:
            logger.error(
                f"Subprocess error checking SLURM job status for job ID {running_job.slurm_job_id}: {e}"
            )
        except Exception as e:
            logger.error(
                f"Error querying SLURM job details for job ID {running_job.slurm_job_id}: {e}"
            )

    # Remove completed/failed jobs
    for key in to_remove:
        del allocated_jobs[key]


def poll_slurm_statuses(sleep_time=THREAD_SLEEP_TIMEOUT):
    """
    Wrapper function to poll check_slurm_status.
    """
    logger.info(f"Starting SLURM status polling thread with {sleep_time}s intervals")

    while True:
        try:
            check_slurm_status()
        except Exception as e:
            logger.error(f"Exception in poll_slurm_statuses: {e}")
        time.sleep(sleep_time)


if __name__ == "__main__":
    logger.info("Starting SLURM GitHub Actions runner with timeout configurations:")
    logger.info(f"  Network timeout: {NETWORK_TIMEOUT}s")
    logger.info(f"  SLURM command timeout: {SLURM_COMMAND_TIMEOUT}s")
    logger.info(f"  Thread sleep timeout: {THREAD_SLEEP_TIMEOUT}s")

    # Thread to poll GitHub for new queued workflows
    github_thread = threading.Thread(
        target=poll_github_actions_and_allocate_runners,
        args=(GitHubClient(GITHUB_ACCESS_TOKEN),),
        name="GitHub-Poller",
    )

    # Thread to poll SLURM job statuses
    slurm_thread = threading.Thread(
        target=poll_slurm_statuses,
        kwargs={"sleep_time": THREAD_SLEEP_TIMEOUT},
        name="SLURM-Status-Poller",
    )

    # Set threads as daemon so they exit when main thread exits
    github_thread.daemon = True
    slurm_thread.daemon = True

    github_thread.start()
    slurm_thread.start()

    try:
        github_thread.join()
        slurm_thread.join()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
    except Exception as e:
        logger.error(f"Unexpected error in main thread: {e}")
    finally:
        logger.info("SLURM GitHub Actions runner shutdown complete.")
