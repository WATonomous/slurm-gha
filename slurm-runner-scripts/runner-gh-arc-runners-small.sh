#!/bin/bash
#SBATCH --job-name=slurm-gh-actions-runner
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --gres tmpdisk:4096
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

# docker run --privileged --name dind -v /tmp/run/docker.sock:/var/run/docker.sock -d docker tail -f /dev/null

# dockerd --debug

# echo "Docker ps"
# docker ps

# DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_$SLURM_JOB_ID" --volumes-from dind -v /tmp/run/docker.sock:/var/run/docker.sock ghcr.io/watonomous/actions-runner-image:main tail -f /dev/null)
DOCKER_CONTAINER_ID=$(docker run -d --name "ghar_$SLURM_JOB_ID" -v /tmp/run/docker.sock:/var/run/docker.sock ghcr.io/watonomous/actions-runner-image:main tail -f /dev/null)

echo "Docker ps"
docker ps
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "sudo chmod 666 /var/run/docker.sock"
docker exec $DOCKER_CONTAINER_ID /bin/bash -c "dockerd --debug"
docker exec $DOCKER_CONTAINER_ID  /bin/bash -c "docker ps"


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

echo "Exiting Docker in Docker"
exit