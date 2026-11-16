#!/bin/bash
# One GitHub Actions runner in one Slurm job, as slurm-gha submits it:
#   sbatch --job-name=<runner name> ... apptainer.sh <idle allowance in seconds>
# with the runner's just-in-time config in RUNNER_JIT_CONFIG (environment, never argv). The runner works in
# /tmp/<runner name>/_work, which its JIT config names and which is the same path inside the container and
# for the rootless Docker daemon outside it.
#
# Who owns the runner is a claim: GitHub's job-started hook and the idle deadline race to mkdir one
# directory, and exactly one wins. A job that wins runs to its end. A deadline that wins stops the runner,
# and a hook that comes after it fails its job before any step runs. The deadline does not claim while a
# job is being set up, and a job's claim is remembered even if one of its steps deletes the directory.
#
# The exit status comes from the claim, never from run.sh, which exits 0 on every failure:
#   0  a job claimed the runner, or the idle deadline did because no job came (also when the listener
#      cannot be stopped: the container's end takes it down)
#   1  the runner ended by itself with no claim; or Docker, apptainer or the image failed; or the deadline
#      passed before the runner started; or no job held the runner and the supervisor had still not ended
#      SUPERVISOR_EXIT_MARGIN_S after its own give-up. Slurm records FAILED, which slurm-gha counts as a
#      failed launch.
set -o pipefail -o errexit -o nounset

readonly RUNNER_IMAGE="/cvmfs/unpacked.cern.ch/ghcr.io/watonomous/actions-runner-image:main"
readonly RUNNER_DIR="/home/runner" # in the image
readonly DOCKER_SOCKET="/tmp/run/docker.sock" # where slurm-start-dockerd.sh puts it
readonly CC_PROFILE="/cvmfs/soft.computecanada.ca/config/profile/bash.sh"
# After the idle claim, how long the listener has to stop before the supervisor exits without it
readonly LISTENER_STOP_WAIT_S=15
# Seconds past the supervisor's own give-up before it counts as stuck. A runner whose supervisor is stuck
# stays online until deadline + LISTENER_STOP_WAIT_S + this, and a job it takes meanwhile must still get J:
# keep LISTENER_STOP_WAIT_S + this below the daemon's D (SCRIPT_START_MARGIN_S) less the script's start delay
readonly SUPERVISOR_EXIT_MARGIN_S=30
export PATH="$PATH:/opt/slurm/bin" # slurm-start-dockerd.sh

log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $*"
}

# --- In the Slurm job, outside the container ---

run_batch_job() {
    local idle_allowance_s=$1
    local deadline=$(($(date +%s) + idle_allowance_s)) # W counts from here, the script's start
    local work_dir="/tmp/$SLURM_JOB_NAME"
    # Out of the environment from here on, so that Docker and the other children never inherit it
    local jit_config=$RUNNER_JIT_CONFIG
    unset RUNNER_JIT_CONFIG

    mkdir -p "$work_dir/_work"
    chmod 777 "$work_dir/_work" # job containers write here under other user ids
    cp "$0" "$work_dir/apptainer.sh" # the container runs supervise_runner from this copy
    write_job_started_hook "$work_dir"
    log "Runner $SLURM_JOB_NAME: idle deadline at $(date -d "@$deadline" +'%H:%M:%S')"

    launch_runner "$work_dir" "$deadline" "$jit_config" &
    local launcher_pid=$!
    unset jit_config

    # Until the supervisor inside the container has started the runner, the deadline is watched from
    # here, so that a hung Docker start or CVMFS read cannot outlive it. Slurm ends what is left.
    while [[ ! -e $work_dir/runner-started ]] && kill -0 "$launcher_pid" 2>/dev/null; do
        if (($(date +%s) >= deadline)); then
            log "Exit 1: the idle deadline passed before the runner started"
            exit 1
        fi
        sleep 1
    done

    if [[ -e $work_dir/runner-started ]]; then
        watch_supervisor_and_end_job_if_stuck "$work_dir" "$deadline" "$launcher_pid"
    fi
    local status=0
    wait "$launcher_pid" || status=$?
    if [[ ! -e $work_dir/runner-started ]]; then
        log "Exit 1: Docker, apptainer or the image failed (status $status) before the runner started"
        exit 1
    fi
    exit "$status" # the supervisor's, passed on by apptainer
}

# Ends the job when the supervisor is stuck: exit 1 with no job, past its give-up plus the margin; exit 0
# once a job's runner has ended. Never while a job holds or is setting up the runner, and never before the
# walk from outside has once seen run.sh under the container, so a walk that cannot see in does nothing.
watch_supervisor_and_end_job_if_stuck() {
    local work_dir=$1 deadline=$2 launcher_pid=$3
    local stuck_after=$((deadline + LISTENER_STOP_WAIT_S + SUPERVISOR_EXIT_MARGIN_S))
    local walk_sees_runner=false job_claimed=false runner_ended_at=0 owner runner_pids
    while kill -0 "$launcher_pid" 2>/dev/null; do
        owner=""
        read -r owner 2>/dev/null <"$work_dir/claim/owner" || true
        if [[ $owner == job ]]; then
            job_claimed=true
        fi
        runner_pids=$(descendants_named "$launcher_pid" run.sh)
        if ! $walk_sees_runner; then
            if [[ -n $runner_pids ]]; then
                walk_sees_runner=true
                log "Watching run.sh (pid ${runner_pids%%$'\n'*}) under apptainer (pid $launcher_pid)"
            fi
        elif $job_claimed; then
            if [[ -n $runner_pids ]]; then
                runner_ended_at=0
            elif ((runner_ended_at == 0)); then
                runner_ended_at=$(date +%s)
            elif (($(date +%s) - runner_ended_at >= SUPERVISOR_EXIT_MARGIN_S)); then
                log "Exit 0: a job claimed this runner and the runner ended, but the supervisor did not"
                exit 0
            fi
        elif (($(date +%s) >= stuck_after)) && [[ -z $(descendants_named "$launcher_pid" Runner.Worker) ]]; then
            log "Exit 1: no job holds the runner, and the supervisor has not ended" \
                "$((stuck_after - deadline)) s after the idle deadline"
            exit 1
        fi
        sleep 1
    done
}

write_job_started_hook() {
    local work_dir=$1
    # GitHub runs this once per job, after the runner acquired it and before its first step; a non-zero
    # exit fails the job. It must exist at that path, or every job fails.
    cat >"$work_dir/job-started-hook.sh" <<EOF
#!/bin/bash
# Claims the runner for the job that is starting, unless the idle deadline claimed it first
if mkdir "$work_dir/claim" 2>/dev/null; then
    echo job > "$work_dir/claim/owner"
    exit 0
fi
echo "This runner reached its idle deadline before the job started." >&2
exit 1
EOF
    chmod +x "$work_dir/job-started-hook.sh"
}

launch_runner() {
    local work_dir=$1 deadline=$2 jit_config=$3
    log "Starting rootless Docker"
    slurm-start-dockerd.sh
    if [[ ! -S $DOCKER_SOCKET ]]; then
        log "Docker did not create $DOCKER_SOCKET"
        exit 1
    fi

    log "Loading apptainer"
    # Compute Canada's profile and Lmod are not written for errexit or nounset; apptainer on PATH is
    # what counts
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

# --- Inside the container, beside run.sh, where the runner's processes can be signalled ---

supervise_runner() {
    local work_dir=$1 deadline=$2
    if (($(date +%s) >= deadline)); then
        log "Exit 1: the idle deadline passed before the runner could start"
        exit 1
    fi
    touch "$work_dir/runner-started"
    "$RUNNER_DIR/run.sh" &
    local runner_pid=$!
    log "Started run.sh (pid $runner_pid)"

    local job_claimed=false stopped_idle=false stop_given_up_at=0 setup_logged=false signalled_pids=" "
    while kill -0 "$runner_pid" 2>/dev/null; do
        # Remembered: a step that cleans /tmp could delete the claim, and the deadline must not take it then
        if ! $stopped_idle && [[ -d $work_dir/claim ]]; then
            job_claimed=true
        fi
        if ! $job_claimed && ! $stopped_idle && (($(date +%s) >= deadline)); then
            if job_being_set_up "$runner_pid"; then
                if ! $setup_logged; then
                    setup_logged=true
                    log "A job is being set up at the idle deadline; leaving the runner to it"
                fi
            elif mkdir "$work_dir/claim" 2>/dev/null; then
                echo idle >"$work_dir/claim/owner"
                stopped_idle=true
                stop_given_up_at=$(($(date +%s) + LISTENER_STOP_WAIT_S))
                log "No job began by the idle deadline; stopping the runner"
            else
                job_claimed=true
                log "The idle deadline passed after a job claimed the runner; leaving it alone"
            fi
        fi
        if $stopped_idle; then
            if (($(date +%s) >= stop_given_up_at)); then
                # The container's main process: its exit ends the container's PID namespace, listener included
                log "Exit 0: the listener was not stopped ${LISTENER_STOP_WAIT_S} s after the idle claim"
                exit 0
            fi
            # run-helper.sh relaunches a listener that exits with code 2 or for an update
            local before=$signalled_pids
            signalled_pids=$(stop_new_listeners "$runner_pid" "$signalled_pids")
            if [[ $before != " " && $signalled_pids != "$before" ]]; then
                log "Stopping a listener relaunched after the idle claim"
            fi
        fi
        sleep 1
    done

    local runner_status=0
    wait "$runner_pid" || runner_status=$?
    log "run.sh exited with status $runner_status"
    if $stopped_idle; then
        log "Exit 0: stopped idle at the deadline"
        exit 0
    fi
    if $job_claimed || [[ -d $work_dir/claim ]]; then
        log "Exit 0: a job claimed this runner"
        exit 0
    fi
    log "Exit 1: the runner ended before any job claimed it"
    exit 1
}

# SIGTERM to Runner.Listener is the only signal that stops the runner: run.sh runs in the background of a
# non-interactive shell, so it and its children ignore SIGINT, and SIGTERM to run.sh orphans the listener.
# Sent once per listener: it exits within about a second, and sending it again changes nothing, but a
# listener relaunched by run-helper.sh needs its own. No signal makes it sign off; slurm-gha deletes the
# registration once Slurm shows the job ended.
# Only listeners under this run.sh are signalled (run.sh -> run-helper.sh -> Runner.Listener). Prints the
# PIDs signalled so far, given those signalled before as " pid pid ".
stop_new_listeners() {
    local run_sh_pid=$1 signalled=$2 listener_pid
    for listener_pid in $(listener_pids "$run_sh_pid"); do
        if [[ $signalled != *" $listener_pid "* ]]; then
            kill -TERM "$listener_pid" 2>/dev/null || true
            signalled+="$listener_pid "
        fi
    done
    echo "$signalled"
}

# The listener starts a Runner.Worker for a job it has taken, before the job's actions are downloaded and
# its hook runs. Like Runner.Listener, the name is undocumented: if it changed, no worker would ever be
# found, and the deadline would claim exactly as it does without this check.
job_being_set_up() {
    local listener_pid worker_pid
    for listener_pid in $(listener_pids "$1"); do
        for worker_pid in $(child_pids "$listener_pid"); do
            if [[ $(cat "/proc/$worker_pid/comm" 2>/dev/null) == Runner.Worker ]]; then
                return 0
            fi
        done
    done
    return 1
}

listener_pids() {
    local run_sh_pid=$1 helper_pid listener_pid
    for helper_pid in $(child_pids "$run_sh_pid"); do
        for listener_pid in $(child_pids "$helper_pid"); do
            if [[ $(cat "/proc/$listener_pid/comm" 2>/dev/null) == Runner.Listener ]]; then
                echo "$listener_pid"
            fi
        done
    done
}

# The live descendants of process $1 whose name is $2, from one pass over /proc (a job can run hundreds of
# processes, and this runs every second). A zombie is not live: an ended run.sh stays one while its parent,
# the supervisor, is too stuck to reap it.
descendants_named() {
    local name=$2 stat line pid state parent rest child
    local -A children=() names=()
    for stat in /proc/[0-9]*/stat; do
        read -r line 2>/dev/null <"$stat" || continue
        pid=${line%% *}
        rest=${line##*) }  # after the name, which may hold spaces or parentheses
        read -r state parent _ <<<"$rest"
        if [[ $state == Z ]]; then
            continue
        fi
        children[$parent]+=" $pid"
        names[$pid]=${line#*(}
        names[$pid]=${names[$pid]%)*}
    done
    local -a to_visit=("$1")
    while ((${#to_visit[@]} > 0)); do
        pid=${to_visit[0]}
        to_visit=("${to_visit[@]:1}")
        for child in ${children[$pid]:-}; do
            if [[ ${names[$child]} == "$name" ]]; then
                echo "$child"
            fi
            to_visit+=("$child")
        done
    done
}

# The children of a process, read from /proc: the runner image may not have pgrep
child_pids() {
    local stat fields
    for stat in /proc/[0-9]*/stat; do
        read -r -a fields 2>/dev/null <"$stat" || continue
        if [[ ${fields[3]} == "$1" ]]; then
            echo "${fields[0]}"
        fi
    done
}

case "${1:-}" in
supervise-runner)
    shift
    supervise_runner "$@"
    ;;
*)
    run_batch_job "$@"
    ;;
esac
