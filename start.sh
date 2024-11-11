#!/bin/bash
set -euo pipefail
# Start the supervisor
/usr/bin/supervisord -c /etc/supervisord.conf

# Start the allocator 
python3 /home/watcloud-slurm-ci/main.py