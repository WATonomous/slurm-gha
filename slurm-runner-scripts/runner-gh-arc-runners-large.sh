#!/bin/bash
#SBATCH --job-name=slurm-gh-actions-runner
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --gres tmpdisk:12288
#SBATCH --time=00:30:00
# The above sbatch configuration is generated dynamically based on the runner label by runner_size_config.py
#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels> 
# Tailored for a use in Slurm environment, running Docker in Docker to allow CI to run docker commands 

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

# start docker
echo "Starting Docker on Slurm"
slurm-start-dockerd.sh
echo "Docker started"

export DOCKER_HOST=unix:///tmp/run/docker.sock

# Ensure the container started correctly
if [ $? -ne 0 ]; then
    echo "Failed to start Docker container."
    exit 1
fi

# Define the parent directory for GitHub Actions
PARENT_DIR="/tmp/runner_$SLURM_JOB_ID"

# Define the GitHub Actions working directory
GITHUB_ACTIONS_WKDIR="$PARENT_DIR/_work"

# Create the parent directory if it doesn't exist
mkdir -p $PARENT_DIR

# Add permissions to the parent directory
chown -R $(id -u):$(id -g) $PARENT_DIR
chmod -R 777 $PARENT_DIR


DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_$SLURM_JOB_ID" --mount type=bind,source=/tmp/run/docker.sock,target=/var/run/docker.sock --mount type=bind,source=$PARENT_DIR,target=$PARENT_DIR ghcr.io/watonomous/actions-runner-image:main tail -f /dev/null)

docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod 666 /var/run/docker.sock" # Allows the runner to access the docker socket
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "mkdir \"$PARENT_DIR\"" 
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chown -R runner:runner \"$PARENT_DIR\""
# docker exec $DOCKER_CONTAINER_ID /bin/bash -c "echo permissions for \"$PARENT_DIR\""
# docker exec $DOCKER_CONTAINER_ID /bin/bash -c "ls -ld \"$PARENT_DIR\""
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod -R 755 \"$PARENT_DIR\"" 


# Execute commands in the container to register, run one job, and then remove the runner
echo "Registering runner..."

# docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./config.sh --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm_$SLURM_JOB_ID\" --unattended --ephemeral"
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh --work "$GITHUB_ACTIONS_WKDIR" --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm_$SLURM_JOB_ID\" --unattended --ephemeral"

echo "Running runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/run.sh"

docker exec $DOCKER_CONTAINER_ID /bin/bash -c "ls" 

echo "Removing runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "/home/runner/config.sh remove --token $REMOVAL_TOKEN"

docker stop $DOCKER_CONTAINER_ID
docker rm $DOCKER_CONTAINER_ID

echo "Docker container removed"
echo "Script finished"

echo "Exiting Docker in Docker"
exit