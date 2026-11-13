"""
Our runners on both sides: their Slurm jobs (squeue, sbatch, scancel; slurmctld only, never slurmdbd)
and their GitHub registrations, which the daemon only reads, apart from creating each one's just-in-time
config and deleting the registration of one whose Slurm job has ended. "Ours" means a name starting with
the repository's runner-name prefix, on both sides; nothing else is read as ours or touched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid

from config import (
    ALLOCATE_RUNNER_SCRIPT_PATH,
    IDLE_ALLOWANCE_S,
    RUNNER_GROUP_ID,
    SLURM_COMMAND_TIMEOUT,
    SLURM_OUTPUT_PATH,
)
from github_client import GitHubClient, GitHubUnavailable
from plan import FINISHED_STATES, Allocation, RunnerRegistration
from runner_size_config import (
    RunnerResources,
    get_runner_resources,
    slurm_time_limit_minutes,
)

logger = logging.getLogger(__name__)

# id, name, comment (size label), state, priority, submit, start, end; none of ours contains "|"
SQUEUE_FORMAT = "%i|%j|%k|%T|%Q|%V|%S|%e"
# squeue prints times in this strftime format; epoch seconds need no time zone
SLURM_TIME_FORMAT = "%s"
# The batch script reads the runner's JIT config from here, so it is never in argv
JIT_CONFIG_VARIABLE = "RUNNER_JIT_CONFIG"


class SlurmUnavailable(Exception):
    """A Slurm command failed or timed out: what it would have reported is unknown, never empty."""


def read_our_allocations(runner_name_prefix: str) -> list[Allocation]:
    """squeue --me --states=all: our Slurm jobs, live and ended within MinJobAge. Raises SlurmUnavailable."""
    output = run_slurm_command(
        ["squeue", "--me", "--states=all", "--noheader", f"--format={SQUEUE_FORMAT}"],
        {"SLURM_TIME_FORMAT": SLURM_TIME_FORMAT},
    )
    allocations = []
    for line in output.splitlines():
        allocation = parse_squeue_line(line, runner_name_prefix)
        if allocation is not None:
            allocations.append(allocation)
    return allocations


def parse_squeue_line(line: str, runner_name_prefix: str) -> Allocation | None:
    """Pure: one SQUEUE_FORMAT line; None for a job that is not ours, such as the old daemon's.
    SlurmUnavailable for one of ours that cannot be read."""
    fields = line.split("|")
    if len(fields) < 2 or not fields[1].startswith(runner_name_prefix):
        return None
    if len(fields) != 8:
        raise SlurmUnavailable(f"squeue: cannot read {line!r}")
    slurm_job_id, runner_name, size_label, state, priority = fields[:5]
    submitted, started, ended = fields[5:]
    submitted_at = epoch_seconds_or_none(submitted)
    if not slurm_job_id.isdigit() or not priority.isdigit() or submitted_at is None:
        raise SlurmUnavailable(f"squeue: cannot read {line!r}")
    return Allocation(
        slurm_job_id=int(slurm_job_id),
        runner_name=runner_name,
        size_label=size_label,
        state=state,
        # Slurm's scontrol manual: "a job priority of zero prevents the job from ever being scheduled"
        is_held=state == "PENDING" and int(priority) == 0,
        submitted_at=submitted_at,
        started_at=None if state == "PENDING" else epoch_seconds_or_none(started),
        ended_at=epoch_seconds_or_none(ended) if state in FINISHED_STATES else None,
    )


def epoch_seconds_or_none(squeue_time: str) -> float | None:
    """Pure: a time squeue printed with SLURM_TIME_FORMAT; None for "Unknown", "None", "N/A" and the like."""
    return float(squeue_time) if squeue_time.isdigit() else None


def read_our_registrations(
    github: GitHubClient, repo_api_base_url: str, runner_name_prefix: str
) -> dict[str, RunnerRegistration]:
    """Our runner registrations, by name, from every page of the runners list. Raises GitHubUnavailable,
    also for one of ours without a runner id."""
    registrations = {}
    for runner in github.get_all_pages(
        f"{repo_api_base_url}/actions/runners", "runners"
    ):
        registration = parse_registration(runner, runner_name_prefix)
        if registration is not None:
            registrations[registration.runner_name] = registration
    return registrations


def parse_registration(
    runner: dict, runner_name_prefix: str
) -> RunnerRegistration | None:
    """Pure: one entry of the runners list; None unless its name has our prefix. GitHubUnavailable for one
    of ours without a runner id, which could not be deleted."""
    runner_name = runner.get("name") or ""
    if not runner_name.startswith(runner_name_prefix):
        return None
    runner_id = runner.get("id")
    if not isinstance(runner_id, int):
        raise GitHubUnavailable(f"runners list: {runner_name} has no runner id")
    return RunnerRegistration(
        runner_id=runner_id,
        runner_name=runner_name,
        is_online=runner.get("status") == "online",
        is_busy=runner.get("busy") is True,
    )


def new_runner_name(runner_name_prefix: str) -> str:
    """The prefix plus 12 random hex digits; the Slurm job and the registration both carry it."""
    return f"{runner_name_prefix}{uuid.uuid4().hex[:12]}"


def sbatch_command(
    runner_name: str, size_label: str, resources: RunnerResources
) -> list[str]:
    """Pure: sbatch argv for one runner: its size, --no-requeue (else a failed launch is held forever, #37),
    --time J + W + D, and W for its batch script. The JIT config goes in the environment, never here."""
    return [
        "sbatch",
        f"--output={SLURM_OUTPUT_PATH}",
        f"--job-name={runner_name}",
        f"--comment={size_label}",
        f"--cpus-per-task={resources.cpus}",
        f"--mem-per-cpu={resources.mem_per_cpu}",
        f"--gres=tmpdisk:{resources.tmpdisk_mib}",
        f"--time={slurm_time_limit_minutes(resources.job_budget_minutes)}",
        # Else JobRequeue=1 turns a failed launch into a held job that never ends
        "--no-requeue",
        ALLOCATE_RUNNER_SCRIPT_PATH,
        str(IDLE_ALLOWANCE_S),
    ]


def submit_runner(
    github: GitHubClient,
    repo_api_base_url: str,
    runner_name_prefix: str,
    size_label: str,
) -> None:
    """POST generate-jitconfig for a new runner of size_label, then sbatch with the config in the
    environment. Raises GitHubUnavailable or SlurmUnavailable, and ValueError for a label that is not a
    size, which demand never reports. A registration left by a failed sbatch never connects, and GitHub
    removes it."""
    resources = get_runner_resources(size_label)
    if resources is None:
        raise ValueError(f"{size_label!r} is not a size label")
    runner_name = new_runner_name(runner_name_prefix)
    jit_response = github.post_json(
        f"{repo_api_base_url}/actions/runners/generate-jitconfig",
        {
            "name": runner_name,
            "runner_group_id": RUNNER_GROUP_ID,
            "labels": [size_label],
            # On the job's tmpdisk, and the same path inside the runner's container (--bind /tmp) and for
            # the rootless Docker daemon outside it, which resolves the workspace mounts of job containers
            "work_folder": f"/tmp/{runner_name}/_work",
        },
    )
    jit_config = jit_response.get("encoded_jit_config")
    if not isinstance(jit_config, str) or not jit_config:
        raise GitHubUnavailable(
            f"generate-jitconfig for {runner_name}: no encoded_jit_config in the answer"
        )
    sbatch_output = run_slurm_command(
        sbatch_command(runner_name, size_label, resources),
        {JIT_CONFIG_VARIABLE: jit_config},
    )
    logger.info(f"Submitted {runner_name} for {size_label}: {sbatch_output.strip()}")


def cancel_pending_allocation(allocation: Allocation) -> None:
    """scancel --ctld --state=PENDING: slurmctld applies the state test under its job lock, so a job that
    started after squeue was read is left alone, and scancel still exits 0. A failure is logged, and the
    next cycle tries again."""
    reason = "held" if allocation.is_held else "unregistered for a day"
    command = ["scancel", "--ctld", "--state=PENDING", str(allocation.slurm_job_id)]
    try:
        run_slurm_command(command)
    except SlurmUnavailable as error:
        logger.warning(f"Could not cancel {allocation.runner_name}: {error}")
        return
    logger.info(
        f"Asked Slurm to cancel {allocation.runner_name} (Slurm job {allocation.slurm_job_id}) if still"
        f" pending; it was pending and {reason}, so it could never take a job"
    )


def delete_registration(
    github: GitHubClient, repo_api_base_url: str, registration: RunnerRegistration
) -> bool:
    """DELETE the registration of a runner whose Slurm job has ended. True once GitHub has answered: deleted
    (204), already gone (404), or refused (422: it believes a job is on that runner, and that job then
    waits as it would without the delete). False after any other failure, logged; the next cycle tries
    again."""
    runner_name = registration.runner_name
    try:
        status = github.delete(
            f"{repo_api_base_url}/actions/runners/{registration.runner_id}"
        )
    except GitHubUnavailable as error:
        logger.warning(f"Could not delete the registration of {runner_name}: {error}")
        return False
    if status == 422:
        logger.warning(
            f"GitHub refused to delete the registration of {runner_name}, whose Slurm job has ended:"
            " it believes a job is on that runner"
        )
    elif status == 404:
        logger.info(f"The registration of {runner_name} was already gone")
    else:
        logger.info(
            f"Deleted the registration of {runner_name}, whose Slurm job has ended"
        )
    return True


def run_slurm_command(
    command: list[str], extra_environment: dict[str, str] | None = None
) -> str:
    """A Slurm client command's stdout; SlurmUnavailable if it fails, times out or is missing. The
    environment is the daemon's plus extra_environment, which never appears in a message."""
    environment = {**os.environ, **(extra_environment or {})}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SLURM_COMMAND_TIMEOUT,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        raise SlurmUnavailable(
            f"{' '.join(command)}: no answer within {SLURM_COMMAND_TIMEOUT} s"
        ) from None
    except OSError as error:
        raise SlurmUnavailable(f"{' '.join(command)}: {error}") from None
    if result.returncode != 0:
        raise SlurmUnavailable(
            f"{' '.join(command)}: exit status {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout
