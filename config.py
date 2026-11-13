from dataclasses import dataclass

# sbatch reads it from the daemon's working directory, where the Dockerfile copies it
ALLOCATE_RUNNER_SCRIPT_PATH = "apptainer.sh"

# Timeout configurations
NETWORK_TIMEOUT = 30  # seconds per HTTP request; GitHub gives up at 10 s on its side
SLURM_COMMAND_TIMEOUT = 60  # seconds for Slurm commands (squeue, sbatch, scancel)

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
# Newest runs read each cycle; a push creates 3, so this outlasts many pushes between two cycles
NEWEST_RUNS_PAGE_SIZE = 30
# One run-history page (2-6 s, one counted request) a minute; trades how long a missed run stays unseen against cost
HISTORY_PAGE_INTERVAL_S = 60
# While a repository's cycles are stopped, how often the log repeats how long and why; the cause can change
# within GitHub (a rate limit turning into bad credentials) without the cycles recovering
OUTAGE_REMINDER_INTERVAL_S = 10 * 60
# How far back the history is read: GitHub allows a re-run up to 30 days after a run
RERUN_WINDOW_S = 30 * 24 * 60 * 60

# Runners
# The repository's Default runner group; generate-jitconfig requires one
RUNNER_GROUP_ID = 1
# Where each runner's batch script writes, on the node that ran it
SLURM_OUTPUT_PATH = "/var/log/slurm-ci/slurm-ci-%j.out"
# W: how long a started runner may wait for a job before its batch script stops it. The slowest recorded
# pickup took 548 s; any value is safe, it trades idle CPUs against tolerance for slow boots
IDLE_ALLOWANCE_S = 10 * 60
# D: from Slurm starting the job to its batch script starting the idle-deadline clock (BatchStartTimeout
# is 10 s), plus from a job being picked up to its job-started hook running
SCRIPT_START_MARGIN_S = 2 * 60
# The compute partition's MaxTime; a runner asking for more would stay pending forever
SLURM_PARTITION_MAX_TIME_MINUTES = 24 * 60
# B: how long a started runner may take to come online before the daemon stops counting on it. 99% of
# recorded boots took under 63 s and 31 of 4,704 over 2 min; it trades a duplicate runner against a job
# waiting on a boot that may never finish. Must stay below W, or such a boot would go unnoticed
BOOT_ALLOWANCE_S = 2 * 60
# Failures in a row a proven size label tolerates before it gets one runner at a time: one slow or broken
# node should not hold back a whole wave
TOLERATED_FAILURES_IN_A_ROW = 1
# The first wait for an unproven label's one runner, once a proven label has failed past its tolerance;
# it doubles per further failure
FIRST_PROBE_WAIT_S = 60
# Cap on that wait: a size label that cannot launch at all costs about 6 allocations an hour
MAX_PROBE_WAIT_S = 10 * 60
# A pending runner's JIT registration can only vanish through GitHub removing a runner that has not
# connected for more than a day (GitHub docs, removing runners); missing for less, the list has not caught up
UNUSED_REGISTRATION_LIFETIME_S = 24 * 60 * 60
# T: how long a busy runner that no job names covers a waiting job of its size. Longer than the longest
# measured gap between GitHub's offer and the job naming its runner (26 s), shorter than GitHub's re-queue of
# an offer that is not acquired (about 75 s)
BUSY_COVER_S = 60


@dataclass(frozen=True)
class Repository:
    """A repository whose waiting jobs get runners on Slurm."""

    name: str  # owner/name, for log lines
    api_base_url: str
    # What makes a Slurm job or a runner registration this repository's, on both sides. Nothing else is read
    # as ours (the old daemon's runners are slurm-<node>-<id>), and no repository's prefix may start
    # another's; the daemon refuses to start otherwise
    runner_name_prefix: str


REPOS_TO_MONITOR = [
    Repository(
        name="WATonomous/infra-config",
        api_base_url="https://api.github.com/repos/WATonomous/infra-config",
        runner_name_prefix="gha-infra-config-",
    ),
]
