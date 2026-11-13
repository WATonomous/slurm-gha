"""plan without Slurm or GitHub: every pure function by table, and the breaker through its whole life."""

import unittest

from config import (
    BOOT_ALLOWANCE_S as B,
    BUSY_COVER_S as T,
    FIRST_PROBE_WAIT_S,
    IDLE_ALLOWANCE_S as W,
    MAX_PROBE_WAIT_S,
    TOLERATED_FAILURES_IN_A_ROW,
    UNUSED_REGISTRATION_LIFETIME_S as A_DAY,
)
from plan import (
    FAILURES_REMEMBERED,
    FINISHED_STATES,
    NO_DEMAND,
    NO_SIGHTINGS,
    UNPROVEN,
    Allocation,
    Demand,
    Evidence,
    LabelBreaker,
    LaunchFailure,
    Phase,
    RunnerRegistration,
    RunnerSightings,
    WaitingJob,
    allocations_to_cancel,
    breaker_log_lines,
    earliest_next_probe_at,
    is_proven,
    jobs_left_waiting_by_label,
    launch_evidence,
    phase_of_allocation,
    plan_submissions,
    record_evidence,
    registrations_to_delete,
    runners_taking_a_job_by_label,
    runners_to_submit_for_label,
    submission_order,
    uncovered_jobs_waiting_past_b_by_label,
    update_breakers,
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


def failure(slurm_job_id, submitted_at, recorded_at=NOW):
    return LaunchFailure(slurm_job_id, submitted_at, recorded_at)


class PhaseOfAllocationTest(unittest.TestCase):
    def test_each_phase(self):
        rows = [
            (
                "pending, registered",
                allocation(1, "PENDING"),
                "offline",
                Phase.WAITING_FOR_NODE,
            ),
            (
                # A list read can miss a fresh registration: still on its way
                "pending, unregistered",
                allocation(1, "PENDING"),
                None,
                Phase.WAITING_FOR_NODE,
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
        waiting_for_node = allocation(3, "PENDING")
        running_unregistered = allocation(4)
        running_busy = allocation(5)
        finished = ended(6, "FAILED")
        allocations = [
            unregistered,
            held,
            just_submitted,
            waiting_for_node,
            running_unregistered,
            running_busy,
            finished,
        ]
        by_name = registrations(
            registration(held, False),
            registration(waiting_for_node, False),
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
            # No job of its size has waited over B, or a runner taking a job covers each: surplus
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
            uncovered_jobs_waiting_past_b_by_label(jobs, {}, NOW), {MEDIUM: 2, SMALL: 1}
        )
        self.assertEqual(
            uncovered_jobs_waiting_past_b_by_label(jobs, {MEDIUM: 3}, NOW),
            {MEDIUM: 0, SMALL: 1},
        )

    def test_pending_booting_and_unregistered_runners_say_nothing(self):
        pending = allocation(1, "PENDING")
        booting = allocation(2, age_s=10)
        unregistered = allocation(3, age_s=B + 1)
        self.assertIsNone(self.evidence(pending, registration(pending, False)))
        self.assertIsNone(self.evidence(booting, registration(booting, False)))
        self.assertIsNone(self.evidence(unregistered, None))


class RecordEvidenceTest(unittest.TestCase):
    def test_a_success_proves_the_label_and_drops_older_failures(self):
        breaker = LabelBreaker(None, (failure(1, NOW - 100), failure(2, NOW - 50)))
        success = allocation(3, submitted_at=NOW - 50)
        updated = record_evidence(breaker, success, Evidence.TOOK_A_JOB, NOW)
        self.assertEqual(updated, LabelBreaker(NOW - 50, ()))
        later_failure = LabelBreaker(None, (failure(4, NOW - 40),))
        updated = record_evidence(later_failure, success, Evidence.TOOK_A_JOB, NOW)
        self.assertEqual(updated, LabelBreaker(NOW - 50, (failure(4, NOW - 40),)))

    def test_evidence_older_than_the_newest_success_changes_nothing(self):
        breaker = LabelBreaker(NOW - 50, (failure(4, NOW - 40),))
        for evidence in Evidence:
            with self.subTest(evidence):
                old = allocation(5, submitted_at=NOW - 60)
                self.assertIs(record_evidence(breaker, old, evidence, NOW), breaker)
                same_age = allocation(6, submitted_at=NOW - 50)
                self.assertIs(
                    record_evidence(breaker, same_age, evidence, NOW), breaker
                )

    def test_a_failure_is_recorded_once(self):
        failed = ended(7, "FAILED")
        once = record_evidence(UNPROVEN, failed, Evidence.FAILED_TO_LAUNCH, NOW)
        self.assertEqual(
            once.failures_since_success, (failure(7, failed.submitted_at, NOW),)
        )
        twice = record_evidence(once, failed, Evidence.FAILED_TO_LAUNCH, NOW + 60)
        self.assertIs(twice, once)

    def test_failures_are_kept_in_submit_order_and_capped(self):
        breaker = UNPROVEN
        for slurm_job_id in (3, 1, 2):
            failed = allocation(slurm_job_id, submitted_at=NOW - 100 + slurm_job_id)
            breaker = record_evidence(breaker, failed, Evidence.FAILED_TO_LAUNCH, NOW)
        self.assertEqual(
            [each.slurm_job_id for each in breaker.failures_since_success], [1, 2, 3]
        )
        for slurm_job_id in range(4, 4 + FAILURES_REMEMBERED):
            failed = allocation(slurm_job_id, submitted_at=NOW - 100 + slurm_job_id)
            breaker = record_evidence(breaker, failed, Evidence.FAILED_TO_LAUNCH, NOW)
        self.assertEqual(len(breaker.failures_since_success), FAILURES_REMEMBERED)
        # An older failure, seen late, would be dropped at once: it must not restart the wait
        late = allocation(99, submitted_at=NOW - 1000)
        self.assertIs(
            record_evidence(breaker, late, Evidence.FAILED_TO_LAUNCH, NOW + 500),
            breaker,
        )


class ProvenAndProbeWaitTest(unittest.TestCase):
    def test_is_proven(self):
        rows = [
            (UNPROVEN, False),
            (LabelBreaker(None, (failure(1, NOW),)), False),
            (LabelBreaker(NOW - 60, ()), True),
            (LabelBreaker(NOW - 60, (failure(1, NOW),)), True),  # one failure tolerated
            (LabelBreaker(NOW - 60, (failure(1, NOW), failure(2, NOW))), False),
        ]
        for breaker, proven in rows:
            with self.subTest(breaker=breaker):
                self.assertEqual(is_proven(breaker), proven)

    def test_the_first_breaker_that_is_not_proven_waits_exactly_the_first_wait(self):
        count = TOLERATED_FAILURES_IN_A_ROW + 1
        failures = tuple(failure(i, NOW, NOW - 100 + i) for i in range(count))
        breaker = LabelBreaker(NOW - 3600, failures)
        self.assertFalse(is_proven(breaker))
        newest_recorded_at = NOW - 100 + count - 1
        self.assertEqual(
            earliest_next_probe_at(breaker), newest_recorded_at + FIRST_PROBE_WAIT_S
        )
        fewer = LabelBreaker(NOW - 3600, failures[:-1])
        self.assertTrue(is_proven(fewer))
        self.assertEqual(earliest_next_probe_at(fewer), 0.0)

    def test_the_wait_doubles_from_the_second_failure_up_to_the_cap(self):
        expected_waits = [0, 0, 60, 120, 240, 480, 600, 600]
        self.assertEqual(expected_waits[2], FIRST_PROBE_WAIT_S)
        self.assertEqual(expected_waits[-1], MAX_PROBE_WAIT_S)
        for count, wait_s in enumerate(expected_waits):
            with self.subTest(failures=count):
                failures = tuple(failure(i, NOW, NOW - 100 + i) for i in range(count))
                breaker = LabelBreaker(None, failures)
                newest_recorded_at = NOW - 100 + count - 1
                expected = 0.0 if count < 2 else newest_recorded_at + wait_s
                self.assertEqual(earliest_next_probe_at(breaker), expected)


class RunnersToSubmitForLabelTest(unittest.TestCase):
    def test_by_table(self):
        proven = LabelBreaker(NOW - 60, ())
        waiting_for_probe = LabelBreaker(
            None, (failure(1, NOW, NOW), failure(2, NOW, NOW))
        )
        rows = [
            ("no shortfall", 0, 3, proven, NOW, 0),
            ("proven: the whole shortfall", 9, 1, proven, NOW, 9),
            ("unproven: one", 9, 0, UNPROVEN, NOW, 1),
            ("unproven, a runner can still take a job", 9, 1, UNPROVEN, NOW, 0),
            ("unproven, waiting for the probe", 9, 0, waiting_for_probe, NOW + 59, 0),
            ("unproven, the wait is over", 9, 0, waiting_for_probe, NOW + 60, 1),
        ]
        for description, shortfall, can_take, breaker, now, expected in rows:
            with self.subTest(description):
                self.assertEqual(
                    runners_to_submit_for_label(shortfall, can_take, breaker, now),
                    expected,
                )


class PlanSubmissionsTest(unittest.TestCase):
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
        proven = {MEDIUM: LabelBreaker(NOW - 60, ()), SMALL: LabelBreaker(NOW - 60, ())}
        jobs = demand(waiting(10) + waiting(1, SMALL), [named_on_a_job.runner_name])
        self.assertEqual(
            plan_submissions(allocations, by_name, jobs, proven, {}, {}, NOW),
            {MEDIUM: 10 - 3, SMALL: 0},
        )

    def test_a_pending_runner_missing_from_the_list_still_counts(self):
        just_submitted = allocation(1, "PENDING")
        proven = {MEDIUM: LabelBreaker(NOW - 60, ())}
        jobs = demand(waiting(3))
        self.assertEqual(
            plan_submissions([just_submitted], {}, jobs, proven, {}, {}, NOW),
            {MEDIUM: 2},
        )

    def test_a_runner_github_leaves_idle_while_a_job_waits_stops_counting(self):
        passed_over = allocation(1, age_s=2 * B + 1)
        by_name = registrations(registration(passed_over))
        proven = {MEDIUM: LabelBreaker(NOW - 3600, ())}
        waited_over_b = demand(waiting(1, waited_s=B + 1))
        self.assertEqual(
            plan_submissions(
                [passed_over], by_name, waited_over_b, proven, {}, {}, NOW
            ),
            {MEDIUM: 1},
        )
        just_waiting = demand(waiting(1, waited_s=B))
        self.assertEqual(
            plan_submissions([passed_over], by_name, just_waiting, proven, {}, {}, NOW),
            {MEDIUM: 0},
        )

    def test_an_unproven_label_gets_one_runner_when_none_can_take_a_job(self):
        jobs = demand(waiting(10))
        self.assertEqual(plan_submissions([], {}, jobs, {}, {}, {}, NOW), {MEDIUM: 1})
        probe = allocation(1, "PENDING")
        by_name = registrations(registration(probe, False))
        self.assertEqual(
            plan_submissions([probe], by_name, jobs, {}, {}, {}, NOW), {MEDIUM: 0}
        )

    def test_runners_submitted_earlier_this_cycle_count_as_able_to_take_a_job(self):
        jobs = demand(waiting(10))
        proven = {MEDIUM: LabelBreaker(NOW - 60, ())}
        self.assertEqual(
            plan_submissions([], {}, jobs, proven, {}, {MEDIUM: 4}, NOW), {MEDIUM: 6}
        )
        # An unproven label's one runner went out earlier this cycle: no second one
        self.assertEqual(
            plan_submissions([], {}, jobs, {}, {}, {MEDIUM: 1}, NOW), {MEDIUM: 0}
        )

    def test_no_waiting_jobs_no_runners(self):
        self.assertEqual(plan_submissions([], {}, demand(), {}, {}, {}, NOW), {})


class BusyRunnerCoversItsJobTest(unittest.TestCase):
    """GitHub marks a runner busy at the offer, before the job stops saying queued and names it."""

    proven = {MEDIUM: LabelBreaker(NOW - 3600, ()), SMALL: LabelBreaker(NOW - 3600, ())}

    def taking(self, slurm_job_id=1, state="RUNNING", size_label=MEDIUM):
        """One of our runners, busy with a job that may still say queued."""
        of = allocation(slurm_job_id, state, size_label=size_label)
        return of, registration(of, is_busy=True)

    def plan(self, allocations, by_name, jobs, sightings, now=NOW):
        taking = runners_taking_a_job_by_label(
            allocations, by_name, sightings, jobs, now
        )
        return plan_submissions(
            allocations, by_name, jobs, self.proven, taking, {}, now
        )

    def test_inside_the_gap_nothing_extra_is_submitted(self):
        of, reg = self.taking()
        by_name = registrations(reg)
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW - 10)
        # Its own job, still queued and naming no runner
        jobs = demand(waiting(1))
        self.assertEqual(self.plan([of], by_name, jobs, sightings), {MEDIUM: 0})
        self.assertEqual(
            self.plan([of], by_name, demand(waiting(3)), sightings), {MEDIUM: 2}
        )

    def test_past_t_it_no_longer_covers(self):
        of, reg = self.taking()
        by_name = registrations(reg)
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW)
        jobs = demand(waiting(1))
        self.assertEqual(
            self.plan([of], by_name, jobs, sightings, NOW + T - 1), {MEDIUM: 0}
        )
        self.assertEqual(
            self.plan([of], by_name, jobs, sightings, NOW + T), {MEDIUM: 1}
        )

    def test_a_runner_once_named_on_a_job_never_covers_again(self):
        of, reg = self.taking()
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
                of, reg = self.taking(state=state)
                by_name = registrations(reg)
                sightings = update_sightings(
                    NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW
                )
                self.assertEqual(
                    self.plan([of], by_name, demand(waiting(1)), sightings), {MEDIUM: 1}
                )

    def test_after_a_restart_it_covers_from_when_it_is_first_seen(self):
        of, reg = self.taking()
        by_name = registrations(reg)
        jobs = demand(waiting(1))
        self.assertEqual(self.plan([of], by_name, jobs, NO_SIGHTINGS), {MEDIUM: 1})
        sightings = update_sightings(NO_SIGHTINGS, [of], by_name, NO_DEMAND, NOW)
        self.assertEqual(self.plan([of], by_name, jobs, sightings), {MEDIUM: 0})

    def test_it_covers_only_jobs_of_its_size_and_never_below_zero(self):
        three = [self.taking(i)[0] for i in (1, 2, 3)]
        by_name = registrations(*(registration(each, is_busy=True) for each in three))
        sightings = update_sightings(NO_SIGHTINGS, three, by_name, NO_DEMAND, NOW)
        jobs = demand(waiting(1) + waiting(2, SMALL))
        self.assertEqual(
            self.plan(three, by_name, jobs, sightings), {MEDIUM: 0, SMALL: 2}
        )
        self.assertEqual(
            jobs_left_waiting_by_label(jobs, {MEDIUM: 3}), {MEDIUM: 0, SMALL: 2}
        )

    def test_an_idle_runner_beside_a_covered_job_is_not_a_failed_launch(self):
        # R took the job K is waiting for; I1 and I2, submitted after R, are online and idle
        r = allocation(1, age_s=3 * B)
        idle = [allocation(i, age_s=2 * B + 10) for i in (2, 3)]
        by_name = registrations(
            registration(r, is_busy=True), *(registration(each) for each in idle)
        )
        allocations = [r, *idle]
        sightings = update_sightings(
            NO_SIGHTINGS, allocations, by_name, NO_DEMAND, NOW - 10
        )
        k = demand(waiting(1, waited_s=B + 5))
        taking = runners_taking_a_job_by_label(allocations, by_name, sightings, k, NOW)
        self.assertEqual(taking, {MEDIUM: 1})
        breakers = update_breakers(self.proven, allocations, by_name, k, taking, NOW)
        self.assertEqual(breakers[MEDIUM], LabelBreaker(r.submitted_at, ()))
        # A second job past B is not covered: then GitHub is passing over the idle runners
        k_and_another = demand(waiting(2, waited_s=B + 5))
        breakers = update_breakers(
            self.proven, allocations, by_name, k_and_another, taking, NOW
        )
        self.assertFalse(is_proven(breakers[MEDIUM]))

    def test_sightings_keep_only_runners_squeue_still_shows(self):
        of, reg = self.taking()
        gone, gone_reg = self.taking(2)
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
        # The breaker reads Slurm's end state, the phase is ENDED, and an ended runner covers nothing
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
                    update_breakers({}, [of], listed, NO_DEMAND, {}, NOW),
                    update_breakers({}, [of], deleted, NO_DEMAND, {}, NOW),
                )
                self.assertIs(phase_of_allocation(of, reg, NOW), Phase.ENDED)
                self.assertIs(phase_of_allocation(of, None, NOW), Phase.ENDED)
                jobs = demand(waiting(1))
                self.assertEqual(
                    plan_submissions([of], listed, jobs, {}, {}, {}, NOW),
                    plan_submissions([of], deleted, jobs, {}, {}, {}, NOW),
                )


class SubmissionOrderTest(unittest.TestCase):
    def test_labels_take_turns(self):
        self.assertEqual(
            submission_order({"a": 3, "b": 1, "c": 2, "d": 0}),
            ["a", "b", "c", "a", "c", "a"],
        )
        self.assertEqual(submission_order({}), [])


class BreakerLogLinesTest(unittest.TestCase):
    def test_one_line_per_label_that_changed(self):
        before = {MEDIUM: LabelBreaker(NOW - 60, ()), SMALL: UNPROVEN}
        after = {
            MEDIUM: LabelBreaker(
                NOW - 60, (failure(11, NOW - 30, NOW - 5), failure(12, NOW - 20, NOW))
            ),
            SMALL: UNPROVEN,
            "slurm-runner-large": LabelBreaker(NOW - 10, ()),
        }
        self.assertEqual(
            breaker_log_lines(before, after),
            [
                "slurm-runner-large: proven, 0 failures in a row",
                f"{MEDIUM}: unproven, 2 failures in a row (newest: Slurm job 12);"
                " one runner at a time, the next 60 s after the newest failure",
            ],
        )
        self.assertEqual(breaker_log_lines(after, after), [])

    def test_a_failure_past_the_remembered_count_still_logs(self):
        full = tuple(
            failure(i, NOW - 100 + i, NOW - 100 + i) for i in range(FAILURES_REMEMBERED)
        )
        before = {MEDIUM: LabelBreaker(NOW - 3600, full)}
        after = {MEDIUM: LabelBreaker(NOW - 3600, full[1:] + (failure(99, NOW),))}
        [line] = breaker_log_lines(before, after)
        self.assertIn(f"{FAILURES_REMEMBERED} or more failures in a row", line)
        self.assertIn("Slurm job 99", line)

    def test_a_proven_label_whose_newest_success_advances_logs_nothing(self):
        before = {MEDIUM: LabelBreaker(NOW - 60, ())}
        after = {MEDIUM: LabelBreaker(NOW - 5, ())}
        self.assertEqual(breaker_log_lines(before, after), [])
        one_failure = {MEDIUM: LabelBreaker(NOW - 5, (failure(3, NOW - 1),))}
        self.assertEqual(
            breaker_log_lines(after, one_failure),
            [f"{MEDIUM}: proven, 1 failure in a row (newest: Slurm job 3)"],
        )


class BreakerLifeTest(unittest.TestCase):
    """One size label through its whole life, a cycle at a time, as main.run_cycle drives it."""

    def setUp(self):
        self.breakers = {}
        self.next_slurm_job_id = 1

    def cycle(self, now, allocations, by_name, jobs):
        self.breakers = update_breakers(
            self.breakers, allocations, by_name, jobs, {}, now
        )
        return plan_submissions(allocations, by_name, jobs, self.breakers, {}, {}, now)[
            MEDIUM
        ]

    def submit(self, now):
        slurm_job_id = self.next_slurm_job_id
        self.next_slurm_job_id += 1
        return Allocation(
            slurm_job_id,
            f"gha-test-{slurm_job_id}",
            MEDIUM,
            "PENDING",
            False,
            now,
            None,
            None,
        )

    def test_unproven_proven_tolerant_open_doubling_and_cleared(self):
        t = NOW
        wave = demand(waiting(10, waited_s=5))
        # A restart or a first start: unproven, so one runner, and none while it can take a job
        self.assertEqual(self.cycle(t, [], {}, wave), 1)
        probe = self.submit(t)
        self.assertEqual(
            self.cycle(t + 5, [probe], registrations(registration(probe, False)), wave),
            0,
        )

        # The probe takes a job: proven, and the rest of the wave goes out at once
        t += 30
        probe = Allocation(
            probe.slurm_job_id,
            probe.runner_name,
            MEDIUM,
            "RUNNING",
            False,
            probe.submitted_at,
            t - 20,
            None,
        )
        nine = demand(waiting(9, waited_s=35), [probe.runner_name])
        self.assertEqual(
            self.cycle(
                t, [probe], registrations(registration(probe, True, True)), nine
            ),
            9,
        )
        self.assertTrue(is_proven(self.breakers[MEDIUM]))

        # One failure is tolerated: one slow node does not hold back the wave
        t += 30
        first = ended(101, "FAILED", submitted_at=t - 40, now=t)
        self.assertEqual(self.cycle(t, [first], {}, nine), 9)
        self.assertTrue(is_proven(self.breakers[MEDIUM]))

        # A second in a row opens the breaker: one runner at a time, after 60 s
        t += 30
        second = ended(102, "FAILED", submitted_at=t - 40, now=t)
        self.assertEqual(self.cycle(t, [first, second], {}, nine), 0)
        self.assertFalse(is_proven(self.breakers[MEDIUM]))
        self.assertEqual(self.cycle(t + 59, [first, second], {}, nine), 0)
        self.assertEqual(self.cycle(t + 60, [first, second], {}, nine), 1)

        # Each further failure doubles the wait, up to the cap
        for slurm_job_id, wait_s in (
            (103, 120),
            (104, 240),
            (105, 480),
            (106, 600),
            (107, 600),
        ):
            t += 1000
            failed = ended(slurm_job_id, "FAILED", submitted_at=t - 40, now=t)
            self.assertEqual(self.cycle(t, [failed], {}, nine), 0)
            self.assertEqual(self.cycle(t + wait_s - 1, [failed], {}, nine), 0)
            self.assertEqual(self.cycle(t + wait_s, [failed], {}, nine), 1)

        # An old runner taking a job says nothing about now: evidence is ordered by submit time
        t += 30
        old = Allocation(
            50, "gha-test-50", MEDIUM, "RUNNING", False, NOW - 100, NOW - 90, None
        )
        self.assertEqual(
            self.cycle(t, [old], registrations(registration(old, True, True)), nine), 0
        )
        self.assertFalse(is_proven(self.breakers[MEDIUM]))

        # A runner submitted after the failures takes a job: the breaker closes
        t += 30
        newer = Allocation(
            200, "gha-test-200", MEDIUM, "RUNNING", False, t - 20, t - 15, None
        )
        eight = demand(waiting(8, waited_s=5), [newer.runner_name])
        self.assertEqual(self.cycle(t, [newer], {}, eight), 8)
        self.assertEqual(self.breakers[MEDIUM], LabelBreaker(t - 20, ()))

    def test_a_runner_github_leaves_idle_while_a_job_waits_counts_as_a_failure(self):
        self.breakers = {MEDIUM: LabelBreaker(NOW - 3600, ())}
        deaf = [
            allocation(i, age_s=2 * B + 10, submitted_at=NOW - 2 * B - 20 + i)
            for i in (1, 2)
        ]
        by_name = registrations(*(registration(each) for each in deaf))
        jobs = demand(waiting(5, waited_s=B + 5))
        # One is tolerated, and no longer counts as a runner on its way: all five jobs get one
        self.assertEqual(self.cycle(NOW, deaf[:1], by_name, jobs), 5)
        self.assertTrue(is_proven(self.breakers[MEDIUM]))
        # Two in a row open the breaker
        self.breakers = {MEDIUM: LabelBreaker(NOW - 3600, ())}
        self.assertEqual(self.cycle(NOW, deaf, by_name, jobs), 0)
        self.assertFalse(is_proven(self.breakers[MEDIUM]))
        self.assertEqual(len(self.breakers[MEDIUM].failures_since_success), 2)
        self.assertEqual(self.cycle(NOW + FIRST_PROBE_WAIT_S, deaf, by_name, jobs), 1)
        # With no job waiting longer than B, idle runners are surplus and say nothing
        self.breakers = {MEDIUM: LabelBreaker(NOW - 3600, ())}
        self.cycle(NOW, deaf, by_name, demand(waiting(5, waited_s=B)))
        self.assertTrue(is_proven(self.breakers[MEDIUM]))


if __name__ == "__main__":
    unittest.main()
