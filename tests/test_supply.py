"""supply without Slurm or GitHub: the parsers by table, the commands against a fake Slurm, the runners list
and the JIT config against the fake GitHub."""

import json
import logging
import re
import subprocess
import unittest
from unittest import mock

import github_client
import supply
from config import (
    ALLOCATE_RUNNER_SCRIPT_PATH,
    BOOT_ALLOWANCE_S as B,
    IDLE_ALLOWANCE_S as W,
    RUNNER_GROUP_ID,
    SLURM_COMMAND_TIMEOUT,
    UNUSED_REGISTRATION_LIFETIME_S,
)
from github_client import GitHubClient, GitHubUnavailable
from plan import (
    FINISHED_STATES,
    Allocation,
    Phase,
    RunnerRegistration,
    Submission,
    phase_of_allocation,
)
from runner_size_config import RunnerResources
from supply import (
    JIT_CONFIG_VARIABLE,
    SlurmTimedOut,
    SbatchLeftNoJob,
    SlurmUnavailable,
    SubmissionRefused,
    cancel_pending_allocation,
    delete_registration,
    parse_registration,
    parse_squeue_line,
    read_our_allocations,
    read_our_registrations,
    sbatch_command,
    narrow_pending_exclusions,
    node_names_in_hostlist,
    nodes_slurm_knows,
    submit_runner,
)
from test_github_client import FakeGitHub, Reply

NOW = 1_800_000_000
PREFIX = "gha-test-"
REPO = "/repos/WATonomous/infra-config"
MEDIUM = "slurm-runner-medium"
# A medium runner that Slurm may put on any node
ANYWHERE = Submission(MEDIUM, frozenset())
# Stands in for a real, secret config
JIT_CONFIG = "eyJzZWNyZXQiOiAianVzdC1pbi10aW1lIn0="


# The compute nodes sinfo lists when a test gives FakeSlurm none
CLUSTER_NODES = (
    "delta-slurm1",
    "thor-slurm1",
    "tr-slurm2",
    "trpro-slurm1",
    "trpro-slurm2",
    "wato2-slurm1",
)


class FakeSlurm:
    """squeue, sbatch, scontrol update and scancel as supply calls them through subprocess.run, over a dict
    of jobs. With nodes set, sbatch places jobs as slurmctld does on our cluster: names checked first, a
    request no allowed node can ever hold refused with a FAILED record, then first fit in node order."""

    def __init__(self, now=NOW, nodes=None):
        self.now = now
        # Node name -> CPUs, in slurmctld's order; None: sbatch only queues the job, unplaced
        self.nodes = nodes
        # Take no new job; a job pinned to one is accepted and pends
        self.drained = set()
        # Seconds each sbatch moves now on before it is submitted, as the daemon spaces its JIT POSTs; now
        # is also the clock the daemon stamps refusals and evidence with during a cycle
        self.submit_spacing_s = 0
        # The sbatch calls, counted from 1, that get no answer from slurmctld and leave no job
        self.sbatch_unreachable_calls = set()
        # sbatch calls after this many are refused with a FAILED record; None: never
        self.sbatch_refused_after = None
        # sinfo exits 0 and prints nothing
        self.sinfo_lists_nothing = False
        self.jobs = {}  # Slurm job id -> squeue fields after the id
        self.next_slurm_job_id = 812000
        self.calls = []  # (argv, the environment added to the daemon's)
        self.failing_commands = set()
        # sbatch exits non-zero after slurmctld created the job anyway, as after a timeout on its answer
        self.sbatch_fails_after_creating_its_job = False
        # sbatch gets no answer within SLURM_COMMAND_TIMEOUT; its request may still be queued in slurmctld
        self.sbatch_times_out = False
        # every eligible node excluded: sbatch exits 1, and slurmctld leaves a FAILED record with no node
        self.sbatch_refuses_leaving_a_failed_record = False

    def add_job(
        self,
        name,
        state="PENDING",
        size_label=MEDIUM,
        priority=4294,
        submitted_at=None,
        started_at="N/A",
        ended_at="N/A",
        node=None,
        pinned_node="",
        excluded="",
        cpus=1,
        excluded_as_printed=None,
    ):
        """node defaults to none while pending and delta-slurm1 once placed. excluded is a comma-separated
        list of node names, which squeue prints sorted and compressed (%x); excluded_as_printed replaces
        what squeue prints."""
        if node is None:
            node = "" if state == "PENDING" else "delta-slurm1"
        slurm_job_id = self.next_slurm_job_id
        self.next_slurm_job_id += 1
        self.jobs[slurm_job_id] = {
            "name": name,
            "comment": size_label,
            "state": state,
            "priority": priority,
            "submitted_at": self.now if submitted_at is None else submitted_at,
            "started_at": started_at,
            "ended_at": ended_at,
            "node": node,
            "pinned_node": pinned_node,
            "excluded": frozenset(filter(None, excluded.split(","))),
            "excluded_as_printed": excluded_as_printed,
            "cpus": cpus,
        }
        return slurm_job_id

    def sbatch_on_nodes(self, argv, options):
        excluded = set(filter(None, options.get("--exclude", "").split(",")))
        pinned = options.get("--nodelist")
        if excluded - set(self.nodes) or (pinned and pinned not in self.nodes):
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "sbatch: error: Batch job submission failed: Invalid node name specified",
            )
        name, size_label = options["--job-name"], options["--comment"]
        cpus = int(options["--cpus-per-task"])
        submitted_at = self.now
        if not self.nodes_allowed(cpus, excluded, pinned or ""):
            # As job 753319: squeue lists it for MinJobAge, submit = start = end, no node
            self.add_job(
                name,
                "FAILED",
                size_label=size_label,
                submitted_at=submitted_at,
                started_at=submitted_at,
                ended_at=submitted_at,
                node="",
                excluded=options.get("--exclude", ""),
            )
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "sbatch: error: Batch job submission failed:"
                " Requested node configuration is not available",
            )
        slurm_job_id = self.add_job(
            name,
            size_label=size_label,
            submitted_at=submitted_at,
            pinned_node=pinned or "",
            excluded=options.get("--exclude", ""),
            cpus=cpus,
        )
        self.start_pending_jobs()
        return subprocess.CompletedProcess(
            argv, 0, f"Submitted batch job {slurm_job_id}\n", ""
        )

    def refuse_leaving_a_failed_record(self, argv):
        """As job 753319 (2026-11-17): squeue lists it for MinJobAge, submit = start = end, no node."""
        options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
        self.add_job(
            options["--job-name"],
            state="FAILED",
            size_label=options["--comment"],
            started_at=self.now,
            ended_at=self.now,
            node="",
            excluded=options.get("--exclude", ""),
        )
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "sbatch: error: Batch job submission failed:"
            " Requested node configuration is not available",
        )

    def nodes_allowed(self, cpus, excluded, pinned):
        """The nodes that could ever hold such a job, in slurmctld's order."""
        return [
            node
            for node, node_cpus in self.nodes.items()
            if node not in excluded and pinned in ("", node) and node_cpus >= cpus
        ]

    def start_pending_jobs(self):
        """First fit, oldest job first, onto nodes not drained with the CPUs free."""
        for slurm_job_id, job in sorted(self.jobs.items()):
            if job["state"] != "PENDING" or job["priority"] == 0:
                continue
            for node in self.nodes_allowed(
                job["cpus"], job["excluded"], job["pinned_node"]
            ):
                in_use = sum(
                    other["cpus"]
                    for other in self.jobs.values()
                    if other["node"] == node and other["state"] == "RUNNING"
                )
                if (
                    node not in self.drained
                    and in_use + job["cpus"] <= self.nodes[node]
                ):
                    started_at = max(self.now, job["submitted_at"])
                    job.update(state="RUNNING", node=node, started_at=started_at)
                    break

    def end_job(self, slurm_job_id, state):
        self.jobs[slurm_job_id].update(state=state, ended_at=self.now)

    def run(self, argv, capture_output, text, timeout, env):
        added = {
            name: value
            for name, value in env.items()
            if name in (JIT_CONFIG_VARIABLE, "SLURM_TIME_FORMAT")
        }
        self.calls.append((list(argv), added))
        command = argv[0]
        if command == "sbatch":
            self.now += self.submit_spacing_s
            if len(self.commands("sbatch")) in self.sbatch_unreachable_calls:
                return subprocess.CompletedProcess(
                    argv, 1, "", "sbatch: error: Unable to contact slurm controller"
                )
            if (
                self.sbatch_refused_after is not None
                and len(self.commands("sbatch")) > self.sbatch_refused_after
            ):
                return self.refuse_leaving_a_failed_record(argv)
        if command in self.failing_commands:
            return subprocess.CompletedProcess(
                argv, 1, "", f"{command}: error: Unable to contact slurm controller"
            )
        options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
        if command == "squeue" and "--name" in options:
            fields = {"%i": "slurm_job_id", "%T": "state", "%N": "node"}
            lines = "".join(
                "|".join(
                    str({**job, "slurm_job_id": slurm_job_id}[fields[code]])
                    for code in options["--format"].split("|")
                )
                + "\n"
                for slurm_job_id, job in sorted(self.jobs.items())
                if job["name"] == options["--name"]
            )
            return subprocess.CompletedProcess(argv, 0, lines, "")
        if command == "squeue":
            return subprocess.CompletedProcess(argv, 0, self.squeue_output(), "")
        if command == "sbatch" and self.nodes is not None:
            return self.sbatch_on_nodes(argv, options)
        if command == "sbatch" and self.sbatch_times_out:
            raise subprocess.TimeoutExpired(argv, timeout)
        if command == "sbatch" and self.sbatch_refuses_leaving_a_failed_record:
            return self.refuse_leaving_a_failed_record(argv)
        if command == "sinfo":
            # Every node once per partition (two here), the login nodes too, never compressed
            names = [*(self.nodes or CLUSTER_NODES), "wato-login1", "wato-login2"]
            if self.sinfo_lists_nothing:
                names = []
            return subprocess.CompletedProcess(argv, 0, "\n".join(names * 2) + "\n", "")
        if command == "sbatch":
            slurm_job_id = self.add_job(
                options["--job-name"], size_label=options["--comment"]
            )
            if self.sbatch_fails_after_creating_its_job:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    "sbatch: error: Socket timed out on send/recv operation",
                )
            return subprocess.CompletedProcess(
                argv, 0, f"Submitted batch job {slurm_job_id}\n", ""
            )
        if command == "scontrol" and argv[1] == "update":
            # As slurmctld 24.05.1 did on the cluster: each pending job updated, each other refused
            refusals = []
            for slurm_job_id in map(int, options["JobId"].split(",")):
                job = self.jobs.get(slurm_job_id)
                if job is None:
                    refusals.append(f"Invalid job id specified for job {slurm_job_id}")
                elif job["state"] in FINISHED_STATES:
                    refusals.append(f"Job has already finished for job {slurm_job_id}")
                elif job["state"] != "PENDING":
                    refusals.append(
                        f"Job is no longer pending execution for job {slurm_job_id}"
                    )
                else:
                    job["excluded"] = frozenset(
                        filter(None, options["ExcNodeList"].split(","))
                    )
            if self.nodes is not None:
                self.start_pending_jobs()
            return subprocess.CompletedProcess(
                argv, 1 if refusals else 0, "", "\n".join(refusals)
            )
        if command == "scancel":
            # Only slurmctld can test the state atomically, so the fake records the call and changes nothing
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def squeue_output(self):
        return "".join(
            f"{slurm_job_id}|{job['name']}|{job['comment']}|{job['state']}|{job['priority']}"
            f"|{job['submitted_at']}|{job['started_at']}|{job['ended_at']}"
            f"|{job['node']}|{job['pinned_node']}"
            f"|{job['excluded_as_printed'] or compressed_hostlist(job['excluded'])}\n"
            for slurm_job_id, job in sorted(self.jobs.items())
        )

    def commands(self, command):
        return [argv for argv, _ in self.calls if argv[0] == command]


def compressed_hostlist(names):
    """A node list as slurmctld prints it in squeue's %x: sorted, and the trailing numbers of names that
    share a prefix compressed into one bracket, "tr-slurm2,trpro-slurm[1-2]"."""
    by_prefix = {}
    for name in sorted(names):
        prefix, digits = re.fullmatch(r"(.*?)(\d*)", name).groups()
        by_prefix.setdefault(prefix, []).append(digits)
    printed = []
    for prefix, numbers in by_prefix.items():
        if len(numbers) == 1:
            printed.append(prefix + numbers[0])
            continue
        ranges, first, last = [], numbers[0], numbers[0]
        for number in numbers[1:] + [None]:
            if number is not None and int(number) == int(last) + 1:
                last = number
                continue
            ranges.append(first if first == last else f"{first}-{last}")
            first = last = number
        printed.append(f"{prefix}[{','.join(ranges)}]")
    return ",".join(printed)


def runner(name, status="online", busy=False):
    return {
        "id": 7,
        "name": name,
        "os": "Linux",
        "status": status,
        "busy": busy,
        "labels": [{"id": 0, "name": MEDIUM, "type": "read-only"}],
    }


class FakeSlurmPlacementTest(unittest.TestCase):
    """The fake's sbatch, checked against what slurmctld 24.05.1 did on the cluster on 2026-11-17."""

    def setUp(self):
        # Two medium runners (2 CPUs) fit on each
        self.slurm = FakeSlurm(nodes={"trpro-slurm1": 4, "thor-slurm1": 4})
        patcher = mock.patch.object(supply.subprocess, "run", self.slurm.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sbatch(self, name, excluded="", pinned=None):
        argv = [
            "sbatch",
            f"--job-name={name}",
            f"--comment={MEDIUM}",
            "--cpus-per-task=2",
        ]
        if excluded:
            argv.append(f"--exclude={excluded}")
        if pinned:
            argv += ["--nodes=1", f"--nodelist={pinned}"]
        return self.slurm.run(argv, True, True, 60, {})

    def placed(self):
        return {
            job["name"]: (job["state"], job["node"]) for job in self.slurm.jobs.values()
        }

    def test_first_fit_honouring_exclude_and_nodelist(self):
        for name in ("a", "b", "c"):
            self.sbatch(name)
        self.sbatch("d", excluded="thor-slurm1")
        self.sbatch("e", pinned="trpro-slurm1")
        self.assertEqual(
            self.placed(),
            {
                "a": ("RUNNING", "trpro-slurm1"),
                "b": ("RUNNING", "trpro-slurm1"),
                "c": ("RUNNING", "thor-slurm1"),
                "d": ("PENDING", ""),
                "e": ("PENDING", ""),
            },
        )
        # A node freed, or an exclusion narrowed, starts the oldest that fits
        self.slurm.end_job(min(self.slurm.jobs), "COMPLETED")
        self.slurm.start_pending_jobs()
        self.assertEqual(self.placed()["d"], ("RUNNING", "trpro-slurm1"))

    def test_squeue_prints_exclusions_as_slurmctld_does(self):
        rows = [
            ({"tr-slurm2"}, "tr-slurm2"),
            ({"trpro-slurm2", "trpro-slurm1"}, "trpro-slurm[1-2]"),
            (
                {"trpro-slurm2", "tr-slurm2", "trpro-slurm1"},
                "tr-slurm2,trpro-slurm[1-2]",
            ),
            ({"trpro-slurm2", "wato2-slurm1"}, "trpro-slurm2,wato2-slurm1"),
            ({"n1", "n2", "n3", "n5"}, "n[1-3,5]"),
        ]
        for names, printed in rows:
            with self.subTest(printed):
                self.assertEqual(compressed_hostlist(names), printed)
                self.assertEqual(node_names_in_hostlist(printed), names)

    def test_refusals(self):
        refused = self.sbatch("all-out", excluded="trpro-slurm1,thor-slurm1")
        self.assertIn("Requested node configuration is not available", refused.stderr)
        unknown = self.sbatch("unknown", excluded="wato3-slurm1")
        self.assertIn("Invalid node name specified", unknown.stderr)
        self.assertEqual(self.placed(), {"all-out": ("FAILED", "")})
        # Pinned to a drained node: accepted, and it pends
        self.slurm.drained.add("trpro-slurm1")
        self.assertEqual(self.sbatch("pinned", pinned="trpro-slurm1").returncode, 0)
        self.assertEqual(self.placed()["pinned"], ("PENDING", ""))


class ParseSqueueLineTest(unittest.TestCase):
    def test_our_jobs_in_each_state(self):
        def ours(
            state,
            is_held,
            started_at,
            ended_at,
            node=None,
            pinned_node=None,
            excluded_nodes=frozenset(),
        ):
            return Allocation(
                812345,
                f"{PREFIX}a",
                MEDIUM,
                state,
                is_held,
                NOW,
                started_at,
                ended_at,
                node,
                pinned_node,
                excluded_nodes,
            )

        line = f"812345|{PREFIX}a|{MEDIUM}"
        rows = [
            # Pending: no node yet
            (
                f"{line}|PENDING|4294|{NOW}|N/A|N/A|||",
                ours("PENDING", False, None, None),
            ),
            # A pending job's start time is only an estimate
            (
                f"{line}|PENDING|4294|{NOW}|{NOW + 60}|{NOW + 2580}|||",
                ours("PENDING", False, None, None),
            ),
            (f"{line}|PENDING|0|{NOW}|N/A|N/A|||", ours("PENDING", True, None, None)),
            # Pinned with --nodelist, still pending
            (
                f"{line}|PENDING|4294|{NOW}|N/A|N/A||trpro-slurm1|",
                ours("PENDING", False, None, None, pinned_node="trpro-slurm1"),
            ),
            # Its end time is only the limit while it runs
            (
                f"{line}|RUNNING|4294|{NOW}|{NOW + 5}|{NOW + 2525}|delta-slurm1||",
                ours("RUNNING", False, NOW + 5, None, node="delta-slurm1"),
            ),
            # Ended, within MinJobAge: squeue still names its node and its pin
            (
                f"{line}|FAILED|0|{NOW}|{NOW + 5}|{NOW + 20}|trpro-slurm1|trpro-slurm1|",
                ours(
                    "FAILED", False, NOW + 5, NOW + 20, "trpro-slurm1", "trpro-slurm1"
                ),
            ),
            # Excluded nodes, sorted and compressed into ranges as squeue prints them (753312)
            (
                f"{line}|PENDING|4294|{NOW}|N/A|N/A|||tr-slurm2,trpro-slurm[1-2]",
                ours(
                    "PENDING",
                    False,
                    None,
                    None,
                    excluded_nodes={"tr-slurm2", "trpro-slurm1", "trpro-slurm2"},
                ),
            ),
            # Excluded nodes it cannot parse: left alone, never guessed
            (
                f"{line}|PENDING|4294|{NOW}|N/A|N/A|||trpro-slurm[1-",
                ours("PENDING", False, None, None, excluded_nodes=None),
            ),
            # Cancelled while pending: never placed
            (
                f"{line}|CANCELLED|0|{NOW}|Unknown|{NOW + 20}|||",
                ours("CANCELLED", False, None, NOW + 20),
            ),
        ]
        for squeue_line, allocation in rows:
            with self.subTest(squeue_line):
                self.assertEqual(parse_squeue_line(squeue_line, PREFIX), allocation)

    def test_jobs_that_are_not_ours_are_skipped(self):
        for line in (
            f"744000|slurm-slurm-runner-medium-129437625759||PENDING|0|{NOW}|N/A|N/A",
            f"744001|gha-other-a|{MEDIUM}|RUNNING|1|{NOW}|{NOW}|{NOW}",
            f"744002|some|name|with|pipes|{PREFIX}|x|y|z",
            "",
        ):
            with self.subTest(line):
                self.assertIsNone(parse_squeue_line(line, PREFIX))

    def test_one_of_ours_that_cannot_be_read_makes_squeue_unknown(self):
        for line in (
            # Without the node fields, as an older SQUEUE_FORMAT printed it
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A|N/A",
            # Without the excluded nodes
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A|N/A||",
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|high|{NOW}|N/A|N/A|||",
            f"812345_1|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A|N/A|||",
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|2026-11-13T00:38:09|N/A|N/A|||",
        ):
            with self.subTest(line):
                with self.assertRaises(SlurmUnavailable):
                    parse_squeue_line(line, PREFIX)


class NodeNamesInHostlistTest(unittest.TestCase):
    def test_what_scontrol_show_hostnames_gives_on_the_cluster(self):
        # What `scontrol show hostnames <expression>` printed on wato-login1, 2026-11-17
        all_six = {
            "delta-slurm1",
            "thor-slurm1",
            "tr-slurm2",
            "trpro-slurm1",
            "trpro-slurm2",
            "wato2-slurm1",
        }
        rows = [
            ("", set()),
            ("trpro-slurm1", {"trpro-slurm1"}),
            ("trpro-slurm[1-2]", {"trpro-slurm1", "trpro-slurm2"}),
            ("trpro-slurm[1,2]", {"trpro-slurm1", "trpro-slurm2"}),
            ("trpro-slurm[2,1]", {"trpro-slurm1", "trpro-slurm2"}),
            (
                "tr-slurm2,trpro-slurm[1-2]",
                {"tr-slurm2", "trpro-slurm1", "trpro-slurm2"},
            ),
            (
                "delta-slurm1,thor-slurm1,tr-slurm2",
                {"delta-slurm1", "thor-slurm1", "tr-slurm2"},
            ),
            ("trpro-slurm2,wato2-slurm1", {"trpro-slurm2", "wato2-slurm1"}),
            (
                "delta-slurm1,thor-slurm1,tr-slurm2,trpro-slurm[1-2],wato2-slurm1",
                all_six,
            ),
            ("node[01-03]", {"node01", "node02", "node03"}),
            ("node[08-11]", {"node08", "node09", "node10", "node11"}),
            ("node[009-011]", {"node009", "node010", "node011"}),
            # The padding comes from the first number of each range
            ("node[1-03]", {"node1", "node2", "node3"}),
            ("node[1-3,5,7-8]", {"node1", "node2", "node3", "node5", "node7", "node8"}),
            ("node[01-02,10]", {"node01", "node02", "node10"}),
            ("a[1-2],b[3-4],c5", {"a1", "a2", "b3", "b4", "c5"}),
            ("node[1-2]-ib", {"node1-ib", "node2-ib"}),
            (
                "rack[1-2]-node[1-2]",
                {"rack1-node1", "rack1-node2", "rack2-node1", "rack2-node2"},
            ),
            ("wato[2-3]-slurm1", {"wato2-slurm1", "wato3-slurm1"}),
            ("node1,,node2", {"node1", "node2"}),
            ("node[1-2],node2", {"node1", "node2"}),
        ]
        for hostlist, names in rows:
            with self.subTest(hostlist):
                self.assertEqual(node_names_in_hostlist(hostlist), names)

    def test_anything_it_does_not_fully_parse_raises(self):
        # The first two scontrol rejects (printing an error, exit 0); the rest are not hostlists
        for hostlist in (
            "node[3-1]",
            "node[1-2",
            "trpro-slurm[a-b]",
            "n]1[",
            "node[]",
            "[1-2]",
            "node[[1]]",
            "node 1",
            "(null)",
        ):
            with self.subTest(hostlist):
                with self.assertRaises(ValueError):
                    node_names_in_hostlist(hostlist)


class ParseRegistrationTest(unittest.TestCase):
    def test_ours_and_not_ours(self):
        rows = [
            (runner(f"{PREFIX}a"), RunnerRegistration(7, f"{PREFIX}a", True, False)),
            (
                runner(f"{PREFIX}a", "offline"),
                RunnerRegistration(7, f"{PREFIX}a", False, False),
            ),
            (
                runner(f"{PREFIX}a", busy=True),
                RunnerRegistration(7, f"{PREFIX}a", True, True),
            ),
            (runner("slurm-trpro-slurm2-749139"), None),
            ({"id": 8}, None),
        ]
        for entry, registration in rows:
            with self.subTest(entry=entry):
                self.assertEqual(parse_registration(entry, PREFIX), registration)

    def test_one_of_ours_without_a_runner_id_cannot_be_read(self):
        entry = runner(f"{PREFIX}a")
        del entry["id"]
        with self.assertRaises(GitHubUnavailable):
            parse_registration(entry, PREFIX)


class PhaseFromSqueueAndRunnersTest(unittest.TestCase):
    """What squeue and the runners list say, through the parsers, decides the phase."""

    def test_each_phase(self):
        def line(state, priority=4294, started=-1, submitted=-100):
            started_at = "N/A" if started is None else NOW + started
            submitted_at = NOW + submitted
            return f"1|{PREFIX}a|{MEDIUM}|{state}|{priority}|{submitted_at}|{started_at}|N/A|||"

        now = NOW
        rows = [
            (
                line("PENDING", started=None),
                runner(f"{PREFIX}a", "offline"),
                Phase.PENDING,
            ),
            (
                line("PENDING", 0, started=None),
                runner(f"{PREFIX}a", "offline"),
                Phase.CANNOT_START,
            ),
            (line("PENDING", started=None), None, Phase.PENDING),
            (
                line(
                    "PENDING", started=None, submitted=-UNUSED_REGISTRATION_LIFETIME_S
                ),
                None,
                Phase.CANNOT_START,
            ),
            (
                line("RUNNING", started=-10),
                runner(f"{PREFIX}a", "offline"),
                Phase.BOOTING,
            ),
            (
                line("RUNNING", started=-B),
                runner(f"{PREFIX}a", "offline"),
                Phase.UNAVAILABLE,
            ),
            (line("RUNNING", started=-10), runner(f"{PREFIX}a"), Phase.IDLE),
            (line("RUNNING", started=-W), runner(f"{PREFIX}a"), Phase.UNAVAILABLE),
            (
                line("RUNNING", started=-10),
                runner(f"{PREFIX}a", busy=True),
                Phase.UNAVAILABLE,
            ),
            (line("RUNNING", started=-10), None, Phase.UNAVAILABLE),
            (line("COMPLETING", started=-10), runner(f"{PREFIX}a"), Phase.UNAVAILABLE),
        ]
        for squeue_line, entry, phase in rows:
            with self.subTest(squeue_line=squeue_line, runner=entry):
                allocation = parse_squeue_line(squeue_line, PREFIX)
                registration = (
                    None if entry is None else parse_registration(entry, PREFIX)
                )
                self.assertEqual(
                    phase_of_allocation(allocation, registration, now), phase
                )


class SbatchCommandTest(unittest.TestCase):
    def test_every_option(self):
        resources = RunnerResources(2, "2G", 16384, 30)
        self.assertEqual(
            sbatch_command(f"{PREFIX}abc", ANYWHERE, resources),
            [
                "sbatch",
                "--output=/var/log/slurm-ci/slurm-ci-%j.out",
                f"--job-name={PREFIX}abc",
                f"--comment={MEDIUM}",
                "--cpus-per-task=2",
                "--mem-per-cpu=2G",
                "--gres=tmpdisk:16384",
                "--time=42",
                "--no-requeue",
                ALLOCATE_RUNNER_SCRIPT_PATH,
                str(W),
            ],
        )

    def test_where_it_may_go(self):
        resources = RunnerResources(2, "2G", 16384, 30)
        rows = [
            (
                Submission(MEDIUM, frozenset({"trpro-slurm2", "trpro-slurm1"})),
                ["--exclude=trpro-slurm1,trpro-slurm2"],
            ),
            # Exactly that node: --nodelist alone lets Slurm add others
            (
                Submission(MEDIUM, frozenset(), pinned_node="trpro-slurm1"),
                ["--nodes=1", "--nodelist=trpro-slurm1"],
            ),
        ]
        anywhere = sbatch_command(f"{PREFIX}abc", ANYWHERE, resources)
        for submission, placement in rows:
            with self.subTest(submission):
                argv = sbatch_command(f"{PREFIX}abc", submission, resources)
                # Before the script and its argument, after every other option
                self.assertEqual(argv, anywhere[:-2] + placement + anywhere[-2:])


class SlurmCommandsTest(unittest.TestCase):
    def setUp(self):
        self.slurm = FakeSlurm()
        patcher = mock.patch.object(supply.subprocess, "run", self.slurm.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_squeue_lists_our_jobs_in_every_state_as_epoch_seconds(self):
        ours = self.slurm.add_job(f"{PREFIX}a")
        self.slurm.add_job("slurm-slurm-runner-medium-129437625759", size_label="")
        allocations = read_our_allocations(PREFIX)
        self.assertEqual([each.slurm_job_id for each in allocations], [ours])
        argv, added_environment = self.slurm.calls[-1]
        self.assertEqual(
            argv,
            [
                "squeue",
                "--me",
                "--states=all",
                "--noheader",
                "--format=%i|%j|%k|%T|%Q|%V|%S|%e|%N|%n|%x",
            ],
        )
        self.assertEqual(added_environment, {"SLURM_TIME_FORMAT": "%s"})

    def test_each_allocation_carries_the_node_squeue_names(self):
        self.slurm.add_job(f"{PREFIX}pending")
        self.slurm.add_job(f"{PREFIX}pinned", pinned_node="trpro-slurm2")
        self.slurm.add_job(
            f"{PREFIX}ended",
            "FAILED",
            started_at=NOW - 20,
            ended_at=NOW - 10,
            node="trpro-slurm1",
        )
        by_name = {each.runner_name: each for each in read_our_allocations(PREFIX)}
        self.assertEqual(
            {name: (each.node, each.pinned_node) for name, each in by_name.items()},
            {
                f"{PREFIX}pending": (None, None),
                f"{PREFIX}pinned": (None, "trpro-slurm2"),
                f"{PREFIX}ended": ("trpro-slurm1", None),
            },
        )

    def test_an_exclusion_it_cannot_read_is_logged_once(self):
        self.slurm.add_job(f"{PREFIX}a", excluded_as_printed="trpro-slurm[1-")
        with self.assertLogs("supply", logging.WARNING) as logs:
            (allocation,) = read_our_allocations(PREFIX)
            read_our_allocations(PREFIX)
        self.assertIsNone(allocation.excluded_nodes)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("cannot read its excluded nodes", logs.output[0])

    def test_squeue_that_fails_times_out_or_is_missing_raises(self):
        self.slurm.failing_commands.add("squeue")
        with self.assertRaises(SlurmUnavailable):
            read_our_allocations(PREFIX)

        def times_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, SLURM_COMMAND_TIMEOUT)

        def is_missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        for fake in (times_out, is_missing):
            with self.subTest(fake.__name__):
                with mock.patch.object(supply.subprocess, "run", fake):
                    with self.assertRaises(SlurmUnavailable):
                        read_our_allocations(PREFIX)

    def test_the_nodes_slurm_knows_are_every_node_sinfo_lists(self):
        self.assertEqual(
            nodes_slurm_knows(), {*CLUSTER_NODES, "wato-login1", "wato-login2"}
        )
        self.assertEqual(
            self.slurm.commands("sinfo"),
            [["sinfo", "--all", "--noheader", "--Node", "--format=%N"]],
        )
        # Could not ask is never "unknown", and neither is a list with no node
        self.slurm.sinfo_lists_nothing = True
        with self.assertRaises(SlurmUnavailable):
            nodes_slurm_knows()
        self.slurm.failing_commands.add("sinfo")
        with self.assertRaises(SlurmUnavailable):
            nodes_slurm_knows()

    def test_cancel_asks_slurmctld_to_cancel_only_if_still_pending(self):
        pending = self.slurm.add_job(f"{PREFIX}a", priority=0)
        allocation = read_our_allocations(PREFIX)[0]
        with self.assertLogs("supply", logging.INFO) as logs:
            cancel_pending_allocation(allocation)
        # Without --ctld, scancel tests the state itself and then kills unconditionally
        self.assertEqual(
            self.slurm.commands("scancel"),
            [["scancel", "--ctld", "--state=PENDING", str(pending)]],
        )
        # With --ctld, scancel exits 0 for a job that had started, so the log cannot say it was cancelled
        self.assertIn(f"Asked Slurm to cancel {PREFIX}a", logs.output[0])
        self.assertIn("if still pending", logs.output[0])
        self.assertIn("held", logs.output[0])

    def test_a_failed_cancel_is_logged_and_left_to_the_next_cycle(self):
        pending = self.slurm.add_job(f"{PREFIX}a")
        self.slurm.failing_commands.add("scancel")
        allocation = read_our_allocations(PREFIX)[0]
        with self.assertLogs("supply", logging.WARNING) as logs:
            cancel_pending_allocation(allocation)
        self.assertIn(f"Could not cancel {PREFIX}a", logs.output[0])
        self.assertEqual(self.slurm.jobs[pending]["state"], "PENDING")

    def test_one_update_narrows_every_waiting_runner_given_the_same_list(self):
        first = self.slurm.add_job(
            f"{PREFIX}a", excluded="thor-slurm1,trpro-slurm1,trpro-slurm2"
        )
        second = self.slurm.add_job(f"{PREFIX}b", excluded="trpro-slurm2,trpro-slurm1")
        waiting = read_our_allocations(PREFIX)
        trpro = frozenset({"trpro-slurm2", "trpro-slurm1"})
        self.assertEqual(narrow_pending_exclusions(waiting, trpro), set())
        self.assertEqual(narrow_pending_exclusions(waiting, frozenset()), set())
        self.assertEqual(
            self.slurm.commands("scontrol"),
            [
                [
                    "scontrol",
                    "update",
                    f"JobId={first},{second}",
                    "ExcNodeList=trpro-slurm1,trpro-slurm2",
                ],
                # An empty value clears it
                ["scontrol", "update", f"JobId={first},{second}", "ExcNodeList="],
            ],
        )

    def test_at_most_a_hundred_job_ids_a_call(self):
        for index in range(250):
            self.slurm.add_job(f"{PREFIX}{index}", excluded="trpro-slurm1")
        narrow_pending_exclusions(read_our_allocations(PREFIX), frozenset())
        updates = self.slurm.commands("scontrol")
        self.assertEqual(
            [len(update[2].split(",")) for update in updates], [100, 100, 50]
        )

    def test_runners_that_started_or_ended_meanwhile_are_left_alone(self):
        for name in ("pending", "running", "ended"):
            self.slurm.add_job(f"{PREFIX}{name}", excluded="trpro-slurm1")
        pending, running, ended = read_our_allocations(PREFIX)
        self.slurm.jobs[running.slurm_job_id]["state"] = "RUNNING"
        self.slurm.jobs[ended.slurm_job_id]["state"] = "COMPLETED"
        with self.assertLogs("supply", logging.DEBUG) as logs:
            refused = narrow_pending_exclusions([pending, running, ended], frozenset())
        self.assertEqual(refused, {running.slurm_job_id, ended.slurm_job_id})
        self.assertEqual(logs.records[0].levelno, logging.DEBUG)
        self.assertEqual(self.slurm.jobs[pending.slurm_job_id]["excluded"], set())
        self.assertEqual(
            self.slurm.jobs[running.slurm_job_id]["excluded"], {"trpro-slurm1"}
        )

    def test_an_update_that_fails_outright_narrows_none(self):
        self.slurm.add_job(f"{PREFIX}a", excluded="trpro-slurm1")
        waiting = read_our_allocations(PREFIX)
        self.slurm.failing_commands.add("scontrol")
        with self.assertLogs("supply", logging.WARNING):
            refused = narrow_pending_exclusions(waiting, frozenset())
        self.assertEqual(refused, {waiting[0].slurm_job_id})


class GitHubSideTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.github = GitHubClient("test-token")
        self.repo = self.fake.base_url + REPO
        self.slurm = FakeSlurm()
        for patcher in (
            mock.patch.object(supply.subprocess, "run", self.slurm.run),
            mock.patch.object(github_client, "MIN_SECONDS_BETWEEN_WRITES", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_deleting_an_ended_runners_registration(self):
        path = f"{REPO}/actions/runners/12"
        rows = [
            (204, True, logging.INFO, f"Deleted the registration of {PREFIX}a"),
            (
                404,
                True,
                logging.INFO,
                f"The registration of {PREFIX}a was already gone",
            ),
            (422, True, logging.WARNING, "it believes a job is on that runner"),
            (
                500,
                False,
                logging.WARNING,
                f"Could not delete the registration of {PREFIX}a",
            ),
        ]
        for status, answered, level, logged in rows:
            with self.subTest(status=status):
                self.fake.script(path, Reply(status, body={"message": "x"}))
                with self.assertLogs("supply", level) as logs:
                    self.assertEqual(
                        delete_registration(
                            self.github,
                            self.repo,
                            12,
                            f"{PREFIX}a",
                            why="its Slurm job has ended",
                        ),
                        answered,
                    )
                self.assertIn(logged, logs.output[-1])
        self.assertEqual(
            {method for method, _, _ in self.fake.requests_to(path)}, {"DELETE"}
        )

    def test_registrations_come_from_every_page_and_only_ours(self):
        first = f"{REPO}/actions/runners?per_page=100"
        second = f"{REPO}/actions/runners?per_page=100&page=2"
        self.fake.script(
            first,
            Reply(
                200,
                {"Link": f'<{self.fake.base_url}{second}>; rel="next"'},
                {"runners": [runner(f"{PREFIX}a"), runner("slurm-thor-slurm1-1")]},
            ),
        )
        self.fake.script(
            second, Reply(200, body={"runners": [runner(f"{PREFIX}b", busy=True)]})
        )
        registrations = read_our_registrations(self.github, self.repo, PREFIX)
        self.assertEqual(sorted(registrations), [f"{PREFIX}a", f"{PREFIX}b"])
        self.assertTrue(registrations[f"{PREFIX}b"].is_busy)

    def test_a_runners_list_that_cannot_be_read_completely_raises(self):
        first = f"{REPO}/actions/runners?per_page=100"
        second = f"{REPO}/actions/runners?per_page=100&page=2"
        self.fake.script(
            first,
            Reply(
                200,
                {"Link": f'<{self.fake.base_url}{second}>; rel="next"'},
                {"runners": [runner(f"{PREFIX}a")]},
            ),
        )
        self.fake.script(second, Reply(502, body={"message": "Server Error"}))
        with self.assertRaises(GitHubUnavailable):
            read_our_registrations(self.github, self.repo, PREFIX)

    def jit_config_reply(self, body=None):
        self.fake.script(
            f"{REPO}/actions/runners/generate-jitconfig",
            Reply(
                201,
                body=body or {"runner": {"id": 9}, "encoded_jit_config": JIT_CONFIG},
            ),
        )

    def test_a_runner_is_registered_with_its_one_size_label_then_submitted(self):
        self.jit_config_reply()
        with self.assertLogs(level=logging.DEBUG) as logs:
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)

        ((method, path, body),) = self.fake.received_bodies
        self.assertEqual(
            (method, path), ("POST", f"{REPO}/actions/runners/generate-jitconfig")
        )
        request = json.loads(body)
        self.assertEqual(request["runner_group_id"], RUNNER_GROUP_ID)
        self.assertEqual(request["labels"], [MEDIUM])
        self.assertRegex(request["name"], f"^{PREFIX}[0-9a-f]{{12}}$")
        self.assertEqual(request["work_folder"], f"/tmp/{request['name']}/_work")

        ((argv, added_environment),) = self.slurm.calls
        self.assertIn(f"--job-name={request['name']}", argv)
        self.assertIn("--no-requeue", argv)
        # The secret goes in the environment only: never in argv, never in a log line
        self.assertEqual(added_environment, {JIT_CONFIG_VARIABLE: JIT_CONFIG})
        self.assertFalse(any(JIT_CONFIG in arg for arg in argv))
        self.assertFalse(any(JIT_CONFIG in line for line in logs.output))
        self.assertTrue(
            any(f"Submitted {request['name']}" in line for line in logs.output)
        )

    def test_no_sbatch_without_a_jit_config(self):
        for reply in (
            Reply(500, body={"message": "Server Error"}),
            Reply(201, body={"runner": {"id": 9}}),
        ):
            with self.subTest(status=reply.status):
                self.fake.script(f"{REPO}/actions/runners/generate-jitconfig", reply)
                with self.assertRaises(GitHubUnavailable):
                    submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
                self.assertEqual(self.slurm.calls, [])

    def deletes(self):
        return [path for method, path, _ in self.fake.received if method == "DELETE"]

    def test_a_sbatch_that_left_no_job_deletes_the_registration_made_for_it(self):
        self.jit_config_reply()
        self.fake.script(f"{REPO}/actions/runners/9", Reply(204, body=None))
        self.slurm.failing_commands.add("sbatch")
        with self.assertLogs("supply", logging.INFO) as logs:
            with self.assertRaises(SbatchLeftNoJob) as raised:
                submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertNotIsInstance(raised.exception, SubmissionRefused)
        self.assertEqual(self.deletes(), [f"{REPO}/actions/runners/9"])
        (squeue,) = self.slurm.commands("squeue")
        self.assertIn("--states=all", squeue)
        self.assertTrue(any(arg.startswith(f"--name={PREFIX}") for arg in squeue))
        self.assertIn("sbatch created no job", logs.output[-1])

    def test_a_refusal_that_leaves_a_failed_record_without_a_node_still_deletes_it(
        self,
    ):
        self.jit_config_reply()
        self.fake.script(f"{REPO}/actions/runners/9", Reply(204, body=None))
        self.slurm.sbatch_refuses_leaving_a_failed_record = True
        with self.assertRaises(SubmissionRefused) as raised:
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertIn("node configuration is not available", str(raised.exception))
        (record,) = self.slurm.jobs.values()
        self.assertEqual((record["state"], record["node"]), ("FAILED", ""))
        self.assertEqual(self.deletes(), [f"{REPO}/actions/runners/9"])

    def test_a_failed_sbatch_whose_job_exists_is_a_submitted_runner(self):
        self.jit_config_reply()
        # slurmctld created the job although sbatch failed: its runner will need the registration
        self.slurm.sbatch_fails_after_creating_its_job = True
        with self.assertLogs("supply", logging.INFO) as logs:
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        (job,) = self.slurm.jobs.values()
        self.assertIn(f"Submitted {job['name']} for {MEDIUM} although", logs.output[-1])
        # A record of that name that has a node ran, or may still run
        self.slurm.sbatch_fails_after_creating_its_job = False
        self.slurm.failing_commands.add("sbatch")
        for state in ("COMPLETING", "FAILED"):
            self.slurm.jobs.clear()
            self.slurm.add_job("x", state=state, node="delta-slurm1")
            with mock.patch("supply.new_runner_name", return_value="x"):
                submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertEqual(self.deletes(), [])

    def test_a_failed_sbatch_that_squeue_cannot_check_keeps_the_registration(self):
        self.jit_config_reply()
        self.slurm.failing_commands.update({"sbatch", "squeue"})
        with self.assertRaises(SlurmUnavailable) as raised:
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertNotIsInstance(raised.exception, SubmissionRefused)
        self.assertEqual(self.deletes(), [])

    def test_an_sbatch_killed_at_its_timeout_keeps_the_registration(self):
        # Its request may still be queued in slurmctld, behind the squeue that would look for the job
        self.jit_config_reply()
        self.slurm.sbatch_times_out = True
        with self.assertRaises(SlurmTimedOut):
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertEqual(self.slurm.commands("squeue"), [])
        self.assertEqual(self.deletes(), [])

    def test_a_failed_delete_is_logged_and_the_sbatch_error_still_raised(self):
        self.jit_config_reply()
        self.fake.script(
            f"{REPO}/actions/runners/9", Reply(500, body={"message": "Server Error"})
        )
        self.slurm.failing_commands.add("sbatch")
        with self.assertLogs("supply", logging.WARNING) as logs:
            with self.assertRaises(SbatchLeftNoJob) as raised:
                submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertIn("Unable to contact slurm controller", str(raised.exception))
        self.assertIn("Could not delete the registration", logs.output[-1])

    def test_a_failed_sbatch_raises_and_leaves_the_secret_out_of_the_message(self):
        self.jit_config_reply()
        self.slurm.failing_commands.add("sbatch")
        with self.assertRaises(SbatchLeftNoJob) as raised:
            submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertNotIn(JIT_CONFIG, str(raised.exception))
        self.assertIn("Unable to contact slurm controller", str(raised.exception))

        def times_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, SLURM_COMMAND_TIMEOUT)

        with mock.patch.object(supply.subprocess, "run", times_out):
            with self.assertRaises(SlurmUnavailable) as raised:
                submit_runner(self.github, self.repo, PREFIX, ANYWHERE)
        self.assertNotIn(JIT_CONFIG, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
