"""runner_size_config by table: which labels are sizes, and what each asks Slurm for."""

import unittest

from runner_size_config import (
    TMPDISK_DEFAULT_MIB,
    RunnerResources,
    get_runner_resources,
    minutes_in_slurm_time,
    size_label_of_job,
    slurm_time_limit_minutes,
)


class GetRunnerResourcesTest(unittest.TestCase):
    def test_named_sizes_keep_their_values(self):
        rows = [
            ("slurm-runner-small", RunnerResources(1, "2G", 4096, 30)),
            ("slurm-runner-medium", RunnerResources(2, "2G", 16 * 1024, 30)),
            ("slurm-runner-large", RunnerResources(4, "2G", 16 * 1024, 30)),
            ("slurm-runner-xlarge", RunnerResources(16, "2G", 16 * 1024, 30)),
            (
                "slurm-runner-medium-long-running",
                RunnerResources(4, "2G", 16 * 1024, 360),
            ),
        ]
        for size_label, resources in rows:
            with self.subTest(size_label=size_label):
                self.assertEqual(get_runner_resources(size_label), resources)

    def test_the_default_tmpdisk_is_16_gib_in_mib(self):
        # Was 16 * 1045, and the custom path multiplied it by 1024 once more
        self.assertEqual(TMPDISK_DEFAULT_MIB, 16384)

    def test_custom_sizes(self):
        rows = [
            ("slurm-runner-4cpu-2mempercpu", RunnerResources(4, "2G", 16384, 30)),
            (
                "slurm-runner-4cpu-2mempercpu-30:00time-16tmpdisk",
                RunnerResources(4, "2G", 16384, 30),
            ),
            (
                "slurm-runner-8cpu-4mempercpu-01:30:00time",
                RunnerResources(8, "4G", 16384, 90),
            ),
            (
                "slurm-runner-1cpu-1mempercpu-45time-2tmpdisk",
                RunnerResources(1, "1G", 2048, 45),
            ),
            (
                "slurm-runner-2cpu-2mempercpu-10:30time",
                RunnerResources(2, "2G", 16384, 11),
            ),
        ]
        for size_label, resources in rows:
            with self.subTest(size_label=size_label):
                self.assertEqual(get_runner_resources(size_label), resources)

    def test_labels_that_are_not_sizes_give_none_instead_of_raising(self):
        for size_label in (
            "",
            "ubuntu-latest",
            "slurm-runner-",
            "slurm-runner-huge",
            "slurm-runner-4cpu",
            "slurm-runner-4cpu-2mempercpu-time",
            "slurm-runner-4cpu-2mempercpu-1:2:3:4time",
            "slurm-runner-0cpu-2mempercpu",
            "slurm-runner-4cpu-0mempercpu",
            "slurm-runner-4cpu-2mempercpu-0tmpdisk",
            "slurm-runner-4cpu-2mempercpu-0:00time",
            "Slurm-Runner-Medium",
            "slurm-runner-4cpu-2mempercpu-gpu",
        ):
            with self.subTest(size_label=size_label):
                self.assertIsNone(get_runner_resources(size_label))


class TimeLimitTest(unittest.TestCase):
    def test_the_slurm_limit_is_the_job_budget_plus_w_plus_d(self):
        self.assertEqual(slurm_time_limit_minutes(30), 30 + 10 + 2)
        self.assertEqual(slurm_time_limit_minutes(360), 372)

    def test_a_size_that_would_need_more_than_the_partitions_day_is_not_served(self):
        # 1428 + 12 minutes is exactly a day; Slurm would keep anything longer pending forever
        self.assertEqual(
            get_runner_resources("slurm-runner-1cpu-1mempercpu-1428time"),
            RunnerResources(1, "1G", 16384, 1428),
        )
        for size_label in (
            "slurm-runner-1cpu-1mempercpu-1429time",
            "slurm-runner-1cpu-1mempercpu-24:00:00time",
        ):
            with self.subTest(size_label=size_label):
                self.assertIsNone(get_runner_resources(size_label))
                self.assertIsNone(size_label_of_job([size_label]))


class MinutesInSlurmTimeTest(unittest.TestCase):
    def test_slurm_time_formats(self):
        rows = [
            ("30", 30),
            ("30:00", 30),
            ("30:01", 31),
            ("00:30:00", 30),
            ("06:00:00", 360),
            ("1:2:3:4", None),
            ("", None),
            (":", None),
        ]
        for slurm_time, minutes in rows:
            with self.subTest(slurm_time=slurm_time):
                self.assertEqual(minutes_in_slurm_time(slurm_time), minutes)


class SizeLabelOfJobTest(unittest.TestCase):
    def test_exactly_one_label_that_is_a_size(self):
        rows = [
            ([], None),
            (["ubuntu-latest"], None),
            (["slurm-runner-medium"], "slurm-runner-medium"),
            (["SLURM-RUNNER-MEDIUM"], "slurm-runner-medium"),
            (["slurm-runner-4cpu-2mempercpu"], "slurm-runner-4cpu-2mempercpu"),
            # A runner registers with exactly the size label, so it could not take these
            (["self-hosted", "slurm-runner-medium"], None),
            (["slurm-runner-medium", "gpu"], None),
            (["slurm-runner-huge"], None),
        ]
        for job_labels, size_label in rows:
            with self.subTest(job_labels=job_labels):
                self.assertEqual(size_label_of_job(job_labels), size_label)


if __name__ == "__main__":
    unittest.main()
