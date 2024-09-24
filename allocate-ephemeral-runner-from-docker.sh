#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-url> <registration-token> <removal-token> <labels> 
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
if [ $# -lt 4 ]; then
    log "ERROR: Missing required arguments"
    log "Usage: $0 <repo-url> <registration-token> <removal-token> <labels>"
    exit 1
fi

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

log "INFO Starting Docker on Slurm"
start_time=$(date +%s)
slurm-start-dockerd.sh
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Docker in Slurm started (Duration: $duration seconds)"
record_timing "Start Docker" $duration

export DOCKER_HOST=unix:///tmp/run/docker.sock

# Define the parent directory for GitHub Actions in the host machine
PARENT_DIR="/tmp/runner-${SLURMD_NODENAME}-${SLURM_JOB_ID}"
log "INFO Parent directory for GitHub Actions: $PARENT_DIR"

start_time=$(date +%s)
GITHUB_ACTIONS_WKDIR="$PARENT_DIR/_work"
mkdir -p $PARENT_DIR
chown -R $(id -u):$(id -g) $PARENT_DIR
chmod -R 777 $PARENT_DIR
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Created and set permissions for parent directory (Duration: $duration seconds)"
record_timing "Create Parent Directory" $duration

# Start the actions runner container
log "INFO Starting actions runner container"
start_time=$(date +%s)
DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_${SLURMD_NODENAME}-${SLURM_JOB_ID}" --mount type=bind,source=/tmp/run/docker.sock,target=/var/run/docker.sock --mount type=bind,source=$PARENT_DIR,target=$PARENT_DIR ghcr.io/watonomous/actions-runner-image:main tail -f /dev/null)
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Started actions runner container with ID $DOCKER_CONTAINER_ID (Duration: $duration seconds)"
record_timing "Start Container" $duration

# Configure the container
log "INFO Configuring container"
start_time=$(date +%s)
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod 666 /var/run/docker.sock" # Allows the runner to access the docker socket
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "mkdir \"$PARENT_DIR\"" 
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chown -R runner:runner \"$PARENT_DIR\""
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod -R 755 \"$PARENT_DIR\"" 
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Configured container (Duration: $duration seconds)"
record_timing "Configure Container" $duration

# Register, run, and remove the runner
log "INFO Registering runner..."
start_time=$(date +%s)
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh --work \"$GITHUB_ACTIONS_WKDIR\" --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm-${SLURMD_NODENAME}-${SLURM_JOB_ID}\" --unattended --ephemeral --disableupdate"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner registered (Duration: $duration seconds)"
record_timing "Register Runner" $duration

log "INFO Running runner..."
start_time=$(date +%s)
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/run.sh"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner finished (Duration: $duration seconds)"
record_timing "Run Runner" $duration

log "INFO Removing runner..."
start_time=$(date +%s)
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh remove --token $REMOVAL_TOKEN"
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Runner removed (Duration: $duration seconds)"
record_timing "Remove Runner" $duration

# Clean up
log "INFO Stopping and removing Docker container"
start_time=$(date +%s)
docker stop $DOCKER_CONTAINER_ID
docker rm $DOCKER_CONTAINER_ID
end_time=$(date +%s)
duration=$((end_time - start_time))
log "INFO Docker container removed (Duration: $duration seconds)"
record_timing "Remove Container" $duration

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