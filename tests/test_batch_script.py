"""The batch script's exit status, which slurm-gha reads as launch evidence, against stand-ins for apptainer,
rootless Docker and the runner; Linux only, since the script finds the runner's processes through /proc.
And the bound the script's constants keep against the daemon's, everywhere."""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import config

SCRIPT = Path(__file__).parent.parent / "allocation_scripts" / "apptainer.sh"
IDLE_ALLOWANCE_S = 2
# The script's LISTENER_STOP_WAIT_S, shortened
LISTENER_STOP_WAIT_S = 2
# The script's SUPERVISOR_EXIT_MARGIN_S, shortened; long enough that only a stuck supervisor meets it
SUPERVISOR_EXIT_MARGIN_S = 10
JIT_CONFIG = "secret-jit-config-value"

# Ignores its options and the image; like --containall, passes on only APPTAINERENV_*, without the prefix.
# The image's own programs, when a test provides some, are in container-bin, first on the PATH. With a
# detach-container file, it runs the container's command detached (a double fork), so that nothing in it is
# a descendant of this process, and waits for its exit status.
APPTAINER = """#!/bin/bash
stub_dir=$(dirname "$0")/..
container_bin=$stub_dir/container-bin
[[ -d $container_bin ]] && PATH="$container_bin:$PATH"
[[ $1 == exec ]] || exit 99
shift
while [[ $1 == --* ]]; do
    if [[ $1 == --bind ]]; then shift; fi
    shift
done
shift
for name in $(compgen -e); do
    case $name in
    APPTAINERENV_*) export "${name#APPTAINERENV_}=${!name}"; unset "$name" ;;
    PATH | HOME) ;;
    *) unset "$name" ;;
    esac
done
if [[ -e $stub_dir/detach-container ]]; then
    ( ( "$@"; echo $? > "$stub_dir/container-status" ) & )
    while [[ ! -s $stub_dir/container-status ]]; do sleep 0.2; done
    exit "$(cat "$stub_dir/container-status")"
fi
exec "$@"
"""

SLURM_START_DOCKERD = """#!/bin/bash
env > "$STUB_DIR/dockerd-environment"
if [[ $(cat "$STUB_DIR/docker") == fail ]]; then exit 1; fi
"$STUB_PYTHON" -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$STUB_DIR/docker.sock"
"""

# As careless as such profiles are about unset variables and exit statuses
CC_PROFILE = """echo "${SOME_UNSET_VARIABLE}" > /dev/null
module() { false; }
false
"""

# As GitHub's: a listener that exits with code 2 is relaunched (after 5 s; here 0.5 s), and otherwise both
# exit 0 however the listener ended
RUN_SH = """#!/bin/bash
while :; do
    "$(dirname "$0")/run-helper.sh"
    [[ $? -eq 2 ]] || exit 0
done
"""
RUN_HELPER_SH = """#!/bin/bash
"$(dirname "$0")/bin/Runner.Listener" run
if [[ $? -eq 2 ]]; then
    sleep 0.5
    exit 2
fi
exit 0
"""

# Ignores SIGINT, as the real one does; SIGTERM stops it. Modes:
#   idle             waits for a job that never comes
#   deaf             the same, but SIGTERM does not stop it either (a listener the script cannot stop)
#   reject           GitHub refuses the registration
#   job              a job arrives after 0.3 s and runs past the deadline
#   job-cleans-tmp   the same, and a step deletes the claim 2 s in
#   slow-setup       a job is taken after 0.3 s, but its hook runs only 1.5 s after the deadline
#   relaunched       SIGTERM makes it exit with code 2, so run-helper.sh relaunches it, idle
#   long-job         a job arrives after 0.3 s and runs 10 s
#   longer-job       a job arrives after 0.3 s and runs 20 s
#   worker-no-hook   a Runner.Worker starts after 0.3 s and lives 18 s, but no hook ever runs; then idle
RUNNER_LISTENER = """#!/bin/bash
runner_dir=$(dirname "$0")/..
events="$runner_dir/events"
trap '' INT
trap 'echo SIGTERM >> "$events"; exit 0' TERM
echo "started jit=$ACTIONS_RUNNER_INPUT_JITCONFIG" >> "$events"
mode=$(cat "$runner_dir/mode")
run_job() {
    if bash "$ACTIONS_RUNNER_HOOK_JOB_STARTED"; then
        echo "job claimed" >> "$events"
        if [[ $mode == job-cleans-tmp ]]; then
            sleep 2
            rm -r "$(dirname "$ACTIONS_RUNNER_HOOK_JOB_STARTED")/claim"
        fi
        sleep "$1"
        echo "job finished" >> "$events"
    fi
}
case $mode in
idle) while :; do sleep 0.1; done ;;
deaf) trap 'echo SIGTERM >> "$events"' TERM; while :; do sleep 0.1; done ;;
relaunched)
    if [[ ! -e $runner_dir/relaunched ]]; then
        touch "$runner_dir/relaunched"
        trap 'echo SIGTERM >> "$events"; exit 2' TERM
    fi
    while :; do sleep 0.1; done
    ;;
reject) exit 1 ;;
job | job-cleans-tmp) sleep 0.3; run_job 3.5 ;;
long-job) sleep 0.3; run_job 10 ;;
longer-job) sleep 0.3; run_job 20 ;;
worker-no-hook)
    sleep 0.3
    "$(dirname "$0")/Runner.Worker" &
    sleep 18
    kill $!
    echo "worker ended" >> "$events"
    while :; do sleep 0.1; done
    ;;
slow-setup)
    sleep 0.3
    "$(dirname "$0")/Runner.Worker" &
    sleep 3.2
    run_job 1
    kill $!
    ;;
esac
"""
RUNNER_WORKER = """#!/bin/bash
sleep 60
"""


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def with_constant(script: str, name: str, value: str) -> str:
    """The script with one of its readonly paths pointed at the stand-ins."""
    pattern = re.compile(rf"^readonly {name}=.*$", re.MULTILINE)
    if len(pattern.findall(script)) != 1:
        raise AssertionError(f"apptainer.sh has no single readonly {name}")
    return pattern.sub(f'readonly {name}="{value}"', script)


# slurm.conf's BatchStartTimeout: the most the script's start can lag its job's start
BATCH_START_TIMEOUT_S = 10


class ConstantsAcrossFilesTest(unittest.TestCase):
    """Runs everywhere: what the script's constants promise the daemon's."""

    def test_a_job_taken_by_a_stuck_runner_still_gets_its_budget(self):
        # A runner whose supervisor is stuck stays online until its deadline plus LISTENER_STOP_WAIT_S plus
        # SUPERVISOR_EXIT_MARGIN_S. A job it takes then still gets J only while those two stay below D less
        # the script's start delay, since its time limit is J + W + D
        script = SCRIPT.read_text()

        def script_constant(name):
            return int(re.search(rf"^readonly {name}=(\d+)$", script, re.M)[1])

        stuck_for_s = script_constant("LISTENER_STOP_WAIT_S") + script_constant(
            "SUPERVISOR_EXIT_MARGIN_S"
        )
        self.assertLess(
            stuck_for_s, config.SCRIPT_START_MARGIN_S - BATCH_START_TIMEOUT_S
        )


@unittest.skipUnless(sys.platform == "linux", "the script reads /proc")
class BatchScriptExitTest(unittest.TestCase):
    def start(
        self,
        docker: str,
        listener_mode: str,
        idle_allowance_s: int = IDLE_ALLOWANCE_S,
        listener_stop_wait_s: int = LISTENER_STOP_WAIT_S,
        stuck_supervisor: bool = False,
        detach_container: bool = False,
    ) -> dict:
        """Runs a copy of apptainer.sh as Slurm would, in the background, against the stand-ins. With
        stuck_supervisor, the image's date never answers once the runner has started, as when CVMFS stops
        answering: the supervisor stops at its next date."""
        stub_dir = Path(tempfile.mkdtemp(prefix="slurm-gha-test-"))
        self.addCleanup(shutil.rmtree, stub_dir, ignore_errors=True)
        runner_dir = stub_dir / "runner"
        write_executable(stub_dir / "bin" / "apptainer", APPTAINER)
        write_executable(
            stub_dir / "bin" / "slurm-start-dockerd.sh", SLURM_START_DOCKERD
        )
        write_executable(runner_dir / "run.sh", RUN_SH)
        write_executable(runner_dir / "run-helper.sh", RUN_HELPER_SH)
        write_executable(runner_dir / "bin" / "Runner.Listener", RUNNER_LISTENER)
        write_executable(runner_dir / "bin" / "Runner.Worker", RUNNER_WORKER)
        (stub_dir / "cc-profile.sh").write_text(CC_PROFILE)
        (stub_dir / "docker").write_text(docker)
        (runner_dir / "mode").write_text(listener_mode)
        (runner_dir / "events").touch()

        script = SCRIPT.read_text()
        script = with_constant(script, "RUNNER_DIR", str(runner_dir))
        script = with_constant(script, "DOCKER_SOCKET", str(stub_dir / "docker.sock"))
        script = with_constant(script, "CC_PROFILE", str(stub_dir / "cc-profile.sh"))
        script = with_constant(
            script, "LISTENER_STOP_WAIT_S", str(listener_stop_wait_s)
        )
        script = with_constant(
            script, "SUPERVISOR_EXIT_MARGIN_S", str(SUPERVISOR_EXIT_MARGIN_S)
        )
        write_executable(stub_dir / "apptainer.sh", script)

        runner_name = f"gha-batch-script-test-{uuid.uuid4().hex[:12]}"
        work_dir = Path("/tmp") / runner_name
        if detach_container:
            (stub_dir / "detach-container").touch()
        if stuck_supervisor:
            write_executable(
                stub_dir / "container-bin" / "date",
                f"#!/bin/bash\n[[ -e {work_dir}/runner-started ]] && exec sleep 3600\n"
                'exec /bin/date "$@"\n',
            )
        self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
        environment = {
            "PATH": f"{stub_dir / 'bin'}:{os.environ['PATH']}",
            "HOME": str(stub_dir),
            "SLURM_JOB_NAME": runner_name,
            "RUNNER_JIT_CONFIG": JIT_CONFIG,
            "STUB_DIR": str(stub_dir),
            "STUB_PYTHON": sys.executable,
        }
        # A file, not a pipe: a listener the script could not stop would hold a pipe open
        with open(stub_dir / "output", "w") as output:
            process = subprocess.Popen(
                ["bash", str(stub_dir / "apptainer.sh"), str(idle_allowance_s)],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # The stand-in apptainer has no PID namespace to end what the script leaves running; the test does
        self.addCleanup(self.kill_process_group, process)
        return {
            "process": process,
            "stub_dir": stub_dir,
            "runner_dir": runner_dir,
            "work_dir": work_dir,
        }

    @staticmethod
    def kill_process_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def finish(self, run: dict) -> tuple[int, str, str]:
        """The exit status, the script's output and what the listener recorded."""
        status = run["process"].wait(timeout=IDLE_ALLOWANCE_S + 30)
        output = (run["stub_dir"] / "output").read_text()
        events = (run["runner_dir"] / "events").read_text()
        return status, output, events

    def test_exit_status_per_outcome(self):
        # Started together, so the four take as long as the slowest
        runs = {
            "ran a job": self.start("ok", "job"),
            "stopped idle at the deadline": self.start("ok", "idle"),
            "the runner exited early": self.start("ok", "reject"),
            "Docker failed": self.start("fail", "idle"),
        }
        expected = {
            "ran a job": 0,
            "stopped idle at the deadline": 0,
            "the runner exited early": 1,
            "Docker failed": 1,
        }
        results = {case: self.finish(run) for case, run in runs.items()}
        for case, (status, output, events) in results.items():
            with self.subTest(case):
                self.assertEqual(status, expected[case], output)

        _, _, events = results["ran a job"]
        self.assertIn("job finished", events)
        self.assertNotIn("SIGTERM", events)
        _, _, events = results["stopped idle at the deadline"]
        self.assertIn(f"started jit={JIT_CONFIG}", events)
        self.assertIn("SIGTERM", events)
        _, _, events = results["Docker failed"]
        self.assertEqual(events, "")

    def test_the_deadline_never_takes_a_job(self):
        runs = {
            # A step that cleans /tmp deletes the claim before the deadline
            "the job deleted its claim": self.start("ok", "job-cleans-tmp", 4),
            # The listener has a Runner.Worker at the deadline: the job's hook is still to come
            "a job was being set up": self.start("ok", "slow-setup"),
        }
        for case, run in runs.items():
            with self.subTest(case):
                status, output, events = self.finish(run)
                self.assertEqual(status, 0, output)
                self.assertIn("job finished", events)
                self.assertNotIn("SIGTERM", events)

    def test_a_listener_that_does_not_stop_is_left_to_the_container_end(self):
        status, output, events = self.finish(self.start("ok", "deaf"))
        self.assertEqual(status, 0, output)
        # One SIGTERM, then a wait: sending it again gains nothing
        self.assertEqual(events.count("SIGTERM"), 1, events)
        self.assertIn("the listener was not stopped", output)

    def test_a_listener_relaunched_after_the_stop_gets_its_own_sigterm(self):
        run = self.start("ok", "relaunched", listener_stop_wait_s=6)
        status, output, events = self.finish(run)
        self.assertEqual(status, 0, output)
        self.assertEqual(events.count("started"), 2, events)
        self.assertEqual(events.count("SIGTERM"), 2, events)
        self.assertIn("Stopping a listener relaunched after the idle claim", output)
        self.assertNotIn("the listener was not stopped", output)

    def finish_all(self, runs: dict, started: float) -> dict:
        """Each run's exit status, output, events and seconds from started (time.monotonic) to its end."""
        ended_after = {}
        while len(ended_after) < len(runs):
            for case, run in runs.items():
                if case not in ended_after and run["process"].poll() is not None:
                    ended_after[case] = time.monotonic() - started
            self.assertLess(time.monotonic() - started, 60, "a run did not end")
            time.sleep(0.2)
        results = {}
        for case, run in runs.items():
            output = (run["stub_dir"] / "output").read_text()
            events = (run["runner_dir"] / "events").read_text()
            results[case] = (
                run["process"].returncode,
                output,
                events,
                ended_after[case],
            )
        return results

    def freeze_supervisor_once_a_job_claims(self, run: dict) -> None:
        """SIGSTOP the supervisor once the job's hook has claimed the runner. A frozen supervisor cannot
        reap its children: the ended run.sh stays a zombie, as on the cluster."""
        owner = run["work_dir"] / "claim" / "owner"
        give_up_at = time.monotonic() + 10
        while not (owner.exists() and owner.read_text().strip() == "job"):
            self.assertLess(time.monotonic(), give_up_at, "no job claimed the runner")
            time.sleep(0.1)
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                argv = cmdline.read_bytes().split(b"\0")
            except OSError:
                continue
            if b"supervise-runner" in argv and str(run["work_dir"]).encode() in argv:
                os.kill(int(cmdline.parent.name), signal.SIGSTOP)
                return
        self.fail("no supervisor found")

    def test_the_outer_guard_ends_a_stuck_supervisor_but_never_a_job(self):
        # The guard may act from W + LISTENER_STOP_WAIT_S + SUPERVISOR_EXIT_MARGIN_S = 14 s
        stuck_after = IDLE_ALLOWANCE_S + LISTENER_STOP_WAIT_S + SUPERVISOR_EXIT_MARGIN_S
        started = time.monotonic()
        runs = {
            "no job": self.start("ok", "idle", stuck_supervisor=True),
            "a job past it": self.start("ok", "long-job", stuck_supervisor=True),
            "a worker, no claim": self.start(
                "ok", "worker-no-hook", stuck_supervisor=True
            ),
            "a job, then a frozen supervisor": self.start("ok", "long-job"),
            # The guard's walk cannot see into the container: it must do nothing
            "a job, container out of sight": self.start(
                "ok", "longer-job", detach_container=True
            ),
        }
        self.freeze_supervisor_once_a_job_claims(
            runs["a job, then a frozen supervisor"]
        )
        results = self.finish_all(runs, started)

        status, output, _, ended_after = results["no job"]
        self.assertEqual(status, 1, output)
        self.assertIn("Exit 1: no job holds the runner", output)
        self.assertGreaterEqual(ended_after, stuck_after - 1)

        # The job holds the claim: it runs to its end, then the guard ends the stuck supervisor
        status, output, events, _ = results["a job past it"]
        self.assertEqual(status, 0, output)
        self.assertIn("job finished", events)
        self.assertIn("the runner ended, but the supervisor did not", output)

        # The same with the ended run.sh left a zombie: it no longer counts as the runner
        status, output, events, _ = results["a job, then a frozen supervisor"]
        self.assertEqual(status, 0, output)
        self.assertIn("job finished", events)
        self.assertIn("the runner ended, but the supervisor did not", output)

        # A walk that never saw run.sh takes neither exit: the 20 s job outlasts both of the guard's
        # triggers (claim + margin at about 10 s, W + stop wait + margin at 14 s) and still runs to its end
        status, output, events, ended_after = results["a job, container out of sight"]
        self.assertEqual(status, 0, output)
        self.assertIn("job finished", events)
        self.assertNotIn("Watching run.sh", output)
        self.assertNotIn("the runner ended, but the supervisor did not", output)
        self.assertIn("Exit 0: a job claimed this runner\n", output)
        self.assertGreaterEqual(ended_after, 20)
        self.assertIn("Watching run.sh", results["a job past it"][1])

        # A Runner.Worker under the container keeps the guard off, whatever the claim says
        status, output, events, ended_after = results["a worker, no claim"]
        self.assertEqual(status, 1, output)
        self.assertIn("worker ended", events)
        self.assertGreaterEqual(ended_after, 18)

    def test_docker_never_sees_the_jit_config(self):
        run = self.start("ok", "reject")
        self.finish(run)
        docker_environment = (run["stub_dir"] / "dockerd-environment").read_text()
        self.assertIn("SLURM_JOB_NAME=", docker_environment)
        self.assertNotIn(JIT_CONFIG, docker_environment)


if __name__ == "__main__":
    unittest.main()
