import requests
import time
import logging
from datetime import datetime
import subprocess
import os
import sys
import threading

from dotenv import load_dotenv
from prometheus_client import start_http_server, Counter, Gauge, Summary

from runner_size_config import get_runner_resources
from config import GITHUB_API_BASE_URL, GITHUB_REPO_URL, ALLOCATE_RUNNER_SCRIPT_PATH 
from RunningJob import RunningJob
from KubernetesLogFormatter import KubernetesLogFormatter


# ------------------------------------------
# Configure logging
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
GITHUB_ACCESS_TOKEN = os.getenv('GITHUB_ACCESS_TOKEN').strip()
os.environ["PATH"] = "/opt/slurm/bin:" + os.environ["PATH"]

# Constants
queued_workflows_url = f'{GITHUB_API_BASE_URL}/actions/runs?status=queued'

# Global tracking for allocated runners
allocated_jobs = {}  # Maps job_id -> RunningJob
POLLED_WITHOUT_ALLOCATING = False

# =========== PROMETHEUS METRICS =============

# Track how many jobs are allocated/running on each machine, labeled by 'machine' and 'job_size'.
CURRENT_ALLOCATED_JOBS = Gauge(
    'slurm_runner_allocated_jobs',
    'Number of jobs currently allocated to SLURM Runner per machine',
    labelnames=['machine', 'job_size']
)

# Count total completed jobs, labeled by 'machine' and 'job_size'
COMPLETED_JOBS = Counter(
    'slurm_runner_jobs_completed',
    'Number of completed SLURM Runner jobs',
    labelnames=['machine', 'job_size']
)

# Track job duration in seconds, labeled by 'machine' and 'job_size'
JOB_DURATION_SUMMARY = Summary(
    'slurm_runner_job_duration_seconds',
    'Duration of SLURM runner job in seconds',
    labelnames=['machine', 'job_size']
)


def get_gh_api(url, token, etag=None):
    """
    Sends a GET request to the GitHub API with the given URL and access token.
    If the rate limit is exceeded, the function will wait until the rate limit 
    is reset before returning.
    """
    try:
        headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        if etag:
            headers['If-None-Match'] = etag

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        if int(response.headers.get('X-RateLimit-Remaining', '0')) % 100 == 0:
            logger.info(f"Rate Limit Remaining: {response.headers['X-RateLimit-Remaining']}")

        if response.status_code == 304:
            return None, etag
        elif response.status_code == 200:
            new_etag = response.headers.get('ETag')
            return response.json(), new_etag
        elif (response.status_code == 403 
              and 'X-RateLimit-Remaining' in response.headers 
              and response.headers['X-RateLimit-Remaining'] == '0'):
            reset_time = int(response.headers['X-RateLimit-Reset'])
            sleep_time = reset_time - time.time() + 1  # Add 1s for safety
            logger.warning(f"Rate limit exceeded. Waiting for {sleep_time} seconds.")
            time.sleep(sleep_time)
            return get_gh_api(url, token, etag)  # Retry the request
        else:
            logger.error(f"Unexpected status code: {response.status_code}")
            return None, etag
    except requests.exceptions.RequestException as e:
        logger.error(f"Exception occurred while calling GitHub API: {e}")
        return None, etag


def poll_github_actions_and_allocate_runners(url, token, sleep_time=5):
    etag = None
    while True:
        try:
            data, etag = get_gh_api(url, token, None)
            if data:
                allocate_runners_for_jobs(data, token)
                global POLLED_WITHOUT_ALLOCATING
                if not POLLED_WITHOUT_ALLOCATING:
                    logger.info("Polling for queued workflows...")
                    POLLED_WITHOUT_ALLOCATING = True
            else:
                logger.debug("No new data from GitHub API.")
            time.sleep(sleep_time)
        except Exception as e:
            logger.error(f"Exception occurred in poll_github_actions_and_allocate_runners: {e}")
            time.sleep(sleep_time)
            continue


def get_all_jobs(workflow_id, token):
    """
    Get all CI jobs for a given workflow ID by iterating through the paginated API response.
    """
    all_jobs = []
    page = 1
    per_page = 100

    while True:
        try:
            url = f"{GITHUB_API_BASE_URL}/actions/runs/{workflow_id}/jobs?per_page={per_page}&page={page}"
            job_data, _ = get_gh_api(url, token)
            if job_data and 'jobs' in job_data:
                all_jobs.extend(job_data['jobs'])
                if len(job_data['jobs']) < per_page:
                    break
                page += 1
                logger.info(f"Getting jobs for workflow {workflow_id} page {page}")
            else:
                logger.error(f"Failed to get job data for workflow {workflow_id}")
                break
        except Exception as e:
            logger.error(f"Exception occurred in get_all_jobs for workflow_id {workflow_id}: {e}")
            break

    return all_jobs


def allocate_runners_for_jobs(workflow_data, token):
    if "workflow_runs" not in workflow_data:
        logger.error("No workflows found.")
        return

    number_of_queued_workflows = len(workflow_data["workflow_runs"])
    for i in range(number_of_queued_workflows):
        workflow_id = workflow_data["workflow_runs"][i]["id"]
        branch = workflow_data["workflow_runs"][i]["head_branch"]
        logger.info(f"Checking branch {branch}")
        try:
            job_data = get_all_jobs(workflow_id, token)
            if not job_data:
                logger.error(f"No job data retrieved for workflow {workflow_id}")
                continue
            for job in job_data:
                if job["status"] == "queued":
                    queued_job_id = job["id"]
                    allocate_actions_runner(queued_job_id, token)
        except Exception as e:
            logger.error(f"Exception occurred in allocate_runners_for_jobs for workflow_id {workflow_id}: {e}")


def allocate_actions_runner(job_id, token):
    """
    Allocates a runner for the given job ID by sending a POST request to the GitHub API 
    to get a registration token, then submits a SLURM job.
    """
    if job_id in allocated_jobs:
        logger.info(f"Runner already allocated for job {job_id}")
        return

    logger.info(f"Allocating runner for job {job_id}")
    global POLLED_WITHOUT_ALLOCATING
    POLLED_WITHOUT_ALLOCATING = False

    # Put a placeholder entry so we don't double-allocate
    allocated_jobs[job_id] = None

    try:
        # get the runner registration token
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        response = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/registration-token', headers=headers)
        response.raise_for_status()
        data = response.json()
        registration_token = data["token"]

        time.sleep(1) # https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api?apiVersion=2022-11-28#pause-between-mutative-requests
        response = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/remove-token', headers=headers)
        response.raise_for_status()
        data = response.json()
        removal_token = data["token"]

        data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/jobs/{job_id}', token)
        if not data:
            logger.error(f"Failed to retrieve job data for job_id {job_id}")
            del allocated_jobs[job_id]
            return
        labels = data.get("labels", [])
        if not labels:
            logger.error(f"No labels found for job_id {job_id}")
            del allocated_jobs[job_id]
            return
        logger.info(f"Job labels: {labels}")

        run_id = data['run_id']

        if "slurm-runner" not in labels[0]:
            logger.info(f"Skipping job because it is not for a slurm-runner. labels: {labels}, labels[0]: {labels[0]}")
            del allocated_jobs[job_id]
            return

        runner_size_label = labels[0]
        runner_resources = get_runner_resources(runner_size_label)

        # Create a RunningJob with start_time; machine_name unknown yet
        allocated_jobs[job_id] = RunningJob(
            job_id=job_id,
            slurm_job_id=None,
            workflow_name=data['workflow_name'],
            job_name=data['name'],
            labels=labels,
            start_time=datetime.now(),
            machine_name=None 
        )

        # We do NOT increment CURRENT_ALLOCATED_JOBS here because we do not yet know the node (machine).
        # We wait until sacct tells us the actual 'NodeList'.

        # Submit the SLURM job
        command = [
            "sbatch",
            f"--job-name=slurm-{runner_size_label}-{job_id}",
            f"--mem-per-cpu={runner_resources['mem-per-cpu']}",
            f"--cpus-per-task={runner_resources['cpu']}",
            f"--gres=tmpdisk:{runner_resources['tmpdisk']}",
            f"--time={runner_resources['time']}",
            ALLOCATE_RUNNER_SCRIPT_PATH,
            GITHUB_REPO_URL,
            registration_token,
            removal_token,
            ','.join(labels),
            str(run_id)
        ]

        logger.info(f"Running command: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True)
        output = result.stdout.strip()
        error_output = result.stderr.strip()
        logger.info(f"Command stdout: {output}")
        logger.error(f"Command stderr: {error_output}")

        if result.returncode == 0:
            # Typically: "Submitted batch job 3828"
            try:
                slurm_job_id = int(output.split()[-1])
                allocated_jobs[job_id].slurm_job_id = slurm_job_id
                logger.info(f"Allocated runner for job {allocated_jobs[job_id]} with SLURM job ID {slurm_job_id}.")
            except (IndexError, ValueError) as e:
                logger.error(f"Failed to parse SLURM job ID: {output}, error: {e}")
                # Clean up
                del allocated_jobs[job_id]
        else:
            logger.error(f"Failed to allocate runner for job {job_id}. Retrying allocation...")
            del allocated_jobs[job_id]
            allocate_actions_runner(job_id, token)

    except Exception as e:
        logger.error(f"Exception in allocate_actions_runner for job_id {job_id}: {e}")
        if job_id in allocated_jobs:
            del allocated_jobs[job_id]


def check_slurm_status():
    """
    Checks the status of SLURM jobs and updates the allocated_jobs dictionary.
    We parse the NodeList from sacct to get the actual machine name.
    
    If a job is in RUNNING (or PENDING) on a node for the first time, we increment the 
    CURRENT_ALLOCATED_JOBS gauge labeled by that node. 
    If the job is completed/failed/cancelled, we decrement and update the relevant metrics.
    """
    if not allocated_jobs:
        return

    # We'll store jobs to remove after they're completed/failed
    to_remove = []

    # Copy the dict so we don't mutate while iterating
    frozen_jobs = allocated_jobs.copy()
    for job_id, runningjob in frozen_jobs.items():
        # If we have no slurm_job_id, skip
        if not runningjob or not runningjob.slurm_job_id:
            continue

        sacct_cmd = [
            'sacct', 
            '-n', 
            '-P', 
            '-o', 'JobID,State,Start,End,NodeList', 
            '--jobs', str(runningjob.slurm_job_id)
        ]

        try:
            sacct_result = subprocess.run(sacct_cmd, capture_output=True, text=True)
            sacct_output = sacct_result.stdout.strip()

            if sacct_result.returncode != 0:
                logger.error(f"sacct command failed (rc={sacct_result.returncode}). Stderr: {sacct_result.stderr}")
                continue

            # Example lines: "3828|RUNNING|2023-01-16T10:05:00|2023-01-16T10:10:05|node01"
            for line in sacct_output.split('\n'):
                parts = line.split('|')
                if len(parts) < 5:
                    continue

                job_component = parts[0]  # e.g. '3828'
                job_state = parts[1]
                start_time_str = parts[2]
                end_time_str = parts[3]
                node_list = parts[4]  # e.g. "node01"

                # Skip .batch, .extern, etc. We only want the main job ID line
                if '.batch' in job_component or '.extern' in job_component:
                    continue

                # If we haven't recorded the machine_name, do it now
                if runningjob.machine_name is None and node_list:
                    runningjob.machine_name = node_list
                    # Because the job is now placed on a node, increment the gauge
                    runner_size_label = runningjob.labels[0] if runningjob.labels else "unknown"
                    CURRENT_ALLOCATED_JOBS.labels(machine=node_list, job_size=runner_size_label).inc()

                # If job is completed/failed/canceled, we finalize
                if job_state.startswith('COMPLETED') or job_state.startswith('FAILED') \
                   or job_state.startswith('CANCELLED') or job_state.startswith('TIMEOUT'):
                    runner_size_label = runningjob.labels[0] if runningjob.labels else "unknown"
                    machine_name = runningjob.machine_name or "unknown"

                    # Calculate duration from sacct Start->End if possible
                    duration_seconds = 0
                    try:
                        st_dt = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S')
                        end_dt = datetime.strptime(end_time_str, '%Y-%m-%dT%H:%M:%S')
                        duration_seconds = (end_dt - st_dt).total_seconds()
                    except ValueError:
                        logger.warning(f"Cannot parse start/end time from sacct for job {job_id}")

                    logger.info(f"Slurm job {job_id} is {job_state} on {machine_name}, took {duration_seconds}s.")

                    # Prometheus metrics
                    JOB_DURATION_SUMMARY.labels(machine=machine_name, job_size=runner_size_label).observe(duration_seconds)
                    COMPLETED_JOBS.labels(machine=machine_name, job_size=runner_size_label).inc()
                    CURRENT_ALLOCATED_JOBS.labels(machine=machine_name, job_size=runner_size_label).dec()

                    # Mark for removal
                    to_remove.append(job_id)

        except Exception as e:
            logger.error(f"Error running sacct for job {job_id}: {e}")

    # Remove completed/failed/cancelled jobs
    for job_id in to_remove:
        if job_id in allocated_jobs:
            del allocated_jobs[job_id]


def poll_slurm_statuses(sleep_time=5):
    """
    Periodically checks SLURM statuses and updates metrics.
    """
    while True:
        check_slurm_status()
        time.sleep(sleep_time)


if __name__ == "__main__":
    # Expose Prometheus metrics on port 8000
    start_http_server(8000)
    logger.info("Prometheus metrics server started on port 8000.")

    # Two threads:
    # 1) Poll GitHub for queued jobs
    github_thread = threading.Thread(
        target=poll_github_actions_and_allocate_runners, 
        args=(queued_workflows_url, GITHUB_ACCESS_TOKEN, 2)
    )
    # 2) Poll SLURM for job statuses
    slurm_thread = threading.Thread(target=poll_slurm_statuses)

    github_thread.start()
    slurm_thread.start()

    github_thread.join()
    slurm_thread.join()
