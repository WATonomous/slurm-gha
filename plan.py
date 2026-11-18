"""
Pure decisions about what our runners are doing, from what was read this cycle: where each allocation
stands, what it says about whether its size can launch runners on its node, which pending jobs to
cancel, which registrations to delete, and which busy runners cover a job that still says queued.
Where each size may go, and how many runners to submit, is node_rule.py, which reads these records.

No I/O. The records it decides on are defined here, so that the tests can import this module alone;
supply.py and demand.py fill them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config import (
    BOOT_ALLOWANCE_S,
    BUSY_COVER_S,
    IDLE_ALLOWANCE_S,
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
    # squeue %N: the node it runs or ran on, kept after it ends for MinJobAge; None while pending
    node: str | None = None
    # squeue %n: the node it was submitted to with --nodelist, as squeue prints it; None when not pinned
    pinned_node: str | None = None
    # squeue %x, ranges expanded: the nodes it may not be placed on; empty when none, None when %x did not
    # parse (then it is left alone)
    excluded_nodes: frozenset[str] | None = frozenset()


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

    job_id: int  # GitHub's; the tests tell waiting jobs apart by it
    size_label: str
    created_at: float  # epoch seconds, from GitHub; the job has waited since then


@dataclass(frozen=True)
class Demand:
    """A lower bound: a run whose read failed this cycle is missing, never stale."""

    waiting_jobs: list[WaitingJob]
    # runner_name of every job read this cycle, any status
    runner_names_on_jobs: set[str]


NO_DEMAND = Demand(waiting_jobs=[], runner_names_on_jobs=set())


class Phase(Enum):
    """Where one of our allocations stands, from squeue and the runners list alone."""

    PENDING = "pending, not held, registered or unregistered for under a day"
    # Cancelled: it never started, so it holds no job
    CANNOT_START = "pending, and held or unregistered for a day"
    BOOTING = "running, runner not online yet, younger than B"
    IDLE = "running, online, no job, younger than W"
    # Left alone: the daemon never ends a running runner
    UNAVAILABLE = "busy, completing, unregistered, W or older, or still offline at B"
    ENDED = "finished; squeue shows it for MinJobAge"


class Evidence(Enum):
    """What one allocation says, this cycle, about whether its size label can launch runners on its node:
    it took a job, or it failed to launch in one of three ways, which point at different faults."""

    TOOK_A_JOB = "took a job"
    # The image, Docker, the runner itself, or Slurm's launch: its log says which
    ENDED_WITHOUT_A_JOB = "ended without taking a job"
    # A slow boot, CVMFS, or a hang
    STILL_OFFLINE_AT_B = "was still offline B after it started"
    # GitHub is not giving jobs to our runners
    LEFT_IDLE_WHILE_A_JOB_WAITED = "stayed idle while a job of its size waited"

    @property
    def failed_to_launch(self) -> bool:
        return self is not Evidence.TOOK_A_JOB


@dataclass(frozen=True)
class RunnerSightings:
    """What the daemon has seen of its runners on GitHub, for those squeue still shows, and the
    registrations GitHub has answered a delete for. A cache: a restart starts empty, after which a busy
    runner can cover a waiting job for up to BUSY_COVER_S longer."""

    # Runner name -> when the daemon first saw it busy
    first_seen_busy_at: dict[str, float]
    # Runner names any job read has carried
    seen_on_a_job: frozenset[str]
    # Runner names whose registration delete GitHub answered (deleted, already gone, or declined); not sent
    # again
    deletes_answered: frozenset[str] = frozenset()


NO_SIGHTINGS = RunnerSightings(first_seen_busy_at={}, seen_on_a_job=frozenset())
PHASES_THAT_CAN_TAKE_A_JOB = frozenset({Phase.PENDING, Phase.BOOTING, Phase.IDLE})
# The batch script exits non-zero only when its runner ended before the idle deadline with no job begun.
# A launch failure as in #37 ends CANCELLED by slurmd with its node kept, but so does a job we cancelled
# while pending, which never had a node.
LAUNCH_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "NODE_FAIL", "BOOT_FAIL"})


def phase_of_allocation(
    allocation: Allocation, registration: RunnerRegistration | None, now: float
) -> Phase:
    """Checked in order: finished; pending (held, or unregistered for a day -> CANNOT_START); busy,
    completing, unregistered or W or older -> UNAVAILABLE; online -> IDLE; younger than B -> BOOTING;
    otherwise UNAVAILABLE. now is when squeue and the runners list were read: an observation is judged
    at the time it was observed."""
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
        return Phase.PENDING
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
    """TOOK_A_JOB if busy or named on a job read this cycle. Failed to launch: ENDED_WITHOUT_A_JOB if it
    ended FAILED, NODE_FAIL or BOOT_FAIL, or ended CANCELLED on a node; STILL_OFFLINE_AT_B if it is RUNNING,
    registered and offline between B and W old; LEFT_IDLE_WHILE_A_JOB_WAITED if it is online and idle past
    2B while a job of its size has waited over B that no runner covers (uncovered_jobs_waiting_past_b > 0).
    Otherwise None."""
    if allocation.runner_name in runner_names_on_jobs or (
        registration is not None and registration.is_busy
    ):
        return Evidence.TOOK_A_JOB
    if allocation.state in LAUNCH_FAILURE_STATES:
        # Cancelled while pending, it never had a node. A launch slurmd gave up on has one, and can end
        # within the second it started, so its start and end do not tell the two apart
        if allocation.state != "CANCELLED" or allocation.node is not None:
            return Evidence.ENDED_WITHOUT_A_JOB
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
            return Evidence.STILL_OFFLINE_AT_B
        return None
    # GitHub gives a waiting job to an online idle runner, so this one is not being given jobs
    if age_s > 2 * BOOT_ALLOWANCE_S and uncovered_jobs_waiting_past_b > 0:
        return Evidence.LEFT_IDLE_WHILE_A_JOB_WAITED
    return None


def uncovered_jobs_waiting_past_b_by_size_label(
    demand: Demand, runners_covering_a_job: dict[str, int], now: float
) -> dict[str, int]:
    """Pure: per size label, its jobs that had waited longer than B at now, when the runners list was
    read, less its runners covering a job; never below zero. A covered job is being taken, so an idle runner
    beside it says nothing."""
    waiting_past_b = {}
    for job in demand.waiting_jobs:
        if now - job.created_at > BOOT_ALLOWANCE_S:
            waiting_past_b[job.size_label] = waiting_past_b.get(job.size_label, 0) + 1
    return {
        size_label: max(0, count - runners_covering_a_job.get(size_label, 0))
        for size_label, count in waiting_past_b.items()
    }


def runner_can_still_take_a_job(
    allocation: Allocation,
    registration: RunnerRegistration | None,
    demand: Demand,
    uncovered_jobs_waiting_past_b: int,
    now: float,
) -> bool:
    """Pure: pending, booting or idle, and no evidence (it took a job, or GitHub left it idle while a job
    of its size waited), judged at now, when the supply was read: the supply test, and what "in flight"
    means for the node rule."""
    phase = phase_of_allocation(allocation, registration, now)
    evidence = launch_evidence(
        allocation,
        registration,
        demand.runner_names_on_jobs,
        uncovered_jobs_waiting_past_b,
        now,
    )
    return phase in PHASES_THAT_CAN_TAKE_A_JOB and evidence is None


def update_sightings(
    sightings: RunnerSightings,
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    seen_at: float,
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
            first_seen_busy_at.setdefault(name, seen_at)
    seen_on_a_job = frozenset(
        name
        for name in sightings.seen_on_a_job | demand.runner_names_on_jobs
        if name in shown
    )
    deletes_answered = frozenset(sightings.deletes_answered & shown)
    return RunnerSightings(first_seen_busy_at, seen_on_a_job, deletes_answered)


def runners_covering_a_job_by_size_label(
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
    covering = {}
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
            size_label = allocation.size_label
            covering[size_label] = covering.get(size_label, 0) + 1
    return covering


def jobs_left_waiting_by_size_label(
    demand: Demand, runners_covering_a_job: dict[str, int]
) -> dict[str, int]:
    """Pure: per size label with a waiting job, how many no runner of that size covers; never
    below zero."""
    waiting = {}
    for job in demand.waiting_jobs:
        waiting[job.size_label] = waiting.get(job.size_label, 0) + 1
    return {
        size_label: max(0, count - runners_covering_a_job.get(size_label, 0))
        for size_label, count in waiting.items()
    }
