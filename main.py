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
from config import GITHUB_API_BASE_URL, GITHUB_REPO_URL, ALLOCATE_RUNNER_SCRIPT_PATH 
from RunningJob import RunningJob
from KubernetesLogFormatter import KubernetesLogFormatter

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
allocated_jobs = {} # Maps job_id -> RunningJob 

POLLED_WITHOUT_ALLOCATING = False


def get_gh_api(url, token, etag=None):
    """
    Sends a GET request to the GitHub API with the given URL and access token.
    If the rate limit is exceeded, the function will wait until the rate limit is reset before returning.
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
    per_page = 100 # Maximum number of jobs per page according to rate limits

    while True:
        try:
            url = f"{GITHUB_API_BASE_URL}/actions/runs/{workflow_id}/jobs?per_page={per_page}&page={page}"
            job_data, _ = get_gh_api(url, token)
            if job_data and 'jobs' in job_data:
                all_jobs.extend(job_data['jobs'])
                if len(job_data['jobs']) < per_page:
                    break  # No more pages
                page += 1
                logger.info(f"Getting jobs for workflow {workflow_id} page {page}")
            else:
                logger.error(f"Failed to get job data for workflow {workflow_id}")
                break  # No more data or error occurred
        except Exception as e:
            logger.error(f"Exception occurred in get_all_jobs for workflow_id {workflow_id}: {e}")
            break  # Decide whether to continue or break

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
            continue


def allocate_actions_runner(job_id, token):
    """
    Allocates a runner for the given job ID by sending a POST request to the GitHub API to get a registration token.
    Proceeds to submit a SLURM job to allocate the runner with the corresponding resources.
    """
    if job_id in allocated_jobs:
        logger.info(f"Runner already allocated for job {job_id}")
        return
    logger.info(f"Allocating runner for job {job_id}")
    global POLLED_WITHOUT_ALLOCATING
    POLLED_WITHOUT_ALLOCATING = False
    allocated_jobs[job_id] = None  # mark as allocated to prevent double allocation

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

        allocated_jobs[job_id] = RunningJob(job_id, None, data['workflow_name'], data['name'], labels)

        if "slurm-runner" not in labels[0]:
            logger.info(f"Skipping job because it is not for the correct runner. labels: {labels}, labels[0]: {labels[0]}")
            del allocated_jobs[job_id]
            return

        runner_size_label = labels[0]

        logger.info(f"Using runner size label: {runner_size_label}")
        runner_resources = get_runner_resources(runner_size_label)

        # sbatch resource allocation command
        command = [
            "sbatch",
			f"--output=/var/log/slurm-ci/slurm-ci-%j.out",
            f"--job-name=slurm-{runner_size_label}-{job_id}",
            f"--mem-per-cpu={runner_resources['mem-per-cpu']}",
            f"--cpus-per-task={runner_resources['cpu']}",
            f"--gres=tmpdisk:{runner_resources['tmpdisk']}",
            f"--time={runner_resources['time']}",
            ALLOCATE_RUNNER_SCRIPT_PATH,  # allocate-ephemeral-runner-from-docker.sh
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
        try:
            slurm_job_id = int(output.split()[-1])  # output is of the form "Submitted batch job 3828"
            allocated_jobs[job_id] = RunningJob(job_id, slurm_job_id, data['workflow_name'], data['name'], labels)
            logger.info(f"Allocated runner for job {allocated_jobs[job_id]} with SLURM job ID {slurm_job_id}.")
            if result.returncode != 0:
                del allocated_jobs[job_id]
                logger.error(f"Failed to allocate runner for job {job_id}.")
                allocate_actions_runner(job_id, token)
        except (IndexError, ValueError) as e:
            logger.error(f"Failed to parse SLURM job ID from command output: {output}. Error: {e}")
            del allocated_jobs[job_id]
            # retry the job allocation
            allocate_actions_runner(job_id, token)
    except Exception as e:
        logger.error(f"Exception occurred in allocate_actions_runner for job_id {job_id}: {e}")
        if job_id in allocated_jobs:
            del allocated_jobs[job_id]

def check_slurm_status():
    """
    Checks the status of SLURM jobs and updates the allocated_jobs dictionary.
    """
    if not allocated_jobs:
        return

    to_remove = []
    frozen_jobs = allocated_jobs.copy()
    for job_id, runningjob in frozen_jobs.items():
        if not runningjob or not runningjob.slurm_job_id:
            continue

        # Use 'sacct' to get job status and start time for a single job
        sacct_cmd = ['sacct', '-n', '-P', '-o', 'JobID,State,Start,End', '--jobs', str(runningjob.slurm_job_id)]
        try:
            sacct_result = subprocess.run(sacct_cmd, capture_output=True, text=True)
            sacct_output = sacct_result.stdout.strip()

            if sacct_result.returncode != 0:
                logger.error(f"sacct command failed with return code {sacct_result.returncode}")
                if sacct_result.stderr:
                    logger.error(f"Error output: {sacct_result.stderr}")
                continue

            for line in sacct_output.split('\n'):
                parts = line.split('|')
                if line == '' or len(parts) < 4:
                    continue # Sometimes it takes a while for the job to appear in the sacct output
                    
                job_component = parts[0]  # e.g., '3840.batch'
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
                    logger.info(f"Slurm job {job_component} {status} in {duration}. Running Job Info: {str(runningjob)}")
                    to_remove.append(job_id)

        except Exception as e:
            logger.error(f"Error querying SLURM job details for job ID {runningjob.slurm_job_id}: {e}")

    for job_id in to_remove:
        del allocated_jobs[job_id]

def poll_slurm_statuses(sleep_time=5):
    """
    Wrapper function to poll check_slurm_status.
    """
    while True:
        check_slurm_status()
        time.sleep(sleep_time)

if __name__ == "__main__":
    # Need to use threading to achieve simultaneous polling
    github_thread = threading.Thread(target=poll_github_actions_and_allocate_runners, args=(queued_workflows_url, GITHUB_ACCESS_TOKEN, 2))
    slurm_thread = threading.Thread(target=poll_slurm_statuses)

    github_thread.start()
    slurm_thread.start()

    github_thread.join()
    slurm_thread.join()
