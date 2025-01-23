import requests
import time
import logging
from datetime import datetime
import subprocess
import os
import sys
import threading
from dotenv import load_dotenv

from runner_size_config import get_runner_resources
from config import ALLOCATE_RUNNER_SCRIPT_PATH,REPOS_TO_MONITOR 
from RunningJob import RunningJob
from KubernetesLogFormatter import KubernetesLogFormatter

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


# All ephemeral runner allocations, keyed by (repo_name, job_id).
# e.g. allocated_jobs[("WATonomous/infra-config", 123456789)] = RunningJob(...)
allocated_jobs = {}

# A small flag used for logging "Polling for queued workflows..." only when we don't allocate anything.
POLLED_WITHOUT_ALLOCATING = False

def get_gh_api(url, token, etag=None):
    """
    Sends a GET request to the GitHub API with the given URL and access token.
    If rate limit is exceeded, the function waits until the rate limit is reset and retries.
    Returns: (json_data, new_etag) or (None, etag)
    """
    try:
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
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
        elif response.status_code == 403 and 'X-RateLimit-Remaining' in response.headers and response.headers['X-RateLimit-Remaining'] == '0':
            reset_time = int(response.headers['X-RateLimit-Reset'])
            sleep_time = reset_time - time.time() + 1  # Adding 1 second to ensure the reset has occurred
            logger.warning(f"Rate limit exceeded. Waiting for {sleep_time} seconds.")
            time.sleep(sleep_time)
            return get_gh_api(url, token, etag)  # Retry the request
        else:
            logger.error(f"Unexpected status code: {response.status_code}")
            return None, etag
    except requests.exceptions.RequestException as e:
        logger.error(f"Exception occurred while calling GitHub API: {e}")
    return None, etag

def poll_github_actions_and_allocate_runners(token, sleep_time=5):
    """
    Polls each repository in REPOS_TO_MONITOR for queued workflows, then tries
    to allocate ephemeral runners.
    """
    global POLLED_WITHOUT_ALLOCATING

    while True:
        something_allocated = False

        for repo in REPOS_TO_MONITOR:
            queued_url = f"{repo['api_base_url']}/actions/runs?status=queued"
            data, _ = get_gh_api(queued_url, token)

            if data:
                new_allocations = allocate_runners_for_jobs(
                    workflow_data=data,
                    token=token,
                    repo_api_base_url=repo['api_base_url'],
                    repo_url=repo['repo_url'],
                    repo_name=repo['name']
                )
                if new_allocations > 0:
                    something_allocated = True

        if not something_allocated and not POLLED_WITHOUT_ALLOCATING:
            logger.info("Polling for queued workflows...")
            POLLED_WITHOUT_ALLOCATING = True

        time.sleep(sleep_time)

def get_all_jobs(workflow_id, token, repo_api_base_url):
    """
    Get all CI jobs for a given workflow ID by paginating through the GitHub API.
    """
    all_jobs = []
    page = 1
    per_page = 100

    while True:
        url = f"{repo_api_base_url}/actions/runs/{workflow_id}/jobs"
        url += f"?per_page={per_page}&page={page}"

        job_data, _ = get_gh_api(url, token)
        if job_data and 'jobs' in job_data:
            all_jobs.extend(job_data['jobs'])
            if len(job_data['jobs']) < per_page:
                break  # No more pages
            page += 1
        else:
            # Possibly no more jobs or an error
            break

    return all_jobs

def allocate_runners_for_jobs(workflow_data, token, repo_api_base_url, repo_url, repo_name):
    """
    For each queued job in a workflow, allocate the ephemeral SLURM runner if appropriate.
    Returns the count of new allocations made.
    """
    if "workflow_runs" not in workflow_data:
        logger.error("No workflow_runs in data.")
        return 0

    new_allocations = 0
    number_of_queued_workflows = len(workflow_data["workflow_runs"])

    for i in range(number_of_queued_workflows):
        workflow_id = workflow_data["workflow_runs"][i]["id"]
        branch = workflow_data["workflow_runs"][i]["head_branch"]
        # if branch != "alexboden/test-slurm-gha-runner" and branch != "alexboden/test-ci-apptainer":
            # continue
        job_data = get_all_jobs(workflow_id, token, repo_api_base_url)
        if not job_data: 
            continue

        for job in job_data:
            if job["status"] == "queued":
                queued_job_id = job["id"]
                allocated = allocate_actions_runner(
                    job_id=queued_job_id,
                    token=token,
                    repo_api_base_url=repo_api_base_url,
                    repo_url=repo_url,
                    repo_name=repo_name
                )
                if allocated:
                    new_allocations += 1

    return new_allocations

def allocate_actions_runner(job_id, token, repo_api_base_url, repo_url, repo_name):
    """
    Allocates a runner for the given job ID.
    """
    global allocated_jobs, POLLED_WITHOUT_ALLOCATING

    # If we already allocated a runner for this job in this repo, skip
    if (repo_name, job_id) in allocated_jobs:
        logger.info(f"Runner already allocated for job {job_id} in {repo_name}")
        return False

    logger.info(f"Allocating runner for job {job_id} in repo {repo_name}")
    allocated_jobs[(repo_name, job_id)] = None
    POLLED_WITHOUT_ALLOCATING = False

    try:
        # Get registration token
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        reg_url = f'{repo_api_base_url}/actions/runners/registration-token'
        remove_url = f'{repo_api_base_url}/actions/runners/remove-token'

        reg_resp = requests.post(reg_url, headers=headers)
        reg_resp.raise_for_status()
        reg_data = reg_resp.json()
        registration_token = reg_data["token"]

        # recommended small delay
        time.sleep(1)

        # Get removal token
        remove_resp = requests.post(remove_url, headers=headers)
        remove_resp.raise_for_status()
        remove_data = remove_resp.json()
        removal_token = remove_data["token"]

        # Get job details to see labels
        job_api_url = f"{repo_api_base_url}/actions/jobs/{job_id}"
        job_data, _ = get_gh_api(job_api_url, token)
        if not job_data:
            logger.error(f"Failed to retrieve job data for job_id {job_id}")
            del allocated_jobs[(repo_name, job_id)]
            return False

        labels = job_data.get("labels", [])
        if not labels:
            logger.error(f"No labels found for job_id {job_id}")
            del allocated_jobs[(repo_name, job_id)]
            return False

        logger.info(f"Job {job_id} labels: {labels}")

        run_id = job_data['run_id']
        allocated_jobs[(repo_name, job_id)] = RunningJob(
            job_id=job_id,
            slurm_job_id=None,
            workflow_name=job_data['workflow_name'],
            job_name=job_data['name'],
            labels=labels
        )

        # For example, you might only allocate if "slurm-runner" in labels
        # if "slurm-runner" not in labels:
        #     logger.info("Skipping job because it is not labeled for slurm-runner.")
        #     del allocated_jobs[(repo_name, job_id)]
        #     return False

        runner_size_label = labels[0]

        if "slurm-runner" not in runner_size_label:
            logger.info(f"Skipping job because it is not labeled for slurm-runner.")
            del allocated_jobs[(repo_name, job_id)]
            return False

        logger.info(f"Using runner size label: {runner_size_label}")
        runner_resources = get_runner_resources(runner_size_label)

        # sbatch resource allocation command
        command = [
            "sbatch",
            f"--job-name=slurm-{runner_size_label}-{job_id}",
            f"--mem-per-cpu={runner_resources['mem-per-cpu']}",
            f"--cpus-per-task={runner_resources['cpu']}",
            f"--gres=tmpdisk:{runner_resources['tmpdisk']}",
            f"--time={runner_resources['time']}",
            ALLOCATE_RUNNER_SCRIPT_PATH,
            repo_url,
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
        if error_output:
            logger.error(f"Command stderr: {error_output}")

        # Attempt to parse the SLURM job ID from output (e.g. "Submitted batch job 3828")
        if result.returncode == 0:
            try:
                slurm_job_id = int(output.split()[-1])
                # Store the SLURM job ID in allocated_jobs
                allocated_jobs[(repo_name, job_id)] = RunningJob(
                    job_id=job_id,
                    slurm_job_id=slurm_job_id,
                    workflow_name=job_data['workflow_name'],
                    job_name=job_data['name'],
                    labels=labels
                )
                logger.info(f"Allocated runner for job {job_id} in {repo_name} with SLURM job ID {slurm_job_id}.")
                return True
            except (IndexError, ValueError) as parse_err:
                logger.error(f"Failed to parse SLURM job ID from: {output}. Error: {parse_err}")
        else:
            logger.error(f"sbatch command failed with return code {result.returncode}")

        # If we get here, something failed, so remove from tracking and consider retry
        del allocated_jobs[(repo_name, job_id)]
        return False

    except Exception as e:
        logger.error(f"Exception in allocate_actions_runner for job_id {job_id}: {e}")
        if (repo_name, job_id) in allocated_jobs:
            del allocated_jobs[(repo_name, job_id)]
        return False

def check_slurm_status():
    """
    Checks the status of SLURM jobs and removes completed or failed entries from allocated_jobs.
    """
    if not allocated_jobs:
        return

    to_remove = []
    frozen_jobs = allocated_jobs.copy()
    for job_id, running_job in frozen_jobs.items():
        if not running_job or not running_job.slurm_job_id:
            continue

        sacct_cmd = [
            'sacct',
            '-n',
            '-P',
            '-o',
            'JobID,State,Start,End',
            '--jobs',
            str(running_job.slurm_job_id)
        ]

        try:
            sacct_result = subprocess.run(sacct_cmd, capture_output=True, text=True)
            if sacct_result.returncode != 0:
                logger.error(f"sacct command failed with return code {sacct_result.returncode}")
                if sacct_result.stderr:
                    logger.error(f"Error output: {sacct_result.stderr}")
                continue

            sacct_output = sacct_result.stdout.strip()
            if not sacct_output:
                continue

            for line in sacct_output.split('\n'):
                parts = line.split('|')
                # Typically lines might look like: 3840|COMPLETED|2025-01-22T10:11:12|2025-01-22T10:16:30
                if len(parts) < 4:
                    continue

                job_component = parts[0]
                status = parts[1]
                start_time_str = parts[2]
                end_time_str = parts[3]

                if '.batch' in job_component or '.extern' in job_component:
                    continue

                # Convert time strings to datetime objects
               
                if status.startswith('COMPLETED') or status.startswith('FAILED') or status.startswith('CANCELLED') or status.startswith('TIMEOUT'):
                    try:
                        start_time = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S')
                        end_time = datetime.strptime(end_time_str, '%Y-%m-%dT%H:%M:%S')
                    except Exception as e:
                        logger.error(f"Error parsing start/end time for job {job_component}: {e}")
                        start_time = None
                        end_time = None
                        duration = "[Unknown Duration]"

                    if start_time and end_time:
                        duration = end_time - start_time
                    logger.info(f"Slurm job {job_component} {status} in {duration}. Running Job Info: {str(running_job)}")
                    to_remove.append(job_id)

        except Exception as e:
            logger.error(f"Error querying SLURM job details for job ID {running_job.slurm_job_id}: {e}")

    # Remove completed/failed jobs
    for key in to_remove:
        del allocated_jobs[key]

def poll_slurm_statuses(sleep_time=5):
    """
    Wrapper function to poll check_slurm_status.
    """
    while True:
        check_slurm_status()
        time.sleep(sleep_time)

if __name__ == "__main__":
    # Thread to poll GitHub for new queued workflows
    github_thread = threading.Thread(
        target=poll_github_actions_and_allocate_runners,
        args=(GITHUB_ACCESS_TOKEN, 2)
    )

    # Thread to poll SLURM job statuses
    slurm_thread = threading.Thread(
        target=poll_slurm_statuses,
        kwargs={'sleep_time': 5}
    )

    github_thread.start()
    slurm_thread.start()

    github_thread.join()
    slurm_thread.join()
