"""supply without Slurm or GitHub: the parsers by table, the commands against a fake Slurm, the runners list
and the JIT config against the fake GitHub."""

import json
import logging
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
from plan import Allocation, Phase, RunnerRegistration, phase_of_allocation
from runner_size_config import RunnerResources
from supply import (
    JIT_CONFIG_VARIABLE,
    SlurmUnavailable,
    cancel_pending_allocation,
    delete_registration,
    parse_registration,
    parse_squeue_line,
    read_our_allocations,
    read_our_registrations,
    sbatch_command,
    submit_runner,
)
from test_github_client import FakeGitHub, Reply

NOW = 1_800_000_000
PREFIX = "gha-test-"
REPO = "/repos/WATonomous/infra-config"
MEDIUM = "slurm-runner-medium"
# Stands in for a real, secret config
JIT_CONFIG = "eyJzZWNyZXQiOiAianVzdC1pbi10aW1lIn0="


class FakeSlurm:
    """squeue, sbatch and scancel as supply calls them through subprocess.run, over a dict of jobs."""

    def __init__(self, now=NOW):
        self.now = now
        self.jobs = {}  # Slurm job id -> squeue fields after the id
        self.next_slurm_job_id = 812000
        self.calls = []  # (argv, the environment added to the daemon's)
        self.failing_commands = set()

    def add_job(
        self,
        name,
        state="PENDING",
        size_label=MEDIUM,
        priority=4294,
        submitted_at=None,
        started_at="N/A",
        ended_at="N/A",
    ):
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
        }
        return slurm_job_id

    def run(self, argv, capture_output, text, timeout, env):
        added = {
            name: value
            for name, value in env.items()
            if name in (JIT_CONFIG_VARIABLE, "SLURM_TIME_FORMAT")
        }
        self.calls.append((list(argv), added))
        command = argv[0]
        if command in self.failing_commands:
            return subprocess.CompletedProcess(
                argv, 1, "", f"{command}: error: Unable to contact slurm controller"
            )
        if command == "squeue":
            return subprocess.CompletedProcess(argv, 0, self.squeue_output(), "")
        if command == "sbatch":
            options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
            slurm_job_id = self.add_job(
                options["--job-name"], size_label=options["--comment"]
            )
            return subprocess.CompletedProcess(
                argv, 0, f"Submitted batch job {slurm_job_id}\n", ""
            )
        if command == "scancel":
            # Only slurmctld can test the state atomically, so the fake records the call and changes nothing
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def squeue_output(self):
        return "".join(
            f"{slurm_job_id}|{job['name']}|{job['comment']}|{job['state']}|{job['priority']}"
            f"|{job['submitted_at']}|{job['started_at']}|{job['ended_at']}\n"
            for slurm_job_id, job in sorted(self.jobs.items())
        )

    def commands(self, command):
        return [argv for argv, _ in self.calls if argv[0] == command]


def runner(name, status="online", busy=False):
    return {
        "id": 7,
        "name": name,
        "os": "Linux",
        "status": status,
        "busy": busy,
        "labels": [{"id": 0, "name": MEDIUM, "type": "read-only"}],
    }


class ParseSqueueLineTest(unittest.TestCase):
    def test_our_jobs_in_each_state(self):
        rows = [
            (
                f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A|N/A",
                Allocation(
                    812345, f"{PREFIX}a", MEDIUM, "PENDING", False, NOW, None, None
                ),
            ),
            # A pending job's start time is only an estimate
            (
                f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|{NOW + 60}|{NOW + 2580}",
                Allocation(
                    812345, f"{PREFIX}a", MEDIUM, "PENDING", False, NOW, None, None
                ),
            ),
            (
                f"812345|{PREFIX}a|{MEDIUM}|PENDING|0|{NOW}|N/A|N/A",
                Allocation(
                    812345, f"{PREFIX}a", MEDIUM, "PENDING", True, NOW, None, None
                ),
            ),
            # Its end time is only the limit while it runs
            (
                f"812345|{PREFIX}a|{MEDIUM}|RUNNING|4294|{NOW}|{NOW + 5}|{NOW + 2525}",
                Allocation(
                    812345, f"{PREFIX}a", MEDIUM, "RUNNING", False, NOW, NOW + 5, None
                ),
            ),
            (
                f"812345|{PREFIX}a|{MEDIUM}|FAILED|0|{NOW}|{NOW + 5}|{NOW + 20}",
                Allocation(
                    812345,
                    f"{PREFIX}a",
                    MEDIUM,
                    "FAILED",
                    False,
                    NOW,
                    NOW + 5,
                    NOW + 20,
                ),
            ),
            (
                f"812345|{PREFIX}a|{MEDIUM}|CANCELLED|0|{NOW}|Unknown|{NOW + 20}",
                Allocation(
                    812345,
                    f"{PREFIX}a",
                    MEDIUM,
                    "CANCELLED",
                    False,
                    NOW,
                    None,
                    NOW + 20,
                ),
            ),
        ]
        for line, allocation in rows:
            with self.subTest(line):
                self.assertEqual(parse_squeue_line(line, PREFIX), allocation)

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
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A",
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|high|{NOW}|N/A|N/A",
            f"812345_1|{PREFIX}a|{MEDIUM}|PENDING|4294|{NOW}|N/A|N/A",
            f"812345|{PREFIX}a|{MEDIUM}|PENDING|4294|2026-11-13T00:38:09|N/A|N/A",
        ):
            with self.subTest(line):
                with self.assertRaises(SlurmUnavailable):
                    parse_squeue_line(line, PREFIX)


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
            return f"1|{PREFIX}a|{MEDIUM}|{state}|{priority}|{submitted_at}|{started_at}|N/A"

        now = NOW
        rows = [
            (
                line("PENDING", started=None),
                runner(f"{PREFIX}a", "offline"),
                Phase.WAITING_FOR_NODE,
            ),
            (
                line("PENDING", 0, started=None),
                runner(f"{PREFIX}a", "offline"),
                Phase.CANNOT_START,
            ),
            (line("PENDING", started=None), None, Phase.WAITING_FOR_NODE),
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
            sbatch_command(f"{PREFIX}abc", MEDIUM, resources),
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
                "--format=%i|%j|%k|%T|%Q|%V|%S|%e",
            ],
        )
        self.assertEqual(added_environment, {"SLURM_TIME_FORMAT": "%s"})

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
        ended = RunnerRegistration(12, f"{PREFIX}a", False, False)
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
                        delete_registration(self.github, self.repo, ended), answered
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
            submit_runner(self.github, self.repo, PREFIX, MEDIUM)

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
                    submit_runner(self.github, self.repo, PREFIX, MEDIUM)
                self.assertEqual(self.slurm.calls, [])

    def test_a_failed_sbatch_raises_and_leaves_the_secret_out_of_the_message(self):
        self.jit_config_reply()
        self.slurm.failing_commands.add("sbatch")
        with self.assertRaises(SlurmUnavailable) as raised:
            submit_runner(self.github, self.repo, PREFIX, MEDIUM)
        self.assertNotIn(JIT_CONFIG, str(raised.exception))
        self.assertIn("Unable to contact slurm controller", str(raised.exception))

        def times_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, SLURM_COMMAND_TIMEOUT)

        with mock.patch.object(supply.subprocess, "run", times_out):
            with self.assertRaises(SlurmUnavailable) as raised:
                submit_runner(self.github, self.repo, PREFIX, MEDIUM)
        self.assertNotIn(JIT_CONFIG, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
