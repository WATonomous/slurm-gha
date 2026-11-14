"""
Runs GitHub Actions jobs on Slurm. Each cycle, for each repository in REPOS_TO_MONITOR, the daemon compares
the jobs waiting for a runner with our runners that can still take one, per size label, and submits
runners for the gap. Everything it decides from is read again every cycle; its memory is only a cache.
"""

from __future__ import annotations

import itertools
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace

from dotenv import load_dotenv

from config import (
    BOOT_ALLOWANCE_S,
    HISTORY_PAGE_INTERVAL_S,
    IDLE_ALLOWANCE_S,
    OUTAGE_REMINDER_INTERVAL_S,
    REPOS_TO_MONITOR,
    Repository,
)
from demand import (
    demand_from_jobs,
    read_latest_jobs,
    stop_watching_finished_runs,
    watch_new_runs,
    watch_runs_from_history_page,
)
from github_client import GitHubClient, GitHubUnavailable
from KubernetesLogFormatter import KubernetesLogFormatter
from plan import (
    NO_DEMAND,
    NO_SIGHTINGS,
    LabelBreaker,
    RunnerSightings,
    allocations_to_cancel,
    breaker_log_lines,
    plan_submissions,
    registrations_to_delete,
    runners_taking_a_job_by_label,
    submission_order,
    update_breakers,
    update_sightings,
)
from supply import (
    SlurmUnavailable,
    cancel_pending_allocation,
    delete_registration,
    read_our_allocations,
    read_our_registrations,
    submit_runner,
)

logger = logging.getLogger()


@dataclass(frozen=True)
class RepositoryMemory:
    """The daemon's caches for one repository. A restart starts empty: runs are found again, and every
    size label is unproven until one of its runners takes a job."""

    # Losing it costs until the runs are found again (status lists, history sweep)
    watched_run_ids: set[int]
    next_history_page: int
    next_history_page_at: float  # epoch seconds
    # Size label -> breaker; losing it costs one runner's boot per size label
    breakers: dict[str, LabelBreaker]
    # Losing it lets a busy runner cover a waiting job for up to BUSY_COVER_S longer
    runner_sightings: RunnerSightings


def empty_memory() -> RepositoryMemory:
    """What a start knows: nothing, so the first cycle reads the first history page."""
    return RepositoryMemory(
        watched_run_ids=set(),
        next_history_page=1,
        next_history_page_at=0.0,
        breakers={},
        runner_sightings=NO_SIGHTINGS,
    )


@dataclass(frozen=True)
class Outage:
    """Consecutive failed cycles of one repository."""

    since: float  # epoch seconds of the first
    unreadable: str  # "Slurm" or "GitHub"
    failed_cycles: int
    last_error: str
    logged_at: float


def outage_after_cycle(
    outage: Outage | None, error: Exception | None, now: float
) -> tuple[Outage | None, str | None]:
    """Pure: the outage after a cycle that raised error (None: it completed), and the line to log, if any:
    when cycles start failing, when what cannot be read changes, every OUTAGE_REMINDER_INTERVAL_S while
    they keep failing, and when they recover, with how long."""
    if error is None:
        if outage is None:
            return None, None
        return None, (
            f"cycles recovered after {now - outage.since:.0f} s and {outage.failed_cycles} failed"
            f" cycles; the last: {outage.last_error}"
        )
    unreadable = "Slurm" if isinstance(error, SlurmUnavailable) else "GitHub"
    if outage is None:
        return Outage(now, unreadable, 1, str(error), logged_at=now), (
            f"cycles stopped, {unreadable} cannot be read: {error}"
        )
    failing = Outage(
        outage.since, unreadable, outage.failed_cycles + 1, str(error), outage.logged_at
    )
    if unreadable != outage.unreadable:
        line = f"cycles still stopped, now {unreadable} cannot be read: {error}"
    elif now - outage.logged_at >= OUTAGE_REMINDER_INTERVAL_S:
        line = (
            f"cycles still stopped after {now - outage.since:.0f} s and {failing.failed_cycles}"
            f" cycles: {error}"
        )
    else:
        return failing, None
    return replace(failing, logged_at=now), line


def main() -> None:
    configure_logging()
    problems = unsafe_settings(BOOT_ALLOWANCE_S, IDLE_ALLOWANCE_S, REPOS_TO_MONITOR)
    for problem in problems:
        logger.error(f"Refusing to start: {problem}")
    if problems:
        sys.exit(1)

    load_dotenv()
    # Removed from the environment, which sbatch copies into runner jobs by default
    github = GitHubClient(os.environ.pop("GITHUB_ACCESS_TOKEN").strip())
    os.environ["PATH"] = "/opt/slurm/bin:" + os.environ["PATH"]

    memory_by_repo = {repo.name: empty_memory() for repo in REPOS_TO_MONITOR}
    outage_by_repo: dict[str, Outage | None] = {
        repo.name: None for repo in REPOS_TO_MONITOR
    }
    logger.info(
        f"Starting: {', '.join(memory_by_repo)}; W {IDLE_ALLOWANCE_S} s, B {BOOT_ALLOWANCE_S} s"
    )
    while True:
        github.start_cycle()
        for repo in REPOS_TO_MONITOR:
            error = None
            try:
                memory_by_repo[repo.name] = run_cycle(
                    github, repo, memory_by_repo[repo.name], now=time.time()
                )
            except (SlurmUnavailable, GitHubUnavailable) as caught:
                # Supply unknown, never empty: nothing was cancelled or submitted (#40)
                error = caught
            outage, line = outage_after_cycle(
                outage_by_repo[repo.name], error, time.time()
            )
            outage_by_repo[repo.name] = outage
            if line is not None:
                level = logging.INFO if outage is None else logging.WARNING
                logger.log(level, f"{repo.name}: {line}")
        time.sleep(github.seconds_until_next_cycle(time.time()))


def run_cycle(
    github: GitHubClient, repo: Repository, memory: RepositoryMemory, now: float
) -> RepositoryMemory:
    """One repository's cycle: read supply, cancel what cannot start and delete ended runners'
    registrations, then read demand run by run, weighing launch evidence and submitting after each. Only
    the two supply reads raise, and they come before anything is changed."""
    # Supply
    allocations = read_our_allocations(repo.runner_name_prefix)
    registrations_by_name = read_our_registrations(
        github, repo.api_base_url, repo.runner_name_prefix
    )
    # From the first cycle a runner is seen busy, it may cover a job that still says queued
    sightings = update_sightings(
        memory.runner_sightings, allocations, registrations_by_name, NO_DEMAND, now
    )

    # Only pending jobs, which hold no job: the daemon never ends a started runner
    for allocation in allocations_to_cancel(allocations, registrations_by_name, now):
        cancel_pending_allocation(allocation)
    # Runners Slurm shows ended: GitHub would keep offering their sessions jobs for about 75 s
    for registration in registrations_to_delete(
        allocations, registrations_by_name, sightings
    ):
        if delete_registration(
            github,
            repo.api_base_url,
            registration.runner_id,
            registration.runner_name,
            why="its Slurm job has ended",
        ):
            answered = sightings.deletes_answered | {registration.runner_name}
            sightings = replace(sightings, deletes_answered=answered)

    # Demand: the queued jobs of every watched run, whatever the run's own status says (#39)
    watched_run_ids = watch_new_runs(github, repo.api_base_url, memory.watched_run_ids)
    next_history_page = memory.next_history_page
    next_history_page_at = memory.next_history_page_at
    if now >= next_history_page_at:
        watched_run_ids, next_history_page = watch_runs_from_history_page(
            github, repo.api_base_url, watched_run_ids, next_history_page, now
        )
        next_history_page_at = now + HISTORY_PAGE_INTERVAL_S

    # Newest run first, submitting after each: demand read so far is a lower bound, so a new wave need not
    # wait for older runs to be read. Runners are counted, never matched to jobs (#38).
    # Launch evidence from supply alone first, so a cycle with no run to read still weighs failures (#37).
    breakers = update_breakers(
        memory.breakers,
        allocations,
        registrations_by_name,
        NO_DEMAND,
        runners_taking_a_job={},
        now=now,
    )
    jobs_by_run = {}
    runners_submitted_this_cycle = Counter()
    for run_id in sorted(watched_run_ids, reverse=True):
        jobs_by_run |= read_latest_jobs(github, repo.api_base_url, {run_id})
        demand = demand_from_jobs(jobs_by_run)
        # Busy runners whose jobs may still say queued: those jobs count neither as waiting nor as evidence
        runners_taking_a_job = runners_taking_a_job_by_label(
            allocations, registrations_by_name, sightings, demand, now
        )
        breakers = update_breakers(
            breakers,
            allocations,
            registrations_by_name,
            demand,
            runners_taking_a_job,
            now,
        )
        runners_to_submit = plan_submissions(
            allocations,
            registrations_by_name,
            demand,
            breakers,
            runners_taking_a_job,
            runners_submitted_this_cycle,
            now,
        )
        runners_submitted_this_cycle.update(
            submit_runners(github, repo, submission_order(runners_to_submit))
        )
    for line in breaker_log_lines(memory.breakers, breakers):
        logger.info(f"{repo.name}: {line}")

    sightings = update_sightings(
        sightings,
        allocations,
        registrations_by_name,
        demand_from_jobs(jobs_by_run),
        now,
    )
    watched_run_ids = stop_watching_finished_runs(
        github, repo.api_base_url, watched_run_ids, jobs_by_run
    )
    return RepositoryMemory(
        watched_run_ids, next_history_page, next_history_page_at, breakers, sightings
    )


def submit_runners(
    github: GitHubClient, repo: Repository, size_labels_in_order: list[str]
) -> list[str]:
    """submit_runner for each entry; the size labels of those submitted. A failure is logged and the next
    entry tried; the next cycle counts the gap again. While GitHub asks the client to stop, the rest fail
    at once without a request."""
    submitted = []
    for size_label in size_labels_in_order:
        try:
            submit_runner(
                github, repo.api_base_url, repo.runner_name_prefix, size_label
            )
        except (GitHubUnavailable, SlurmUnavailable) as error:
            logger.warning(
                f"{repo.name}: could not submit a runner for {size_label}: {error}"
            )
            continue
        submitted.append(size_label)
    return submitted


def unsafe_settings(
    boot_allowance_s: float, idle_allowance_s: float, repos: list[Repository]
) -> list[str]:
    """Pure: why these settings would be unsafe; empty when the daemon may start."""
    problems = []
    if not boot_allowance_s < idle_allowance_s:
        problems.append(
            f"BOOT_ALLOWANCE_S ({boot_allowance_s}) must be below IDLE_ALLOWANCE_S"
            f" ({idle_allowance_s}), or a runner that never comes online goes unnoticed"
        )
    for repo in repos:
        if not repo.runner_name_prefix:
            problems.append(f"{repo.name} has an empty runner-name prefix")
    for repo, other in itertools.permutations(repos, 2):
        if other.runner_name_prefix.startswith(repo.runner_name_prefix):
            problems.append(
                f"{other.name}'s runner-name prefix {other.runner_name_prefix!r} starts with"
                f" {repo.name}'s {repo.runner_name_prefix!r}, so both would call its runners theirs"
            )
    return problems


def configure_logging() -> None:
    """INFO and above to stdout, WARNING and above also to stderr, in the Kubernetes log format."""
    formatter = KubernetesLogFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)


if __name__ == "__main__":
    main()
