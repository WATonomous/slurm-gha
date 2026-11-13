"""The batch script's exit status, which slurm-gha reads as launch evidence, against stand-ins for apptainer,
rootless Docker and the runner. Linux only: the script finds the runner's listener through /proc."""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "allocation_scripts" / "apptainer.sh"
IDLE_ALLOWANCE_S = 2
# The script's LISTENER_STOP_WAIT_S, shortened
LISTENER_STOP_WAIT_S = 2
JIT_CONFIG = "secret-jit-config-value"

# Ignores its options and the image; like --containall, passes on only APPTAINERENV_*, without the prefix
APPTAINER = """#!/bin/bash
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


@unittest.skipUnless(sys.platform == "linux", "the script reads /proc")
class BatchScriptExitTest(unittest.TestCase):
    def start(
        self,
        docker: str,
        listener_mode: str,
        idle_allowance_s: int = IDLE_ALLOWANCE_S,
        listener_stop_wait_s: int = LISTENER_STOP_WAIT_S,
    ) -> dict:
        """Runs a copy of apptainer.sh as Slurm would, in the background, against the stand-ins."""
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
        write_executable(stub_dir / "apptainer.sh", script)

        runner_name = f"gha-batch-script-test-{uuid.uuid4().hex[:12]}"
        work_dir = Path("/tmp") / runner_name
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
        return {"process": process, "stub_dir": stub_dir, "runner_dir": runner_dir}

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

    def test_docker_never_sees_the_jit_config(self):
        run = self.start("ok", "reject")
        self.finish(run)
        docker_environment = (run["stub_dir"] / "dockerd-environment").read_text()
        self.assertIn("SLURM_JOB_NAME=", docker_environment)
        self.assertNotIn(JIT_CONFIG, docker_environment)


if __name__ == "__main__":
    unittest.main()
