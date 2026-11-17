"""main without Slurm or GitHub: one repository's cycle against the fake Slurm and the fake GitHub."""

import logging
import unittest
from unittest import mock

import github_client
import main
import supply
from config import (
    BOOT_ALLOWANCE_S as B,
    BUSY_COVER_S as T,
    HISTORY_PAGE_INTERVAL_S,
    IDLE_ALLOWANCE_S as W,
    UNUSED_REGISTRATION_LIFETIME_S as A_DAY,
    Repository,
)
from github_client import GitHubClient, GitHubUnavailable
from main import (
    RepositoryMemory,
    empty_memory,
    outage_after_cycle,
    run_cycle,
    unsafe_settings,
)
from plan import (
    FINISHED_STATES,
    NO_LAUNCH_RECORDS,
    NO_SIGHTINGS,
    nodes_out,
    size_usable_somewhere,
)
from supply import JIT_CONFIG_VARIABLE, SlurmUnavailable
from test_demand import (
    IN_PROGRESS_RUNS,
    NEWEST_RUNS,
    QUEUED_RUNS,
    REPO,
    history_path,
    job,
    jobs_answer,
    jobs_path,
    run,
    run_path,
    runs_answer,
)
from test_github_client import FakeGitHub, Reply
from test_plan import out_on, usable_on
from test_supply import JIT_CONFIG, FakeSlurm, runner

NOW = 1_800_000_000
PREFIX = "gha-test-"
MEDIUM = "slurm-runner-medium"
SMALL = "slurm-runner-small"
RUNNERS = f"{REPO}/actions/runners?per_page=100"
GENERATE_JIT_CONFIG = f"{REPO}/actions/runners/generate-jitconfig"


def memory_with(launch_records):
    """A daemon that has run for a while: these launch records, and the history page not due."""
    return RepositoryMemory(
        set(), 1, NOW + HISTORY_PAGE_INTERVAL_S, launch_records, NO_SIGHTINGS
    )


class CycleAgainstFakes(unittest.TestCase):
    """One repository, the fake Slurm and the fake GitHub, and a cycle at a time; no tests of its own."""

    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.github = GitHubClient("test-token")
        self.repo = Repository(
            name="WATonomous/infra-config",
            api_base_url=self.fake.base_url + REPO,
            runner_name_prefix=PREFIX,
        )
        self.slurm = FakeSlurm(NOW)
        for patcher in (
            mock.patch.object(supply.subprocess, "run", self.slurm.run),
            mock.patch.object(github_client, "MIN_SECONDS_BETWEEN_WRITES", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.fake.script(
            NEWEST_RUNS, Reply(200, body=runs_answer(run(1, "in_progress")))
        )
        self.fake.script(QUEUED_RUNS, Reply(200, body=runs_answer()))
        self.fake.script(IN_PROGRESS_RUNS, Reply(200, body=runs_answer()))
        self.fake.script(run_path(1), Reply(200, body=run(1, "in_progress")))
        self.fake.script(history_path(1), Reply(200, body=runs_answer()))
        self.runners = []  # the runners list as GitHub answers it, changed by the tests
        self.fake.script(
            RUNNERS, lambda headers: Reply(200, body={"runners": self.runners})
        )
        self.jit_config_statuses = []  # statuses of the next generate-jitconfig answers; 201 after
        self.fake.script(GENERATE_JIT_CONFIG, self.answer_generate_jit_config)

    def answer_generate_jit_config(self, request_headers):
        status = self.jit_config_statuses.pop(0) if self.jit_config_statuses else 201
        if status != 201:
            return Reply(status, body={"message": "Server Error"})
        return Reply(201, body={"runner": {"id": 9}, "encoded_jit_config": JIT_CONFIG})

    def jobs(self, *jobs_of_run_1):
        self.fake.script(jobs_path(1), Reply(200, body=jobs_answer(*jobs_of_run_1)))

    def cycle(self, memory, now=NOW):
        """One cycle starting at now; the daemon's clock during it is the fake Slurm's, which each sbatch
        moves on by its submit spacing."""
        self.github.start_cycle()
        self.slurm.now = now
        return run_cycle(
            self.github, self.repo, memory, now, clock=lambda: self.slurm.now
        )

    def submitted_size_labels(self):
        return [
            next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--comment="))
            for argv in self.slurm.commands("sbatch")
        ]

    def add_runner(self, state, status="online", busy=False, started_ago_s=30):
        """One of our allocations in Slurm and, with status, its registration."""
        name = f"{PREFIX}{len(self.slurm.jobs):012x}"
        started_at = "N/A" if state == "PENDING" else NOW - started_ago_s
        self.slurm.add_job(
            name, state, submitted_at=NOW - started_ago_s - 5, started_at=started_at
        )
        if status is not None:
            self.runners.append(
                {**runner(name, status, busy), "id": len(self.slurm.jobs)}
            )
        return name


class RunCycleTest(CycleAgainstFakes):
    def test_a_first_cycle_sends_one_runner_per_size_usable_nowhere(self):
        self.jobs(
            *(job(i, "queued", [MEDIUM]) for i in range(5)), job(9, "queued", [SMALL])
        )
        memory = self.cycle(empty_memory())
        self.assertEqual(self.submitted_size_labels(), [MEDIUM, SMALL])
        self.assertEqual(
            memory,
            RepositoryMemory(
                {1}, 1, NOW + HISTORY_PAGE_INTERVAL_S, NO_LAUNCH_RECORDS, NO_SIGHTINGS
            ),
        )
        # Supply before demand: a runner that took a job after the jobs were read would look free
        paths = [path for _, path, _ in self.fake.received]
        self.assertLess(paths.index(RUNNERS), paths.index(jobs_path(1)))

    def test_the_history_is_read_one_page_per_interval(self):
        self.jobs()
        memory = empty_memory()
        for now in (NOW, NOW + 1, NOW + HISTORY_PAGE_INTERVAL_S - 1):
            memory = self.cycle(memory, now)
        self.assertEqual(len(self.fake.requests_to(history_path(1))), 1)
        memory = self.cycle(memory, NOW + HISTORY_PAGE_INTERVAL_S)
        self.assertEqual(len(self.fake.requests_to(history_path(1))), 2)
        self.assertEqual(memory.next_history_page_at, NOW + 2 * HISTORY_PAGE_INTERVAL_S)

    def test_a_restart_in_the_middle_of_a_wave(self):
        busy = [self.add_runner("RUNNING", busy=True) for _ in range(6)]
        for _ in range(2):
            # Online and idle, and registered and waiting for a node: both can take a job
            self.add_runner("RUNNING")
            self.add_runner("PENDING", "offline")
        # Its registration is not listed yet: still on its way
        self.add_runner("PENDING", status=None)
        never_registered = self.add_runner("PENDING", status=None, started_ago_s=A_DAY)
        self.runners.append(runner(f"{PREFIX}leftover", "offline"))  # its sbatch failed
        self.runners.append(runner("slurm-thor-slurm1-747004"))  # the old daemon's
        self.jobs(
            *(
                job(i, "in_progress", [MEDIUM], runner_name=name)
                for i, name in enumerate(busy)
            ),
            *(job(100 + i, "queued", [MEDIUM]) for i in range(15)),
        )

        memory = self.cycle(
            empty_memory()
        )  # memory lost: every size starts usable nowhere

        # The busy runners show where the size works in this first cycle, so the whole gap goes out: 15 - 5
        self.assertTrue(size_usable_somewhere(memory.launch_records, MEDIUM))
        self.assertEqual(self.submitted_size_labels(), [MEDIUM] * 10)
        never_registered_id = next(
            slurm_job_id
            for slurm_job_id, each in self.slurm.jobs.items()
            if each["name"] == never_registered
        )
        self.assertEqual(
            self.slurm.commands("scancel"),
            [["scancel", "--ctld", "--state=PENDING", str(never_registered_id)]],
        )
        # No Slurm job has ended, so nothing on GitHub is deleted: only JIT configs are created
        self.assertEqual(
            {method for method, _, _ in self.fake.received}, {"GET", "POST"}
        )
        self.assertEqual(
            {path for method, path, _ in self.fake.received if method == "POST"},
            {GENERATE_JIT_CONFIG},
        )

    def test_supply_that_cannot_be_read_changes_nothing(self):
        # Would be cancelled
        self.add_runner("PENDING", status=None, started_ago_s=A_DAY)
        self.jobs(job(1, "queued", [MEDIUM]))

        self.slurm.failing_commands.add("squeue")
        with self.assertRaises(SlurmUnavailable):
            self.cycle(empty_memory())
        self.slurm.failing_commands.clear()

        self.fake.script(RUNNERS, Reply(502, body={"message": "Server Error"}))
        with self.assertRaises(GitHubUnavailable):
            self.cycle(empty_memory())

        self.assertEqual(self.slurm.commands("scancel"), [])
        self.assertEqual(self.slurm.commands("sbatch"), [])
        self.assertEqual(self.fake.requests_to(GENERATE_JIT_CONFIG), [])

    def test_submissions_alternate_between_labels_and_a_failed_one_is_skipped(self):
        memory = memory_with(usable_on("delta-slurm1", MEDIUM, SMALL))
        self.jobs(
            *(job(i, "queued", [MEDIUM]) for i in range(3)),
            *(job(10 + i, "queued", [SMALL]) for i in range(2)),
        )
        self.jit_config_statuses = [201, 500]
        with self.assertLogs(level=logging.WARNING) as logs:
            self.cycle(memory)
        # Planned: medium, small, medium, small, medium; the first small failed at GitHub
        self.assertEqual(self.submitted_size_labels(), [MEDIUM, MEDIUM, SMALL, MEDIUM])
        self.assertEqual(len(self.fake.requests_to(GENERATE_JIT_CONFIG)), 5)
        self.assertIn(f"could not submit a runner for {SMALL}", logs.output[0])

    def test_a_second_sbatch_that_leaves_no_job_stops_the_cycle(self):
        # slurmctld does not answer: squeue shows nothing by the runner's name, and it named no node
        records = usable_on("delta-slurm1", MEDIUM)
        self.jobs(*(job(i, "queued", [MEDIUM]) for i in range(3)))
        self.slurm.failing_commands.add("sbatch")
        # Deleting the registrations made for them fails too
        self.fake.script(
            f"{REPO}/actions/runners/9", Reply(500, body={"message": "Server Error"})
        )
        with self.assertLogs(level=logging.WARNING) as logs:
            memory = self.cycle(memory_with(records))
        # The first skips only its own entry; the second stops the cycle, so the third is not tried
        self.assertEqual(len(self.slurm.commands("sbatch")), 2)
        deletes = [path for method, path, _ in self.fake.received if method == "DELETE"]
        self.assertEqual(deletes, [f"{REPO}/actions/runners/9"] * 2)
        self.assertEqual(
            sum("Could not delete the registration" in line for line in logs.output), 2
        )
        self.assertIn("no more submissions this cycle", logs.output[-1])
        # Nothing is counted against the size or any node, and sinfo is not asked
        self.assertEqual(memory.launch_records, records)
        self.assertEqual(self.slurm.commands("sinfo"), [])
        self.assertEqual(memory.watched_run_ids, {1})

    def test_a_refusal_is_stamped_after_the_runners_accepted_before_it(self):
        # U1 is accepted; U2 is refused for its constraints (every node it may use is excluded)
        records = out_on(
            usable_on("thor-slurm1", MEDIUM), "trpro-slurm1", MEDIUM, seen_at=NOW - 30
        )
        self.slurm.submit_spacing_s = 1
        self.slurm.sbatch_refused_after = 1
        self.jobs(*(job(i, "queued", [MEDIUM]) for i in range(4)))
        with self.assertLogs(level=logging.WARNING):
            memory = self.cycle(memory_with(records))
        self.assertEqual(len(self.slurm.commands("sbatch")), 2)
        # Next cycle U1 has taken a job; U1 was submitted before the refusal, so the size stays usable
        # nowhere and gets one runner, not the gap
        u1, _ = self.slurm.jobs
        self.slurm.jobs[u1].update(
            state="RUNNING", node="thor-slurm1", started_at=NOW + 2
        )
        self.runners.append(
            {**runner(self.slurm.jobs[u1]["name"], busy=True), "id": u1}
        )
        self.slurm.sbatch_refused_after = None
        before = len(self.slurm.commands("sbatch"))
        self.cycle(memory, NOW + 30)
        self.assertEqual(len(self.slurm.commands("sbatch")) - before, 1)

    def test_the_jit_config_reaches_sbatch_only_through_the_environment(self):
        self.jobs(job(1, "queued", [MEDIUM]))
        with self.assertLogs(level=logging.DEBUG) as logs:
            self.cycle(empty_memory())
        ((argv, added_environment),) = [
            call for call in self.slurm.calls if call[0][0] == "sbatch"
        ]
        self.assertEqual(added_environment[JIT_CONFIG_VARIABLE], JIT_CONFIG)
        self.assertFalse(any(JIT_CONFIG in arg for arg in argv))
        self.assertFalse(any(JIT_CONFIG in line for line in logs.output))


class NodeRuleScenarioTest(CycleAgainstFakes):
    """The node rule over many passes, against a fake Slurm with named nodes and first-fit placement. A
    runner that starts on a node either dies there before the next pass, or takes a waiting job and
    finishes it two passes later. 30 s between passes; submissions 1 s apart, as the daemon spaces them."""

    def setUp(self):
        super().setUp()
        # Medium runners take 2 CPUs: four fit on each. trpro-slurm1 is Slurm's first choice, as on 11-14
        # trpro-slurm1 and trpro-slurm2 make squeue print trpro-slurm[1-2] in %x
        self.slurm.nodes = {"trpro-slurm1": 8, "thor-slurm1": 8, "trpro-slurm2": 8}
        self.slurm.submit_spacing_s = 1
        self.now = NOW
        self.github_jobs = []
        self.took_a_job_in = {}  # Slurm job id -> the pass in which it took a job
        self.pass_index = 0

    def queue(self, count):
        first = len(self.github_jobs)
        self.github_jobs += [job(first + i, "queued", [MEDIUM]) for i in range(count)]

    def passes(self, memory, count, kills=frozenset(), kills_every_other=frozenset()):
        """count passes; before each, the runners that started earlier act on their node. The memory, and
        per pass the sbatch argv sent in it."""
        sent_per_pass = []
        for _ in range(count):
            self.slurm.now = self.now
            self.act(kills, kills_every_other)
            self.slurm.start_pending_jobs()
            listed = {entry["name"] for entry in self.runners}
            for slurm_job_id, each in self.slurm.jobs.items():
                if each["name"] not in listed and each["state"] not in FINISHED_STATES:
                    entry = {**runner(each["name"], "offline"), "id": slurm_job_id}
                    self.runners.append(entry)
            self.jobs(*self.github_jobs)
            before = len(self.slurm.commands("sbatch"))
            memory = self.cycle(memory, self.now)
            sent_per_pass.append(self.slurm.commands("sbatch")[before:])
            self.now += 30
            self.pass_index += 1
        return memory, sent_per_pass

    def act(self, kills, kills_every_other):
        for slurm_job_id, each in sorted(self.slurm.jobs.items()):
            if each["state"] != "RUNNING" or each["started_at"] >= self.now:
                continue
            name, node = each["name"], each["node"]
            if slurm_job_id in self.took_a_job_in:
                if self.pass_index - self.took_a_job_in[slurm_job_id] >= 2:
                    self.end(slurm_job_id, "COMPLETED")
                    for github_job in self.github_jobs:
                        if github_job["runner_name"] == name:
                            github_job["status"] = "completed"
                continue
            if node in kills or (node in kills_every_other and slurm_job_id % 2 == 0):
                self.end(slurm_job_id, "FAILED")
                continue
            queued = [j for j in self.github_jobs if j["status"] == "queued"]
            for entry in self.runners:
                if entry["name"] == name:
                    entry.update(status="online", busy=bool(queued))
            if queued:
                queued[0].update(status="in_progress", runner_name=name)
                self.took_a_job_in[slurm_job_id] = self.pass_index

    def end(self, slurm_job_id, state):
        name = self.slurm.jobs[slurm_job_id]["name"]
        self.slurm.end_job(slurm_job_id, state)
        self.runners = [entry for entry in self.runners if entry["name"] != name]

    def served(self):
        return all(j["status"] != "queued" for j in self.github_jobs)

    def placements(self, sent):
        """Where each sbatch of a pass asked to go: "pinned:<node>", "excluding:<nodes>" or "anywhere"."""
        where = []
        for argv in sent:
            options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
            if "--nodelist" in options:
                where.append(f"pinned:{options['--nodelist']}")
            elif "--exclude" in options:
                where.append(f"excluding:{options['--exclude']}")
            else:
                where.append("anywhere")
        return where

    def runners_started_on(self, node):
        return sum(
            1
            for each in self.slurm.jobs.values()
            if each["node"] == node and each["started_at"] != "N/A"
        )

    def test_restart_next_to_a_node_that_kills_every_runner(self):
        self.queue(12)
        memory, sent = self.passes(empty_memory(), 8, kills={"trpro-slurm1"})
        where = [self.placements(each) for each in sent]
        # Usable nowhere: one runner, which Slurm puts on trpro-slurm1, where it dies
        self.assertEqual(where[0], ["anywhere"])
        # Still usable nowhere: one runner, away from it; it takes a job
        self.assertEqual(where[1], ["excluding:trpro-slurm1"])
        # Usable somewhere: the rest of the wave, away from trpro-slurm1
        self.assertEqual(where[2], ["excluding:trpro-slurm1"] * 11)
        # A minute after the failure was seen, one pinned retry while jobs wait; it dies too
        self.assertEqual(where[3], ["pinned:trpro-slurm1"])
        self.assertEqual(self.runners_started_on("trpro-slurm1"), 2)
        self.assertEqual(nodes_out(memory.launch_records, MEDIUM), {"trpro-slurm1"})
        self.assertTrue(self.served())

    def test_a_node_that_fails_some_runners_is_used_again_after_each_success(self):
        memory = memory_with(usable_on("thor-slurm1", MEDIUM))
        self.slurm.nodes = {"trpro-slurm1": 8, "thor-slurm1": 4}
        self.queue(16)
        with self.assertLogs(level=logging.INFO) as logs:
            memory, sent = self.passes(memory, 20, kills_every_other={"trpro-slurm1"})
        lines = "\n".join(logs.output)
        self.assertIn(f"{MEDIUM}: out on trpro-slurm1", lines)
        self.assertIn(f"{MEDIUM}: usable again on trpro-slurm1", lines)
        self.assertTrue(self.served())
        # Never more than a wave on it while it was out
        excluded = sum(
            w == "excluding:trpro-slurm1"
            for each in sent
            for w in self.placements(each)
        )
        self.assertGreater(excluded, 0)

    def test_a_size_that_fails_everywhere_gets_one_runner_at_a_time(self):
        self.queue(20)
        everywhere = set(self.slurm.nodes)
        memory, sent = self.passes(empty_memory(), 40, kills=everywhere)
        self.assertLessEqual(max(len(each) for each in sent), 1)
        where = [w for each in sent for w in self.placements(each)]
        self.assertFalse(any(w.startswith("pinned") for w in where))
        self.assertFalse(size_usable_somewhere(memory.launch_records, MEDIUM))
        # 20 minutes: each node fails once, then once per wait (1, 2, 4, 8 minutes)
        started = sum(self.runners_started_on(node) for node in everywhere)
        self.assertGreaterEqual(started, 3)
        self.assertLessEqual(started, 3 * 5)

    def test_a_fault_on_every_node_then_recovery_releases_the_waiting_runners(self):
        # Every node went out for the size, and the runners submitted meanwhile exclude all of them
        records = NO_LAUNCH_RECORDS
        for node in self.slurm.nodes:
            records = out_on(records, node, MEDIUM, seen_at=NOW - 30)
        everything = ",".join(sorted(self.slurm.nodes))
        stuck = [
            self.slurm.add_job(f"{PREFIX}stuck{i}", excluded=everything, cpus=2)
            for i in range(6)
        ]
        self.queue(6)
        self.assertIn("|thor-slurm1,trpro-slurm[1-2]\n", self.slurm.squeue_output())
        # The fault is gone. As each node's wait passes, the waiting runners are released onto it
        memory, sent = self.passes(memory_with(records), 8)
        self.assertTrue(
            all(self.slurm.jobs[each]["started_at"] != "N/A" for each in stuck)
        )
        self.assertTrue(self.served())
        self.assertTrue(size_usable_somewhere(memory.launch_records, MEDIUM))
        self.assertTrue(self.slurm.commands("scontrol"))

    def test_a_node_slurm_no_longer_knows_is_forgotten_for_every_size(self):
        # wato3-slurm1 went out, then was removed from Slurm: sbatch fails for any runner naming it, and
        # leaves no job
        records = usable_on("thor-slurm1", MEDIUM, SMALL)
        for size_label in (MEDIUM, SMALL):
            records = out_on(records, "wato3-slurm1", size_label, seen_at=NOW - 3600)
        records = out_on(records, "trpro-slurm2", MEDIUM, seen_at=NOW)
        self.queue(4)
        with self.assertLogs(level=logging.INFO) as logs:
            memory, sent = self.passes(memory_with(records), 3)
        where = [self.placements(each) for each in sent]
        # Its retry fails; sinfo does not list it, so it is forgotten, and the runners planned naming it
        # are not sent
        self.assertEqual(where[0], ["pinned:wato3-slurm1"])
        self.assertIn(
            "Slurm does not know wato3-slurm1, forgotten for every size",
            "\n".join(logs.output),
        )
        for size_label in (MEDIUM, SMALL):
            record = memory.launch_records.by_node_and_size[
                ("wato3-slurm1", size_label)
            ]
            self.assertIsNotNone(record.forgotten_at)
        # Only that node: the size is still usable and the sick node still excluded
        self.assertEqual(nodes_out(memory.launch_records, MEDIUM), {"trpro-slurm2"})
        self.assertEqual(where[1], ["excluding:trpro-slurm2"] * 4)
        self.assertTrue(self.served())

    def test_one_blip_in_a_wave_changes_no_record_and_the_wave_goes_on(self):
        # The third sbatch of the wave gets no answer from slurmctld and leaves no job
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "trpro-slurm1", MEDIUM, seen_at=NOW)
        self.slurm.sbatch_unreachable_calls = {3}
        self.queue(6)
        memory = memory_with(records)
        with self.assertLogs(level=logging.WARNING) as logs:
            memory, sent = self.passes(memory, 1)
        self.assertEqual(self.placements(sent[0]), ["excluding:trpro-slurm1"] * 6)
        self.assertEqual(sum("not counted" in line for line in logs.output), 1)
        self.assertFalse(any("no more submissions" in line for line in logs.output))
        # sinfo lists the sick node it excluded: not forgotten, and nothing counted against the size
        self.assertEqual(nodes_out(memory.launch_records, MEDIUM), {"trpro-slurm1"})
        self.assertTrue(size_usable_somewhere(memory.launch_records, MEDIUM))
        self.assertEqual(len(self.slurm.commands("sinfo")), 1)
        # Next cycle the one job left without a runner gets one, still away from the sick node
        memory, sent = self.passes(memory, 1)
        self.assertEqual(self.placements(sent[0]), ["excluding:trpro-slurm1"])

    def test_the_count_of_blips_starts_again_each_cycle(self):
        records = usable_on("thor-slurm1", MEDIUM)
        # One blip in each of two cycles, each on the cycle's first sbatch: neither stops its cycle
        self.slurm.sbatch_unreachable_calls = {1, 7}
        self.queue(6)
        with self.assertLogs(level=logging.WARNING) as logs:
            memory, first = self.passes(memory_with(records), 1)
            self.queue(3)
            memory, second = self.passes(memory, 1)
        self.assertEqual([len(first[0]), len(second[0])], [6, 4])
        self.assertFalse(any("no more submissions" in line for line in logs.output))

    def test_when_sinfo_cannot_answer_nothing_is_forgotten(self):
        records = usable_on("thor-slurm1", MEDIUM)
        records = out_on(records, "wato3-slurm1", MEDIUM, seen_at=NOW - 3600)
        self.slurm.failing_commands.add("sinfo")
        self.queue(2)
        with self.assertLogs(level=logging.WARNING) as logs:
            memory, sent = self.passes(memory_with(records), 1)
        # Both name the removed node; without sinfo each is a blip, and the second stops the cycle
        self.assertEqual(
            self.placements(sent[0]), ["pinned:wato3-slurm1", "excluding:wato3-slurm1"]
        )
        self.assertIn(
            "Could not ask Slurm which nodes it knows", "\n".join(logs.output)
        )
        self.assertEqual(memory.launch_records, records)


class DeleteEndedRunnersTest(CycleAgainstFakes):
    """A stopped runner never signs off, and GitHub offers its session jobs until its registration goes."""

    def ended_runner(self, status="offline", busy=False):
        name = self.add_runner("COMPLETED", status, busy)
        slurm_job_id = next(
            slurm_job_id
            for slurm_job_id, each in self.slurm.jobs.items()
            if each["name"] == name
        )
        self.slurm.jobs[slurm_job_id]["ended_at"] = NOW - 5
        return name, f"{REPO}/actions/runners/{len(self.slurm.jobs)}"

    def deletes(self):
        return [path for method, path, _ in self.fake.received if method == "DELETE"]

    def test_an_ended_runners_idle_registration_is_deleted_once(self):
        _, path = self.ended_runner()
        # The runners list may still show it a cycle later
        self.fake.script(path, Reply(204, body=None))
        self.jobs()
        memory = self.cycle(empty_memory())
        self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path])

    def test_never_a_busy_pending_or_running_runners_nor_one_absent_from_squeue(self):
        self.ended_runner(busy=True)
        self.add_runner("PENDING", "offline")
        self.add_runner("RUNNING")
        # Its sbatch has not happened yet, or another daemon's
        self.runners.append({**runner(f"{PREFIX}not-in-squeue", "offline"), "id": 99})
        self.jobs()
        self.cycle(empty_memory())
        self.assertEqual(self.deletes(), [])

    def test_a_refused_delete_is_left_alone_and_logged_once(self):
        name, path = self.ended_runner()
        self.fake.script(path, Reply(422, body={"message": "Runner is busy"}))
        self.jobs()
        with self.assertLogs(level=logging.WARNING) as logs:
            memory = self.cycle(empty_memory())
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path])
        self.assertEqual(len([line for line in logs.output if name in line]), 1)

    def test_a_failed_delete_does_not_stop_the_cycle_and_is_tried_again(self):
        _, path = self.ended_runner()
        self.fake.script(path, Reply(500, body={"message": "Server Error"}))
        self.jobs(job(1, "queued", [MEDIUM]))
        memory = memory_with(usable_on("delta-slurm1", MEDIUM))
        with self.assertLogs(level=logging.WARNING):
            memory = self.cycle(memory)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])
        with self.assertLogs(level=logging.WARNING):
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.deletes(), [path, path])


class SubmitAsRunsAreReadTest(CycleAgainstFakes):
    """Runners go out after each run's jobs are read, newest run first, without counting a job twice."""

    def setUp(self):
        super().setUp()
        self.fake.script(
            NEWEST_RUNS,
            Reply(200, body=runs_answer(run(2, "in_progress"), run(1, "in_progress"))),
        )
        self.fake.script(run_path(2), Reply(200, body=run(2, "in_progress")))

    def jobs_of(self, run_id, *jobs):
        self.fake.script(jobs_path(run_id), Reply(200, body=jobs_answer(*jobs)))

    def test_the_newest_runs_jobs_get_runners_before_the_next_run_is_read(self):
        self.jobs_of(2, *(job(20 + i, "queued", [MEDIUM]) for i in range(3)))
        self.jobs_of(1, *(job(10 + i, "queued", [MEDIUM]) for i in range(2)))
        self.cycle(memory_with(usable_on("delta-slurm1", MEDIUM)))
        paths = [path for _, path, _ in self.fake.received]
        first_jit_config = paths.index(GENERATE_JIT_CONFIG)
        self.assertLess(paths.index(RUNNERS), paths.index(jobs_path(2)))
        self.assertLess(paths.index(jobs_path(2)), first_jit_config)
        self.assertLess(first_jit_config, paths.index(jobs_path(1)))
        self.assertEqual(self.submitted_size_labels(), [MEDIUM] * 5)

    def test_launch_evidence_is_weighed_even_with_no_run_to_read(self):
        self.fake.script(NEWEST_RUNS, Reply(200, body=runs_answer()))
        failed = self.slurm.add_job(f"{PREFIX}a", "FAILED", started_at=NOW - 20)
        self.slurm.jobs[failed]["ended_at"] = NOW - 10
        memory = self.cycle(memory_with(usable_on("delta-slurm1", MEDIUM)))
        self.assertEqual(nodes_out(memory.launch_records, MEDIUM), {"delta-slurm1"})

    def test_waiting_runners_exclusions_are_narrowed_once_per_pass_not_per_run(self):
        self.jobs_of(2, job(20, "queued", [MEDIUM]))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        first = self.slurm.add_job(f"{PREFIX}a", excluded="trpro-slurm1,trpro-slurm2")
        second = self.slurm.add_job(f"{PREFIX}b", excluded="trpro-slurm2,trpro-slurm1")
        with self.assertLogs(level=logging.INFO) as logs:
            self.cycle(memory_with(usable_on("delta-slurm1", MEDIUM)))
        # No runner is submitted with an exclusion yet, so the ones found are cleared, in one call
        self.assertEqual(
            self.slurm.commands("scontrol"),
            [["scontrol", "update", f"JobId={first},{second}", "ExcNodeList="]],
        )
        self.assertIn(
            f"{MEDIUM}: narrowed the exclusion of 2 pending runners from"
            " trpro-slurm1,trpro-slurm2 to none",
            "\n".join(logs.output),
        )

    def test_a_sbatch_that_timed_out_counts_as_in_flight(self):
        # Its job may exist: a size usable nowhere gets no second runner in the cycle
        self.jobs_of(2, job(20, "queued", [MEDIUM]))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.slurm.sbatch_times_out = True
        with self.assertLogs(level=logging.WARNING):
            self.cycle(empty_memory())
        self.assertEqual(len(self.slurm.commands("sbatch")), 1)

    def test_rule_4_uses_this_cycles_evidence(self):
        # Last cycle trpro-slurm1 was out; this cycle squeue shows a newer runner there taking a job
        records = out_on(
            usable_on("thor-slurm1", MEDIUM), "trpro-slurm1", MEDIUM, seen_at=NOW - 300
        )
        self.slurm.add_job(f"{PREFIX}waiting", excluded="trpro-slurm1")
        took = self.slurm.add_job(
            f"{PREFIX}took", "RUNNING", node="trpro-slurm1", started_at=NOW - 20
        )
        self.runners.append({**runner(f"{PREFIX}took", busy=True), "id": took})
        self.cycle(memory_with(records))
        (update,) = self.slurm.commands("scontrol")
        self.assertEqual(update[-1], "ExcNodeList=")

    def test_evidence_filed_while_runs_are_read_is_stamped_when_it_is_filed(self):
        # Run 2's jobs get runner U on thor-slurm1. Run 1's old job then shows that GitHub left the idle
        # runner R there for over B: thor-slurm1 goes out after U went out, so U's failure there is the
        # same event, not a failed retry that would double the wait
        self.slurm.nodes = {"thor-slurm1": 8}
        self.slurm.submit_spacing_s = 1
        r = self.slurm.add_job(
            f"{PREFIX}r",
            "RUNNING",
            submitted_at=NOW - 2 * B - 20,
            started_at=NOW - 2 * B - 10,
            node="thor-slurm1",
        )
        self.runners.append({**runner(f"{PREFIX}r"), "id": r})
        self.jobs_of(
            2, *(job(20 + i, "queued", [MEDIUM], created_at=NOW - 10) for i in range(2))
        )
        self.jobs_of(1, job(10, "queued", [MEDIUM], created_at=NOW - B - 30))
        memory = self.cycle(memory_with(usable_on("thor-slurm1", MEDIUM)))
        (u,) = [each for each in self.slurm.jobs if each != r]
        self.assertEqual(self.slurm.jobs[u]["node"], "thor-slurm1")
        self.slurm.now = NOW + 20
        self.slurm.end_job(u, "FAILED")
        memory = self.cycle(memory, NOW + 30)
        record = memory.launch_records.by_node_and_size[("thor-slurm1", MEDIUM)]
        self.assertEqual(record.newest_failure_slurm_job_id, u)
        self.assertEqual(record.failed_retries, 0)

    def test_after_slurm_is_taken_as_not_answering_later_runs_submit_nothing(self):
        self.jobs_of(2, *(job(20 + i, "queued", [MEDIUM]) for i in range(2)))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.slurm.failing_commands.add("sbatch")
        with self.assertLogs(level=logging.WARNING):
            memory = self.cycle(memory_with(usable_on("thor-slurm1", MEDIUM)))
        # Both of run 2's runners leave no job; run 1 is still read, and nothing is submitted for it
        self.assertEqual(len(self.slurm.commands("sbatch")), 2)
        self.assertIn(jobs_path(1), [path for _, path, _ in self.fake.received])
        self.assertEqual(memory.watched_run_ids, {1, 2})

    def test_each_run_is_planned_when_it_is_read(self):
        # A wave of 40 runners takes 40 s; trpro-slurm1's retry comes due 30 s into the cycle
        records = out_on(
            usable_on("thor-slurm1", MEDIUM), "trpro-slurm1", MEDIUM, seen_at=NOW - 30
        )
        self.slurm.submit_spacing_s = 1
        self.jobs_of(2, *(job(100 + i, "queued", [MEDIUM]) for i in range(40)))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.cycle(memory_with(records))
        pinned = [argv for argv in self.slurm.commands("sbatch") if "--nodes=1" in argv]
        self.assertEqual(len(pinned), 1)
        self.assertIn("--nodelist=trpro-slurm1", pinned[0])

    def test_runners_are_judged_as_the_runners_list_showed_them(self):
        # The runners list is read at the cycle start, when a runner on thor-slurm1 is 100 s old and
        # still booting; the run is read 40 s later, when it would be past B
        self.add_runner("RUNNING", status="offline", started_ago_s=100)
        self.slurm.jobs[max(self.slurm.jobs)]["node"] = "thor-slurm1"
        self.jobs_of(2)
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.github.start_cycle()
        memory = run_cycle(
            self.github,
            self.repo,
            memory_with(usable_on("thor-slurm1", MEDIUM)),
            NOW,
            clock=lambda: NOW + 40,
        )
        # Still booting: no failed launch, no duplicate runner
        self.assertEqual(self.slurm.commands("sbatch"), [])
        self.assertEqual(nodes_out(memory.launch_records, MEDIUM), set())
        self.assertTrue(size_usable_somewhere(memory.launch_records, MEDIUM))

    def test_a_size_usable_nowhere_still_gets_one_runner_in_the_whole_cycle(self):
        self.jobs_of(2, job(20, "queued", [MEDIUM]))
        self.jobs_of(1, job(10, "queued", [MEDIUM]))
        self.cycle(empty_memory())
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])


class BusyRunnerCoversItsJobTest(CycleAgainstFakes):
    """A runner GitHub has offered a job covers it until the job names it, for at most T."""

    def test_a_cycle_inside_the_gap_submits_nothing_extra_until_t(self):
        self.add_runner("RUNNING", busy=True)
        # Its job, still queued and naming no runner
        self.jobs(job(1, "queued", [MEDIUM]))
        memory = memory_with(usable_on("delta-slurm1", MEDIUM))
        # The first cycle after a restart sees it busy for the first time, and counts on it from then
        memory = self.cycle(memory, NOW)
        memory = self.cycle(memory, NOW + T - 1)
        self.assertEqual(self.submitted_size_labels(), [])
        self.cycle(memory, NOW + T)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])

    def test_a_runner_once_named_does_not_cover_when_its_runs_read_fails(self):
        named = self.add_runner("RUNNING", busy=True)
        self.fake.script(
            NEWEST_RUNS,
            Reply(200, body=runs_answer(run(2, "in_progress"), run(1, "in_progress"))),
        )
        self.fake.script(run_path(2), Reply(200, body=run(2, "in_progress")))
        self.jobs(job(1, "in_progress", [MEDIUM], runner_name=named))
        self.fake.script(jobs_path(2), Reply(200, body=jobs_answer()))
        memory = self.cycle(memory_with(usable_on("delta-slurm1", MEDIUM)))
        self.fake.script(jobs_path(1), Reply(502, body={"message": "Server Error"}))
        self.fake.script(
            jobs_path(2), Reply(200, body=jobs_answer(job(2, "queued", [MEDIUM])))
        )
        with self.assertLogs(level=logging.WARNING):
            self.cycle(memory, NOW + 2)
        self.assertEqual(self.submitted_size_labels(), [MEDIUM])


class OutageLogTest(unittest.TestCase):
    def test_lines_when_cycles_stop_change_cause_remind_and_recover(self):
        rate_limited = GitHubUnavailable(
            "GET runners: HTTP 403 (API rate limit exceeded)"
        )
        bad_token = GitHubUnavailable("GET runners: HTTP 401 (Bad credentials)")
        slurm_down = SlurmUnavailable("squeue: exit status 1")
        self.assertEqual(outage_after_cycle(None, None, NOW), (None, None))
        # A cycle every 2 s: rate limited, then a bad token from 300 s, then Slurm down from 1,200 s
        outage, lines = None, []
        for now in range(NOW, NOW + 1802, 2):
            error = rate_limited if now < NOW + 300 else bad_token
            error = slurm_down if now >= NOW + 1200 else error
            outage, line = outage_after_cycle(outage, error, now)
            if line is not None:
                lines.append((now - NOW, line))
        outage, line = outage_after_cycle(outage, None, NOW + 1802)
        self.assertIsNone(outage)
        lines.append((1802, line))
        self.assertEqual(
            lines,
            [
                (0, f"cycles stopped, GitHub cannot be read: {rate_limited}"),
                # The change of cause within GitHub shows at the next reminder
                (600, f"cycles still stopped after 600 s and 301 cycles: {bad_token}"),
                (1200, f"cycles still stopped, now Slurm cannot be read: {slurm_down}"),
                (
                    1800,
                    f"cycles still stopped after 1800 s and 901 cycles: {slurm_down}",
                ),
                (
                    1802,
                    f"cycles recovered after 1802 s and 901 failed cycles; the last: {slurm_down}",
                ),
            ],
        )


class UnsafeSettingsTest(unittest.TestCase):
    def repos(self, *prefixes):
        return [
            Repository(
                f"o/r{index}", f"https://api.github.com/repos/o/r{index}", prefix
            )
            for index, prefix in enumerate(prefixes)
        ]

    def test_b_must_stay_below_w(self):
        self.assertEqual(unsafe_settings(120, 600, self.repos("gha-a-")), [])
        for boot_allowance_s in (600, 900):
            with self.subTest(boot_allowance_s=boot_allowance_s):
                self.assertEqual(
                    len(unsafe_settings(boot_allowance_s, 600, self.repos("gha-a-"))), 1
                )

    def test_no_repository_may_call_another_ones_runners_its_own(self):
        self.assertEqual(unsafe_settings(120, 600, self.repos("gha-a-", "gha-b-")), [])
        for prefixes in (("gha-a-", "gha-a-"), ("gha-a", "gha-a-b-"), ("", "gha-b-")):
            with self.subTest(prefixes=prefixes):
                self.assertTrue(unsafe_settings(120, 600, self.repos(*prefixes)))

    def test_the_daemon_refuses_to_start_with_b_at_or_above_w(self):
        with (
            mock.patch.object(main, "BOOT_ALLOWANCE_S", W),
            mock.patch.object(main, "configure_logging"),
            mock.patch.object(main, "GitHubClient") as client,
        ):
            with self.assertLogs(level=logging.ERROR), self.assertRaises(SystemExit):
                main.main()
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
