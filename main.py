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
from collections.abc import Callable
from dataclasses import dataclass, replace

from dotenv import load_dotenv

from config import (
    BOOT_ALLOWANCE_S,
    HISTORY_PAGE_INTERVAL_S,
    IDLE_ALLOWANCE_S,
    IDLE_STOP_MARGIN_S,
    NO_JOB_SBATCH_FAILURES_TO_STOP_A_CYCLE,
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
from node_rule import (
    NO_LAUNCH_RECORDS,
    LaunchRecords,
    Submission,
    file_launch_evidence,
    forget_nodes_slurm_does_not_know,
    launch_record_log_lines,
    pending_runners_by_narrowed_exclusion,
    next_unpinned_runner_at,
    plan_submissions,
    record_refused_submission,
)
from plan import (
    NO_DEMAND,
    NO_SIGHTINGS,
    Allocation,
    RunnerRegistration,
    RunnerSightings,
    allocations_to_cancel,
    registrations_to_delete,
    runners_covering_a_job_by_size_label,
    update_sightings,
)
from supply import (
    SlurmTimedOut,
    SlurmUnavailable,
    SbatchLeftNoJob,
    SubmissionRefused,
    cancel_pending_allocation,
    delete_registration,
    narrow_pending_exclusions,
    nodes_slurm_knows,
    read_our_allocations,
    read_our_registrations,
    submit_runner,
)

logger = logging.getLogger()


@dataclass(frozen=True)
class RepositoryMemory:
    """The daemon's caches for one repository. A restart starts empty: runs are found again, and every
    size is usable nowhere until squeue and the runners list show where it works."""

    # Losing it costs until the runs are found again (status lists, history sweep)
    watched_run_ids: set[int]
    next_history_page: int
    next_history_page_at: float  # epoch seconds
    # Where each size can launch (the node rule); losing it costs one runner's boot per size
    launch_records: LaunchRecords
    # Losing it lets a busy runner cover a waiting job for up to BUSY_COVER_S longer
    runner_sightings: RunnerSightings


def empty_memory() -> RepositoryMemory:
    """What a start knows: nothing, so the first cycle reads the first history page."""
    return RepositoryMemory(
        watched_run_ids=set(),
        next_history_page=1,
        next_history_page_at=0.0,
        launch_records=NO_LAUNCH_RECORDS,
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
    problems = unsafe_settings(
        BOOT_ALLOWANCE_S, IDLE_ALLOWANCE_S, IDLE_STOP_MARGIN_S, REPOS_TO_MONITOR
    )
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
                    github,
                    repo,
                    memory_by_repo[repo.name],
                    cycle_started_at=time.time(),
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
    github: GitHubClient,
    repo: Repository,
    memory: RepositoryMemory,
    cycle_started_at: float,
    clock: Callable[[], float] = time.time,
) -> RepositoryMemory:
    """One repository's cycle: read supply, cancel what cannot start and delete ended runners'
    registrations, then read demand run by run, weighing launch evidence and submitting after each. Only
    the two supply reads raise, and they come before anything is changed. What squeue and the runners list
    showed is judged at cycle_started_at, when they were read, except whether a runner is online, which is
    judged when the runners list was read. Each run is planned at clock() when it is
    read, and what is recorded is stamped with clock(): a cycle that submits 150 runners lasts about 3
    minutes, nodes come due during it, and runners submitted earlier in it must count as submitted before
    what is recorded later."""
    # 1. Supply: squeue, read now, then the runners list, stamped when read
    allocations = read_our_allocations(repo.runner_name_prefix)
    registrations_by_name = read_our_registrations(
        github, repo.api_base_url, repo.runner_name_prefix, clock
    )
    # From the first cycle a runner is seen busy, it may cover a job that still says queued
    sightings = update_sightings(
        memory.runner_sightings,
        allocations,
        registrations_by_name,
        NO_DEMAND,
        cycle_started_at,
    )
    # 2. Clean up what can never take a job
    sightings = clean_up_runners(
        github, repo, allocations, registrations_by_name, sightings, cycle_started_at
    )

    # 3. Demand: which runs to read. Their queued jobs count whatever the run's own status says (#39)
    watched_run_ids = watch_new_runs(github, repo.api_base_url, memory.watched_run_ids)
    next_history_page = memory.next_history_page
    next_history_page_at = memory.next_history_page_at
    if cycle_started_at >= next_history_page_at:
        watched_run_ids, next_history_page = watch_runs_from_history_page(
            github,
            repo.api_base_url,
            watched_run_ids,
            next_history_page,
            cycle_started_at,
        )
        next_history_page_at = cycle_started_at + HISTORY_PAGE_INTERVAL_S

    # 4. Launch evidence from supply alone, so a cycle with no run to read still weighs failures (#37)
    records = file_launch_evidence(
        memory.launch_records,
        allocations,
        registrations_by_name,
        NO_DEMAND,
        runners_covering_a_job={},
        cycle_started_at=cycle_started_at,
        seen_at=cycle_started_at,
    )
    # 5. Rule 4, once per cycle and before demand is read, so a released pending runner can start meanwhile
    narrow_pending_runners(repo, allocations, records, cycle_started_at)

    # 6. Each run, newest first: read its jobs, weigh evidence, submit. Demand read so far is a lower bound,
    # so a new wave need not wait for older runs to be read. Runners are counted, never matched to jobs (#38)
    jobs_by_run = {}
    submitted_this_cycle = []
    # sbatch failures this cycle that left no job and named no node Slurm does not know
    sbatch_left_no_job = 0
    for run_id in sorted(watched_run_ids, reverse=True):
        jobs_by_run |= read_latest_jobs(github, repo.api_base_url, {run_id})
        run_read_at = clock()
        demand = demand_from_jobs(jobs_by_run)
        # Busy runners whose jobs may still say queued: those jobs count neither as waiting nor as evidence
        runners_covering_a_job = runners_covering_a_job_by_size_label(
            allocations, registrations_by_name, sightings, demand, run_read_at
        )
        records = file_launch_evidence(
            records,
            allocations,
            registrations_by_name,
            demand,
            runners_covering_a_job,
            cycle_started_at=cycle_started_at,
            seen_at=run_read_at,
        )
        slurm_not_answering = (
            sbatch_left_no_job >= NO_JOB_SBATCH_FAILURES_TO_STOP_A_CYCLE
        )
        if slurm_not_answering:
            # Nothing more is submitted, but the rest of the runs are still read: their jobs still give
            # evidence, and stop_watching_finished_runs needs them
            continue
        submissions = plan_submissions(
            allocations,
            registrations_by_name,
            demand,
            records,
            runners_covering_a_job,
            submitted_this_cycle,
            planned_at=run_read_at,
            cycle_started_at=cycle_started_at,
        )
        submitted, records, sbatch_left_no_job = submit_runners(
            github, repo, submissions, records, clock, sbatch_left_no_job
        )
        submitted_this_cycle += submitted

    # 7. Log what the node rule now believes, where it changed
    for level, line in launch_record_log_lines(memory.launch_records, records, clock()):
        logger.log(level, f"{repo.name}: {line}")

    # 8. Remember what this cycle saw
    sightings = update_sightings(
        sightings,
        allocations,
        registrations_by_name,
        demand_from_jobs(jobs_by_run),
        cycle_started_at,
    )
    watched_run_ids = stop_watching_finished_runs(
        github, repo.api_base_url, watched_run_ids, jobs_by_run
    )
    return RepositoryMemory(
        watched_run_ids, next_history_page, next_history_page_at, records, sightings
    )


def clean_up_runners(
    github: GitHubClient,
    repo: Repository,
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    sightings: RunnerSightings,
    now: float,
) -> RunnerSightings:
    """Cancel our pending runners that can never start, and delete the registrations of runners whose
    Slurm job has ended; sightings, with the deletes GitHub answered. Neither can touch a runner that holds
    a job: only pending jobs are cancelled, and the daemon never ends a started runner."""
    for allocation in allocations_to_cancel(allocations, registrations_by_name, now):
        cancel_pending_allocation(allocation)
    # Runners Slurm shows ended: GitHub would keep offering their sessions jobs for about 75 s
    for registration in registrations_to_delete(
        allocations, registrations_by_name, sightings
    ):
        github_answered = delete_registration(
            github,
            repo.api_base_url,
            registration.runner_id,
            registration.runner_name,
            why="its Slurm job has ended",
        )
        if github_answered:
            answered = sightings.deletes_answered | {registration.runner_name}
            sightings = replace(sightings, deletes_answered=answered)
    return sightings


def narrow_pending_runners(
    repo: Repository, allocations: list[Allocation], records: LaunchRecords, now: float
) -> None:
    """narrow_pending_exclusions for each group of pending_runners_by_narrowed_exclusion, and one log line
    per size and change."""
    narrowed = Counter()
    groups = pending_runners_by_narrowed_exclusion(allocations, records, now)
    for excluded_nodes, pending in sorted(
        groups.items(), key=lambda item: sorted(item[0])
    ):
        not_updated = narrow_pending_exclusions(pending, excluded_nodes)
        for allocation in pending:
            if allocation.slurm_job_id not in not_updated:
                before = ",".join(sorted(allocation.excluded_nodes))
                after = ",".join(sorted(excluded_nodes)) or "none"
                narrowed[(allocation.size_label, before, after)] += 1
    for (size_label, before, after), count in sorted(narrowed.items()):
        logger.info(
            f"{repo.name}: {size_label}: narrowed the exclusion of {count} pending"
            f" runner{'' if count == 1 else 's'} from {before} to {after}"
        )


def submit_runners(
    github: GitHubClient,
    repo: Repository,
    submissions_in_order: list[Submission],
    records: LaunchRecords,
    clock: Callable[[], float],
    sbatch_left_no_job: int,
) -> tuple[list[Submission], LaunchRecords, int]:
    """submit_runner for each entry; those submitted, the records, and how many sbatch calls this cycle,
    sbatch_left_no_job of them before this call, left no job and named no node Slurm does not know. What
    the cycle records is stamped clock() when it happens.
    - Refused (the FAILED record): record_refused_submission; the size submits nothing more this cycle.
    - sbatch left no job and named nodes Slurm does not know: those nodes are forgotten for every size, and
      the entries naming them are skipped. Otherwise (nothing named, all named nodes known, or Slurm could
      not be asked) nothing is recorded: the first such failure in a cycle skips its own entry, since the
      squeue that classified it answered; at NO_JOB_SBATCH_FAILURES_TO_STOP_A_CYCLE Slurm is taken as not
      answering and nothing more is submitted this cycle.
    - Timed out: its job may exist, so it counts as submitted.
    Any other failure is logged and the next entry tried; the next cycle counts the gap again. While GitHub
    asks the client to stop, the rest fail at once without a request."""
    submitted = []
    refused_size_labels = set()
    unknown_nodes = frozenset()
    for submission in submissions_in_order:
        size_label = submission.size_label
        if size_label in refused_size_labels or named_nodes(submission) & unknown_nodes:
            continue
        try:
            submit_runner(
                github, repo.api_base_url, repo.runner_name_prefix, submission
            )
        except SubmissionRefused as refused:
            refused_at = clock()
            records = record_refused_submission(
                records, submission, refused.runner_name, refused_at
            )
            refused_size_labels.add(size_label)
            line = refusal_log_line(submission, refused, records, refused_at)
            logger.warning(f"{repo.name}: {line}")
            continue
        except SbatchLeftNoJob as left:
            unknown = nodes_slurm_does_not_know(named_nodes(submission))
            if not unknown:
                sbatch_left_no_job += 1
                if sbatch_left_no_job < NO_JOB_SBATCH_FAILURES_TO_STOP_A_CYCLE:
                    logger.warning(
                        f"{repo.name}: {size_label}: sbatch failed and squeue shows no"
                        f" {left.runner_name} ({left}); not counted"
                    )
                    continue
                logger.warning(
                    f"{repo.name}: {size_label}: sbatch failed and squeue shows no"
                    f" {left.runner_name} ({left}); not counted, and Slurm is"
                    f" taken as not answering: no more submissions this cycle"
                )
                return submitted, records, sbatch_left_no_job
            records = forget_nodes_slurm_does_not_know(records, unknown, clock())
            unknown_nodes |= unknown
            logger.warning(
                f"{repo.name}: {size_label}: sbatch failed for {left.runner_name};"
                f" Slurm does not know {','.join(sorted(unknown))}, forgotten for every size"
            )
            continue
        except SlurmTimedOut as error:
            # Its job may exist: in flight
            logger.warning(
                f"{repo.name}: a runner for {size_label} may have been submitted: {error}"
            )
            submitted.append(submission)
            continue
        except (GitHubUnavailable, SlurmUnavailable) as error:
            logger.warning(
                f"{repo.name}: could not submit a runner for {size_label}: {error}"
            )
            continue
        submitted.append(submission)
    return submitted, records, sbatch_left_no_job


def named_nodes(submission: Submission) -> frozenset[str]:
    """Pure: the nodes a submission names, excluded or pinned."""
    if submission.pinned_node is None:
        return submission.excluded_nodes
    return frozenset({submission.pinned_node})


def nodes_slurm_does_not_know(nodes: frozenset[str]) -> frozenset[str]:
    """Those of nodes that sinfo does not list; none when nodes is empty or Slurm cannot be asked, which
    is never taken to mean a node is unknown."""
    if not nodes:
        return frozenset()
    try:
        return nodes - nodes_slurm_knows()
    except SlurmUnavailable as error:
        logger.warning(f"Could not ask Slurm which nodes it knows: {error}")
        return frozenset()


def refusal_log_line(
    submission: Submission,
    refused: SubmissionRefused,
    records: LaunchRecords,
    refused_at: float,
) -> str:
    """Pure: e.g. "slurm-runner-medium: Slurm refused gha-runner-1a2b3c (<sbatch's message>), excluding
    trpro-slurm1; next runner in 40 s at the earliest"."""
    line = f"{submission.size_label}: Slurm refused {refused.runner_name} ({refused})"
    if submission.pinned_node is not None:
        return (
            f"{line}, pinned to {submission.pinned_node}; {submission.pinned_node} is untried for"
            " this size again"
        )
    if submission.excluded_nodes:
        line += f", excluding {','.join(sorted(submission.excluded_nodes))}"
    wait_s = max(
        0.0, next_unpinned_runner_at(records, submission.size_label) - refused_at
    )
    return f"{line}; next runner in {wait_s:.0f} s at the earliest"


def unsafe_settings(
    boot_allowance_s: float,
    idle_allowance_s: float,
    idle_stop_margin_s: float,
    repos: list[Repository],
) -> list[str]:
    """Pure: why these settings would be unsafe; empty when the daemon may start."""
    problems = []
    if not boot_allowance_s < idle_allowance_s - idle_stop_margin_s:
        problems.append(
            f"BOOT_ALLOWANCE_S ({boot_allowance_s}) must be below IDLE_ALLOWANCE_S ({idle_allowance_s})"
            f" less IDLE_STOP_MARGIN_S ({idle_stop_margin_s}), or a runner that never comes online"
            " goes unnoticed"
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
