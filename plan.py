"""
Pure decisions about one repository's runners, from what was read this cycle: where each allocation
stands, what it says about whether its size label can launch runners now (a circuit breaker per size
label), which pending jobs to cancel and how many runners to submit.

No I/O. The records it decides on are defined here, so that the tests and the model in sim/ can import
this module alone; supply.py and demand.py fill them in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from config import (
    BOOT_ALLOWANCE_S,
    BUSY_COVER_S,
    FIRST_PROBE_WAIT_S,
    IDLE_ALLOWANCE_S,
    MAX_PROBE_WAIT_S,
    TOLERATED_FAILURES_IN_A_ROW,
    UNUSED_REGISTRATION_LIFETIME_S,
)

# squeue %T states in which every process of the job has ended; COMPLETING is not one (KillWait 30 s)
FINISHED_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "BOOT_FAIL",
        "DEADLINE",
        "PREEMPTED",
    }
)


@dataclass(frozen=True)
class Allocation:
    """One of our Slurm batch jobs; squeue --states=all also returns ended ones for MinJobAge (300 s)."""

    slurm_job_id: int
    runner_name: str  # Slurm job name == JIT registration name
    # The Slurm job comment, so an ended job still says which label it served
    size_label: str
    state: str  # squeue %T
    is_held: bool  # pending at priority 0, which Slurm never schedules
    submitted_at: float  # epoch seconds; orders launch evidence
    started_at: float | None  # None while pending (squeue only estimates it then)
    ended_at: float | None  # None unless state is in FINISHED_STATES


@dataclass(frozen=True)
class RunnerRegistration:
    """One of our runners as GET /actions/runners reports it."""

    runner_id: int  # what DELETE /actions/runners/{runner_id} takes
    runner_name: str
    is_online: bool
    is_busy: bool


@dataclass(frozen=True)
class WaitingJob:
    """A job GitHub reported queued this cycle, for a size label we serve."""

    job_id: int
    run_id: int
    size_label: str
    created_at: float  # epoch seconds, from GitHub; the job has waited since then
    workflow_name: str
    job_name: str


@dataclass(frozen=True)
class Demand:
    """A lower bound: a run whose read failed this cycle is missing, never stale."""

    waiting_jobs: list[WaitingJob]
    # runner_name of every job read this cycle, any status
    runner_names_on_jobs: set[str]


NO_DEMAND = Demand(waiting_jobs=[], runner_names_on_jobs=set())


class Phase(Enum):
    """Where one of our allocations stands, from squeue and the runners list alone."""

    WAITING_FOR_NODE = "pending, not held, registered or unregistered for under a day"
    # Cancelled: it never started, so it holds no job
    CANNOT_START = "pending, and held or unregistered for a day"
    BOOTING = "running, runner not online yet, younger than B"
    IDLE = "running, online, no job, younger than W"
    # Left alone: the daemon never ends a running runner
    UNAVAILABLE = "busy, completing, unregistered, W or older, or still offline at B"
    ENDED = "finished; squeue shows it for MinJobAge"


class Evidence(Enum):
    """What one allocation says, this cycle, about whether its size label can launch runners now."""

    TOOK_A_JOB = "took a job"
    FAILED_TO_LAUNCH = "failed to launch"


@dataclass(frozen=True)
class LaunchFailure:
    """One allocation that failed to launch, as a breaker remembers it."""

    slurm_job_id: int  # the same failure seen on many cycles is recorded once
    submitted_at: float  # orders evidence: a newer launch says more about now
    recorded_at: float  # when the daemon first saw it; the next probe's wait counts from the newest


@dataclass(frozen=True)
class LabelBreaker:
    """What runners of one size label have shown since the daemon started. A cache: a restart starts from
    UNPROVEN, which costs one runner's boot before that label's waiting jobs are served in full."""

    # Submit time of the newest allocation seen taking a job; None: none yet
    newest_success_submitted_at: float | None
    # Failures of allocations submitted after it, oldest submission first
    failures_since_success: tuple[LaunchFailure, ...]


UNPROVEN = LabelBreaker(newest_success_submitted_at=None, failures_since_success=())


@dataclass(frozen=True)
class RunnerSightings:
    """What the daemon has seen of its runners on GitHub, for those squeue still shows. A cache: a restart
    starts empty, after which a busy runner can cover a waiting job for up to BUSY_COVER_S longer."""

    # Runner name -> when the daemon first saw it busy
    first_seen_busy_at: dict[str, float]
    # Runner names any job read has carried
    seen_on_a_job: frozenset[str]
    # Runner names whose registration delete GitHub answered (deleted, already gone, or refused); not sent again
    deletes_answered: frozenset[str] = frozenset()


NO_SIGHTINGS = RunnerSightings(first_seen_busy_at={}, seen_on_a_job=frozenset())
PHASES_THAT_CAN_TAKE_A_JOB = frozenset(
    {Phase.WAITING_FOR_NODE, Phase.BOOTING, Phase.IDLE}
)
# The batch script exits non-zero only when its runner ended before the idle deadline with no job begun.
# A launch failure as in #37 ends CANCELLED by slurmd, but so does a job we cancelled while pending.
LAUNCH_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "NODE_FAIL", "BOOT_FAIL"})
# From this many failures in a row the probe wait is at its cap, so older failures change nothing
FAILURES_REMEMBERED = (
    TOLERATED_FAILURES_IN_A_ROW
    + 1
    + math.ceil(math.log2(MAX_PROBE_WAIT_S / FIRST_PROBE_WAIT_S))
)


def phase_of_allocation(
    allocation: Allocation, registration: RunnerRegistration | None, now: float
) -> Phase:
    """Checked in order: finished; pending (held, or unregistered for a day -> CANNOT_START); busy,
    completing, unregistered or W or older -> UNAVAILABLE; online -> IDLE; younger than B -> BOOTING;
    otherwise UNAVAILABLE."""
    if allocation.state in FINISHED_STATES:
        return Phase.ENDED
    if allocation.state == "PENDING":
        if allocation.is_held:
            return Phase.CANNOT_START
        # A list read can miss a fresh registration; only GitHub's removal after a day really loses it
        unregistered_for_s = (
            now - allocation.submitted_at if registration is None else 0
        )
        if unregistered_for_s >= UNUSED_REGISTRATION_LIFETIME_S:
            return Phase.CANNOT_START
        return Phase.WAITING_FOR_NODE
    if (
        allocation.state == "COMPLETING"
        or allocation.started_at is None
        or registration is None
        or registration.is_busy
        or now - allocation.started_at >= IDLE_ALLOWANCE_S
    ):
        return Phase.UNAVAILABLE
    if registration.is_online:
        return Phase.IDLE
    if now - allocation.started_at < BOOT_ALLOWANCE_S:
        return Phase.BOOTING
    return Phase.UNAVAILABLE


def allocations_to_cancel(
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    now: float,
) -> list[Allocation]:
    """The CANNOT_START ones; all pending, so cancelling them cannot touch a job."""
    return [
        allocation
        for allocation in allocations
        if phase_of_allocation(
            allocation, registrations_by_name.get(allocation.runner_name), now
        )
        is Phase.CANNOT_START
    ]


def registrations_to_delete(
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    sightings: RunnerSightings,
) -> list[RunnerRegistration]:
    """Pure: the registrations, still listed and not busy, of our runners whose Slurm job squeue shows
    ended, unless a delete was already answered. Slurm ends every process of a finished job, but GitHub
    keeps offering a stopped runner's session jobs for about 75 s until its registration is deleted. A
    registration with no Slurm job in squeue is never one of these: its sbatch may not have happened yet,
    or it is another daemon's."""
    registrations = []
    for allocation in allocations:
        registration = registrations_by_name.get(allocation.runner_name)
        if (
            allocation.state in FINISHED_STATES
            and registration is not None
            and not registration.is_busy
            and allocation.runner_name not in sightings.deletes_answered
        ):
            registrations.append(registration)
    return registrations


def launch_evidence(
    allocation: Allocation,
    registration: RunnerRegistration | None,
    runner_names_on_jobs: set[str],
    uncovered_jobs_waiting_past_b: int,
    now: float,
) -> Evidence | None:
    """TOOK_A_JOB if busy or named on a job read this cycle. FAILED_TO_LAUNCH if it ended FAILED, NODE_FAIL
    or BOOT_FAIL, or ran and ended CANCELLED; or is RUNNING, registered and offline between B and W old; or
    is online and idle past 2B while a job of its size has waited over B that no runner taking a job covers
    (uncovered_jobs_waiting_past_b > 0). Otherwise None."""
    if allocation.runner_name in runner_names_on_jobs or (
        registration is not None and registration.is_busy
    ):
        return Evidence.TOOK_A_JOB
    if allocation.state in LAUNCH_FAILURE_STATES:
        started_at, ended_at = allocation.started_at, allocation.ended_at
        # Slurm stamps a job cancelled while pending with a start equal to its end
        ran = started_at is not None and ended_at is not None and started_at < ended_at
        if allocation.state != "CANCELLED" or ran:
            return Evidence.FAILED_TO_LAUNCH
        return None
    if (
        allocation.state != "RUNNING"
        or allocation.started_at is None
        or registration is None
    ):
        return None
    age_s = now - allocation.started_at
    if age_s >= IDLE_ALLOWANCE_S:
        return None  # its batch script stops it at W; what it showed before W is already recorded
    if not registration.is_online:
        if age_s >= BOOT_ALLOWANCE_S:
            return Evidence.FAILED_TO_LAUNCH
        return None
    # GitHub gives a waiting job to an online idle runner, so this one is not being given jobs
    if age_s > 2 * BOOT_ALLOWANCE_S and uncovered_jobs_waiting_past_b > 0:
        return Evidence.FAILED_TO_LAUNCH
    return None


def record_evidence(
    breaker: LabelBreaker, allocation: Allocation, evidence: Evidence, now: float
) -> LabelBreaker:
    """Newest by submit time wins: a success drops failures submitted at or before it; evidence submitted
    at or before the newest success, or a failure already recorded, changes nothing; at most
    FAILURES_REMEMBERED failures are kept, the most recently submitted."""
    newest_success = breaker.newest_success_submitted_at
    if newest_success is not None and allocation.submitted_at <= newest_success:
        return breaker
    if evidence is Evidence.TOOK_A_JOB:
        newer_failures = tuple(
            failure
            for failure in breaker.failures_since_success
            if failure.submitted_at > allocation.submitted_at
        )
        return LabelBreaker(allocation.submitted_at, newer_failures)
    if any(
        failure.slurm_job_id == allocation.slurm_job_id
        for failure in breaker.failures_since_success
    ):
        return breaker
    failure = LaunchFailure(allocation.slurm_job_id, allocation.submitted_at, now)
    failures = sorted(
        breaker.failures_since_success + (failure,),
        key=lambda remembered: remembered.submitted_at,
    )[-FAILURES_REMEMBERED:]
    if failure not in failures:
        # Older than every failure it would displace; recording it would only restart the wait
        return breaker
    return LabelBreaker(newest_success, tuple(failures))


def update_breakers(
    breakers: dict[str, LabelBreaker],
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    runners_taking_a_job: dict[str, int],
    now: float,
) -> dict[str, LabelBreaker]:
    """record_evidence for every allocation with evidence this cycle; returns a new dict, in which a
    missing size label means UNPROVEN."""
    uncovered = uncovered_jobs_waiting_past_b_by_label(
        demand, runners_taking_a_job, now
    )
    updated = dict(breakers)
    for allocation in sorted(allocations, key=lambda each: each.submitted_at):
        evidence = launch_evidence(
            allocation,
            registrations_by_name.get(allocation.runner_name),
            demand.runner_names_on_jobs,
            uncovered.get(allocation.size_label, 0),
            now,
        )
        if evidence is not None:
            breaker = updated.get(allocation.size_label, UNPROVEN)
            updated[allocation.size_label] = record_evidence(
                breaker, allocation, evidence, now
            )
    return updated


def uncovered_jobs_waiting_past_b_by_label(
    demand: Demand, runners_taking_a_job: dict[str, int], now: float
) -> dict[str, int]:
    """Pure: per size label, its jobs that have waited longer than B, less its runners taking a job; never
    below zero. A covered job is being taken, so an idle runner beside it says nothing."""
    waiting_past_b = {}
    for job in demand.waiting_jobs:
        if now - job.created_at > BOOT_ALLOWANCE_S:
            waiting_past_b[job.size_label] = waiting_past_b.get(job.size_label, 0) + 1
    return {
        label: max(0, count - runners_taking_a_job.get(label, 0))
        for label, count in waiting_past_b.items()
    }


def is_proven(breaker: LabelBreaker) -> bool:
    """A runner of the label took a job since the daemon started, and at most TOLERATED_FAILURES_IN_A_ROW
    launches submitted after it have failed."""
    return (
        breaker.newest_success_submitted_at is not None
        and len(breaker.failures_since_success) <= TOLERATED_FAILURES_IN_A_ROW
    )


def earliest_next_probe_at(breaker: LabelBreaker) -> float:
    """No wait while the failures in a row are within TOLERATED_FAILURES_IN_A_ROW + 1; from the first count
    that leaves a proven label unproven, FIRST_PROBE_WAIT_S doubling per further failure, at most
    MAX_PROBE_WAIT_S, from when the newest was recorded."""
    failures = breaker.failures_since_success
    doublings = len(failures) - TOLERATED_FAILURES_IN_A_ROW - 1
    if doublings < 0:
        return 0.0
    wait_s = min(FIRST_PROBE_WAIT_S * 2**doublings, MAX_PROBE_WAIT_S)
    return max(failure.recorded_at for failure in failures) + wait_s


def runners_to_submit_for_label(
    shortfall: int, runners_that_can_take_a_job: int, breaker: LabelBreaker, now: float
) -> int:
    """Proven: the shortfall. Unproven: one, only when no runner of the label can still take a job and the
    wait is over."""
    if shortfall <= 0:
        return 0
    if is_proven(breaker):
        return shortfall
    if runners_that_can_take_a_job > 0 or now < earliest_next_probe_at(breaker):
        return 0
    return 1


def plan_submissions(
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    breakers: dict[str, LabelBreaker],
    runners_taking_a_job: dict[str, int],
    now: float,
) -> dict[str, int]:
    """Runners to submit per size label with a waiting job: waiting jobs, less those runners_taking_a_job
    cover, minus runners that can still take one, through the label's breaker. A runner that is evidence
    this cycle cannot: it took a job, or it failed to launch, which includes one GitHub left idle while an
    uncovered job of its size waited."""
    waiting_jobs_by_label = jobs_left_waiting_by_label(demand, runners_taking_a_job)
    uncovered = uncovered_jobs_waiting_past_b_by_label(
        demand, runners_taking_a_job, now
    )
    runners_that_can_take_a_job_by_label = {}
    for allocation in allocations:
        registration = registrations_by_name.get(allocation.runner_name)
        phase = phase_of_allocation(allocation, registration, now)
        evidence = launch_evidence(
            allocation,
            registration,
            demand.runner_names_on_jobs,
            uncovered.get(allocation.size_label, 0),
            now,
        )
        if phase in PHASES_THAT_CAN_TAKE_A_JOB and evidence is None:
            label = allocation.size_label
            runners_that_can_take_a_job_by_label[label] = (
                runners_that_can_take_a_job_by_label.get(label, 0) + 1
            )

    runners_to_submit = {}
    for label, waiting_jobs in sorted(waiting_jobs_by_label.items()):
        can_take = runners_that_can_take_a_job_by_label.get(label, 0)
        runners_to_submit[label] = runners_to_submit_for_label(
            waiting_jobs - can_take, can_take, breakers.get(label, UNPROVEN), now
        )
    return runners_to_submit


def update_sightings(
    sightings: RunnerSightings,
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    now: float,
) -> RunnerSightings:
    """Pure: sightings plus when each busy runner was first seen busy and every name on a job read so far;
    runners squeue no longer shows are forgotten, along with the deletes answered for them."""
    shown = {allocation.runner_name for allocation in allocations}
    first_seen_busy_at = {
        name: seen_at
        for name, seen_at in sightings.first_seen_busy_at.items()
        if name in shown
    }
    for name, registration in registrations_by_name.items():
        if registration.is_busy and name in shown:
            first_seen_busy_at.setdefault(name, now)
    seen_on_a_job = frozenset(
        name
        for name in sightings.seen_on_a_job | demand.runner_names_on_jobs
        if name in shown
    )
    deletes_answered = frozenset(sightings.deletes_answered & shown)
    return RunnerSightings(first_seen_busy_at, seen_on_a_job, deletes_answered)


def runners_taking_a_job_by_label(
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    sightings: RunnerSightings,
    demand: Demand,
    now: float,
) -> dict[str, int]:
    """Pure: per size label, our runners GitHub has offered a job that may still say queued, each covering
    one waiting job: busy, RUNNING in Slurm, named on no job read so far or remembered, and first seen busy
    less than BUSY_COVER_S ago. Past that, a busy runner no job names serves a job the daemon cannot see;
    one whose Slurm job ended died holding its offer."""
    named = sightings.seen_on_a_job | demand.runner_names_on_jobs
    taking = {}
    for allocation in allocations:
        registration = registrations_by_name.get(allocation.runner_name)
        first_seen_busy_at = sightings.first_seen_busy_at.get(allocation.runner_name)
        if (
            allocation.state == "RUNNING"
            and registration is not None
            and registration.is_busy
            and allocation.runner_name not in named
            and first_seen_busy_at is not None
            and now - first_seen_busy_at < BUSY_COVER_S
        ):
            label = allocation.size_label
            taking[label] = taking.get(label, 0) + 1
    return taking


def jobs_left_waiting_by_label(
    demand: Demand, runners_taking_a_job: dict[str, int]
) -> dict[str, int]:
    """Pure: per size label with a waiting job, how many no runner taking a job of that size covers; never
    below zero."""
    waiting = {}
    for job in demand.waiting_jobs:
        waiting[job.size_label] = waiting.get(job.size_label, 0) + 1
    return {
        label: max(0, count - runners_taking_a_job.get(label, 0))
        for label, count in waiting.items()
    }


def submission_order(runners_to_submit: dict[str, int]) -> list[str]:
    """Size labels taken in turn, one runner each, so a large wave cannot hold up another label's single
    job."""
    order = []
    for round_index in range(max(runners_to_submit.values(), default=0)):
        for label in sorted(runners_to_submit):
            if runners_to_submit[label] > round_index:
                order.append(label)
    return order


def breaker_log_lines(
    before: dict[str, LabelBreaker], after: dict[str, LabelBreaker]
) -> list[str]:
    """One line per size label that became proven or unproven or whose failures in a row changed, e.g.
    "slurm-runner-medium: unproven, 2 failures in a row (newest: Slurm job 812345); one runner at a time,
    the next 60 s after the newest failure"."""
    lines = []
    for label in sorted(after):
        breaker = after[label]
        old = before.get(label, UNPROVEN)
        failures = breaker.failures_since_success
        # Not the count alone: past FAILURES_REMEMBERED a new failure replaces the oldest
        if (
            is_proven(old) == is_proven(breaker)
            and old.failures_since_success == failures
        ):
            continue
        at_cap = len(failures) == FAILURES_REMEMBERED
        count = f"{len(failures)}{' or more' if at_cap else ''}"
        line = f"{label}: {'proven' if is_proven(breaker) else 'unproven'}"
        line += f", {count} failure{'' if len(failures) == 1 else 's'} in a row"
        if failures:
            newest = max(failures, key=lambda failure: failure.recorded_at)
            line += f" (newest: Slurm job {newest.slurm_job_id})"
            if not is_proven(breaker):
                line += "; one runner at a time"
                wait_s = earliest_next_probe_at(breaker) - newest.recorded_at
                if wait_s > 0:
                    line += f", the next {wait_s:.0f} s after the newest failure"
        elif not is_proven(breaker):
            line += "; one runner at a time"
        lines.append(line)
    return lines
