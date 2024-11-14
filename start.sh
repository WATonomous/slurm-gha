#!/bin/bash
set -euo pipefail

# Start supervisord in the background
/usr/bin/supervisord -c /etc/supervisord.conf &

# Wait until the Munge socket is ready
MUNGE_SOCKET_PATH="/var/run/munge/munge.socket.2"

echo "Waiting for Munge socket to be ready..."
while [ ! -S "$MUNGE_SOCKET_PATH" ]; do
    sleep 1  # Wait for 1 second before checking again
done

echo "Munge socket is ready. Starting Python script."

# Start the Python script as the watcloud-slurm-ci user
su -s /bin/bash -c "python3 /home/watcloud-slurm-ci/debug.py" watcloud-slurm-ci