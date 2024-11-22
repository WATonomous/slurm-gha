import requests
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import subprocess
import os
import threading
from dotenv import load_dotenv

from runner_size_config import get_runner_resources
from config import GITHUB_API_BASE_URL, GITHUB_REPO_URL, ALLOCATE_RUNNER_SCRIPT_PATH 
from RunningJob import RunningJob

# Configure logging
log_file = '/tmp/slurm-action-runners.log'
log_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)  # 10 MB per file, 5 backup files
log_handler.setLevel(logging.INFO)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# Load GitHub access token from .env file
# Only secret required is the GitHub access token
load_dotenv()
GITHUB_ACCESS_TOKEN = os.getenv('GITHUB_ACCESS_TOKEN').strip()
os.environ["PATH"] = "/opt/slurm/bin:" + os.environ["PATH"]

# Constants
queued_workflows_url = f'{GITHUB_API_BASE_URL}/actions/runs?status=queued'

# Global tracking for allocated runners
allocated_jobs = {} # Maps job_id -> RunningJob 

def get_gh_api(url, token, etag=None):
    """
    Sends a GET request to the GitHub API with the given URL and access token.
    If the rate limit is exceeded, the function will wait until the rate limit is reset before returning.
    """
    
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    if (etag):
        headers['If-None-Match'] = etag

    response = requests.get(url, headers=headers)
    if int(response.headers['X-RateLimit-Remaining']) % 100 == 0:
        logging.info(f"Rate Limit Remaining: {response.headers['X-RateLimit-Remaining']}") 
    if response.status_code == 304:
        return None, etag 
    elif response.status_code == 200:
        new_etag = response.headers.get('ETag')
        return response.json(), new_etag 
    elif response.status_code == 403 and 'X-RateLimit-Remaining' in response.headers and response.headers['X-RateLimit-Remaining'] == '0':
        reset_time = int(response.headers['X-RateLimit-Reset'])
        sleep_time = reset_time - time.time() + 1  # Adding 1 second to ensure the reset has occurred
        logging.warning(f"Rate limit exceeded. Waiting for {sleep_time} seconds.")
        time.sleep(sleep_time)
        return get_gh_api(url, token, etag)  # Retry the request
    else:
        logging.error(f"Unexpected status code: {response.status_code}")
        response.raise_for_status()  # Handle HTTP errors

def poll_github_actions_and_allocate_runners(url, token, sleep_time=5):
    etag = None
    while True:
        data, _ = get_gh_api(url, token, etag)
        if data:
            allocate_runners_for_jobs(data, token)
            logging.info("Polling for queued workflows...")
        time.sleep(sleep_time) # issues occur if you request to frequently


def get_all_jobs(workflow_id, token):
    """
     Get all CI jobs for a given workflow ID by iterating through the paginated API response.
      """
    all_jobs = []
    page = 1
    per_page = 100 # Maximum number of jobs per page according to rate limits

    while True:
        url = f"{GITHUB_API_BASE_URL}/actions/runs/{workflow_id}/jobs?per_page={per_page}&page={page}"
        job_data, _ = get_gh_api(url, token)
        if job_data and 'jobs' in job_data:
            all_jobs.extend(job_data['jobs'])
            if len(job_data['jobs']) < per_page:
                break  # No more pages
            page += 1
            logging.info(f"Getting jobs for workflow {workflow_id} page {page}")
        else:
            break  # No more data

    return all_jobs

def allocate_runners_for_jobs(workflow_data, token):
    if "workflow_runs" not in workflow_data:
        logging.error("No workflows found.")
        return
    
    number_of_queued_workflows = workflow_data["total_count"] 
    # logging.info(f"Total number of queued workflows: {number_of_queued_workflows}")
    number_of_queued_workflows = len(workflow_data["workflow_runs"])
    # logging.info(f"Number of workflow runs: {number_of_queued_workflows}")
    
    for i in range(number_of_queued_workflows):
        workflow_id = workflow_data["workflow_runs"][i]["id"]
        # logging.info(f"Evaluating workflow ID: {workflow_id}")
        # If statement to check if the workflow is on the testing branch, remove this for production
        branch = workflow_data["workflow_runs"][i]["head_branch"]
        if branch != "alexboden/test-slurm-gha-runner" and branch != "alexboden/test-ci-apptainer":
            # logging.info(f"Skipping workflow {workflow_id} because it is not on the correct branch, branch: {branch}.")
            continue
        # else:
            # logging.info(f"Processing workflow {workflow_id} because it is on the correct branch, branch: {branch}.")
        job_data = get_all_jobs(workflow_id, token)
        # logging.info(f"There are {len(job_data)} jobs in the workflow.")
        for job in job_data:
            if job["status"] == "queued":
                queued_job_id = job["id"]
                allocate_actions_runner(queued_job_id, token) 
                # logging.info(f"Job {job['name']} {job['id']} is queued.")
            # else:
            #     logging.info(f"Job {job['name']} {job['id']} is not queued.")

def allocate_actions_runner(job_id, token):
    """
    Allocates a runner for the given job ID by sending a POST request to the GitHub API to get a registration token.
    Proceeds to submit a SLURM job to allocate the runner with the corresponding resources.
     """
    if job_id in allocated_jobs:
        logging.info(f"Runner already allocated for job {job_id}")
        return
    logging.info(f"Allocating runner for job {job_id}")
    allocated_jobs[job_id] = None # mark as allocated to prevent double allocation

    # get the runner registration token
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    data = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/registration-token', headers=headers)
    registration_token = data.json()["token"]

    time.sleep(1) # https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api?apiVersion=2022-11-28#pause-between-mutative-requests

    data = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/remove-token', headers=headers)
    removal_token = data.json()["token"]
    
    data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/jobs/{job_id}', token)
    labels = data["labels"] # should only be one label in prod
    logging.info(f"Job labels: {labels}")
    
    run_id = data['run_id']
    
    allocated_jobs[job_id] = RunningJob(job_id, None, data['workflow_name'], data['name'], labels)

	# Jobs should only specify one label
	if "slurm-runner" in labels[0]:
		runner_size_label = labels[0]
	else:
		logging.error(f"Invalid label {labels[0]} for job {job_id}.")
		del allocated_jobs[job_id]
		return
    
    logging.info(f"Using runner size label: {runner_size_label}")
    runner_resources = get_runner_resources(runner_size_label)

    # sbatch resource allocation command
    command = [
        "sbatch",
        # f"--nodelist=thor-slurm1",
        f"--job-name=slurm-{runner_size_label}-{job_id}",
        f"--mem-per-cpu={runner_resources['mem-per-cpu']}",
        f"--cpus-per-task={runner_resources['cpu']}",
        f"--gres=tmpdisk:{runner_resources['tmpdisk']}",
        f"--time={runner_resources['time']}",
        ALLOCATE_RUNNER_SCRIPT_PATH, # allocate-ephemeral-runner-from-docker.sh
        GITHUB_REPO_URL,
        registration_token,
        removal_token,
        ','.join(labels),
        str(run_id)
    ]
    
    logging.info(f"Running command: {' '.join(command)}")

    result = subprocess.run(command, capture_output=True, text=True)
    output = result.stdout.strip()
    error_output = result.stderr.strip()
    logging.info(f"Command stdout: {output}")
    logging.error(f"Command stderr: {error_output}")
    try:
        slurm_job_id = int(output.split()[-1])  # output is of the form "Submitted batch job 3828"
        allocated_jobs[job_id] = RunningJob(job_id, slurm_job_id, data['workflow_name'], data['name'], labels)
        logging.info(f"Allocated runner for job {allocated_jobs[job_id]} with SLURM job ID {slurm_job_id}.")
        if result.returncode != 0:
            del allocated_jobs[job_id]
            logging.error(f"Failed to allocate runner for job {job_id}.")
            allocate_actions_runner(job_id, token)
    except (IndexError, ValueError) as e:
        logging.error(f"Failed to parse SLURM job ID from command output: {output}. Error: {e}")
        del allocated_jobs[job_id]
        # retry the job allocation
        allocate_actions_runner(job_id, token)



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
                logging.error(f"sacct command failed with return code {sacct_result.returncode}")
                logging.error(f"Error output: {sacct_result.stderr}")
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
                        logging.error(f"Error parsing start/end time for job {job_component}: {e}")
                        start_time = None
                        end_time = None
                        duration = "[Unknown Duration]"
                    
                    if start_time and end_time:
                        duration = end_time - start_time
                    logging.info(f"Slurm job {job_component} {status} in {duration}. Running Job Info: {str(runningjob)}")
                    to_remove.append(job_id)

        except Exception as e:
            logging.error(f"Error querying SLURM job details for job ID {runningjob.slurm_job_id}: {e}")

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
    # need to use threading to achieve simultaneous polling
    github_thread = threading.Thread(target=poll_github_actions_and_allocate_runners, args=(queued_workflows_url, GITHUB_ACCESS_TOKEN, 2))
    slurm_thread = threading.Thread(target=poll_slurm_statuses)

    github_thread.start()
    slurm_thread.start()

    github_thread.join()
    slurm_thread.join()
