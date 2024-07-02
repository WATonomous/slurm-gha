import requests
import time
from datetime import datetime
import subprocess
import os
import threading
from dotenv import load_dotenv

from runner_size_config import create_runner_sbatch_files
from config import GITHUB_API_BASE_URL, GITHUB_REPO_URL 
from RunningJob import RunningJob

load_dotenv()
GITHUB_ACCESS_TOKEN = os.getenv('GITHUB_ACCESS_TOKEN')

# constants
queued_workflows_url = f'{GITHUB_API_BASE_URL}/actions/runs?status=queued'

# global tracking for allocated runners
allocated_jobs = {} # Maps job_id -> RunningJob 

def get_gh_api(url, token, etag=None):
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    if etag:
        headers['If-None-Match'] = etag

    response = requests.get(url, headers=headers)
    if response.status_code == 304:
        return None, etag 
    elif response.status_code == 200:
        new_etag = response.headers.get('ETag')
        return response.json(), new_etag 
    else:
        print(f"Unexpected status code: {response.status_code}")
        response.raise_for_status()  # Handle HTTP errors

def allocate_runners_for_jobs(workflow_data, token):
    if "workflow_runs" not in workflow_data:
        print("No workflows found.")
        return
    
    number_of_queued_workflows = workflow_data["total_count"] 
    number_of_queued_workflows = len(workflow_data["workflow_runs"])
    
    for i in range(number_of_queued_workflows):
        workflow_id = workflow_data["workflow_runs"][i]["id"]
        if workflow_data["workflow_runs"][i]["head_branch"] != "alexboden/test-slurm-gha-runner":
            print(f"Skipping workflow {workflow_id} because it is not on the correct branch.")
            print(f"Branch is {workflow_data['workflow_runs'][i]['head_branch']}")
            continue
        else:
            print(f"Processing workflow {workflow_id} because it is on the correct branch.")
            print(f"Branch is {workflow_data['workflow_runs'][i]['head_branch']}")
        job_data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/runs/{workflow_id}/jobs?per_page=300', token) #TODO get jobs per page dynamically
        for job in job_data["jobs"]:
            if job["status"] == "queued":
                queued_job_id = job["id"]
                allocate_actions_runner(queued_job_id, token) 
                print(f"Job {job['name']} {job['id']} is queued.")
            else:
                print(f"Job {job['name']} {job['id']} is not queued.")

def allocate_actions_runner(job_id, token):
    if job_id in allocated_jobs:
        print(f"Runner already allocated for job {job_id}")
        return
    print(f"Allocating runner for job {job_id}")
    allocated_jobs[job_id] = None # mark as allocated to prevent double allocation

    # get the runner registration token
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    data = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/registration-token', headers=headers)
    registration_token = data.json()["token"]

    time.sleep(0.5) # was returning the same token without this

    data = requests.post(f'{GITHUB_API_BASE_URL}/actions/runners/remove-token', headers=headers)
    removal_token = data.json()["token"]
    
    data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/jobs/{job_id}', token)
    labels = data["labels"] # should only be one label in prod

    # print(f"Allocating runner for: {data['workflow_name']}-{data['name']} with labels: {labels} and job_id: {job_id}")
    allocated_jobs[job_id] = RunningJob(job_id, None, data['workflow_name'], data['name'], labels)

    if "alextest" not in labels[0]:
        print("Skipping job because it is not for the correct runner.")
        return
    
    runner_size_label = "gh-arc-runners-small" # default to small runner
    if "alextest-gh-arc-runners-medium" in labels:
        runner_size_label = "gh-arc-runners-medium"
    elif "alextest-gh-arc-runners-large" in labels:
        runner_size_label = "gh-arc-runners-large"
    elif "alextest-gh-arc-runners-xlarge" in labels:
        runner_size_label = "gh-arc-runners-xlarge"
    
    command = [
        "sbatch",
        f"./slurm-runner-scripts/runner-{runner_size_label}.sh",
        GITHUB_REPO_URL,
        registration_token,
        removal_token,
        ','.join(labels)
    ]
    
    # Execute the command with the modified environment
    result = subprocess.run(command, capture_output=True, text=True) 
    output = result.stdout.strip()
    slurm_job_id = int(output.split()[-1]) # output is of the form "Submitted batch job 3828"
    allocated_jobs[job_id] = RunningJob(job_id, slurm_job_id, data['workflow_name'], data['name'], labels)
    print(f"Allocated runner for job {allocated_jobs[job_id]} with SLURM job ID {slurm_job_id}.")

    if result.returncode != 0:
        print(f"Failed to allocate runner for job {job_id}.")
        allocated_jobs.remove(job_id) 
        # retry the job allocation
        allocate_actions_runner(job_id, token)

def poll_github_actions_and_allocate_runners(url, token, sleep_time=1):
    etag = None
    # add count to reset the etag
    i = 0
    while True:
        data, etag = get_gh_api(url, token, etag)
        if data:
            print("Changes detected.")
            allocate_runners_for_jobs(data, token)
            print("Polling for queued workflows...")
        time.sleep(sleep_time) # issues occur if you request to frequently
        
        if i % 15 == 0 and len(allocated_jobs) > 0:
            print(f"Current {len(allocated_jobs)} allocated jobs:")
            print(allocated_jobs)
        i += 1
    
def check_slurm_status():
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
                print(f"sacct command failed with return code {sacct_result.returncode}")
                print(f"Error output: {sacct_result.stderr}")
                continue

            for line in sacct_output.split('\n'):
                parts = line.split('|')
                if line == '' or len(parts) < 4:
                    continue # Sometimes it takes a while for the job to appear in the sacct output
                    
                job_component = parts[0]  # e.g., '3840.batch'
                status = parts[1]
                start_time_str = parts[2]
                end_time_str = parts[3]

                # Focus only on the main job ID and ignore '.batch' or '.extern' components
                if '.batch' in job_component or '.extern' in job_component:
                    continue

                # Convert time strings to datetime objects
                start_time = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S') if start_time_str != 'Unknown' else None
                end_time = datetime.strptime(end_time_str, '%Y-%m-%dT%H:%M:%S') if end_time_str != 'Unknown' else None

                if status in ['COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT']: # otherwise job is not finished running
                    duration = "[Unknown Duration]"
                    if start_time and end_time:
                        duration = end_time - start_time
                    print(f"Slurm job {job_component} {status} in {duration}. Running Job Info: {str(runningjob)}")
                    to_remove.append(job_id)

        except Exception as e:
            print(f"Error querying SLURM job details for job ID {runningjob.slurm_job_id}: {e}")

    for job_id in to_remove:
        del allocated_jobs[job_id]

def poll_slurm_statuses(sleep_time=1):
    while True:
        check_slurm_status()
        time.sleep(sleep_time)

if __name__ == "__main__":
    # create sbatch files for runners
    create_runner_sbatch_files()

    # need to use threading to achieve simultaneous polling
    github_thread = threading.Thread(target=poll_github_actions_and_allocate_runners, args=(queued_workflows_url, GITHUB_ACCESS_TOKEN))
    slurm_thread = threading.Thread(target=poll_slurm_statuses)

    github_thread.start()
    slurm_thread.start()

    github_thread.join()
    slurm_thread.join()

# Weird job wasn't appearing on api