"""
Pure decisions about one repository's runners, from what was read this cycle: where each allocation
stands, what it says about where its size label can launch runners, which pending jobs to cancel, and
which runners to submit where.

Where a size may go is the node rule. For each size and node, the daemon remembers how the newest runner
of that size on that node fared:
1. A size is not sent to a node where its newest runner failed to launch. It is used there again as soon
   as a runner of that size takes a job there.
2. Such a node is tried again with one extra runner pinned to it, 1 minute after the failure, then 2, 4, 8
   and at most 10 minutes after each further failure there, while a job of that size waits.
3. A size with no node where it is known to work (after a restart, or when it fails everywhere) gets one
   runner at a time.
4. A runner still pending in Slurm's queue is never left excluding a node that a new runner of its size
   would now be allowed on.
5. A failure that belongs to no node (Slurm refused the submission, or the failed job shows no node)
   counts against the size: its next runner waits 1, 2, 4, 8, at most 10 minutes.

No I/O. The records it decides on are defined here, so that the tests and a model of the daemon can
import this module alone; supply.py and demand.py fill them in.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum

from config import (
    BOOT_ALLOWANCE_S,
    BUSY_COVER_S,
    FIRST_RETRY_WAIT_S,
    IDLE_ALLOWANCE_S,
    MAX_RETRY_WAIT_S,
    REFUSED_NAMES_KEPT_S,
    SLURM_OUTPUT_PATH,
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

    job_id: int
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
class NodeRecord:
    """Rules 1 and 2: how runners of one size have fared on one node, ordered by submit time. A cache: after
    a restart there is none. NodeRecord() is an untried node, allowed like a usable one."""

    # The newest runner of the size there that took a job, and its Slurm job (for the log)
    newest_success_submitted_at: float | None = None
    newest_success_slurm_job_id: int | None = None
    # The newest that failed to launch there; the node is out when this is newer (a tie is usable)
    newest_failure_submitted_at: float | None = None
    newest_failure_slurm_job_id: int | None = None
    newest_failure_kind: Evidence | None = None  # for the log: how it failed
    # When that failure was first seen: the retry wait counts from here
    newest_failure_seen_at: float | None = None
    # When the failure that put the node out was seen; None while usable
    out_since: float | None = None
    # Failures there of runners submitted after out_since: the wait doubles per one
    failed_retries: int = 0
    # When Slurm refused a submission naming the node and the record was forgotten: untried since, and
    # evidence from runners submitted before then is not filed again
    forgotten_at: float | None = None


@dataclass(frozen=True)
class SizeRecord:
    """What holds one size usable nowhere beyond its node records: refused submissions, and failures that
    belong to no node (rule 5). Outstanding until a runner of the size submitted after outstanding_since
    takes a job, on any node; then everything resets but the names and newest_evidence_submitted_at."""

    # The newest refusal or failure with no node; None: nothing outstanding
    outstanding_since: float | None = None
    # Rule 5: the next runner waits from here; None: no failure with no node outstanding
    nodeless_failure_seen_at: float | None = None
    # Failures with no node of runners submitted after the previous one was seen
    nodeless_retries: int = 0
    # The nodes the last refused unpinned submission excluded; empty: none
    refused_exclusion: frozenset[str] = frozenset()
    # Every refused runner's name -> when; kept REFUSED_NAMES_KEPT_S, so the FAILED record its refusal may
    # leave in squeue is never counted as a launch failure
    refused_runner_names: dict[str, float] = field(default_factory=dict)
    # The newest runner of the size whose failure with no node was counted, or that took a job: a failure
    # with no node of a runner submitted no later changes nothing (the same one seen again included)
    newest_evidence_submitted_at: float | None = None


@dataclass(frozen=True)
class LaunchRecords:
    """Everything the node rule remembers. A cache: a restart starts with none, so every size is usable
    nowhere and gets one runner until squeue and the runners list show where it works."""

    by_node_and_size: dict[tuple[str, str], NodeRecord]
    by_size: dict[str, SizeRecord]


NO_LAUNCH_RECORDS = LaunchRecords(by_node_and_size={}, by_size={})


@dataclass(frozen=True)
class Submission:
    """One runner to submit, and where Slurm may put it."""

    size_label: str
    excluded_nodes: frozenset[str]  # sbatch --exclude; empty for a pinned runner
    # sbatch --nodes=1 --nodelist: the retry of a node that is out for the size (rule 2)
    pinned_node: str | None = None
    retry: int = 0  # for the log: which retry of that node this is, from 1


@dataclass(frozen=True)
class RunnerSightings:
    """What the daemon has seen of its runners on GitHub, for those squeue still shows. A cache: a restart
    starts empty, after which a busy runner can cover a waiting job for up to BUSY_COVER_S longer."""

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
# A launch failure as in #37 ends CANCELLED by slurmd, but so does a job we cancelled while pending.
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
    ended FAILED, NODE_FAIL or BOOT_FAIL, or ran and ended CANCELLED; STILL_OFFLINE_AT_B if it is RUNNING,
    registered and offline between B and W old; LEFT_IDLE_WHILE_A_JOB_WAITED if it is online and idle past
    2B while a job of its size has waited over B that no runner covers (uncovered_jobs_waiting_past_b > 0).
    Otherwise None."""
    if allocation.runner_name in runner_names_on_jobs or (
        registration is not None and registration.is_busy
    ):
        return Evidence.TOOK_A_JOB
    if allocation.state in LAUNCH_FAILURE_STATES:
        started_at, ended_at = allocation.started_at, allocation.ended_at
        # Slurm stamps a job cancelled while pending with a start equal to its end
        ran = started_at is not None and ended_at is not None and started_at < ended_at
        if allocation.state != "CANCELLED" or ran:
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
    of its size waited), judged at now, when the supply was read: v8's supply test, and what "in flight"
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


# Evidence, filed per node and size


def file_launch_evidence(
    records: LaunchRecords,
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    runners_covering_a_job: dict[str, int],
    cycle_started_at: float,
    seen_at: float,
) -> LaunchRecords:
    """Pure: launch_evidence for every allocation, oldest submission first, filed under (its node, its
    size) by record_node_evidence. A failure that shows no node is filed under its size by
    record_nodeless_failure, unless it is the FAILED record a refused submission left. A runner that took a
    job clears what is outstanding for its size if it was submitted after outstanding_since. Refused names
    older than REFUSED_NAMES_KEPT_S are dropped. What squeue and the runners list showed is judged at
    cycle_started_at, when they were read; what is recorded is stamped seen_at, when this is filed."""
    uncovered = uncovered_jobs_waiting_past_b_by_size_label(
        demand, runners_covering_a_job, cycle_started_at
    )
    by_node_and_size = dict(records.by_node_and_size)
    by_size = {
        size_label: replace(
            record,
            refused_runner_names={
                name: refused_at
                for name, refused_at in record.refused_runner_names.items()
                if seen_at - refused_at < REFUSED_NAMES_KEPT_S
            },
        )
        for size_label, record in records.by_size.items()
    }
    evidence_by_allocation = [
        (
            allocation,
            launch_evidence(
                allocation,
                registrations_by_name.get(allocation.runner_name),
                demand.runner_names_on_jobs,
                uncovered.get(allocation.size_label, 0),
                cycle_started_at,
            ),
        )
        for allocation in sorted(allocations, key=lambda each: each.submitted_at)
    ]
    # A failure with no node of a runner no newer than one of its size that took a job says nothing about
    # now, also before the size has a record (after a restart)
    newest_success_by_size = {
        allocation.size_label: allocation.submitted_at
        for allocation, evidence in evidence_by_allocation
        if evidence is Evidence.TOOK_A_JOB
    }
    for allocation, evidence in evidence_by_allocation:
        if evidence is None:
            continue
        size_label = allocation.size_label
        size_record = by_size.get(size_label)
        if evidence is Evidence.TOOK_A_JOB and size_record is not None:
            by_size[size_label] = record_size_took_a_job(
                size_record, allocation.submitted_at
            )
        if allocation.node is not None:
            key = (allocation.node, size_label)
            by_node_and_size[key] = record_node_evidence(
                by_node_and_size.get(key), allocation, evidence, seen_at
            )
        elif (
            evidence.failed_to_launch
            and allocation.submitted_at > newest_success_by_size.get(size_label, 0.0)
            and (
                size_record is None
                or allocation.runner_name not in size_record.refused_runner_names
            )
        ):
            by_size[size_label] = record_nodeless_failure(
                size_record, allocation.submitted_at, seen_at
            )
    return LaunchRecords(by_node_and_size, by_size)


# Rule 1: not sent to a node where its newest runner failed to launch; used there again once one takes a job


def record_node_evidence(
    record: NodeRecord | None,
    allocation: Allocation,
    evidence: Evidence,
    seen_at: float,
) -> NodeRecord:
    """Pure: the newest runner by submit time decides, a success winning a tie, and evidence from a runner
    no newer than the newest with evidence there changes nothing (the same failure seen on many cycles
    included). A success at or after the newest failure makes the node usable. A failure puts a usable node
    out; while the node is out, it restarts the wait from when it was seen, and doubles it only if its
    runner was submitted after the node went out (rule 2): runners already in flight then are one event.
    Runners submitted before the record was forgotten are not filed."""
    record = record or NodeRecord()
    submitted_at = allocation.submitted_at
    if record.forgotten_at is not None and submitted_at <= record.forgotten_at:
        return record
    if evidence is Evidence.TOOK_A_JOB:
        if submitted_at <= (record.newest_success_submitted_at or 0.0):
            return record
        record = replace(
            record,
            newest_success_submitted_at=submitted_at,
            newest_success_slurm_job_id=allocation.slurm_job_id,
        )
        if submitted_at >= (record.newest_failure_submitted_at or 0.0):
            return replace(record, out_since=None, failed_retries=0)
        return record
    newest = max(
        record.newest_success_submitted_at or 0.0,
        record.newest_failure_submitted_at or 0.0,
    )
    if submitted_at <= newest:
        return record
    record = replace(
        record,
        newest_failure_submitted_at=submitted_at,
        newest_failure_slurm_job_id=allocation.slurm_job_id,
        newest_failure_kind=evidence,
        newest_failure_seen_at=seen_at,
    )
    if record.out_since is None:
        return replace(record, out_since=seen_at, failed_retries=0)
    if submitted_at > record.out_since:
        return replace(record, failed_retries=record.failed_retries + 1)
    return record


def node_is_out(record: NodeRecord | None) -> bool:
    """Pure: the newest runner of the size with evidence on the node failed to launch."""
    return record is not None and record.out_since is not None


def node_records_of(records: LaunchRecords, size_label: str) -> dict[str, NodeRecord]:
    """Pure: the size's records, by node."""
    return {
        node: record
        for (node, record_size_label), record in records.by_node_and_size.items()
        if record_size_label == size_label
    }


def nodes_out(records: LaunchRecords, size_label: str) -> frozenset[str]:
    """Pure: the nodes out for the size."""
    return frozenset(
        node
        for node, record in node_records_of(records, size_label).items()
        if node_is_out(record)
    )


# Rule 2: such a node is retried with one extra pinned runner after 1, 2, 4, 8, at most 10 minutes. The wait
# doubles in record_node_evidence, where a failed retry is filed


def retry_wait_s(failed_retries: int) -> float:
    """Pure: FIRST_RETRY_WAIT_S doubled per failed retry, at most MAX_RETRY_WAIT_S (rules 2 and 5)."""
    return min(FIRST_RETRY_WAIT_S * 2**failed_retries, MAX_RETRY_WAIT_S)


def node_retry_due_at(record: NodeRecord) -> float:
    """Pure: when a node out for a size may get a runner of it pinned to it."""
    return (record.newest_failure_seen_at or 0.0) + retry_wait_s(record.failed_retries)


def nodes_due_a_pinned_runner(
    records: LaunchRecords,
    size_label: str,
    nodes_with_a_pinned_runner_in_flight: set[str],
    now: float,
) -> list[str]:
    """Pure: the nodes out for the size whose retry is due and with no runner of the size pinned to them in
    flight; none while the size is usable nowhere, whose one runner is the retry (rule 3)."""
    if not size_usable_somewhere(records, size_label):
        return []
    return sorted(
        node
        for node in nodes_out(records, size_label)
        if node not in nodes_with_a_pinned_runner_in_flight
        and now >= node_retry_due_at(records.by_node_and_size[(node, size_label)])
    )


# Rule 3: a size with no node where it is known to work gets one runner at a time


def size_usable_somewhere(records: LaunchRecords, size_label: str) -> bool:
    """Pure: some node is usable for the size, and nothing is outstanding (no refusal or failure with no
    node, rule 5)."""
    size_record = records.by_size.get(size_label)
    if size_record is not None and size_record.outstanding_since is not None:
        return False
    return any(
        not node_is_out(record) and record.newest_success_submitted_at is not None
        for record in node_records_of(records, size_label).values()
    )


def nodes_a_new_runner_excludes(
    records: LaunchRecords, size_label: str, now: float
) -> frozenset[str]:
    """Pure: usable somewhere, every node out for the size (rule 1). Usable nowhere, only those whose retry
    is not yet due, so that its one runner can be the retry of any other (rule 3)."""
    out = nodes_out(records, size_label)
    if size_usable_somewhere(records, size_label):
        return out
    return frozenset(
        node
        for node in out
        if now < node_retry_due_at(records.by_node_and_size[(node, size_label)])
    )


# Rule 4: a pending runner never keeps excluding a node a new runner of its size may use


def pending_runners_by_narrowed_exclusion(
    allocations: list[Allocation], records: LaunchRecords, now: float
) -> dict[frozenset[str], list[Allocation]]:
    """Pure: our unpinned runners pending in Slurm's queue whose exclusion names a node that a runner of
    their size submitted now would be allowed on, grouped by their new exclusion (the old one less those
    nodes), so that each group is one scontrol call. Never adds a node: a pending runner left out of nodes
    that came back would hold its job until they are full, and one that excluded every node would never
    start. A runner whose exclusion could not be read is left alone."""
    groups = {}
    for allocation in allocations:
        if (
            allocation.state != "PENDING"
            or allocation.is_held
            or allocation.pinned_node is not None
            or allocation.excluded_nodes is None
        ):
            continue
        kept = allocation.excluded_nodes & nodes_a_new_runner_excludes(
            records, allocation.size_label, now
        )
        if kept != allocation.excluded_nodes:
            groups.setdefault(kept, []).append(allocation)
    return groups


# Rule 5: a failure that belongs to no node makes the size's next runner wait 1, 2, 4, 8, at most 10 minutes


def record_nodeless_failure(
    record: SizeRecord | None, submitted_at: float, seen_at: float
) -> SizeRecord:
    """Pure: a runner of the size failed with no node (a refused one counts as submitted when refused).
    Outstanding from seen_at, and the next runner waits retry_wait_s(nodeless_retries) from then; the retries
    grow only if this runner was submitted after the previous outstanding failure with no node was seen."""
    record = record or SizeRecord()
    if submitted_at <= (record.newest_evidence_submitted_at or 0.0):
        return record
    retries = 0
    if record.nodeless_failure_seen_at is not None:
        retries = record.nodeless_retries
        if submitted_at > record.nodeless_failure_seen_at:
            retries += 1
    return replace(
        record,
        outstanding_since=seen_at,
        nodeless_failure_seen_at=seen_at,
        nodeless_retries=retries,
        newest_evidence_submitted_at=submitted_at,
    )


def record_size_took_a_job(record: SizeRecord, submitted_at: float) -> SizeRecord:
    """Pure: a runner of the size took a job, on any node. Submitted after outstanding_since, it clears
    what was outstanding."""
    newest = max(record.newest_evidence_submitted_at or 0.0, submitted_at)
    record = replace(record, newest_evidence_submitted_at=newest)
    if record.outstanding_since is None or submitted_at <= record.outstanding_since:
        return record
    return replace(
        record,
        outstanding_since=None,
        nodeless_failure_seen_at=None,
        nodeless_retries=0,
        refused_exclusion=frozenset(),
    )


def next_unpinned_runner_at(records: LaunchRecords, size_label: str) -> float:
    """Pure: when a size usable nowhere may send its one runner, given what is outstanding; 0 when nothing
    is. The later of rule 5's wait after a failure with no node, and, after a refused exclusion, the
    earliest retry due among its nodes, or no wait if one of them is no longer out (usable again, or
    forgotten)."""
    record = records.by_size.get(size_label)
    if record is None or record.outstanding_since is None:
        return 0.0
    times = [0.0]
    if record.nodeless_failure_seen_at is not None:
        times.append(
            record.nodeless_failure_seen_at + retry_wait_s(record.nodeless_retries)
        )
    excluded = [
        records.by_node_and_size.get((node, size_label))
        for node in record.refused_exclusion
    ]
    if excluded and all(node_is_out(node_record) for node_record in excluded):
        times.append(min(node_retry_due_at(node_record) for node_record in excluded))
    return max(times)


# A failed sbatch: what squeue shows by the runner's name decides (supply.AfterFailedSbatch)


def record_refused_submission(
    records: LaunchRecords, submission: Submission, runner_name: str, refused_at: float
) -> LaunchRecords:
    """Pure: Slurm refused a submission for its constraints, leaving a FAILED record with no node. It makes
    the size usable nowhere until a runner of it submitted after refused_at takes a job, and keeps the
    runner's name, so that record is not counted again. Then:
    - unpinned with nothing excluded: a failure with no node (rule 5), whose wait doubles;
    - unpinned with nodes excluded: nothing doubles; the size's next runner waits for the earliest of those
      nodes to come due (next_unpinned_runner_at), when its own exclusion will be smaller;
    - pinned: the node's record is forgotten, or a node that can no longer hold the size would be refused
      at every retry; not rule 5."""
    size_label = submission.size_label
    record = records.by_size.get(size_label, SizeRecord())
    record = replace(
        record,
        outstanding_since=refused_at,
        refused_runner_names={**record.refused_runner_names, runner_name: refused_at},
    )
    by_node_and_size = dict(records.by_node_and_size)
    if submission.pinned_node is not None:
        by_node_and_size[(submission.pinned_node, size_label)] = NodeRecord(
            forgotten_at=refused_at
        )
    elif submission.excluded_nodes:
        record = replace(record, refused_exclusion=submission.excluded_nodes)
    else:
        record = record_nodeless_failure(record, refused_at, refused_at)
    return LaunchRecords(by_node_and_size, {**records.by_size, size_label: record})


def forget_nodes_slurm_does_not_know(
    records: LaunchRecords, unknown_nodes: frozenset[str], forgotten_at: float
) -> LaunchRecords:
    """Pure: after a sbatch that left no job, named nodes Slurm does not know (removed or renamed): their
    records are forgotten for every size, or every submission naming them would fail the same way. Nothing
    is counted against any size."""
    by_node_and_size = dict(records.by_node_and_size)
    for node, size_label in records.by_node_and_size:
        if node in unknown_nodes:
            by_node_and_size[(node, size_label)] = NodeRecord(forgotten_at=forgotten_at)
    return replace(records, by_node_and_size=by_node_and_size)


# What to submit, from the five rules


def plan_submissions(
    allocations: list[Allocation],
    registrations_by_name: dict[str, RunnerRegistration],
    demand: Demand,
    records: LaunchRecords,
    runners_covering_a_job: dict[str, int],
    submitted_this_cycle: list[Submission],
    planned_at: float,
    cycle_started_at: float,
) -> list[Submission]:
    """Pure: per size label with a job waiting that no runner covers.
    - Usable somewhere: one pinned runner per node due a retry (rule 2), then the waiting jobs less the
      unpinned runners that can still take one (this cycle's submissions included), each excluding the
      nodes out for the size (rule 1).
    - Usable nowhere: one unpinned runner, only when no unpinned runner of the size can still take a job and
      next_unpinned_runner_at has come (rules 3 and 5); no pinned runner.
    Pinned runners never count as able to take a job, so one pending on a drained node never holds a size
    back. What squeue and the runners list showed is judged at cycle_started_at, when they were read;
    which nodes are due, at planned_at."""
    waiting_jobs_by_size_label = jobs_left_waiting_by_size_label(
        demand, runners_covering_a_job
    )
    uncovered = uncovered_jobs_waiting_past_b_by_size_label(
        demand, runners_covering_a_job, cycle_started_at
    )
    unpinned_in_flight = Counter(
        submission.size_label
        for submission in submitted_this_cycle
        if submission.pinned_node is None
    )
    pinned_in_flight = {
        (submission.pinned_node, submission.size_label)
        for submission in submitted_this_cycle
        if submission.pinned_node is not None
    }
    for allocation in allocations:
        if not runner_can_still_take_a_job(
            allocation,
            registrations_by_name.get(allocation.runner_name),
            demand,
            uncovered.get(allocation.size_label, 0),
            cycle_started_at,
        ):
            continue
        if allocation.pinned_node is None:
            unpinned_in_flight[allocation.size_label] += 1
        else:
            pinned_in_flight.add((allocation.pinned_node, allocation.size_label))

    submissions = []
    for size_label, waiting_jobs in sorted(waiting_jobs_by_size_label.items()):
        if waiting_jobs == 0:
            continue
        nodes_in_flight = {
            node
            for node, pinned_size_label in pinned_in_flight
            if pinned_size_label == size_label
        }
        for node in nodes_due_a_pinned_runner(
            records, size_label, nodes_in_flight, planned_at
        ):
            retry = records.by_node_and_size[(node, size_label)].failed_retries + 1
            submissions.append(Submission(size_label, frozenset(), node, retry))
        excluded = nodes_a_new_runner_excludes(records, size_label, planned_at)
        shortfall = waiting_jobs - unpinned_in_flight[size_label]
        size_record = records.by_size.get(size_label, SizeRecord())
        if size_usable_somewhere(records, size_label):
            submissions += [Submission(size_label, excluded)] * max(0, shortfall)
        elif (
            shortfall > 0
            and unpinned_in_flight[size_label] == 0
            and planned_at >= next_unpinned_runner_at(records, size_label)
            # A refusal, or a failure with no node, recorded this cycle ends its submissions for the cycle
            and (size_record.outstanding_since or 0.0) < cycle_started_at
        ):
            submissions.append(Submission(size_label, excluded))
    return submission_order(submissions)


def submission_order(submissions: list[Submission]) -> list[Submission]:
    """Pure: size labels taken in turn, each label's pinned runners first, so a large wave cannot hold up
    another label's single job."""
    by_size_label = {}
    for submission in sorted(submissions, key=lambda each: each.pinned_node is None):
        by_size_label.setdefault(submission.size_label, []).append(submission)
    ordered = []
    for round_index in range(max(map(len, by_size_label.values()), default=0)):
        for size_label in sorted(by_size_label):
            if round_index < len(by_size_label[size_label]):
                ordered.append(by_size_label[size_label][round_index])
    return ordered


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


def launch_record_log_lines(
    before: LaunchRecords, after: LaunchRecords, now: float
) -> list[tuple[int, str]]:
    """Pure: (logging level, line) per change. WARNING: a node out for a size, its retry failed, a size
    usable nowhere, a failure with no node. INFO: a node or a size usable again. A refusal, and what it made
    the daemon forget, is logged where it happens, with sbatch's message."""
    lines = []
    for (node, size_label), new in sorted(after.by_node_and_size.items()):
        old = before.by_node_and_size.get((node, size_label))
        if node_is_out(new) and not node_is_out(old):
            lines.append(
                (
                    logging.WARNING,
                    f"{size_label}: out on {node}: {failure_on_node(new, node)}; next pinned"
                    f" runner there in {node_retry_due_at(new) - now:.0f} s",
                )
            )
        elif node_is_out(new) and new.failed_retries != old.failed_retries:
            lines.append(
                (
                    logging.WARNING,
                    f"{size_label}: still out on {node}: {failure_on_node(new, node)};"
                    f" {ordinal(new.failed_retries)} failed retry; next pinned runner there in"
                    f" {node_retry_due_at(new) - now:.0f} s",
                )
            )
        elif node_is_out(old) and not node_is_out(new) and new.forgotten_at is None:
            lines.append(
                (
                    logging.INFO,
                    f"{size_label}: usable again on {node} (Slurm job"
                    f" {new.newest_success_slurm_job_id} took a job)",
                )
            )
    size_labels = {size_label for _, size_label in after.by_node_and_size}
    size_labels |= {size_label for _, size_label in before.by_node_and_size}
    for size_label in sorted(size_labels | set(before.by_size) | set(after.by_size)):
        new = after.by_size.get(size_label, SizeRecord())
        old = before.by_size.get(size_label, SizeRecord())
        refused = new.refused_runner_names.keys() - old.refused_runner_names.keys()
        if new.nodeless_failure_seen_at not in (None, old.nodeless_failure_seen_at):
            # A refusal is logged where it happens, with sbatch's message
            if not refused:
                wait_s = next_unpinned_runner_at(after, size_label) - now
                lines.append(
                    (
                        logging.WARNING,
                        f"{size_label}: a runner failed with no node; next runner in"
                        f" {wait_s:.0f} s",
                    )
                )
        was_usable = size_usable_somewhere(before, size_label)
        if was_usable and not size_usable_somewhere(after, size_label):
            lines.append(
                (
                    logging.WARNING,
                    f"{size_label}: one runner at a time: {why_usable_nowhere(after, size_label)}",
                )
            )
        elif not was_usable and size_usable_somewhere(after, size_label):
            lines.append(
                (
                    logging.INFO,
                    f"{size_label}: works on some node again; every waiting job gets a runner",
                )
            )
    return lines


def failure_on_node(record: NodeRecord, node: str) -> str:
    """Pure: the newest failure on the node, and where its runner's log is."""
    slurm_job_id = record.newest_failure_slurm_job_id
    how = {
        Evidence.STILL_OFFLINE_AT_B: f"was still offline {BOOT_ALLOWANCE_S} s after it started",
        Evidence.LEFT_IDLE_WHILE_A_JOB_WAITED: (
            "stayed idle while a job of its size waited, and GitHub did not give it the job"
        ),
    }.get(record.newest_failure_kind, "ended without taking a job")
    log_path = SLURM_OUTPUT_PATH.replace("%j", str(slurm_job_id))
    return f"Slurm job {slurm_job_id} {how} (its log: {log_path} on {node})"


def why_usable_nowhere(records: LaunchRecords, size_label: str) -> str:
    """Pure: what holds the size to one runner at a time."""
    size_record = records.by_size.get(size_label, SizeRecord())
    if size_record.outstanding_since is None:
        return "no node is known to work for it"
    if size_record.outstanding_since in size_record.refused_runner_names.values():
        return "Slurm refused a submission (see above)"
    return "a runner failed with no node"


def ordinal(number: int) -> str:
    """Pure: 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st."""
    if 10 <= number % 100 <= 20:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"
