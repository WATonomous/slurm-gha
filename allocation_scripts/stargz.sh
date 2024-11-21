#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-url> <registration-token> <removal-token> <label> <run_id>
# Tailored for use in a Slurm environment, running Docker in Docker to allow CI to run docker commands 

# Function to log messages with a timestamp
log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $@"
}

# Array to store timing information
declare -A timings

# Function to record timing
record_timing() {
    timings["$1"]=$2
}

# Check if all required arguments are provided
if [ $# -lt 5 ]; then
    log "ERROR: Missing required arguments"
    log "Usage: $0 <repo-url> <registration-token> <removal-token> <label> <run_id>"
    exit 1
fi

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4
RUN_ID=$5


export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/tmp/run}
export XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-/tmp/config}
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME"

export SNAPSHOTTER_ADDRESS="$XDG_RUNTIME_DIR/containerd-stargz-grpc/containerd-stargz-grpc.sock"
export CONTAINERD_ADDRESS="$XDG_RUNTIME_DIR/containerd/containerd.sock"

log "INFO Starting Docker on Slurm"
start_time=$(date +%s)
slurm-start-dockerd.sh
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Docker in Slurm started (Duration: $duration seconds)"
record_timing "Start Docker" $duration

log "INFO Starting stargz"
start_time=$(date +%s)
./bin/containerd-rootless.sh --config containerd-config.toml --root /tmp/containerd --address "$CONTAINERD_ADDRESS" --state "$XDG_RUNTIME_DIR/containerd-rootless" &
CONTAINERD_PID=$!
echo "Waiting for containerd address at $CONTAINERD_ADDRESS..."
while [ ! -S "$CONTAINERD_ADDRESS" ]; do
    sleep 0.1
done
echo "Containerd address is ready!"

./bin/containerd-rootless-setuptool.sh nsenter -- ./bin/containerd-stargz-grpc -address "$SNAPSHOTTER_ADDRESS" -root /tmp/containerd-stargz-grpc &
SNAPSHOTTER_PID=$!
# Wait until SNAPSHOTTER_ADDRESS exists
echo "Waiting for snapshotter address at $SNAPSHOTTER_ADDRESS..."
while [ ! -S "$SNAPSHOTTER_ADDRESS" ]; do
    sleep 0.1
done
echo "Snapshotter address is ready!"

end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO stargz started (Duration: $duration seconds)"
record_timing "Start stargz" $duration

export DOCKER_HOST=unix:///tmp/run/docker.sock

# Define the parent directory for GitHub Actions in the host machine
PARENT_DIR="/dev/shm/docker/runner-${SLURMD_NODENAME}-${SLURM_JOB_ID}"
PROVISIONER_DIR="/mnt/wato-drive2/alexboden/provisioner-cache/$RUN_ID"
log "INFO Parent directory for GitHub Actions: $PARENT_DIR"

start_time=$(date +%s)
GITHUB_ACTIONS_WKDIR="$PARENT_DIR/_work"
mkdir -p $PARENT_DIR
ls -l /mnt/wato-drive2/alexboden/provisioner-cache
mkdir -p $PROVISIONER_DIR
chown -R $(id -u):$(id -g) $PARENT_DIR
chmod -R 777 $PARENT_DIR
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Created and set permissions for parent and provisioner directories (Duration: $duration seconds)"
record_timing "Create Directories" $duration

# Start the actions runner container
log "INFO Starting actions runner container"
start_time=$(date +%s)
DOCKER_CONTAINER_ID=$(./bin/nerdctl --address "$CONTAINERD_ADDRESS" --cgroup-manager=none --cni-path=$(pwd)/cni --cni-netconfpath "$XDG_CONFIG_HOME/cni/net.d" --data-root /tmp/nerdctl --snapshotter=stargz run -d --mount type=bind,source=/tmp/run/docker.sock,target=/var/run/docker.sock --mount type=bind,source=$PARENT_DIR,target=$PARENT_DIR --mount type=bind,source=$PROVISIONER_DIR,target=$PROVISIONER_DIR ghcr.io/watonomous/actions-runner-image:pr-2 tail -f /dev/null)
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Started actions runner container with ID $DOCKER_CONTAINER_ID (Duration: $duration seconds)"
record_timing "Start Container" $duration

# Configure the container
log "INFO Configuring container"
start_time=$(date +%s)
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod 666 /var/run/docker.sock" # Allows the runner to access the docker socket
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "mkdir -p \"$PARENT_DIR\"" 
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chown -R runner:runner \"$PARENT_DIR\""
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod -R 755 \"$PARENT_DIR\"" 

# ./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "mkdir -p \"$PROVISIONER_DIR\""
# ./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chown -R root:root \"$PROVISIONER_DIR\""
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod -R 777 \"$PROVISIONER_DIR\"" 

end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Configured container (Duration: $duration seconds)"
record_timing "Configure Container" $duration

# Register, run, and remove the runner
log "INFO Registering runner..."
start_time=$(date +%s)
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh --work \"$GITHUB_ACTIONS_WKDIR\" --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm-${SLURMD_NODENAME}-${SLURM_JOB_ID}\" --unattended --ephemeral --disableupdate"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner registered (Duration: $duration seconds)"
record_timing "Register Runner" $duration

log "INFO Running runner..."
start_time=$(date +%s)
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/run.sh"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner finished (Duration: $duration seconds)"
record_timing "Run Runner" $duration

log "INFO Removing runner..."
start_time=$(date +%s)
./bin/nerdctl exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh remove --token $REMOVAL_TOKEN"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner removed (Duration: $duration seconds)"
record_timing "Remove Runner" $duration

# Clean up
log "INFO Stopping and removing Docker container"
start_time=$(date +%s)
./bin/nerdctl stop $DOCKER_CONTAINER_ID
./bin/nerdctl rm $DOCKER_CONTAINER_ID
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Docker container removed (Duration: $duration seconds)"
record_timing "Remove Container" $duration

log "Cleaning up containerd and stargz"
start_time=$(date +%s)
echo "Stopping containerd-rootless (PID: $CONTAINERD_PID)..."
kill $CONTAINERD_PID

echo "Stopping containerd-stargz-grpc (PID: $SNAPSHOTTER_PID)..."
kill $SNAPSHOTTER_PID

wait $CONTAINERD_PID
wait $SNAPSHOTTER_PID

rootlesskit rm -rf /tmp/{run,config,containerd*,nerdctl}
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Containerd and Stargz cleaned up (Duration: $duration seconds)"

log "INFO allocate-ephemeral-runner-from-docker.sh finished, exiting..."

# Print out all the recorded times
log "Time Summary:"
for key in "${!timings[@]}"; do
    log "$key: ${timings[$key]} seconds"
done

# Calculate and print total time
total_time=0
for duration in "${timings[@]}"; do
    total_time=$((total_time + duration))
done
log "Total Time: $total_time seconds"

exit 0
