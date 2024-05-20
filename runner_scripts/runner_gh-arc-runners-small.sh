#!/bin/bash
#SBATCH--job-name=slurm-gh-actions-runner
#SBATCH--cpus-per-task=1
#SBATCH--mem=2G
#SBATCH--grestmpdisk:2048
#SBATCH--time=00:30:00
# The above sbatch configuration is generated dynamically based on the runner label by runner_size_config.py
#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels>

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

# Setup
runner_dir="runners/runner_$REGISTRATION_TOKEN"

# start docker
echo "Starting Docker on Slurm"
slurm-start-dockerd.sh
echo "Docker started"

# Set Docker host
export DOCKER_HOST=unix:///tmp/run/docker.sock

echo "Waiting for Docker to be ready..."
until docker info > /dev/null 2>&1
do
  sleep 1
done
echo "Docker is ready."

# wait for docker to start

echo "Building Docker image"
docker build -t gha-runner:latest https://raw.githubusercontent.com/WATonomous/actions-runner-image/main/Dockerfile

DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_$SLURM_JOB_ID" gha-runner:latest tail -f /dev/null)
echo $DOCKER_CONTAINER_ID > docker_id.txt

# Ensure the container started correctly
if [ $? -ne 0 ]; then
    echo "Failed to start Docker container."
    exit 1
fi

# Register and run
echo "Registering runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./config.sh --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm_$SLURM_JOB_ID\" --unattended --ephemeral"

echo "Running runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./run.sh"

echo "Removing runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./config.sh remove --token $REMOVAL_TOKEN"

docker stop $DOCKER_CONTAINER_ID
docker rm $DOCKER_CONTAINER_ID
