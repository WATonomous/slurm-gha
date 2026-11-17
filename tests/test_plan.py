"""plan without Slurm or GitHub: every pure function by table, and each rule of the node rule."""

import unittest
from dataclasses import replace

from config import (
    BOOT_ALLOWANCE_S as B,
    BUSY_COVER_S as T,
    FIRST_RETRY_WAIT_S,
    IDLE_ALLOWANCE_S as W,
    MAX_RETRY_WAIT_S,
    REFUSED_NAMES_KEPT_S,
    UNUSED_REGISTRATION_LIFETIME_S as A_DAY,
)
from plan import (
    FINISHED_STATES,
    NO_DEMAND,
    NO_LAUNCH_RECORDS,
    NO_SIGHTINGS,
    Allocation,
    Demand,
    Evidence,
    LaunchRecords,
    NodeRecord,
    Phase,
    RunnerRegistration,
    RunnerSightings,
    Submission,
    WaitingJob,
    allocations_to_cancel,
    file_launch_evidence,
    forget_nodes_slurm_does_not_know,
    jobs_left_waiting_by_size_label,
    launch_evidence,
    launch_record_log_lines,
    pending_runners_by_narrowed_exclusion,
    next_unpinned_runner_at,
    node_retry_due_at,
    nodes_a_new_runner_excludes,
    nodes_out,
    phase_of_allocation,
    plan_submissions,
    record_refused_submission,
    registrations_to_delete,
    retry_wait_s,
    runners_covering_a_job_by_size_label,
    size_usable_somewhere,
    submission_order,
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
                self.assertEqual(
                    self.evidence(ended(1, state), None), Evidence.FAILED_TO_LAUNCH
                )

    def test_a_failure_within_its_first_second_still_counts(self):
        # Slurm times are whole seconds: a batch script that dies at once starts and ends in one
        for state in ("FAILED", "NODE_FAIL", "BOOT_FAIL"):
            with self.subTest(state):
                at_once = ended(1, state, ran_s=0)
                self.assertEqual(at_once.started_at, at_once.ended_at)
                self.assertEqual(
                    self.evidence(at_once, None), Evidence.FAILED_TO_LAUNCH
                )
        never_started = Allocation(
            2, "gha-test-2", MEDIUM, "FAILED", False, NOW - 60, None, NOW - 10
        )
        self.assertEqual(self.evidence(never_started, None), Evidence.FAILED_TO_LAUNCH)

    def test_ends_that_say_nothing(self):
        cancelled_while_pending = Allocation(
            1, "gha-test-1", MEDIUM, "CANCELLED", False, NOW - 60, None, NOW - 10
        )
        stamped_start_at_end = Allocation(
            2, "gha-test-2", MEDIUM, "CANCELLED", False, NOW - 60, NOW - 10, NOW - 10
        )
        rows = [
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
            (B, Evidence.FAILED_TO_LAUNCH),
            (W - 1, Evidence.FAILED_TO_LAUNCH),
            (W, None),
        ]
        for age_s, evidence in rows:
            with self.subTest(age_s=age_s):
                of = allocation(1, age_s=age_s)
                self.assertEqual(self.evidence(of, registration(of, False)), evidence)

    def test_a_runner_github_leaves_idle_while_a_job_of_its_size_waits(self):
        rows = [
            (2 * B + 1, 1, Evidence.FAILED_TO_LAUNCH),
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


class PlanSubmissionsTest(unittest.TestCase):
    """The count of unpinned runners, as v8 had it, for a size usable somewhere."""

    def test_waiting_jobs_minus_runners_that_can_take_one(self):
        pending = allocation(1, "PENDING")
        booting = allocation(2, age_s=10)
        idle = allocation(3, age_s=10)
        named_on_a_job = allocation(4, age_s=10)  # the runners list has not caught up
        busy = allocation(5, age_s=10)
        hung = allocation(6, age_s=B + 1)
        held = allocation(7, "PENDING", is_held=True)
        small_idle = allocation(8, size_label=SMALL)
        allocations = [
            pending,
            booting,
            idle,
            named_on_a_job,
            busy,
            hung,
            held,
            small_idle,
        ]
        by_name = registrations(
            registration(pending, False),
            registration(booting, False),
            registration(idle),
            registration(named_on_a_job),
            registration(busy, True, True),
            registration(hung, False),
            registration(held, False),
            registration(small_idle),
        )
        records = usable_on("thor-slurm1", MEDIUM, SMALL)
        jobs = demand(waiting(10) + waiting(1, SMALL), [named_on_a_job.runner_name])
        submissions = plan_submissions(
            allocations, by_name, jobs, records, {}, [], NOW, NOW
        )
        self.assertEqual(unpinned_counts(submissions), {MEDIUM: 10 - 3})
        self.assertEqual(
            set(submissions), {Submission(MEDIUM, frozenset())}, "no node is out"
        )

    def test_a_pending_runner_missing_from_the_list_still_counts(self):
        just_submitted = allocation(1, "PENDING")
        records = usable_on("thor-slurm1", MEDIUM)
        jobs = demand(waiting(3))
        self.assertEqual(
            unpinned_counts(
                plan_submissions([just_submitted], {}, jobs, records, {}, [], NOW, NOW)
            ),
            {MEDIUM: 2},
        )

    def test_a_runner_github_leaves_idle_while_a_job_waits_stops_counting(self):
        passed_over = allocation(1, age_s=2 * B + 1)
        by_name = registrations(registration(passed_over))
        records = usable_on("thor-slurm1", MEDIUM)
        waited_over_b = demand(waiting(1, waited_s=B + 1))
        self.assertEqual(
            unpinned_counts(
                plan_submissions(
                    [passed_over], by_name, waited_over_b, records, {}, [], NOW, NOW
                )
            ),
            {MEDIUM: 1},
        )
        just_waiting = demand(waiting(1, waited_s=B))
        self.assertEqual(
            plan_submissions(
                [passed_over], by_name, just_waiting, records, {}, [], NOW, NOW
            ),
            [],
        )

    def test_runners_submitted_earlier_this_cycle_count_as_able_to_take_a_job(self):
        jobs = demand(waiting(10))
        records = usable_on("thor-slurm1", MEDIUM)
        earlier = [Submission(MEDIUM, frozenset())] * 4
        self.assertEqual(
            unpinned_counts(
                plan_submissions([], {}, jobs, records, {}, earlier, NOW, NOW)
            ),
            {MEDIUM: 6},
        )

    def test_no_waiting_jobs_no_runners(self):
        records = usable_on("thor-slurm1", MEDIUM)
        self.assertEqual(
            plan_submissions([], {}, demand(), records, {}, [], NOW, NOW), []
        )


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


class SeenAtTest(unittest.TestCase):
    """What squeue and the runners list showed is judged at the time they were read, not at the later time a
    run is planned: a runner offline at the read and younger than B is booting, however long the cycle."""

    def test_a_runner_booting_when_the_list_was_read_is_still_booting_later_in_the_cycle(
        self,
    ):
        booting = on("thor-slurm1", allocation(1, age_s=B - 20))
        offline = registration(booting, False)
        records = usable_on("thor-slurm1", MEDIUM)
        later = NOW + 40  # it would be B + 20 old now
        filed_later = file_launch_evidence(
            records,
            [booting],
            registrations(offline),
            demand(waiting(1)),
            {},
            cycle_started_at=NOW,
            seen_at=later,
        )
        self.assertEqual(filed_later, records)
        self.assertEqual(
            plan_submissions(
                [booting],
                registrations(offline),
                demand(waiting(1)),
                records,
                {},
                [],
                planned_at=later,
                cycle_started_at=NOW,
            ),
            [],
        )
        # Judged at the later time it would be a failed launch, and the size usable nowhere
        judged_later = file_launch_evidence(
            records,
            [booting],
            registrations(offline),
            demand(waiting(1)),
            {},
            later,
            later,
        )
        self.assertEqual(nodes_out(judged_later, MEDIUM), {"thor-slurm1"})

    def test_a_job_created_after_the_list_was_read_shows_no_runner_was_passed_over(
        self,
    ):
        idle = on("thor-slurm1", allocation(1, age_s=2 * B + 10))
        records = usable_on("thor-slurm1", MEDIUM)
        # Waited past B by the time its run is read, but it did not exist when the runner was seen idle
        newer = demand([WaitingJob(1, MEDIUM, NOW + 5)])
        filed_later = file_launch_evidence(
            records,
            [idle],
            registrations(registration(idle)),
            newer,
            {},
            cycle_started_at=NOW,
            seen_at=NOW + B + 10,
        )
        self.assertEqual(filed_later, records)


class RuleOneNewestRunnerDecidesTest(unittest.TestCase):
    """1. A size is not sent to a node where its newest runner failed to launch. It is used there again as
    soon as a runner of that size takes a job there."""

    def test_a_failure_puts_the_node_out_and_a_newer_success_brings_it_back(self):
        first = on("trpro-slurm1", ended(1, "FAILED", submitted_at=NOW - 100))
        records = filed(NO_LAUNCH_RECORDS, (first, None))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm1"})
        newer = on("trpro-slurm1", allocation(2, submitted_at=NOW - 50))
        busy = registration(newer, is_busy=True)
        records = filed(records, (first, None), (newer, busy), now=NOW + 5)
        self.assertEqual(nodes_out(records, MEDIUM), set())
        self.assertTrue(size_usable_somewhere(records, MEDIUM))

    def test_a_runner_counted_as_failed_that_later_takes_a_job_clears_its_own_node(
        self,
    ):
        slow = on("trpro-slurm2", allocation(1, age_s=B + 5))
        records = filed(NO_LAUNCH_RECORDS, (slow, registration(slow, False)))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm2"})
        records = filed(records, (slow, registration(slow, is_busy=True)), now=NOW + 60)
        self.assertEqual(nodes_out(records, MEDIUM), set())

    def test_evidence_from_an_older_runner_changes_nothing(self):
        older = on("trpro-slurm1", allocation(1, submitted_at=NOW - 200))
        newer = on("trpro-slurm1", ended(2, "FAILED", submitted_at=NOW - 100))
        records = filed(NO_LAUNCH_RECORDS, (newer, None))
        # Took a job, but submitted before the newest failure there
        records = filed(records, (older, registration(older, is_busy=True)))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm1"})
        # And a failure older than the newest success leaves a usable node usable
        usable = filed(NO_LAUNCH_RECORDS, (newer, None), (older, None))
        later = on("trpro-slurm1", allocation(3, submitted_at=NOW - 10))
        usable = filed(usable, (later, registration(later, is_busy=True)))
        self.assertEqual(filed(usable, (newer, None)), usable)

    def test_a_failure_older_than_the_newest_success_seen_late_changes_nothing(self):
        first = on("trpro-slurm1", ended(1, "FAILED", submitted_at=NOW - 300))
        records = filed(NO_LAUNCH_RECORDS, (first, None), now=NOW - 280)
        third = on("trpro-slurm1", allocation(3, submitted_at=NOW - 100))
        records = filed(
            records, (third, registration(third, is_busy=True)), now=NOW - 90
        )
        # Submitted between the two, its hang at B is seen only now
        second = on("trpro-slurm1", allocation(2, submitted_at=NOW - 200, age_s=B + 5))
        late = filed(records, (second, registration(second, False)))
        self.assertEqual(late, records)
        self.assertEqual(nodes_out(late, MEDIUM), set())

    def test_the_same_failure_seen_on_many_cycles_is_one_event(self):
        failed = on("trpro-slurm1", ended(1, "FAILED"))
        once = filed(NO_LAUNCH_RECORDS, (failed, None))
        self.assertEqual(filed(once, (failed, None), now=NOW + 200), once)

    def test_sizes_are_kept_apart(self):
        failed = on("trpro-slurm1", ended(1, "FAILED"))
        records = filed(usable_on("trpro-slurm1", MEDIUM, SMALL), (failed, None))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm1"})
        self.assertEqual(nodes_out(records, SMALL), set())

    def test_unpinned_runners_exclude_every_node_out_for_the_size(self):
        records = usable_on("thor-slurm1", MEDIUM, SMALL)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 10)
        # Its wait has passed: still excluded, its retry is a pinned runner
        records = out_on(records, "trpro-slurm2", MEDIUM, seen_at=NOW - 600)
        jobs = demand(waiting(2) + waiting(1, SMALL))
        submissions = plan_submissions([], {}, jobs, records, {}, [], NOW, NOW)
        trpro = frozenset({"trpro-slurm1", "trpro-slurm2"})
        self.assertEqual(
            submissions,
            [
                # trpro-slurm2's retry takes medium's first turn
                Submission(MEDIUM, frozenset(), "trpro-slurm2"),
                Submission(SMALL, frozenset()),
                Submission(MEDIUM, trpro),
                Submission(MEDIUM, trpro),
            ],
        )


class RuleTwoPinnedRetryTest(unittest.TestCase):
    """2. Such a node is tried again with one extra runner pinned to it, 1 minute after the failure, then 2,
    4, 8 and at most 10 minutes after each further failure there, while a job of that size waits."""

    def test_the_wait_doubles_per_failed_retry_up_to_ten_minutes(self):
        self.assertEqual(
            [retry_wait_s(retries) for retries in range(6)],
            [60, 120, 240, 480, 600, 600],
        )
        self.assertEqual((FIRST_RETRY_WAIT_S, MAX_RETRY_WAIT_S), (60, 600))

    def test_runners_in_flight_when_it_went_out_restart_the_wait_but_never_double(self):
        first = on("trpro-slurm1", ended(1, "FAILED", submitted_at=NOW - 100))
        in_flight = on("trpro-slurm1", ended(2, "FAILED", submitted_at=NOW - 90))
        records = filed(NO_LAUNCH_RECORDS, (first, None))
        records = filed(records, (first, None), (in_flight, None), now=NOW + 30)
        record = records.by_node_and_size[("trpro-slurm1", MEDIUM)]
        self.assertEqual((record.out_since, record.failed_retries), (NOW, 0))
        self.assertEqual(node_retry_due_at(record), NOW + 30 + 60)

    def test_a_runner_submitted_before_the_failure_was_seen_is_in_flight_not_a_retry(
        self,
    ):
        # The cycle started at NOW; a runner went out at NOW + 5, and the failure was filed at NOW + 20
        first = on("trpro-slurm1", ended(1, "FAILED", submitted_at=NOW - 100))
        records = filed(NO_LAUNCH_RECORDS, (first, None), now=NOW + 20)
        same_cycle = on(
            "trpro-slurm1", ended(2, "FAILED", submitted_at=NOW + 5, now=NOW + 50)
        )
        records = filed(records, (same_cycle, None), now=NOW + 50)
        self.assertEqual(
            records.by_node_and_size[("trpro-slurm1", MEDIUM)].failed_retries, 0
        )

    def test_a_failed_retry_doubles_the_wait_and_a_success_resets_it(self):
        first = on("trpro-slurm1", ended(1, "FAILED", submitted_at=NOW - 100))
        records = filed(NO_LAUNCH_RECORDS, (first, None))
        retry = on(
            "trpro-slurm1", ended(2, "FAILED", submitted_at=NOW + 60, now=NOW + 90)
        )
        records = filed(records, (retry, None), now=NOW + 90)
        record = records.by_node_and_size[("trpro-slurm1", MEDIUM)]
        self.assertEqual(record.failed_retries, 1)
        self.assertEqual(node_retry_due_at(record), NOW + 90 + 120)
        took = on("trpro-slurm1", allocation(3, submitted_at=NOW + 210))
        records = filed(
            records, (took, registration(took, is_busy=True)), now=NOW + 230
        )
        record = records.by_node_and_size[("trpro-slurm1", MEDIUM)]
        self.assertEqual((record.out_since, record.failed_retries), (None, 0))

    def test_one_pinned_runner_per_node_due_while_a_job_waits(self):
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 60)
        records = out_on(records, "trpro-slurm2", MEDIUM, seen_at=NOW - 59)
        submissions = plan_submissions(
            [], {}, demand(waiting(2)), records, {}, [], NOW, NOW
        )
        excluded = frozenset({"trpro-slurm1", "trpro-slurm2"})
        # Pinned first, and extra: the two waiting jobs still get two unpinned runners
        self.assertEqual(
            submissions,
            [
                Submission(MEDIUM, frozenset(), "trpro-slurm1"),
                Submission(MEDIUM, excluded),
                Submission(MEDIUM, excluded),
            ],
        )
        self.assertEqual(
            plan_submissions([], {}, demand(), records, {}, [], NOW, NOW), []
        )

    def test_no_second_pinned_runner_while_one_can_still_take_a_job(self):
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 600)
        excluded = frozenset({"trpro-slurm1"})
        jobs = demand(waiting(1))
        # Pending on that node (it may be drained): in flight, and never supply
        pending_pin = pinned_to("trpro-slurm1", allocation(1, "PENDING"))
        by_name = registrations(registration(pending_pin, False))
        self.assertEqual(
            plan_submissions([pending_pin], by_name, jobs, records, {}, [], NOW, NOW),
            [Submission(MEDIUM, excluded)],
        )
        # Sent earlier this cycle
        sent = [Submission(MEDIUM, frozenset(), "trpro-slurm1")]
        self.assertEqual(
            plan_submissions([], {}, jobs, records, {}, sent, NOW, NOW),
            [Submission(MEDIUM, excluded)],
        )
        # A pinned runner of another size is no retry of this one
        small_pin = pinned_to(
            "trpro-slurm1", allocation(3, "PENDING", size_label=SMALL)
        )
        by_name = registrations(registration(small_pin, False))
        self.assertIn(
            Submission(MEDIUM, frozenset(), "trpro-slurm1"),
            plan_submissions([small_pin], by_name, jobs, records, {}, [], NOW, NOW),
        )
        # One that failed is a failed retry, not in flight
        hung = pinned_to("trpro-slurm1", allocation(2, age_s=B + 1))
        by_name = registrations(registration(hung, False))
        self.assertIn(
            Submission(MEDIUM, frozenset(), "trpro-slurm1"),
            plan_submissions([hung], by_name, jobs, records, {}, [], NOW, NOW),
        )


class RuleThreeUsableNowhereTest(unittest.TestCase):
    """3. A size with no node where it is known to work (after a restart, or when it fails everywhere) gets
    one runner at a time."""

    def test_after_a_restart_one_unpinned_runner_at_a_time(self):
        jobs = demand(waiting(10))
        self.assertEqual(
            plan_submissions([], {}, jobs, NO_LAUNCH_RECORDS, {}, [], NOW, NOW),
            [Submission(MEDIUM, frozenset())],
        )
        first = allocation(1, "PENDING")
        by_name = registrations(registration(first, False))
        self.assertEqual(
            plan_submissions(
                [first], by_name, jobs, NO_LAUNCH_RECORDS, {}, [], NOW, NOW
            ),
            [],
        )
        sent = [Submission(MEDIUM, frozenset())]
        self.assertEqual(
            plan_submissions([], {}, jobs, NO_LAUNCH_RECORDS, {}, sent, NOW, NOW), []
        )

    def test_a_pinned_runner_does_not_hold_back_the_one_runner(self):
        # Left from when the size was usable somewhere, pending on a drained node for up to a day
        stuck = pinned_to("trpro-slurm1", allocation(1, "PENDING"))
        records = out_on(NO_LAUNCH_RECORDS, "trpro-slurm1", MEDIUM, seen_at=NOW - 30)
        by_name = registrations(registration(stuck, False))
        self.assertEqual(
            plan_submissions(
                [stuck], by_name, demand(waiting(3)), records, {}, [], NOW, NOW
            ),
            [Submission(MEDIUM, frozenset({"trpro-slurm1"}))],
        )

    def test_failing_everywhere_its_one_runner_is_the_retry_of_a_node_due(self):
        records = out_on(NO_LAUNCH_RECORDS, "trpro-slurm1", MEDIUM, seen_at=NOW - 30)
        records = out_on(records, "thor-slurm1", MEDIUM, seen_at=NOW - 60)
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        # thor-slurm1's wait has passed: allowed, and no pinned runner to it
        self.assertEqual(
            plan_submissions([], {}, demand(waiting(5)), records, {}, [], NOW, NOW),
            [Submission(MEDIUM, frozenset({"trpro-slurm1"}))],
        )

    def test_the_first_squeue_after_a_restart_restores_what_it_can(self):
        busy = on("thor-slurm1", allocation(1))
        failed = on("trpro-slurm1", ended(2, "FAILED"))
        records = filed(
            NO_LAUNCH_RECORDS, (busy, registration(busy, is_busy=True)), (failed, None)
        )
        self.assertTrue(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(nodes_out(records, MEDIUM), {"trpro-slurm1"})
        self.assertFalse(size_usable_somewhere(records, SMALL))


class RuleFourNarrowedExclusionsTest(unittest.TestCase):
    """4. A runner still pending in Slurm's queue is never left excluding a node that a new runner of its
    size would now be allowed on."""

    def pending(self, slurm_job_id, excluded, **changes):
        return replace(
            allocation(slurm_job_id, "PENDING"),
            excluded_nodes=frozenset(excluded),
            **changes,
        )

    def test_only_pending_unpinned_runners_and_never_a_node_added(self):
        trpro = {"trpro-slurm1", "trpro-slurm2"}
        allowed_back = self.pending(1, trpro)
        unchanged = self.pending(2, {"trpro-slurm1"})
        small = self.pending(3, trpro, size_label=SMALL)
        pinned = self.pending(4, trpro, pinned_node="tr-slurm2")
        held = self.pending(5, trpro, is_held=True)
        running = replace(allocation(6), excluded_nodes=frozenset(trpro))
        none_yet = self.pending(7, set())
        # A new medium runner would exclude trpro-slurm1 and thor-slurm1; a small one nothing
        records = usable_on("delta-slurm1", MEDIUM, SMALL)
        records = out_on(records, "trpro-slurm1", MEDIUM)
        records = out_on(records, "thor-slurm1", MEDIUM)
        unreadable = self.pending(8, set())
        unreadable = replace(unreadable, excluded_nodes=None)
        # Grouped by new exclusion: one scontrol call each
        self.assertEqual(
            pending_runners_by_narrowed_exclusion(
                [
                    allowed_back,
                    unchanged,
                    small,
                    pinned,
                    held,
                    running,
                    none_yet,
                    unreadable,
                ],
                records,
                NOW,
            ),
            {frozenset({"trpro-slurm1"}): [allowed_back], frozenset(): [small]},
        )

    def test_a_size_usable_nowhere_releases_its_pending_runner_as_waits_pass(self):
        # After a fault on every node its one pending runner excluded them all; it would never start
        everything = {"delta-slurm1", "thor-slurm1", "trpro-slurm1"}
        stuck = self.pending(1, everything)
        records = NO_LAUNCH_RECORDS
        for node, seen_at in (("delta-slurm1", NOW - 60), ("thor-slurm1", NOW - 10)):
            records = out_on(records, node, MEDIUM, seen_at=seen_at)
        self.assertEqual(
            pending_runners_by_narrowed_exclusion([stuck], records, NOW),
            {frozenset({"thor-slurm1"}): [stuck]},
        )


class RuleFiveNodelessFailuresTest(unittest.TestCase):
    """5. A failure that belongs to no node (Slurm refused the submission, or the failed job shows no node)
    counts against the size: its next runner waits 1, 2, 4, 8, at most 10 minutes."""

    def nodeless(self, slurm_job_id, submitted_at):
        """Ran and failed, and squeue shows no node (not seen yet under --no-requeue)."""
        return ended(slurm_job_id, "FAILED", submitted_at=submitted_at)

    def test_a_failed_job_with_no_node_makes_its_size_wait(self):
        records = usable_on("thor-slurm1", MEDIUM)
        records = filed(records, (self.nodeless(1, NOW - 100), None))
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(nodes_out(records, MEDIUM), set())
        jobs = demand(waiting(4))
        self.assertEqual(
            plan_submissions([], {}, jobs, records, {}, [], NOW + 59, NOW + 59), []
        )
        self.assertEqual(
            plan_submissions([], {}, jobs, records, {}, [], NOW + 60, NOW + 60),
            [Submission(MEDIUM, frozenset())],
        )

    def test_the_wait_doubles_only_for_a_runner_submitted_after_the_last_was_seen(self):
        records = filed(NO_LAUNCH_RECORDS, (self.nodeless(1, NOW - 100), None))
        in_flight = self.nodeless(2, NOW - 90)
        records = filed(records, (in_flight, None), now=NOW + 10)
        self.assertEqual(records.by_size[MEDIUM].nodeless_retries, 0)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 70)
        next_one = self.nodeless(3, NOW + 70)
        records = filed(records, (next_one, None), now=NOW + 100)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 220)

    def test_the_same_failure_seen_on_a_second_cycle_does_not_restart_the_wait(self):
        failed = self.nodeless(1, NOW - 100)
        records = filed(NO_LAUNCH_RECORDS, (failed, None))
        self.assertEqual(filed(records, (failed, None), now=NOW + 30), records)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 60)

    def test_one_older_than_a_runner_of_its_size_taking_a_job_says_nothing(self):
        # After a restart: a refusal's FAILED record from before it, and a newer runner busy
        refused_before = Allocation(
            1, "gha-test-1", MEDIUM, "FAILED", False, NOW - 90, NOW - 90, NOW - 90
        )
        busy = on("thor-slurm1", allocation(2, submitted_at=NOW - 60))
        records = filed(
            NO_LAUNCH_RECORDS,
            (refused_before, None),
            (busy, registration(busy, is_busy=True)),
        )
        self.assertTrue(size_usable_somewhere(records, MEDIUM))

    def test_a_runner_submitted_after_it_was_seen_taking_a_job_clears_it(self):
        records = filed(
            usable_on("thor-slurm1", MEDIUM), (self.nodeless(1, NOW - 100), None)
        )
        before = on("thor-slurm1", allocation(2, submitted_at=NOW - 1))
        took = filed(records, (before, registration(before, is_busy=True)))
        self.assertFalse(size_usable_somewhere(took, MEDIUM))
        after = on("thor-slurm1", allocation(3, submitted_at=NOW + 1))
        took = filed(records, (after, registration(after, is_busy=True)), now=NOW + 20)
        self.assertTrue(size_usable_somewhere(took, MEDIUM))
        # The old failure, still in squeue, is not counted again
        self.assertEqual(
            filed(took, (self.nodeless(1, NOW - 100), None), now=NOW + 30), took
        )

    def test_the_failed_record_of_a_refused_submission_is_not_counted_again(self):
        # As job 753319: sbatch refused it, and squeue lists it FAILED with no node for MinJobAge. sbatch
        # ran 5 s into the cycle, so its record is newer than anything the refusal stamped
        refused = Allocation(
            1, "gha-test-1", MEDIUM, "FAILED", False, NOW + 5, NOW + 5, NOW + 5
        )
        records = record_refused_submission(
            usable_on("thor-slurm1", MEDIUM),
            Submission(MEDIUM, frozenset({"trpro-slurm1"})),
            refused.runner_name,
            refused_at=NOW,
        )
        self.assertEqual(filed(records, (refused, None), now=NOW + 30), records)
        # Its name is dropped once squeue has purged the record
        later = NOW + REFUSED_NAMES_KEPT_S
        self.assertEqual(
            filed(records, now=later).by_size[MEDIUM].refused_runner_names, {}
        )


class RefusedSubmissionTest(unittest.TestCase):
    """sbatch failed and squeue shows nothing that could run by the runner's name. Any refusal holds the
    size usable nowhere until a runner of it submitted after the refusal takes a job."""

    def refuse(self, records, submission, now=NOW):
        return record_refused_submission(records, submission, "gha-test-x", now)

    def test_with_nothing_excluded_it_is_a_rule_5_failure(self):
        records = self.refuse(
            usable_on("thor-slurm1", MEDIUM), Submission(MEDIUM, frozenset())
        )
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 60)
        records = self.refuse(records, Submission(MEDIUM, frozenset()), now=NOW + 60)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 180)

    def test_with_nodes_excluded_it_doubles_nothing_and_waits_for_the_earliest(self):
        records = out_on(NO_LAUNCH_RECORDS, "trpro-slurm1", MEDIUM, seen_at=NOW - 30)
        records = out_on(records, "thor-slurm1", MEDIUM, seen_at=NOW - 10)
        submission = Submission(MEDIUM, frozenset({"trpro-slurm1", "thor-slurm1"}))
        records = self.refuse(records, submission)
        self.assertEqual(records.by_size[MEDIUM].nodeless_retries, 0)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 30)
        jobs = demand(waiting(1))
        self.assertEqual(
            plan_submissions([], {}, jobs, records, {}, [], NOW + 29, NOW + 29), []
        )
        # Then it excludes only the node not yet due: a different submission
        self.assertEqual(
            plan_submissions([], {}, jobs, records, {}, [], NOW + 30, NOW + 30),
            [Submission(MEDIUM, frozenset({"thor-slurm1"}))],
        )

    def test_a_runner_accepted_before_the_refusal_does_not_clear_it(self):
        # The cycle started at NOW: U1 was accepted at NOW + 5, U2 refused at NOW + 6
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 30)
        u2 = Submission(MEDIUM, frozenset({"trpro-slurm1"}))
        records = self.refuse(records, u2, now=NOW + 6)
        u1 = on("thor-slurm1", allocation(11, submitted_at=NOW + 5))
        records = filed(records, (u1, registration(u1, is_busy=True)), now=NOW + 30)
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(
            plan_submissions(
                [], {}, demand(waiting(4)), records, {}, [], NOW + 30, NOW + 30
            ),
            [Submission(MEDIUM, frozenset())],
        )

    def test_a_refused_node_that_is_usable_again_ends_the_wait(self):
        records = usable_on("tr-slurm2", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 10)
        records = out_on(records, "trpro-slurm2", MEDIUM, seen_at=NOW)
        trpro = frozenset({"trpro-slurm1", "trpro-slurm2"})
        records = self.refuse(records, Submission(MEDIUM, trpro))
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), NOW + 50)
        # A runner pinned there before the refusal took a job
        pinned = pinned_to(
            "trpro-slurm1", on("trpro-slurm1", allocation(1, submitted_at=NOW - 5))
        )
        records = filed(records, (pinned, registration(pinned, is_busy=True)))
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), 0)

    def test_the_cycle_of_a_refusal_sends_nothing_more_of_the_size(self):
        records = self.refuse(
            usable_on("thor-slurm1", MEDIUM), Submission(MEDIUM, frozenset())
        )
        records = replace(
            records,
            by_size={
                MEDIUM: replace(records.by_size[MEDIUM], nodeless_failure_seen_at=None)
            },
        )
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), 0)
        jobs = demand(waiting(1))
        # A later run of the same cycle, planned 40 s after the refusal
        self.assertEqual(
            plan_submissions(
                [], {}, jobs, records, {}, [], NOW + 40, cycle_started_at=NOW - 1
            ),
            [],
        )
        # The next cycle
        self.assertEqual(
            len(
                plan_submissions(
                    [], {}, jobs, records, {}, [], NOW + 40, cycle_started_at=NOW + 2
                )
            ),
            1,
        )

    def test_a_refused_pinned_runner_forgets_its_node_and_is_not_rule_5(self):
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 600)
        pinned = Submission(MEDIUM, frozenset(), "trpro-slurm1")
        records = self.refuse(records, pinned)
        self.assertEqual(nodes_out(records, MEDIUM), set())
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        self.assertIsNone(records.by_size[MEDIUM].nodeless_failure_seen_at)
        self.assertEqual(next_unpinned_runner_at(records, MEDIUM), 0)
        # A runner submitted after the refusal that takes a job makes it usable again
        took = on("thor-slurm1", allocation(1, submitted_at=NOW + 5))
        records = filed(records, (took, registration(took, is_busy=True)), now=NOW + 30)
        self.assertTrue(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(
            records.by_size[MEDIUM].refused_runner_names, {"gha-test-x": NOW}
        )


class ForgetNodesSlurmDoesNotKnowTest(unittest.TestCase):
    """sbatch failed, squeue shows nothing by the runner's name, and sinfo does not list a node it named."""

    def test_their_records_are_forgotten_for_every_size_and_nothing_else(self):
        records = usable_on("thor-slurm1", MEDIUM, SMALL)
        records = out_on(records, "wato3-slurm1", MEDIUM, seen_at=NOW - 30)
        records = out_on(records, "wato3-slurm1", SMALL, seen_at=NOW - 30)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 30)
        forgot = forget_nodes_slurm_does_not_know(records, {"wato3-slurm1"}, NOW)
        self.assertEqual(nodes_out(forgot, MEDIUM), {"trpro-slurm1"})
        self.assertEqual(nodes_out(forgot, SMALL), set())
        # So no submission names it any more
        self.assertEqual(
            nodes_a_new_runner_excludes(forgot, MEDIUM, NOW), {"trpro-slurm1"}
        )
        # Nothing is counted against either size
        self.assertEqual(forgot.by_size, records.by_size)
        self.assertTrue(size_usable_somewhere(forgot, MEDIUM))
        # Its old failure, still in squeue, is not filed again
        old = on("wato3-slurm1", ended(1, "FAILED", submitted_at=NOW - 100))
        self.assertEqual(filed(forgot, (old, None), now=NOW + 2), forgot)

    def test_a_forgotten_node_is_untried_not_usable(self):
        # A refused pinned runner forgot N; a runner on M then cleared the hold, and M's newest runner failed
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW - 600)
        records = record_refused_submission(
            records, Submission(MEDIUM, frozenset(), "trpro-slurm1"), "gha-x", NOW
        )
        took = on("thor-slurm1", allocation(1, submitted_at=NOW + 5))
        records = filed(records, (took, registration(took, is_busy=True)), now=NOW + 20)
        self.assertTrue(size_usable_somewhere(records, MEDIUM))
        failed = on(
            "thor-slurm1", ended(2, "FAILED", submitted_at=NOW + 30, now=NOW + 60)
        )
        records = filed(records, (failed, None), now=NOW + 60)
        self.assertFalse(size_usable_somewhere(records, MEDIUM))
        self.assertEqual(
            plan_submissions(
                [], {}, demand(waiting(5)), records, {}, [], NOW + 60, NOW + 60
            ),
            [Submission(MEDIUM, frozenset({"thor-slurm1"}))],
        )


class SubmissionOrderTest(unittest.TestCase):
    def test_labels_take_turns_each_with_its_pinned_runners_first(self):
        a, b = Submission("a", frozenset()), Submission("b", frozenset())
        a_pinned = Submission("a", frozenset(), "trpro-slurm1")
        self.assertEqual(submission_order([a, a, b, a_pinned]), [a_pinned, b, a, a])
        self.assertEqual(submission_order([]), [])


class LaunchRecordLogLinesTest(unittest.TestCase):
    def test_one_line_per_change(self):
        before = usable_on("thor-slurm1", MEDIUM)
        failed = on("trpro-slurm1", ended(812345, "FAILED"))
        after = filed(before, (failed, None))
        self.assertEqual(
            launch_record_log_lines(before, after, NOW),
            [
                f"{MEDIUM}: out on trpro-slurm1 (Slurm job 812345 failed to launch);"
                " retry there in 60 s"
            ],
        )
        self.assertEqual(launch_record_log_lines(after, after, NOW), [])
        retry = on(
            "trpro-slurm1",
            ended(812350, "FAILED", submitted_at=NOW + 70, now=NOW + 100),
        )
        doubled = filed(after, (retry, None), now=NOW + 100)
        self.assertEqual(
            launch_record_log_lines(after, doubled, NOW + 100),
            [
                f"{MEDIUM}: out on trpro-slurm1 (Slurm job 812350 failed to launch,"
                " retry 1); retry there in 120 s"
            ],
        )
        took = on("trpro-slurm1", allocation(812360, submitted_at=NOW + 230))
        back = filed(doubled, (took, registration(took, is_busy=True)), now=NOW + 240)
        self.assertEqual(
            launch_record_log_lines(doubled, back, NOW + 240),
            [f"{MEDIUM}: usable again on trpro-slurm1 (Slurm job 812360 took a job)"],
        )

    def test_a_size_usable_nowhere_and_a_failure_with_no_node(self):
        before = usable_on("thor-slurm1", MEDIUM)
        failed = on("thor-slurm1", ended(7, "FAILED", submitted_at=NOW - 30))
        after = filed(before, (failed, None))
        self.assertEqual(
            launch_record_log_lines(before, after, NOW)[-1],
            f"{MEDIUM}: usable on no node; one runner at a time",
        )
        nodeless = ended(8, "FAILED", submitted_at=NOW - 20)
        later = filed(after, (nodeless, None), now=NOW + 30)
        self.assertEqual(
            launch_record_log_lines(after, later, NOW + 30),
            [f"{MEDIUM}: a runner failed with no node; next runner in 60 s"],
        )
        # A refusal is logged where it happens, with sbatch's message
        refused = record_refused_submission(
            after, Submission(MEDIUM, frozenset()), "gha-test-x", NOW + 30
        )
        self.assertEqual(launch_record_log_lines(after, refused, NOW + 30), [])


if __name__ == "__main__":
    unittest.main()
