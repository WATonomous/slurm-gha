#!/bin/bash

set -o pipefail -o errexit -o nounset

if [ -z "${SLURM_JOB_ID:-}" ]; then
    1>&2 echo "This script is meant to be run from within a Slurm job. We are not in a Slurm job. Exiting..."
    exit 1
fi

__orig_docker_host="${DOCKER_HOST:-}"

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/tmp/run}
export XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-/tmp/config}
export DOCKER_DATA_ROOT=${DOCKER_DATA_ROOT:-/tmp/docker}
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"

__dockerd_log_file="/tmp/dockerd.log"

mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME"

if docker ps > /dev/null 2>&1; then
    echo "Docker is already running! Execute the following command to use it:"
    echo
    echo "export DOCKER_HOST=$DOCKER_HOST"
    echo "docker run --rm hello-world"
    echo
    exit 0
fi

echo "Starting dockerd. Data root: '${DOCKER_DATA_ROOT}'. Log file: '${__dockerd_log_file}'"

# --exec-opt native.cgroupdriver=cgroupfs is required because we don't have systemd
/usr/bin/dockerd-rootless.sh --data-root "${DOCKER_DATA_ROOT}" --exec-opt native.cgroupdriver=cgroupfs --config-file /etc/docker/daemon.json > "$__dockerd_log_file" 2>&1 &

__count=0

while ! docker ps > /dev/null 2>&1; do
    if [ $__count -gt 15 ]; then
        echo "Dockerd still not started after $__count seconds. Printing the logs from $__dockerd_log_file and exiting..."
        cat $__dockerd_log_file
        exit 1
    fi

    __count=$((__count+1))

    echo "Waiting for dockerd to start..."
    sleep 1
done

echo "Dockerd started successfully!"
if [ "$__orig_docker_host" != "$DOCKER_HOST" ]; then
    echo
    echo "Execute the following command to use it:"
    echo "export DOCKER_HOST=$DOCKER_HOST"
fi

echo
echo "Test it with:"
echo "docker run --rm hello-world"
echo

