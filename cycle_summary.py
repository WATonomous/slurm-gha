"""
Where the daemon's time and GitHub requests go, cycle by cycle: a DEBUG line per cycle and, while there is work,
an INFO line per CYCLE_SUMMARY_INTERVAL_S. Only measures; nothing here changes what the daemon does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace


@dataclass
class Spent:
    """Running totals since the daemon started. The GitHub client and the Slurm command runner add to SPENT;
    a cycle's share is the difference of two snapshots."""

    github_read_s: float = 0.0
    github_write_s: float = 0.0
    # Waited before a write so that writes start MIN_SECONDS_BETWEEN_WRITES apart
    write_spacing_s: float = 0.0
    slurm_s: float = 0.0
    reads: int = 0
    # Reads GitHub counted against the rate limit: every read but a 304
    charged_reads: int = 0
    writes: int = 0
    runners_submitted: int = 0

    def snapshot(self) -> Spent:
        return replace(self)

    def minus(self, earlier: Spent) -> Spent:
        return Spent(
            **{
                part.name: getattr(self, part.name) - getattr(earlier, part.name)
                for part in fields(self)
            }
        )


SPENT = Spent()


@dataclass(frozen=True)
class CycleTimes:
    """One cycle, from its start to the next cycle's start. The parts are measured, except other_s: what is
    left (planning, logging), so that the parts add up to duration_s."""

    duration_s: float
    pause_s: float
    other_s: float
    spent: Spent
    # Queued jobs of a size we serve, as read by the repositories whose cycle completed; None when none did
    jobs_queued: int | None


def cycle_times(
    spent: Spent, duration_s: float, pause_s: float, jobs_queued: int | None
) -> CycleTimes:
    """Pure: a cycle's times from what it spent and its measured duration and pause."""
    measured_s = (
        spent.github_read_s
        + spent.github_write_s
        + spent.write_spacing_s
        + spent.slurm_s
        + pause_s
    )
    return CycleTimes(
        duration_s=duration_s,
        pause_s=pause_s,
        other_s=max(0.0, duration_s - measured_s),
        spent=spent,
        jobs_queued=jobs_queued,
    )


def cycle_line(cycle: CycleTimes) -> str:
    """Pure: the DEBUG line for one cycle."""
    spent = cycle.spent
    return (
        f"Cycle took {cycle.duration_s:.1f} s: {_time_split(spent, cycle.pause_s, cycle.other_s)};"
        f" {_requests(spent)}; {_plural(spent.runners_submitted, 'runner')} submitted;"
        f" {_queued(cycle.jobs_queued, '', 'the cycle')}"
    )


@dataclass(frozen=True)
class SummaryWindow:
    """The cycles since the last INFO summary."""

    started_at: float  # time.monotonic() at the first cycle's start
    cycles: int = 0
    longest_s: float = 0.0
    pause_s: float = 0.0
    other_s: float = 0.0
    spent: Spent = field(default_factory=Spent)
    # Cycles in which no repository's cycle completed: their reads failed, or GitHub asked for a stop
    failed_cycles: int = 0
    # A cycle that completed ended with a job queued; a failed cycle's old count does not count
    jobs_waited: bool = False
    jobs_queued_at_end: int | None = None


def window_with_cycle(window: SummaryWindow, cycle: CycleTimes) -> SummaryWindow:
    """Pure: the window with one more cycle."""
    spent = Spent(
        **{
            part.name: getattr(window.spent, part.name)
            + getattr(cycle.spent, part.name)
            for part in fields(Spent)
        }
    )
    return replace(
        window,
        cycles=window.cycles + 1,
        longest_s=max(window.longest_s, cycle.duration_s),
        pause_s=window.pause_s + cycle.pause_s,
        other_s=window.other_s + cycle.other_s,
        spent=spent,
        failed_cycles=window.failed_cycles + (cycle.jobs_queued is None),
        jobs_waited=window.jobs_waited or (cycle.jobs_queued or 0) > 0,
        jobs_queued_at_end=cycle.jobs_queued,
    )


def summary_line(
    window: SummaryWindow,
    ended_at: float,
    lowest_remaining: int | None,
    resets_in_s: float | None,
) -> str | None:
    """Pure: the INFO line for the window, or None when no cycle that completed ended with a job queued and no
    runner was submitted in it. So an outage alone logs nothing here: its own lines say so."""
    if not window.jobs_waited and window.spent.runners_submitted == 0:
        return None
    failed = f" ({window.failed_cycles} failed)" if window.failed_cycles else ""
    line = (
        f"Last {ended_at - window.started_at:.0f} s: {_plural(window.cycles, 'cycle')}{failed}, longest"
        f" {window.longest_s:.1f} s; {_time_split(window.spent, window.pause_s, window.other_s)};"
        f" {_requests(window.spent)}; {_plural(window.spent.runners_submitted, 'runner')} submitted;"
        f" {_queued(window.jobs_queued_at_end, ' at the end', 'the last cycle')}"
    )
    if lowest_remaining is not None and resets_in_s is not None:
        line += (
            f"; rate limit {lowest_remaining} left at this hour's lowest reading, resets in"
            f" {max(0.0, resets_in_s) / 60:.0f} min"
        )
    return line


def _time_split(spent: Spent, pause_s: float, other_s: float) -> str:
    return (
        f"GitHub reads {spent.github_read_s:.1f} s, GitHub writes {spent.github_write_s:.1f} s"
        f" + {spent.write_spacing_s:.1f} s spacing, Slurm {spent.slurm_s:.1f} s, pause {pause_s:.1f} s,"
        f" other {other_s:.1f} s"
    )


def _requests(spent: Spent) -> str:
    return (
        f"{_plural(spent.reads, 'read')} ({spent.charged_reads} charged),"
        f" {_plural(spent.writes, 'write')}"
    )


def _queued(jobs_queued: int | None, when: str, which_cycle: str) -> str:
    if jobs_queued is None:
        return f"jobs queued{when} unknown: {which_cycle} did not complete"
    return f"{_plural(jobs_queued, 'job')} queued{when}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
