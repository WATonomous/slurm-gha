#!/bin/bash
# Runs one GitHub Actions just-in-time runner in one Slurm job. slurm-gha submits it as
#   sbatch --job-name=<runner name> ... apptainer.sh <idle allowance in seconds>
# with the runner's JIT config in RUNNER_JIT_CONFIG (environment, never argv).
#
# Two halves in one file:
#   - the Slurm job, outside the container: starts rootless Docker and the runner's container, and ends the
#     job if what runs in the container hangs;
#   - the supervisor, inside the container (supervise-runner): runs the runner and stops it if no job
#     starts by the idle deadline.
# A job (through GitHub's job-started hook) and the idle deadline race to claim the runner by creating
# $work_dir/claim; exactly one wins. A hook that loses fails its job before any step runs.
#
# Exit status, which slurm-gha reads as launch evidence for the node (run.sh exits 0 whatever happened):
#   0  a job claimed the runner, or the idle deadline did
#   1  the runner did not launch, or ended with no claim, or its supervisor hung with no job
set -o pipefail -o errexit -o nounset

readonly RUNNER_IMAGE="/cvmfs/unpacked.cern.ch/ghcr.io/watonomous/actions-runner-image:main"
readonly RUNNER_DIR="/home/runner" # in the image
readonly DOCKER_SOCKET="/tmp/run/docker.sock" # created by slurm-start-dockerd.sh
readonly CC_PROFILE="/cvmfs/soft.computecanada.ca/config/profile/bash.sh"
# How long the supervisor waits for the listener to stop after the idle claim
readonly LISTENER_STOP_WAIT_S=15
# How long past the supervisor's give-up the Slurm job waits before ending a hung supervisor. Plus
# LISTENER_STOP_WAIT_S, it must stay below config.SCRIPT_START_MARGIN_S less BatchStartTimeout, so that a
# job taken by a hung runner still gets its full time limit (checked in tests/test_batch_script.py)
readonly SUPERVISOR_EXIT_MARGIN_S=30
export PATH="$PATH:/opt/slurm/bin" # slurm-start-dockerd.sh

main() {
    if [[ ${1:-} == supervise-runner ]]; then
        supervise_runner "${@:2}"
    else
        run_slurm_job "$@"
    fi
}

# --- The Slurm job, outside the container ---

run_slurm_job() {
    local idle_allowance_s=$1
    local deadline=$(($(date +%s) + idle_allowance_s))
    local work_dir="/tmp/$SLURM_JOB_NAME"
    # Out of the environment, so that Docker and the job's other children never inherit it
    local jit_config=$RUNNER_JIT_CONFIG
    unset RUNNER_JIT_CONFIG

    prepare_work_dir "$work_dir"
    log "Runner $SLURM_JOB_NAME: idle deadline at $(date -d "@$deadline" +'%H:%M:%S')"

    launch_runner_container "$work_dir" "$deadline" "$jit_config" &
    local launcher_pid=$!
    unset jit_config

    wait_for_runner_to_start "$work_dir" "$deadline" "$launcher_pid"
    if runner_has_started "$work_dir"; then
        end_job_if_supervisor_hangs "$work_dir" "$deadline" "$launcher_pid"
    fi

    local launcher_status=0
    wait "$launcher_pid" || launcher_status=$?
    if ! runner_has_started "$work_dir"; then
        log "Exit 1: Docker, apptainer or the image failed (status $launcher_status) before the runner started"
        exit 1
    fi
    exit "$launcher_status" # the supervisor's, passed on by apptainer
}

prepare_work_dir() {
    local work_dir=$1
    mkdir -p "$work_dir/_work"
    chmod 777 "$work_dir/_work" # job containers write here under other user ids
    cp "$0" "$work_dir/apptainer.sh" # the container cannot see Slurm's copy of this script
    write_job_started_hook "$work_dir"
}

# https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts#triggering-the-scripts
# GitHub triggers the script after the job has been assigned to a runner, but before the job starts.
write_job_started_hook() {
    local work_dir=$1
    cat >"$work_dir/job-started-hook.sh" <<EOF
#!/bin/bash
# Claims the runner for the starting job, unless the idle deadline claimed it first
if mkdir "$work_dir/claim" 2>/dev/null; then
    echo job > "$work_dir/claim/owner"
    exit 0
fi
echo "This runner reached its idle deadline before the job started." >&2
exit 1
EOF
    chmod +x "$work_dir/job-started-hook.sh"
}

# Runs in the background and becomes apptainer, which runs the supervisor from the work dir's copy
launch_runner_container() {
    local work_dir=$1 deadline=$2 jit_config=$3

    log "Starting rootless Docker"
    slurm-start-dockerd.sh
    if [[ ! -S $DOCKER_SOCKET ]]; then
        log "Docker did not create $DOCKER_SOCKET"
        exit 1
    fi

    log "Loading apptainer"
    # Compute Canada's profile and Lmod are not written for errexit or nounset. Sourced in the function
    # that execs apptainer, so that nothing they declare goes out of scope first
    set +o errexit +o nounset
    # shellcheck source=/dev/null
    source "$CC_PROFILE"
    module load apptainer
    set -o errexit -o nounset
    if ! command -v apptainer >/dev/null; then
        log "apptainer is not on PATH after module load apptainer"
        exit 1
    fi

    log "Starting the runner's container"
    # --containall passes only APPTAINERENV_* into the container, without the prefix
    export APPTAINERENV_ACTIONS_RUNNER_INPUT_JITCONFIG=$jit_config
    export APPTAINERENV_ACTIONS_RUNNER_HOOK_JOB_STARTED="$work_dir/job-started-hook.sh"
    export APPTAINERENV_DOCKER_HOST="unix://$DOCKER_SOCKET"
    export APPTAINERENV_RUNNER_ALLOW_RUNASROOT=1
    export APPTAINERENV_PYTHONPATH="$RUNNER_DIR/.local/lib/python3.10/site-packages"
    exec apptainer exec --writable-tmpfs --containall --fakeroot \
        --bind /dev/fuse --bind "$DOCKER_SOCKET:$DOCKER_SOCKET" --bind /cvmfs:/cvmfs --bind /tmp:/tmp \
        "$RUNNER_IMAGE" /bin/bash "$work_dir/apptainer.sh" supervise-runner "$work_dir" "$deadline"
}

# Returns once the supervisor has started the runner or the launch has ended. Ends the job if the idle
# deadline comes first, so that a hung Docker start or CVMFS read cannot outlive it; Slurm ends the rest.
wait_for_runner_to_start() {
    local work_dir=$1 deadline=$2 launcher_pid=$3
    while ! runner_has_started "$work_dir" && is_running "$launcher_pid"; do
        if is_past "$deadline"; then
            log "Exit 1: the idle deadline passed before the runner started"
            exit 1
        fi
        sleep 1
    done
}

# Ends the job when the supervisor hangs (e.g. on CVMFS): exit 0 once a job's run.sh has ended, exit 1 when
# no job holds the runner past the supervisor's own give-up. Never while a job holds or is setting up the
# runner, and never before it has seen run.sh, so that a process walk that cannot see in does nothing.
end_job_if_supervisor_hangs() {
    local work_dir=$1 deadline=$2 launcher_pid=$3
    local hung_after=$((deadline + LISTENER_STOP_WAIT_S + SUPERVISOR_EXIT_MARGIN_S))
    local seen_run_sh=false job_claimed=false run_sh_ended_at=0 run_sh_pids
    while is_running "$launcher_pid"; do
        if [[ $(claim_owner "$work_dir") == job ]]; then
            job_claimed=true # remembered: a job's step may delete the claim
        fi
        run_sh_pids=$(descendant_pids_named "$launcher_pid" run.sh)

        if ! $seen_run_sh; then
            if [[ -n $run_sh_pids ]]; then
                seen_run_sh=true
                log "Watching run.sh (pid ${run_sh_pids%%$'\n'*}) under apptainer (pid $launcher_pid)"
            fi
        elif $job_claimed; then
            if [[ -n $run_sh_pids ]]; then
                run_sh_ended_at=0
            elif ((run_sh_ended_at == 0)); then
                run_sh_ended_at=$(date +%s)
            elif is_past $((run_sh_ended_at + SUPERVISOR_EXIT_MARGIN_S)); then
                log "Exit 0: a job claimed this runner and the runner ended, but the supervisor did not"
                exit 0
            fi
        elif is_past "$hung_after" && ! has_descendant_named "$launcher_pid" Runner.Worker; then
            log "Exit 1: no job holds the runner, and the supervisor has not ended" \
                "$((hung_after - deadline)) s after the idle deadline"
            exit 1
        fi
        sleep 1
    done
}

# --- The supervisor, inside the container, beside run.sh ---

supervise_runner() {
    local work_dir=$1 deadline=$2
    if is_past "$deadline"; then
        log "Exit 1: the idle deadline passed before the runner could start"
        exit 1
    fi

    touch "$work_dir/runner-started"
    "$RUNNER_DIR/run.sh" &
    local run_sh_pid=$!
    log "Started run.sh (pid $run_sh_pid)"

    local claimed_by="" # job, idle, or empty while unclaimed; set by wait_for_a_claim
    wait_for_a_claim "$work_dir" "$deadline" "$run_sh_pid"
    if [[ $claimed_by == idle ]]; then
        stop_runner "$run_sh_pid"
    fi

    local run_sh_status=0
    wait "$run_sh_pid" || run_sh_status=$?
    log "run.sh exited with status $run_sh_status"
    if [[ $claimed_by == idle ]]; then
        log "Exit 0: stopped idle at the deadline"
        exit 0
    fi
    if [[ $claimed_by == job || -d $work_dir/claim ]]; then
        log "Exit 0: a job claimed this runner"
        exit 0
    fi
    log "Exit 1: the runner ended before any job claimed it"
    exit 1
}

# Returns once a job or the idle deadline has claimed the runner, or run.sh has ended unclaimed. Sets the
# caller's claimed_by, decided here once: a job's step may later delete the claim
wait_for_a_claim() {
    local work_dir=$1 deadline=$2 run_sh_pid=$3
    local setup_logged=false
    while is_running "$run_sh_pid"; do
        if [[ -d $work_dir/claim ]]; then
            claimed_by=job
            return
        fi
        if is_past "$deadline"; then
            # The listener starts a Runner.Worker for a job it took, before the job's hook runs. If that
            # undocumented name changed, the deadline would claim just as it does with no job
            if has_descendant_named "$run_sh_pid" Runner.Worker; then
                if ! $setup_logged; then
                    setup_logged=true
                    log "A job is being set up at the idle deadline; leaving the runner to it"
                fi
            elif mkdir "$work_dir/claim" 2>/dev/null; then
                echo idle >"$work_dir/claim/owner"
                claimed_by=idle
                return
            else
                claimed_by=job
                log "The idle deadline passed after a job claimed the runner; leaving it alone"
                return
            fi
        fi
        sleep 1
    done
}

# SIGTERM to Runner.Listener is the only signal that stops the runner: run.sh runs in the background of a
# non-interactive shell, so it and its children ignore SIGINT, and SIGTERM to run.sh orphans the listener.
# run-helper.sh relaunches a listener that exits with code 2 or for an update, so each new one is signalled
# too. If the listener has not stopped after LISTENER_STOP_WAIT_S, exits 0 anyway: the supervisor is the
# container's main process, and its exit ends the container's PID namespace, listener included.
stop_runner() {
    local run_sh_pid=$1
    local give_up_at=$(($(date +%s) + LISTENER_STOP_WAIT_S))
    log "No job began by the idle deadline; stopping the runner"
    local -A signalled=()
    local listener_pid
    while is_running "$run_sh_pid"; do
        if is_past "$give_up_at"; then
            log "Exit 0: the listener was not stopped ${LISTENER_STOP_WAIT_S} s after the idle claim"
            exit 0
        fi
        for listener_pid in $(descendant_pids_named "$run_sh_pid" Runner.Listener); do
            if [[ -n ${signalled[$listener_pid]:-} ]]; then
                continue
            fi
            if ((${#signalled[@]} > 0)); then
                log "Stopping a listener relaunched after the idle claim"
            fi
            kill -TERM "$listener_pid" 2>/dev/null || true
            signalled[$listener_pid]=1
        done
        sleep 1
    done
}

# --- Shared by both halves ---

log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $*"
}

is_past() { (($(date +%s) >= $1)); }
is_running() { kill -0 "$1" 2>/dev/null; }
runner_has_started() { [[ -e $1/runner-started ]]; }
has_descendant_named() { [[ -n $(descendant_pids_named "$1" "$2") ]]; }

# Prints what the owner file says: job, idle, or nothing (no claim, or a claim whose owner is not written yet)
claim_owner() {
    local work_dir=$1 owner=""
    read -r owner 2>/dev/null <"$work_dir/claim/owner" || true
    echo "$owner"
}

# The live descendants of a process with the given name, from one pass over /proc (a job can run hundreds of
# processes, and this runs every second; the image may lack pgrep). A zombie is not live: an ended run.sh
# stays one while its parent, a hung supervisor, does not reap it.
descendant_pids_named() {
    local ancestor_pid=$1 name=$2
    local stat_file stat pid state parent_pid after_name child_pid
    local -A children_of=() name_of=()
    for stat_file in /proc/[0-9]*/stat; do
        read -r stat 2>/dev/null <"$stat_file" || continue
        pid=${stat%% *}
        after_name=${stat##*) } # the name may hold spaces or parentheses
        read -r state parent_pid _ <<<"$after_name"
        if [[ $state == Z ]]; then
            continue
        fi
        children_of[$parent_pid]+=" $pid"
        name_of[$pid]=${stat#*(}
        name_of[$pid]=${name_of[$pid]%)*}
    done
    local -a to_visit=("$ancestor_pid")
    while ((${#to_visit[@]} > 0)); do
        pid=${to_visit[0]}
        to_visit=("${to_visit[@]:1}")
        for child_pid in ${children_of[$pid]:-}; do
            if [[ ${name_of[$child_pid]} == "$name" ]]; then
                echo "$child_pid"
            fi
            to_visit+=("$child_pid")
        done
    done
}

main "$@"
