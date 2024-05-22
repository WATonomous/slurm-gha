#!/bin/bash
#SBATCH --job-name=slurm-gh-actions-runner
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --gres tmpdisk:2048
#SBATCH --time=00:30:00
# The above sbatch configuration is generated dynamically based on the runner label by runner_size_config.py
#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels> 

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

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

# Building the custom actions runner image
REPO_NAME="WATonomous/actions-runner-image" 
LATEST_COMMIT=$(curl -s https://api.github.com/repos/$REPO_NAME/commits/main | jq -r '.sha')
IMAGE_NAME="gha-runner:$LATEST_COMMIT"

# Check if Docker image already exists, only build if it doesn't
if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
  echo "Building Docker image with commit $LATEST_COMMIT"
  docker build -t $IMAGE_NAME https://raw.githubusercontent.com/$REPO_NAME/main/Dockerfile
else
  echo "Docker image $IMAGE_NAME already exists. Skipping build."
fi

echo "Running Docker container..."
DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_$SLURM_JOB_ID" "$IMAGE_NAME" tail -f /dev/null)

# Ensure the container started correctly
if [ $? -ne 0 ]; then
    echo "Failed to start Docker container."
    exit 1
fi

# Execute commands in the container to register, run one job, and then remove the runner
echo "Registering runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./config.sh --url \"$REPO_URL\" --token \"$REGISTRATION_TOKEN\" --labels \"$LABELS\" --name \"slurm_$SLURM_JOB_ID\" --unattended --ephemeral"

echo "Running runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./run.sh"

echo "Removing runner..."
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "./config.sh remove --token $REMOVAL_TOKEN"

docker stop $DOCKER_CONTAINER_ID
docker rm $DOCKER_CONTAINER_ID

echo "Docker container removed"
echo "Script finished"