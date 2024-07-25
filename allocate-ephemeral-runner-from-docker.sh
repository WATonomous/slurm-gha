#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels> 
# Tailored for a use in Slurm environment, running Docker in Docker to allow CI to run docker commands 

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

# start docker
echo "INFO Starting Docker on Slurm"
slurm-start-dockerd.sh

export DOCKER_HOST=unix:///tmp/run/docker.sock

# Ensure the container started correctly
if [ $? -ne 0 ]; then
    echo "Failed to start Docker container."
    exit 1
fi

echo "INFO Docker in Slurm started"

# Define the parent directory for GitHub Actions in the host machine
PARENT_DIR="/tmp/runner-${SLURMD_NODENAME}-${SLURM_JOB_ID}"
echo "INFO Parent directory for GitHub Actions: $PARENT_DIR"
GITHUB_ACTIONS_WKDIR="$PARENT_DIR/_work"
mkdir -p $PARENT_DIR
chown -R $(id -u):$(id -g) $PARENT_DIR
chmod -R 777 $PARENT_DIR


# Start the actions runner container. Mount the docker socket and the parent directory (of the working directory).
DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_${SLURMD_NODENAME}-${SLURM_JOB_ID}" --mount type=bind,source=/tmp/run/docker.sock,target=/var/run/docker.sock --mount type=bind,source=$PARENT_DIR,target=$PARENT_DIR ghcr.io/watonomous/actions-runner-image:main tail -f /dev/null)
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod 666 /var/run/docker.sock" # Allows the runner to access the docker socket
# Configure the permissions of the parent directory within the container
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "mkdir \"$PARENT_DIR\"" 
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chown -R runner:runner \"$PARENT_DIR\""
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod -R 755 \"$PARENT_DIR\"" 

# Execute commands in the container to register, run one job, and then remove the runner
echo "INFO Registering runner..."

docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh --work "$GITHUB_ACTIONS_WKDIR" --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm-${SLURMD_NODENAME}-${SLURM_JOB_ID}\" --unattended --ephemeral"

echo "INFO Running runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/run.sh"

docker exec $DOCKER_CONTAINER_ID /bin/bash -c "ls" 

echo "INFO Removing runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh remove --token $REMOVAL_TOKEN"

docker stop $DOCKER_CONTAINER_ID
docker rm $DOCKER_CONTAINER_ID

echo "INFO Docker container removed"
echo "INFO allocate-ephemeral-runner-from-docker.sh finished, now exiting."
exit