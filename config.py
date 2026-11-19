GITHUB_API_BASE_URL = "https://api.github.com/repos/WATonomous/infra-config"
GITHUB_REPO_URL = "https://github.com/WATonomous/infra-config"
ALLOCATE_RUNNER_SCRIPT_PATH = "apptainer.sh"  # relative path from '/allocation_script'

# Timeout configurations
NETWORK_TIMEOUT = 30  # seconds per HTTP request; GitHub gives up at 10 s on its side
SLURM_COMMAND_TIMEOUT = 60  # seconds for SLURM commands (sbatch, sacct, etc.)
THREAD_SLEEP_TIMEOUT = 5  # seconds between polling cycles for threads

# GitHub API
# Pinned so response shapes cannot change under a running daemon
GITHUB_API_VERSION = "2022-11-28"
# Today's poll interval; trades latency against requests, nothing else
MIN_SECONDS_BETWEEN_CYCLES = 2
# GitHub REST best practices: at least 1 s between mutative requests
MIN_SECONDS_BETWEEN_WRITES = 1
# GitHub rate-limit docs: no retry-after and no reset -> wait 1 min, longer if repeated
FIRST_WAIT_WITHOUT_RETRY_AFTER_S = 60
# Caps the doubling; bounds how late the end of a long GitHub fault is noticed
LONGEST_WAIT_WITHOUT_RETRY_AFTER_S = 15 * 60

REPOS_TO_MONITOR = [
    {
        "name": "WATonomous/infra-config",
        "api_base_url": "https://api.github.com/repos/WATonomous/infra-config",
        "repo_url": "https://github.com/WATonomous/infra-config",
    },
]
