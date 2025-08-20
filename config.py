GITHUB_API_BASE_URL = "https://api.github.com/repos/WATonomous/infra-config"
GITHUB_REPO_URL = "https://github.com/WATonomous/infra-config"
ALLOCATE_RUNNER_SCRIPT_PATH = "apptainer.sh"  # relative path from '/allocation_script'

# Timeout configurations
NETWORK_TIMEOUT = 30  # seconds for HTTP requests (GitHub API calls)
SLURM_COMMAND_TIMEOUT = 60  # seconds for SLURM commands (sbatch, sacct, etc.)
THREAD_SLEEP_TIMEOUT = 5  # seconds between polling cycles for threads

REPOS_TO_MONITOR = [
    {
        "name": "WATonomous/infra-config",
        "api_base_url": "https://api.github.com/repos/WATonomous/infra-config",
        "repo_url": "https://github.com/WATonomous/infra-config",
    },
]
