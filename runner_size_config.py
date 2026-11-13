"""
What a runner of each size label asks Slurm for. A job is ours to serve when its runs-on is exactly one
size label that this file can size.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from config import (
    IDLE_ALLOWANCE_S,
    SCRIPT_START_MARGIN_S,
    SLURM_PARTITION_MAX_TIME_MINUTES,
)


@dataclass(frozen=True)
class RunnerResources:
    """What one runner of a size label asks Slurm for."""

    cpus: int
    mem_per_cpu: str  # sbatch --mem-per-cpu, e.g. "2G"
    tmpdisk_mib: int  # sbatch --gres=tmpdisk:<MiB>
    job_budget_minutes: int  # J: how long a job may run once a runner has picked it up


TMPDISK_DEFAULT_MIB = 16 * 1024  # 16 GiB

# Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
NAMED_SIZES = {
    "slurm-runner-small": RunnerResources(1, "2G", 4096, 30),
    "slurm-runner-medium": RunnerResources(2, "2G", TMPDISK_DEFAULT_MIB, 30),
    "slurm-runner-large": RunnerResources(4, "2G", TMPDISK_DEFAULT_MIB, 30),
    "slurm-runner-xlarge": RunnerResources(16, "2G", TMPDISK_DEFAULT_MIB, 30),
    "slurm-runner-medium-long-running": RunnerResources(
        4, "2G", TMPDISK_DEFAULT_MIB, 6 * 60
    ),
}

# Experimental custom sizes: slurm-runner-4cpu-2mempercpu-30:00time-16tmpdisk asks for 4 CPUs,
# 2 GiB per CPU, a 30-minute job and 16 GiB of tmpdisk. The time and tmpdisk parts are optional.
CUSTOM_SIZE_LABEL = re.compile(
    r"slurm-runner-"
    r"(?P<cpus>\d+)cpu-"
    r"(?P<mem_per_cpu_gib>\d+)mempercpu"
    r"(?:-(?P<job_budget>[\d:]+)time)?"
    r"(?:-(?P<tmpdisk_gib>\d+)tmpdisk)?"
)
# The named sizes' budget, in Slurm's time format
CUSTOM_SIZE_DEFAULT_JOB_BUDGET = "30:00"


def get_runner_resources(size_label: str) -> RunnerResources | None:
    """Resources for a named or custom size label; None if it cannot be parsed, asks for nothing, or would
    need more time than the partition allows, so that Slurm would keep it pending forever."""
    resources = NAMED_SIZES.get(size_label) or custom_size_resources(size_label)
    if resources is None:
        return None
    if (
        slurm_time_limit_minutes(resources.job_budget_minutes)
        > SLURM_PARTITION_MAX_TIME_MINUTES
    ):
        return None
    return resources


def custom_size_resources(size_label: str) -> RunnerResources | None:
    """Resources for a CUSTOM_SIZE_LABEL; None if it does not match or asks for nothing."""
    match = CUSTOM_SIZE_LABEL.fullmatch(size_label)
    if match is None:
        return None
    cpus = int(match["cpus"])
    mem_per_cpu_gib = int(match["mem_per_cpu_gib"])
    tmpdisk_mib = TMPDISK_DEFAULT_MIB
    if match["tmpdisk_gib"]:
        tmpdisk_mib = int(match["tmpdisk_gib"]) * 1024
    job_budget_minutes = minutes_in_slurm_time(
        match["job_budget"] or CUSTOM_SIZE_DEFAULT_JOB_BUDGET
    )
    if job_budget_minutes is None:
        return None
    if min(cpus, mem_per_cpu_gib, tmpdisk_mib, job_budget_minutes) < 1:
        return None
    return RunnerResources(cpus, f"{mem_per_cpu_gib}G", tmpdisk_mib, job_budget_minutes)


def slurm_time_limit_minutes(job_budget_minutes: int) -> int:
    """Pure: J + W + D, so a job begun just before the batch script's idle deadline still gets J."""
    return job_budget_minutes + math.ceil(
        (IDLE_ALLOWANCE_S + SCRIPT_START_MARGIN_S) / 60
    )


def minutes_in_slurm_time(slurm_time: str) -> int | None:
    """Whole minutes, rounded up, in "minutes", "minutes:seconds" or "hours:minutes:seconds"; None otherwise."""
    try:
        parts = [int(part) for part in slurm_time.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        minutes, seconds = parts[0], 0
    elif len(parts) == 2:
        minutes, seconds = parts
    elif len(parts) == 3:
        minutes, seconds = parts[0] * 60 + parts[1], parts[2]
    else:
        return None
    return minutes + math.ceil(seconds / 60)


def size_label_of_job(job_labels: list[str]) -> str | None:
    """The size label, lower-cased as GitHub compares labels, if runs-on is exactly one label we can size;
    None for any other shape."""
    if len(job_labels) != 1:
        return None
    size_label = job_labels[0].lower()
    if get_runner_resources(size_label) is None:
        return None
    return size_label
