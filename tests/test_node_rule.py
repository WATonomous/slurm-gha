"""node_rule without Slurm or GitHub: the submission count, each of the five rules, refusals and the log lines."""

import logging
import unittest
from dataclasses import replace

from config import (
    BOOT_ALLOWANCE_S as B,
    FIRST_RETRY_WAIT_S,
    MAX_RETRY_WAIT_S,
    REFUSED_NAMES_KEPT_S,
)
from node_rule import (
    NO_LAUNCH_RECORDS,
    Submission,
    file_launch_evidence,
    forget_nodes_slurm_does_not_know,
    launch_record_log_lines,
    pending_runners_by_narrowed_exclusion,
    next_unpinned_runner_at,
    node_retry_due_at,
    nodes_a_new_runner_excludes,
    nodes_out,
    ordinal,
    plan_submissions,
    record_refused_submission,
    retry_wait_s,
    size_usable_somewhere,
    submission_order,
)
from plan import (
    Allocation,
    WaitingJob,
)
from test_plan import (
    MEDIUM,
    NOW,
    SMALL,
    allocation,
    demand,
    ended,
    filed,
    on,
    out_on,
    pinned_to,
    registration,
    registrations,
    unpinned_counts,
    usable_on,
    waiting,
)


class PlanSubmissionsTest(unittest.TestCase):
    """The count of unpinned runners for a size usable somewhere: the waiting jobs less the runners that
    can still take one."""

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
                # trpro-slurm2's first retry takes medium's first turn
                Submission(MEDIUM, frozenset(), "trpro-slurm2", retry=1),
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
                Submission(MEDIUM, frozenset(), "trpro-slurm1", retry=1),
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
            Submission(MEDIUM, frozenset(), "trpro-slurm1", retry=1),
            plan_submissions([small_pin], by_name, jobs, records, {}, [], NOW, NOW),
        )
        # One that failed is a failed retry, not in flight
        hung = pinned_to("trpro-slurm1", allocation(2, age_s=B + 1))
        by_name = registrations(registration(hung, False))
        self.assertIn(
            Submission(MEDIUM, frozenset(), "trpro-slurm1", retry=1),
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
        # As on the cluster: sbatch refused it, and squeue lists it FAILED with no node for MinJobAge. sbatch
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
    def test_one_line_per_change_with_what_failed_and_where_to_look(self):
        before = usable_on("thor-slurm1", MEDIUM)
        failed = on("trpro-slurm1", ended(812345, "FAILED"))
        after = filed(before, (failed, None))
        self.assertEqual(
            launch_record_log_lines(before, after, NOW),
            [
                (
                    logging.WARNING,
                    f"{MEDIUM}: out on trpro-slurm1: Slurm job 812345 ended without taking a job"
                    " (its log: /var/log/slurm-ci/slurm-ci-812345.out on trpro-slurm1); next"
                    " pinned runner there in 60 s",
                )
            ],
        )
        self.assertEqual(launch_record_log_lines(after, after, NOW), [])
        retry = on("trpro-slurm1", allocation(812350, submitted_at=NOW + 70, age_s=B))
        doubled = filed(after, (retry, registration(retry, False)), now=NOW + 100)
        self.assertEqual(
            launch_record_log_lines(after, doubled, NOW + 100),
            [
                (
                    logging.WARNING,
                    f"{MEDIUM}: still out on trpro-slurm1: Slurm job 812350 was still offline"
                    f" {B} s after it started (its log: /var/log/slurm-ci/slurm-ci-812350.out on"
                    " trpro-slurm1); 1st failed retry; next pinned runner there in 120 s",
                )
            ],
        )
        took = on("trpro-slurm1", allocation(812360, submitted_at=NOW + 230))
        back = filed(doubled, (took, registration(took, is_busy=True)), now=NOW + 240)
        self.assertEqual(
            launch_record_log_lines(doubled, back, NOW + 240),
            [
                (
                    logging.INFO,
                    f"{MEDIUM}: usable again on trpro-slurm1 (Slurm job 812360 took a job)",
                )
            ],
        )

    def test_a_size_usable_nowhere_says_why(self):
        before = usable_on("thor-slurm1", MEDIUM)
        failed = on("thor-slurm1", ended(7, "FAILED", submitted_at=NOW - 30))
        after = filed(before, (failed, None))
        self.assertEqual(
            launch_record_log_lines(before, after, NOW)[-1],
            (
                logging.WARNING,
                f"{MEDIUM}: one runner at a time: no node is known to work for it",
            ),
        )
        nodeless = ended(8, "FAILED", submitted_at=NOW - 20)
        later = filed(before, (nodeless, None), now=NOW + 30)
        self.assertEqual(
            launch_record_log_lines(before, later, NOW + 30),
            [
                (
                    logging.WARNING,
                    f"{MEDIUM}: a runner failed with no node; next runner in 60 s",
                ),
                (
                    logging.WARNING,
                    f"{MEDIUM}: one runner at a time: a runner failed with no node",
                ),
            ],
        )
        # A refusal itself is logged where it happens, with sbatch's message
        refused = record_refused_submission(
            before, Submission(MEDIUM, frozenset()), "gha-test-x", NOW + 30
        )
        self.assertEqual(
            launch_record_log_lines(before, refused, NOW + 30),
            [
                (
                    logging.WARNING,
                    f"{MEDIUM}: one runner at a time: Slurm refused a submission (see above)",
                )
            ],
        )
        took = on("thor-slurm1", allocation(9, submitted_at=NOW + 40))
        back = filed(refused, (took, registration(took, is_busy=True)), now=NOW + 60)
        self.assertEqual(
            launch_record_log_lines(refused, back, NOW + 60),
            [
                (
                    logging.INFO,
                    f"{MEDIUM}: works on some node again; every waiting job gets a runner",
                )
            ],
        )

    def test_ordinals(self):
        self.assertEqual(
            [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)],
            [
                "1st",
                "2nd",
                "3rd",
                "4th",
                "11th",
                "12th",
                "13th",
                "21st",
                "22nd",
                "101st",
            ],
        )


if __name__ == "__main__":
    unittest.main()
