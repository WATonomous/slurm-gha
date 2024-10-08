#!/bin/bash
# Use: ./allocate-ephemeral-runner-from-apptainer.sh <repo-url> <registration-token> <removal-token> <labels>
# Tailored for use in a Slurm environment on ComputeCanada, using Apptainer to run GitHub Actions

# Function to log messages with a timestamp
log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $@"
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
RUN_ID=$5

export DOCKER_HOST=unix:///tmp/run/docker.sock

# Define the parent directory for GitHub Actions in the host machine
PARENT_DIR="/tmp/runner-${SLURMD_NODENAME}-${SLURM_JOB_ID}"
PROVISIONER_DIR="/mnt/wato-drive/alexboden/provisioner-cache/$RUN_ID"
log "INFO Parent directory for GitHub Actions: $PARENT_DIR"

start_time=$(date +%s)
mkdir -p $PROVISIONER_DIR
chmod -R 777 $PROVISIONER_DIR
GITHUB_ACTIONS_WKDIR="$PARENT_DIR/_work"
mkdir -p $PARENT_DIR $GITHUB_ACTIONS_WKDIR
chmod -R 777 $PARENT_DIR
end_time=$(date +%s)
log "INFO Created and set permissions for parent directory (Duration: $(($end_time - $start_time)) seconds)"

log "INFO Starting Docker on Slurm"
start_time=$(date +%s)
slurm-start-dockerd.sh
end_time=$(date +%s)
log "INFO Docker in Slurm started (Duration: $(($end_time - $start_time)) seconds)"

# Load Apptainer
log "INFO Loading Apptainer"
source /cvmfs/soft.computecanada.ca/config/profile/bash.sh
module load apptainer

# Define the Docker image to use
DOCKER_IMAGE="/cvmfs/unpacked.cern.ch/ghcr.io/watonomous/actions-runner-image:main"

log "INFO Starting Apptainer container and configuring runner"

apptainer exec --writable-tmpfs --fakeroot --bind /tmp/run/docker.sock:/tmp/run/docker.sock --bind /tmp:/tmp /cvmfs/unpacked.cern.ch/ghcr.io/watonomous/actions-runner-image:main /bin/bash -c "export RUNNER_ALLOW_RUNASROOT=1 && export PYTHONPATH=/home/runner/.local/lib/python3.10/site-packages && /home/runner/config.sh --work \"${GITHUB_ACTIONS_WKDIR}\" --url \"${REPO_URL}\" --token \"${REGISTRATION_TOKEN}\" --labels \"${LABELS}\" --name \"slurm-${SLURMD_NODENAME}-${SLURM_JOB_ID}\" --unattended --ephemeral && /home/runner/run.sh && /home/runner/config.sh remove --token \"${REMOVAL_TOKEN}\""

# mount tmp to get rid of https://github.com/WATonomous/infra-config/actions/runs/10822451152/job/30026308856#step:4:88

log "exiting apptainer"
log "INFO Runner removed (Duration: $(($end_time - $start_time)) seconds)"

log "INFO allocate-ephemeral-runner-from-apptainer.sh finished, exiting..."
exit 0