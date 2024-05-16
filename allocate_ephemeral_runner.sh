#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels>

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

# Setup
runner_dir="runner_$REGISTRATION_TOKEN"
mkdir -p "$runner_dir"
ls
cd "$runner_dir"

curl -o actions-runner-osx-x64-2.316.1.tar.gz -L https://github.com/actions/runner/releases/download/v2.316.1/actions-runner-osx-x64-2.316.1.tar.gz
tar xzf ./actions-runner-osx-x64-2.316.1.tar.gz


# Register and run
./config.sh --url "$REPO_URL" --token "$REGISTRATION_TOKEN" --labels "$LABELS" --unattended --ephemeral

./run.sh

# Wait for the runner to finish
wait $!

# Cleanup
./config.sh remove --token $TOKEN
cd ..
rm -rf "$runner_dir"