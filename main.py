import requests
import time
import subprocess
import os
from dotenv import load_dotenv

from runner_size_config import get_runner_resources

from config import GITHUB_API_BASE_URL, GITHUB_REPO_URL, DOCKER_FILE_URL, LIST_OF_RUNNER_LABELS

load_dotenv()
GITHUB_ACCESS_TOKEN = os.getenv('GITHUB_ACCESS_TOKEN')

# constants
queued_workflows_url = f'{GITHUB_API_BASE_URL}/actions/runs?status=queued'

# global tracking for allocated runners
allocated_jobs = set()

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

    # if number_of_queued_workflows != number_of_queued_workflows_2:
    #     print(f"number_of_queued_workflows: {number_of_queued_workflows} number_of_queued_workflows_2 : {number_of_queued_workflows_2}")
        # print(workflow_data)
    
    for i in range(number_of_queued_workflows):
        workflow_id = workflow_data["workflow_runs"][i]["id"]
        job_data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/runs/{workflow_id}/jobs', token)
        for job in job_data["jobs"]:
            if job["status"] == "queued":
                queued_job_id = job["id"]
                allocate_actions_runner(queued_job_id, token) 
                print(f"Job {job['id']} is queued.")

def allocate_actions_runner(job_id, token):
    if job_id in allocated_jobs:
        print(f"Runner already allocated for job {job_id}")
        return
    allocated_jobs.add(job_id)

    
    data, _ = get_gh_api(f'{GITHUB_API_BASE_URL}/actions/jobs/{job_id}', token)
    labels = data["labels"] # should only be one label
    size_label = labels[0]

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
    
    print(labels, registration_token, removal_token)
 
    command = [
        "sbatch",
        "./allocate_ephemeral_runner_from_docker.sh",
        GITHUB_REPO_URL,
        registration_token,
        removal_token,
        ','.join(labels)
    ]
    
    # Execute the command with the modified environment
    result = subprocess.run(command)
    if result.returncode != 0:
        print(f"Failed to allocate runner for job {job_id}.")
        allocated_jobs.remove(job_id) 
        # retry the job allocation
        allocate_actions_runner(job_id, token)

def poll_github_actions_and_allocate_runners(url, token):
    etag = None
    while True:
        data, etag = get_gh_api(url, token, etag)
        if data:
            print(data)
            print("Changes detected.")
            allocate_runners_for_jobs(data, token)
            print("Polling for queued workflows...")
        time.sleep(1) # issues occur if you request to frequently

def pull_custom_docker_image(docker_github_file_url, token):
    data, _ = get_gh_api(docker_github_file_url, token)
    download_url = data['download_url']

    # download the dockerfile
    response = requests.get(download_url)
    print(response.text)

    # save the dockerfile
    with open('GHA_Dockerfile', 'w') as f:
        f.write(response.text)
    
if __name__ == "__main__":
    poll_github_actions_and_allocate_runners(url=queued_workflows_url, token=GITHUB_ACCESS_TOKEN)
    # pull_custom_docker_image(DOCKER_FILE_URL, GITHUB_ACCESS_TOKEN)