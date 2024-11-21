#!/bin/bash
# Use: ./ephemeral_runner.sh <repo-urL> <registration-token> <removal-token> <labels>

REPO_URL=$1
REGISTRATION_TOKEN=$2
REMOVAL_TOKEN=$3
LABELS=$4

# Setup
runner_dir="runners/runner_$REGISTRATION_TOKEN"
mkdir -p "$runner_dir"
ls
cd "$runner_dir"

# Mac
# curl -o actions-runner-osx-x64-2.316.1.tar.gz -L https://github.com/actions/runner/releases/download/v2.316.1/actions-runner-osx-x64-2.316.1.tar.gz
# tar xzf ./actions-runner-osx-x64-2.316.1.tar.gz

# Linux
curl -o actions-runner-linux-x64-2.316.1.tar.gz -L https://github.com/actions/runner/releases/download/v2.316.1/actions-runner-linux-x64-2.316.1.tar.gz
tar xzf ./actions-runner-linux-x64-2.316.1.tar.gz

# Register and run
./config.sh --url "$REPO_URL" --token "$REGISTRATION_TOKEN" --labels "$LABELS" --name "runner_$REGISTRATION_TOKEN" --unattended --ephemeral

./run.sh

# Wait for the runner to finish
wait $!
# Cleanup
./config.sh remove --token $REMOVAL_TOKEN
cd ../..
echo "Removing $runner_dir"
rm -rf "$runner_dir" 
