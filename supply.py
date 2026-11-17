"""
Our runners on both sides: their Slurm jobs (squeue, sbatch, scancel; slurmctld only, never slurmdbd)
and their GitHub registrations, which the daemon only reads, apart from creating each one's just-in-time
config and deleting the registration of one whose Slurm job has ended. "Ours" means a name starting with
the repository's runner-name prefix, on both sides; nothing else is read as ours or touched.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from enum import Enum

from config import (
    ALLOCATE_RUNNER_SCRIPT_PATH,
    IDLE_ALLOWANCE_S,
    RUNNER_GROUP_ID,
    SLURM_COMMAND_TIMEOUT,
    SLURM_OUTPUT_PATH,
)
from github_client import GitHubClient, GitHubUnavailable
from plan import FINISHED_STATES, Allocation, RunnerRegistration, Submission
from runner_size_config import (
    RunnerResources,
    get_runner_resources,
    slurm_time_limit_minutes,
)

logger = logging.getLogger(__name__)

# id, name, comment (size label), state, priority, submit, start, end, node, requested node, excluded
# nodes; none of ours contains "|"
SQUEUE_FORMAT = "%i|%j|%k|%T|%Q|%V|%S|%e|%N|%n|%x"
# squeue prints times in this strftime format; epoch seconds need no time zone
SLURM_TIME_FORMAT = "%s"
# Job ids per scontrol update: about 10 ms each in slurmctld, so a call stays well inside the timeout
SCONTROL_JOB_IDS_PER_CALL = 100
# The batch script reads the runner's JIT config from here, so it is never in argv
JIT_CONFIG_VARIABLE = "RUNNER_JIT_CONFIG"


# Slurm job ids whose unreadable %x was logged: once each
unreadable_exclusions_logged: set[int] = set()


class SlurmUnavailable(Exception):
    """A Slurm command failed or timed out: what it would have reported is unknown, never empty."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        # What the command printed on stderr, when it ran and failed
        self.stderr = stderr


class SlurmTimedOut(SlurmUnavailable):
    """A Slurm command was killed at SLURM_COMMAND_TIMEOUT: whether its request took effect is unknown."""


class SubmissionRefused(SlurmUnavailable):
    """sbatch failed and squeue shows only the FAILED record, with no node, that Slurm leaves for a
    submission it refused for its constraints; the registration made for it has been deleted."""

    def __init__(self, runner_name: str, message: str):
        super().__init__(message)
        self.runner_name = runner_name
        self.message = message  # sbatch's stderr, for the log


class SbatchLeftNoJob(SlurmUnavailable):
    """sbatch failed and squeue shows nothing by the runner's name; the registration made for it has been
    deleted. Not a refusal: slurmctld may not have answered, or a node the submission named may be unknown
    to it, which only nodes_slurm_knows can tell."""

    def __init__(self, runner_name: str, message: str):
        super().__init__(message)
        self.runner_name = runner_name
        self.message = message  # sbatch's stderr, for the log


class AfterFailedSbatch(Enum):
    """What squeue shows by a runner's name after its sbatch exited non-zero."""

    LIVE_JOB = "a job not ended, or ended with a node: Slurm accepted it after all"
    # With our flags, because every node that can hold it is excluded, or the pinned one cannot hold it
    REFUSED_RECORD = "only records that ended with no node (FAILED, BadConstraints)"
    # A node it named is unknown to Slurm, or slurmctld was not reached
    NOTHING = "no job by the runner's name"


def read_our_allocations(runner_name_prefix: str) -> list[Allocation]:
    """squeue --me --states=all: our Slurm jobs, live and ended within MinJobAge. Raises SlurmUnavailable."""
    output = run_slurm_command(
        ["squeue", "--me", "--states=all", "--noheader", f"--format={SQUEUE_FORMAT}"],
        {"SLURM_TIME_FORMAT": SLURM_TIME_FORMAT},
    )
    allocations = []
    for line in output.splitlines():
        allocation = parse_squeue_line(line, runner_name_prefix)
        if allocation is None:
            continue
        allocations.append(allocation)
        if (
            allocation.excluded_nodes is None
            and allocation.slurm_job_id not in unreadable_exclusions_logged
        ):
            unreadable_exclusions_logged.add(allocation.slurm_job_id)
            logger.warning(
                f"{allocation.runner_name}: cannot read its excluded nodes in {line!r};"
                " its exclusion is left as it is"
            )
    return allocations


def parse_squeue_line(line: str, runner_name_prefix: str) -> Allocation | None:
    """Pure: one SQUEUE_FORMAT line; None for a job that is not ours, such as the old daemon's.
    SlurmUnavailable for one of ours that cannot be read, except its excluded nodes: None when %x does not
    parse."""
    fields = line.split("|")
    if len(fields) < 2 or not fields[1].startswith(runner_name_prefix):
        return None
    if len(fields) != 11:
        raise SlurmUnavailable(f"squeue: cannot read {line!r}")
    slurm_job_id, runner_name, size_label, state, priority = fields[:5]
    submitted, started, ended, node, pinned_node, excluded = fields[5:]
    submitted_at = epoch_seconds_or_none(submitted)
    if not slurm_job_id.isdigit() or not priority.isdigit() or submitted_at is None:
        raise SlurmUnavailable(f"squeue: cannot read {line!r}")
    try:
        excluded_nodes = node_names_in_hostlist(excluded)
    except ValueError:
        excluded_nodes = None  # left alone rather than guessed
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
        node=node or None,
        pinned_node=pinned_node or None,
        excluded_nodes=excluded_nodes,
    )


def node_names_in_hostlist(hostlist: str) -> frozenset[str]:
    """Pure: the node names in a hostlist as squeue prints %x: sorted and compressed into ranges,
    "tr-slurm2,trpro-slurm[1-2]" for tr-slurm2, trpro-slurm1 and trpro-slurm2. Each range keeps the width
    of its first number ("node[08-11]", "node[1-03]"), as `scontrol show hostnames` does. Empty for "";
    ValueError for anything it does not fully parse, where `scontrol show hostnames` exits 0 regardless."""
    names = set()
    item, depth = "", 0
    for character in hostlist + ",":
        depth += {"[": 1, "]": -1}.get(character, 0)
        if depth not in (0, 1):
            raise ValueError(f"unbalanced brackets in {hostlist!r}")
        if character == "," and depth == 0:
            names |= hostlist_item_names(item)
            item = ""
        else:
            item += character
    if depth != 0:
        raise ValueError(f"unbalanced brackets in {hostlist!r}")
    return frozenset(names - {""})


def hostlist_item_names(item: str) -> set[str]:
    """Pure: one comma-free hostlist entry with any number of bracketed ranges, e.g. "rack[1-2]-n[1,3]"."""
    if "[" not in item:
        if not re.fullmatch(r"[A-Za-z0-9._-]*", item):
            raise ValueError(f"not a node name: {item!r}")
        return {item}
    start = item.index("[")
    end = item.index("]", start)
    prefix, ranges, rest = item[:start], item[start + 1 : end], item[end + 1 :]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise ValueError(f"not a node name: {item!r}")
    numbers = []
    for bounds in ranges.split(","):
        first, _, last = bounds.partition("-")
        last = last or first
        if not first.isdigit() or not last.isdigit() or int(first) > int(last):
            raise ValueError(f"cannot read the range {bounds!r} in {item!r}")
        numbers += [str(n).zfill(len(first)) for n in range(int(first), int(last) + 1)]
    return {
        prefix + number + tail
        for number in numbers
        for tail in hostlist_item_names(rest)
    }


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
    runner_name: str, submission: Submission, resources: RunnerResources
) -> list[str]:
    """Pure: sbatch argv for one runner: its size, where it may go (--exclude, or --nodes=1 --nodelist for
    a pinned one), --no-requeue (else a failed launch is held forever, #37), --time J + W + D, and W for its
    batch script. The JIT config goes in the environment, never here."""
    placement = []
    if submission.pinned_node is not None:
        placement = ["--nodes=1", f"--nodelist={submission.pinned_node}"]
    elif submission.excluded_nodes:
        placement = [f"--exclude={','.join(sorted(submission.excluded_nodes))}"]
    return [
        "sbatch",
        f"--output={SLURM_OUTPUT_PATH}",
        f"--job-name={runner_name}",
        f"--comment={submission.size_label}",
        f"--cpus-per-task={resources.cpus}",
        f"--mem-per-cpu={resources.mem_per_cpu}",
        f"--gres=tmpdisk:{resources.tmpdisk_mib}",
        f"--time={slurm_time_limit_minutes(resources.job_budget_minutes)}",
        # Else JobRequeue=1 turns a failed launch into a held job that never ends
        "--no-requeue",
        *placement,
        ALLOCATE_RUNNER_SCRIPT_PATH,
        str(IDLE_ALLOWANCE_S),
    ]


def submit_runner(
    github: GitHubClient,
    repo_api_base_url: str,
    runner_name_prefix: str,
    submission: Submission,
) -> str:
    """POST generate-jitconfig for a new runner of the submission's size, then sbatch with the config in the
    environment; the runner's name. Raises GitHubUnavailable or SlurmUnavailable, and ValueError for a label
    that is not a size, which demand never reports. When sbatch exits non-zero, squeue decides: a live job
    is a submitted runner after all; otherwise the registration is deleted (a failed delete is only
    logged) and SubmissionRefused raised for a FAILED record with no node, SbatchLeftNoJob for nothing. If
    squeue cannot be read, or sbatch timed out and its request may still be queued in slurmctld, the
    registration is left and SlurmUnavailable raised."""
    size_label = submission.size_label
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
    try:
        sbatch_output = run_slurm_command(
            sbatch_command(runner_name, submission, resources),
            {JIT_CONFIG_VARIABLE: jit_config},
        )
    except SlurmTimedOut:
        # The request may still be queued in slurmctld, after the squeue that would check for it
        raise
    except SlurmUnavailable as error:
        after = what_squeue_shows_after_failed_sbatch(runner_name)
        if after is None:
            raise
        if after is AfterFailedSbatch.LIVE_JOB:
            logger.info(f"Submitted {runner_name} for {size_label} although {error}")
            return runner_name
        # Otherwise the registration waits a day for a runner that will never start
        runner_id = (jit_response.get("runner") or {}).get("id")
        if isinstance(runner_id, int):
            delete_registration(
                github,
                repo_api_base_url,
                runner_id,
                runner_name,
                why="sbatch created no job",
            )
        message = error.stderr or str(error)
        if after is AfterFailedSbatch.NOTHING:
            raise SbatchLeftNoJob(runner_name, message) from None
        raise SubmissionRefused(runner_name, message) from None
    placement = ""
    if submission.pinned_node is not None:
        placement = f", pinned to {submission.pinned_node}"
    elif submission.excluded_nodes:
        placement = f", excluding {','.join(sorted(submission.excluded_nodes))}"
    logger.info(
        f"Submitted {runner_name} for {size_label}{placement}: {sbatch_output.strip()}"
    )
    return runner_name


def what_squeue_shows_after_failed_sbatch(
    runner_name: str,
) -> AfterFailedSbatch | None:
    """squeue --me --states=all --name=<runner_name>; None when squeue cannot be read. sbatch can fail after
    slurmctld created its job, whose runner needs the registration. A submission refused because every
    eligible node is excluded still leaves a record under the name for MinJobAge, which can never run:
    FAILED, reason BadConstraints, no node (job 753319)."""
    try:
        output = run_slurm_command(
            [
                "squeue",
                "--me",
                "--states=all",
                "--noheader",
                f"--name={runner_name}",
                "--format=%T|%N",
            ]
        )
    except SlurmUnavailable:
        return None
    records = [line.partition("|") for line in output.splitlines()]
    if any(state not in FINISHED_STATES or node for state, _, node in records):
        return AfterFailedSbatch.LIVE_JOB
    if records:
        return AfterFailedSbatch.REFUSED_RECORD
    return AfterFailedSbatch.NOTHING


def nodes_slurm_knows() -> frozenset[str]:
    """sinfo --all --noheader --Node --format=%N: every node of every partition, hidden ones included, one
    per line and once per partition, never compressed; drained nodes are listed. Asked only after an
    SbatchLeftNoJob that named nodes. Raises SlurmUnavailable when slurmctld cannot be asked (exit 1,
    "Unable to contact slurm controller", after about 9 s) or lists no node; never read as "unknown"."""
    output = run_slurm_command(
        ["sinfo", "--all", "--noheader", "--Node", "--format=%N"]
    )
    nodes = frozenset(line.strip() for line in output.splitlines() if line.strip())
    if not nodes:
        # Read as the full list, it would make every node named unknown
        raise SlurmUnavailable("sinfo listed no node")
    return nodes


def narrow_pending_exclusions(
    allocations: list[Allocation], excluded_nodes: frozenset[str]
) -> set[int]:
    """scontrol update JobId=<id>,<id>,... ExcNodeList=<nodes> for these pending runners of ours, at most
    SCONTROL_JOB_IDS_PER_CALL ids a call: the whole new list, empty to clear it. The ids Slurm did not update.
    slurmctld updates each listed job still pending, starts it at once if it can now run, and refuses the
    others (started, ended, gone) while still updating the rest; it then exits 1 with one "<reason> for job
    <id>" line each, logged at debug. A call that fails otherwise is logged at warning and all its ids are
    returned. Either way the next cycle reads %x again."""
    not_updated = set()
    ids = [allocation.slurm_job_id for allocation in allocations]
    for first in range(0, len(ids), SCONTROL_JOB_IDS_PER_CALL):
        chunk = ids[first : first + SCONTROL_JOB_IDS_PER_CALL]
        command = [
            "scontrol",
            "update",
            f"JobId={','.join(map(str, chunk))}",
            f"ExcNodeList={','.join(sorted(excluded_nodes))}",
        ]
        try:
            run_slurm_command(command)
        except SlurmUnavailable as error:
            named = {int(job_id) for job_id in re.findall(r"for job (\d+)", str(error))}
            if named:
                logger.debug(f"Slurm left some pending runners as they were: {error}")
                not_updated |= named
            else:
                logger.warning(f"Could not narrow pending runners' exclusions: {error}")
                not_updated |= set(chunk)
    return not_updated


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
    github: GitHubClient,
    repo_api_base_url: str,
    runner_id: int,
    runner_name: str,
    why: str,
) -> bool:
    """DELETE the registration of a runner that will take no job (why says which, for the log: its Slurm
    job has ended, or sbatch created none). True once GitHub has answered: deleted (204), already gone
    (404), or declined (422: it believes a job is on that runner, and that job then waits as it would
    without the delete). False after any other failure, logged; the next cycle tries again."""
    try:
        status = github.delete(f"{repo_api_base_url}/actions/runners/{runner_id}")
    except GitHubUnavailable as error:
        logger.warning(
            f"Could not delete the registration of {runner_name} ({why}): {error}"
        )
        return False
    if status == 422:
        logger.warning(
            f"GitHub declined to delete the registration of {runner_name} ({why}):"
            " it believes a job is on that runner"
        )
    elif status == 404:
        logger.info(f"The registration of {runner_name} was already gone ({why})")
    else:
        logger.info(f"Deleted the registration of {runner_name}: {why}")
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
        raise SlurmTimedOut(
            f"{' '.join(command)}: no answer within {SLURM_COMMAND_TIMEOUT} s"
        ) from None
    except OSError as error:
        raise SlurmUnavailable(f"{' '.join(command)}: {error}") from None
    if result.returncode != 0:
        raise SlurmUnavailable(
            f"{' '.join(command)}: exit status {result.returncode}: {result.stderr.strip()}",
            stderr=result.stderr.strip(),
        )
    return result.stdout
