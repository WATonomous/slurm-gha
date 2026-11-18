"""plan without Slurm or GitHub: every pure function by table, and the helpers the node rule's tests share."""

import unittest
from dataclasses import replace

from config import (
    BOOT_ALLOWANCE_S as B,
    BUSY_COVER_S as T,
    IDLE_ALLOWANCE_S as W,
    UNUSED_REGISTRATION_LIFETIME_S as A_DAY,
)
from node_rule import (
    NO_LAUNCH_RECORDS,
    LaunchRecords,
    NodeRecord,
    file_launch_evidence,
    nodes_out,
    plan_submissions,
    size_usable_somewhere,
)
from plan import (
    FINISHED_STATES,
    NO_DEMAND,
    NO_SIGHTINGS,
    Allocation,
    Demand,
    Evidence,
    Phase,
    RunnerRegistration,
    RunnerSightings,
    WaitingJob,
    allocations_to_cancel,
    jobs_left_waiting_by_size_label,
    launch_evidence,
    phase_of_allocation,
    registrations_to_delete,
    runners_covering_a_job_by_size_label,
    uncovered_jobs_waiting_past_b_by_size_label,
    update_sightings,
)

NOW = 1_800_000_000.0
MEDIUM = "slurm-runner-medium"
SMALL = "slurm-runner-small"


def allocation(
    slurm_job_id,
    state="RUNNING",
    age_s=10,
    size_label=MEDIUM,
    is_held=False,
    submitted_at=None,
    ended_at=None,
):
    """One of our allocations; age_s is how long ago it started (pending: not started)."""
    started_at = None if state == "PENDING" else NOW - age_s
    if submitted_at is None:
        submitted_at = NOW - age_s - 5
    return Allocation(
        slurm_job_id=slurm_job_id,
        runner_name=f"gha-test-{slurm_job_id}",
        size_label=size_label,
        state=state,
        is_held=is_held,
        submitted_at=submitted_at,
        started_at=started_at,
        ended_at=ended_at,
    )


def ended(slurm_job_id, state, ran_s=30, size_label=MEDIUM, submitted_at=None, now=NOW):
    """An allocation that started, ran ran_s and ended in state 10 s before now."""
    ended_at = now - 10
    if submitted_at is None:
        submitted_at = ended_at - ran_s - 5
    return Allocation(
        slurm_job_id=slurm_job_id,
        runner_name=f"gha-test-{slurm_job_id}",
        size_label=size_label,
        state=state,
        is_held=False,
        submitted_at=submitted_at,
        started_at=ended_at - ran_s,
        ended_at=ended_at,
    )


def registration(of, is_online=True, is_busy=False):
    return RunnerRegistration(of.slurm_job_id, of.runner_name, is_online, is_busy)


def as_seen(of, runner):
    """The allocation's registration as the runners list shows it: None, "offline", "online" or "busy"."""
    if runner is None:
        return None
    return registration(of, runner != "offline", runner == "busy")


def registrations(*registrations_list):
    return {each.runner_name: each for each in registrations_list}


def waiting(count, size_label=MEDIUM, waited_s=10):
    return [WaitingJob(index, size_label, NOW - waited_s) for index in range(count)]


def demand(waiting_jobs=(), runner_names_on_jobs=()):
    return Demand(list(waiting_jobs), set(runner_names_on_jobs))


def on(node, of, **changes):
    """The allocation as squeue shows it once Slurm placed it on node."""
    return replace(of, node=node, **changes)


def usable_on(node, *size_labels):
    """Records in which a runner of each size took a job on node an hour ago: usable somewhere, nothing out."""
    took_a_job = NodeRecord(
        newest_success_submitted_at=NOW - 3600, newest_success_slurm_job_id=1
    )
    return LaunchRecords(
        {(node, size_label): took_a_job for size_label in size_labels}, {}
    )


def filed(records, *evidence, demand_read=NO_DEMAND, now=NOW):
    """file_launch_evidence for (allocation, registration) pairs."""
    allocations = [of for of, _ in evidence]
    by_name = registrations(*(reg for _, reg in evidence if reg is not None))
    return file_launch_evidence(
        records, allocations, by_name, demand_read, {}, now, now
    )


def unpinned_counts(submissions):
    """Unpinned runners per size label, as the breaker's plan counted them."""
    counts = {}
    for submission in submissions:
        if submission.pinned_node is None:
            counts[submission.size_label] = counts.get(submission.size_label, 0) + 1
    return counts


def out_on(records, node, size_label, seen_at=NOW, failed_retries=0, slurm_job_id=99):
    """records with node out for the size since seen_at, the failure that put it out seen then."""
    record = NodeRecord(
        newest_failure_submitted_at=seen_at - 30,
        newest_failure_slurm_job_id=slurm_job_id,
        newest_failure_seen_at=seen_at,
        out_since=seen_at,
        failed_retries=failed_retries,
    )
    return replace(
        records,
        by_node_and_size={**records.by_node_and_size, (node, size_label): record},
    )


def pinned_to(node, of, **changes):
    """A runner submitted with --nodes=1 --nodelist=node."""
    return replace(of, pinned_node=node, **changes)


class PhaseOfAllocationTest(unittest.TestCase):
    def test_each_phase(self):
        rows = [
            (
                "pending, registered",
                allocation(1, "PENDING"),
                "offline",
                Phase.PENDING,
            ),
            (
                # A list read can miss a fresh registration: still on its way
                "pending, unregistered",
                allocation(1, "PENDING"),
                None,
                Phase.PENDING,
            ),
            (
                "pending, unregistered for a day",
                allocation(1, "PENDING", submitted_at=NOW - A_DAY),
                None,
                Phase.CANNOT_START,
            ),
            (
                "pending, held",
                allocation(1, "PENDING", is_held=True),
                "offline",
                Phase.CANNOT_START,
            ),
            (
                "running, offline, young",
                allocation(1, age_s=10),
                "offline",
                Phase.BOOTING,
            ),
            (
                "running, offline, just under B",
                allocation(1, age_s=B - 1),
                "offline",
                Phase.BOOTING,
            ),
            (
                "running, offline, at B",
                allocation(1, age_s=B),
                "offline",
                Phase.UNAVAILABLE,
            ),
            ("running, online, idle", allocation(1, age_s=10), "online", Phase.IDLE),
            (
                "running, online, just under W",
                allocation(1, age_s=W - 1),
                "online",
                Phase.IDLE,
            ),
            (
                "running, online, at W",
                allocation(1, age_s=W),
                "online",
                Phase.UNAVAILABLE,
            ),
            ("running, busy", allocation(1), "busy", Phase.UNAVAILABLE),
            ("running, unregistered", allocation(1), None, Phase.UNAVAILABLE),
            (
                "completing, online",
                allocation(1, "COMPLETING"),
                "online",
                Phase.UNAVAILABLE,
            ),
        ]
        for description, of, runner, phase in rows:
            with self.subTest(description):
                self.assertEqual(
                    phase_of_allocation(of, as_seen(of, runner), NOW), phase
                )

    def test_every_finished_state_is_ended_whatever_the_runners_list_says(self):
        for state in sorted(FINISHED_STATES):
            with self.subTest(state):
                of = ended(1, state)
                self.assertEqual(phase_of_allocation(of, None, NOW), Phase.ENDED)
                busy = registration(of, True, True)
                self.assertEqual(phase_of_allocation(of, busy, NOW), Phase.ENDED)

    def test_running_without_a_start_time_is_not_counted_on(self):
        of = Allocation(1, "gha-test-1", MEDIUM, "RUNNING", False, NOW, None, None)
        self.assertEqual(
            phase_of_allocation(of, registration(of), NOW), Phase.UNAVAILABLE
        )


class AllocationsToCancelTest(unittest.TestCase):
    def test_only_pending_jobs_that_cannot_start(self):
        unregistered = allocation(1, "PENDING", submitted_at=NOW - A_DAY)
        held = allocation(2, "PENDING", is_held=True)
        just_submitted = allocation(7, "PENDING")  # its registration not listed yet
        pending_registered = allocation(3, "PENDING")
        running_unregistered = allocation(4)
        running_busy = allocation(5)
        finished = ended(6, "FAILED")
        allocations = [
            unregistered,
            held,
            just_submitted,
            pending_registered,
            running_unregistered,
            running_busy,
            finished,
        ]
        by_name = registrations(
            registration(held, False),
            registration(pending_registered, False),
            registration(running_busy, True, True),
        )
        to_cancel = allocations_to_cancel(allocations, by_name, NOW)
        self.assertEqual(to_cancel, [unregistered, held])
        self.assertTrue(all(each.state == "PENDING" for each in to_cancel))


class LaunchEvidenceTest(unittest.TestCase):
    def evidence(self, of, reg, names_on_jobs=(), uncovered_past_b=0):
        return launch_evidence(of, reg, set(names_on_jobs), uncovered_past_b, NOW)

    def test_taking_a_job(self):
        idle = allocation(1)
        self.assertEqual(
            self.evidence(idle, registration(idle), [idle.runner_name]),
            Evidence.TOOK_A_JOB,
        )
        self.assertEqual(
            self.evidence(idle, registration(idle, True, True)), Evidence.TOOK_A_JOB
        )
        # An ended runner is still named on its job; a failed end after a job is still a job taken
        for state in ("COMPLETED", "FAILED"):
            done = ended(2, state)
            self.assertEqual(
                self.evidence(done, None, [done.runner_name]), Evidence.TOOK_A_JOB
            )

    def test_ending_in_a_launch_failure_state_after_running(self):
        for state in ("FAILED", "CANCELLED", "NODE_FAIL", "BOOT_FAIL"):
            with self.subTest(state):
                ran = on("trpro-slurm1", ended(1, state))
                self.assertEqual(self.evidence(ran, None), Evidence.ENDED_WITHOUT_A_JOB)

    def test_a_launch_slurmd_gave_up_on_is_a_failure_on_its_node(self):
        # Under --no-requeue slurmd kills a job it could not start: CANCELLED, its node kept, and it can
        # end within the second it started
        gave_up = on("trpro-slurm1", ended(1, "CANCELLED", ran_s=0))
        self.assertEqual(gave_up.started_at, gave_up.ended_at)
        self.assertEqual(self.evidence(gave_up, None), Evidence.ENDED_WITHOUT_A_JOB)
        records = filed(NO_LAUNCH_RECORDS, (gave_up, None))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm1"})

    def test_a_runner_cancelled_after_taking_a_job_took_a_job(self):
        cancelled = on("trpro-slurm1", ended(1, "CANCELLED"))
        self.assertEqual(
            self.evidence(cancelled, None, [cancelled.runner_name]), Evidence.TOOK_A_JOB
        )

    def test_a_failure_within_its_first_second_still_counts(self):
        # Slurm times are whole seconds: a batch script that dies at once starts and ends in one
        for state in ("FAILED", "NODE_FAIL", "BOOT_FAIL"):
            with self.subTest(state):
                at_once = ended(1, state, ran_s=0)
                self.assertEqual(at_once.started_at, at_once.ended_at)
                self.assertEqual(
                    self.evidence(at_once, None), Evidence.ENDED_WITHOUT_A_JOB
                )
        never_started = Allocation(
            2, "gha-test-2", MEDIUM, "FAILED", False, NOW - 60, None, NOW - 10
        )
        self.assertEqual(
            self.evidence(never_started, None), Evidence.ENDED_WITHOUT_A_JOB
        )

    def test_ends_that_say_nothing(self):
        cancelled_while_pending = Allocation(
            1, "gha-test-1", MEDIUM, "CANCELLED", False, NOW - 60, None, NOW - 10
        )
        stamped_start_at_end = Allocation(
            2, "gha-test-2", MEDIUM, "CANCELLED", False, NOW - 60, NOW - 10, NOW - 10
        )
        rows = [
            # Cancelled while pending: never a node, whatever squeue prints for its start
            ("cancelled while pending", cancelled_while_pending),
            ("start stamped with its end", stamped_start_at_end),
            ("stopped itself at its idle deadline", ended(3, "COMPLETED", ran_s=W)),
            ("hit the Slurm time limit", ended(4, "TIMEOUT")),
            ("out of memory", ended(5, "OUT_OF_MEMORY")),
        ]
        for description, of in rows:
            with self.subTest(description):
                self.assertIsNone(self.evidence(of, None))

    def test_a_runner_offline_between_b_and_w(self):
        rows = [
            (B - 1, None),
            (B, Evidence.STILL_OFFLINE_AT_B),
            (W - 1, Evidence.STILL_OFFLINE_AT_B),
            (W, None),
        ]
        for age_s, evidence in rows:
            with self.subTest(age_s=age_s):
                of = allocation(1, age_s=age_s)
                self.assertEqual(self.evidence(of, registration(of, False)), evidence)

    def test_a_runner_github_leaves_idle_while_a_job_of_its_size_waits(self):
        rows = [
            (2 * B + 1, 1, Evidence.LEFT_IDLE_WHILE_A_JOB_WAITED),
            (2 * B, 1, None),  # not yet idle for long enough
            # No job of its size has waited over B, or a runner covers each: surplus
            (2 * B + 1, 0, None),
            (W, 1, None),  # its batch script has stopped it
        ]
        for age_s, uncovered_past_b, evidence in rows:
            with self.subTest(age_s=age_s, uncovered_past_b=uncovered_past_b):
                of = allocation(1, age_s=age_s)
                self.assertEqual(
                    self.evidence(of, registration(of), (), uncovered_past_b), evidence
                )

    def test_jobs_waiting_past_b_less_runners_taking_a_job(self):
        jobs = demand(
            waiting(1, waited_s=B)
            + waiting(2, waited_s=B + 1)
            + waiting(1, SMALL, waited_s=B + 1)
        )
        self.assertEqual(
            uncovered_jobs_waiting_past_b_by_size_label(jobs, {}, NOW),
            {MEDIUM: 2, SMALL: 1},
        )
        self.assertEqual(
            uncovered_jobs_waiting_past_b_by_size_label(jobs, {MEDIUM: 3}, NOW),
            {MEDIUM: 0, SMALL: 1},
        )

    def test_pending_booting_and_unregistered_runners_say_nothing(self):
        pending = allocation(1, "PENDING")
        booting = allocation(2, age_s=10)
        unregistered = allocation(3, age_s=B + 1)
        self.assertIsNone(self.evidence(pending, registration(pending, False)))
        self.assertIsNone(self.evidence(booting, registration(booting, False)))
        self.assertIsNone(self.evidence(unregistered, None))


class BusyRunnerCoversItsJobTest(unittest.TestCase):
    """GitHub marks a runner busy at the offer, before the job stops saying queued and names it."""

    records = usable_on("thor-slurm1", MEDIUM, SMALL)

    def covering(self, slurm_job_id=1, state="RUNNING", size_label=MEDIUM):
        """One of our runners, busy with a job that may still say queued."""
        of = allocation(slurm_job_id, state, size_label=size_label)
        return of, registration(of, is_busy=True)

    def plan(self, allocations, by_name, jobs, sightings, now=NOW):
        covering = runners_covering_a_job_by_size_label(
            allocations, by_name, sightings, jobs, now
        )
        return unpinned_counts(
            plan_submissions(
                allocations, by_name, jobs, self.records, covering, [], now, now
            )
        )

    def test_inside_the_gap_nothing_extra_is_submitted(self):
        of, reg = self.covering()
        by_name = registrations(reg)
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW - 10)
        # Its own job, still queued and naming no runner
        jobs = demand(waiting(1))
        self.assertEqual(self.plan([of], by_name, jobs, sightings), {})
        self.assertEqual(
            self.plan([of], by_name, demand(waiting(3)), sightings), {MEDIUM: 2}
        )

    def test_past_t_it_no_longer_covers(self):
        of, reg = self.covering()
        by_name = registrations(reg)
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW)
        jobs = demand(waiting(1))
        self.assertEqual(self.plan([of], by_name, jobs, sightings, NOW + T - 1), {})
        self.assertEqual(
            self.plan([of], by_name, jobs, sightings, NOW + T), {MEDIUM: 1}
        )

    def test_a_runner_once_named_on_a_job_never_covers_again(self):
        of, reg = self.covering()
        by_name = registrations(reg)
        other_runs_job = demand(waiting(1))
        named_now = demand(waiting(1), [of.runner_name])
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW)
        # Named by a job read so far this cycle
        self.assertEqual(self.plan([of], by_name, named_now, sightings), {MEDIUM: 1})
        # Named in an earlier cycle; this cycle the read of its run failed
        sightings = update_sightings(sightings, [of], by_name, named_now, NOW)
        sightings = update_sightings(sightings, [of], by_name, NO_DEMAND, NOW + 2)
        self.assertEqual(
            self.plan([of], by_name, other_runs_job, sightings, NOW + 2), {MEDIUM: 1}
        )

    def test_a_busy_runner_whose_slurm_job_ended_does_not_cover(self):
        for state in ("COMPLETING", "COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(state):
                of, reg = self.covering(state=state)
                by_name = registrations(reg)
                sightings = update_sightings(
                    NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW
                )
                self.assertEqual(
                    self.plan([of], by_name, demand(waiting(1)), sightings), {MEDIUM: 1}
                )

    def test_after_a_restart_it_covers_from_when_it_is_first_seen(self):
        of, reg = self.covering()
        by_name = registrations(reg)
        jobs = demand(waiting(1))
        self.assertEqual(self.plan([of], by_name, jobs, NO_SIGHTINGS), {MEDIUM: 1})
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW)
        self.assertEqual(self.plan([of], by_name, jobs, sightings), {})

    def test_it_covers_only_jobs_of_its_size_and_never_below_zero(self):
        three = [self.covering(i)[0] for i in (1, 2, 3)]
        by_name = registrations(*(registration(each, is_busy=True) for each in three))
        sightings = update_sightings(NO_SIGHTINGS, three, by_name, NO_DEMAND, NOW)
        jobs = demand(waiting(1) + waiting(2, SMALL))
        self.assertEqual(self.plan(three, by_name, jobs, sightings), {SMALL: 2})
        self.assertEqual(
            jobs_left_waiting_by_size_label(jobs, {MEDIUM: 3}), {MEDIUM: 0, SMALL: 2}
        )

    def test_an_idle_runner_beside_a_covered_job_is_not_a_failed_launch(self):
        # R took the job K is waiting for; I1 and I2, submitted after R, are online and idle
        r = on("thor-slurm1", allocation(1, age_s=3 * B))
        idle = [on("thor-slurm1", allocation(i, age_s=2 * B + 10)) for i in (2, 3)]
        by_name = registrations(
            registration(r, is_busy=True), *(registration(each) for each in idle)
        )
        allocations = [r, *idle]
        sightings = update_sightings(
            NO_SIGHTINGS, allocations, by_name, NO_DEMAND, NOW - 10
        )
        k = demand(waiting(1, waited_s=B + 5))
        covering = runners_covering_a_job_by_size_label(
            allocations, by_name, sightings, k, NOW
        )
        self.assertEqual(covering, {MEDIUM: 1})
        records = file_launch_evidence(
            NO_LAUNCH_RECORDS, allocations, by_name, k, covering, NOW, NOW
        )
        self.assertEqual(nodes_out(records, MEDIUM), set())
        self.assertTrue(size_usable_somewhere(records, MEDIUM))
        # A second job past B is not covered: then GitHub is passing over the idle runners
        k_and_another = demand(waiting(2, waited_s=B + 5))
        records = file_launch_evidence(
            NO_LAUNCH_RECORDS, allocations, by_name, k_and_another, covering, NOW, NOW
        )
        self.assertEqual(nodes_out(records, MEDIUM), {"thor-slurm1"})

    def test_sightings_keep_only_runners_squeue_still_shows(self):
        of, reg = self.covering()
        gone, gone_reg = self.covering(2)
        by_name = registrations(reg, gone_reg)
        jobs = demand(
            runner_names_on_jobs=[of.runner_name, gone.runner_name, "slurm-thor-1"]
        )
        sightings = update_sightings(NO_SIGHTINGS, [of, gone], by_name, jobs, NOW)
        sightings = RunnerSightings(
            sightings.first_seen_busy_at,
            sightings.seen_on_a_job,
            frozenset({of.runner_name, gone.runner_name}),
        )
        self.assertEqual(
            update_sightings(sightings, [of], by_name, NO_DEMAND, NOW + 5),
            RunnerSightings(
                {of.runner_name: NOW},
                frozenset({of.runner_name}),
                frozenset({of.runner_name}),
            ),
        )


class RegistrationsToDeleteTest(unittest.TestCase):
    """GitHub keeps offering a stopped runner's session jobs until its registration is deleted."""

    def test_an_ended_runners_idle_registration(self):
        for state in sorted(FINISHED_STATES):
            with self.subTest(state):
                of = ended(1, state)
                for reg in (registration(of, False), registration(of, True)):
                    self.assertEqual(
                        registrations_to_delete([of], registrations(reg), NO_SIGHTINGS),
                        [reg],
                    )

    def test_never_one_that_is_busy_running_pending_or_not_in_squeue(self):
        busy = ended(1, "COMPLETED")
        unfinished = [
            allocation(2, "PENDING"),
            allocation(3, "RUNNING"),
            allocation(4, "COMPLETING"),
        ]
        # Its sbatch has not happened yet, or another daemon's
        not_in_squeue = RunnerRegistration(9, "gha-test-9", False, False)
        by_name = registrations(
            registration(busy, False, True),
            *(registration(each, False) for each in unfinished),
            not_in_squeue,
        )
        self.assertEqual(
            registrations_to_delete([busy, *unfinished], by_name, NO_SIGHTINGS), []
        )

    def test_not_again_once_github_answered(self):
        of = ended(1, "COMPLETED")
        answered = RunnerSightings({}, frozenset(), frozenset({of.runner_name}))
        self.assertEqual(
            registrations_to_delete([of], registrations(registration(of)), answered), []
        )
        self.assertEqual(registrations_to_delete([of], {}, NO_SIGHTINGS), [])

    def test_a_deleted_registration_changes_nothing_else_about_an_ended_runner(self):
        # Evidence reads Slurm's end state, the phase is ENDED, and an ended runner covers nothing
        for state in sorted(FINISHED_STATES):
            with self.subTest(state):
                of = ended(1, state)
                reg = registration(of, False)
                listed, deleted = registrations(reg), {}
                self.assertEqual(
                    launch_evidence(of, reg, set(), 1, NOW),
                    launch_evidence(of, None, set(), 1, NOW),
                )
                self.assertEqual(
                    file_launch_evidence(
                        NO_LAUNCH_RECORDS, [of], listed, NO_DEMAND, {}, NOW, NOW
                    ),
                    file_launch_evidence(
                        NO_LAUNCH_RECORDS, [of], deleted, NO_DEMAND, {}, NOW, NOW
                    ),
                )
                self.assertIs(phase_of_allocation(of, reg, NOW), Phase.ENDED)
                self.assertIs(phase_of_allocation(of, None, NOW), Phase.ENDED)
                jobs = demand(waiting(1))
                self.assertEqual(
                    plan_submissions(
                        [of], listed, jobs, NO_LAUNCH_RECORDS, {}, [], NOW, NOW
                    ),
                    plan_submissions(
                        [of], deleted, jobs, NO_LAUNCH_RECORDS, {}, [], NOW, NOW
                    ),
                )


if __name__ == "__main__":
    unittest.main()
