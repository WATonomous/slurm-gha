#!/usr/bin/env bash

# ------------------------------------------------------------------------------
# Script: gather-traces.sh
# ------------------------------------------------------------------------------
# Usage: 
#   1) chmod +x gather-traces.sh
#   2) ./gather-traces.sh
# ------------------------------------------------------------------------------
	   
GITHUB_TOKEN=""

# ------------------------------------------------------------------------------
# BEFORE: USER INGESTION
# ------------------------------------------------------------------------------
mkdir -p before/user-ingestion
before_user_ingestion=(
  "https://github.com/WATonomous/infra-config/actions/runs/12427574689"
  "https://github.com/WATonomous/infra-config/actions/runs/12427547885"
  "https://github.com/WATonomous/infra-config/actions/runs/12424954086"
  "https://github.com/WATonomous/infra-config/actions/runs/12424337584"
  "https://github.com/WATonomous/infra-config/actions/runs/12422341445"
  "https://github.com/WATonomous/infra-config/actions/runs/12420112260"
)

for url in "${before_user_ingestion[@]}"; do
  run_id="${url##*/}"  # Extract the last part of the URL
  echo "Processing BEFORE - User Ingestion: $run_id"
  gatrace generate-trace "$url" \
    --github-token "$GITHUB_TOKEN" \
    --output-file "before/user-ingestion/${run_id}"
done

# ------------------------------------------------------------------------------
# BEFORE: MASTER - SCHEDULED
# ------------------------------------------------------------------------------
mkdir -p before/master-scheduled
before_master_scheduled=(
  "https://github.com/WATonomous/infra-config/actions/runs/12553360748"
  "https://github.com/WATonomous/infra-config/actions/runs/12530736888"
  "https://github.com/WATonomous/infra-config/actions/runs/12521935862"
  "https://github.com/WATonomous/infra-config/actions/runs/12509665511"
  "https://github.com/WATonomous/infra-config/actions/runs/12487314642"
  "https://github.com/WATonomous/infra-config/actions/runs/12449453195"
  "https://github.com/WATonomous/infra-config/actions/runs/12440178947"
  "https://github.com/WATonomous/infra-config/actions/runs/12422821414"
)

for url in "${before_master_scheduled[@]}"; do
  run_id="${url##*/}"
  echo "Processing BEFORE - Master Scheduled: $run_id"
  gatrace generate-trace "$url" \
    --github-token "$GITHUB_TOKEN" \
    --output-file "before/master-scheduled/${run_id}"
done

# ------------------------------------------------------------------------------
# AFTER: USER INGESTION
# ------------------------------------------------------------------------------
mkdir -p after/user-ingestion
after_user_ingestion=(
  "https://github.com/WATonomous/infra-config/actions/runs/12854845210"
  "https://github.com/WATonomous/infra-config/actions/runs/12851571085"
  "https://github.com/WATonomous/infra-config/actions/runs/12850108807"
  "https://github.com/WATonomous/infra-config/actions/runs/12696207371"
  "https://github.com/WATonomous/infra-config/actions/runs/12682617238"
)

for url in "${after_user_ingestion[@]}"; do
  run_id="${url##*/}"
  echo "Processing AFTER - User Ingestion: $run_id"
  gatrace generate-trace "$url" \
    --github-token "$GITHUB_TOKEN" \
    --output-file "after/user-ingestion/${run_id}"
done

# ------------------------------------------------------------------------------
# AFTER: MASTER - SCHEDULED
# ------------------------------------------------------------------------------
mkdir -p after/master-scheduled
after_master_scheduled=(
  "https://github.com/WATonomous/infra-config/actions/runs/12848528449"
  "https://github.com/WATonomous/infra-config/actions/runs/12838792205"
  "https://github.com/WATonomous/infra-config/actions/runs/12819860145"
  "https://github.com/WATonomous/infra-config/actions/runs/12799262666"
  "https://github.com/WATonomous/infra-config/actions/runs/12778673306"
  "https://github.com/WATonomous/infra-config/actions/runs/12738272633"
  "https://github.com/WATonomous/infra-config/actions/runs/12738272633" # Duplicate URL
)

for url in "${after_master_scheduled[@]}"; do
  run_id="${url##*/}"
  echo "Processing AFTER - Master Scheduled: $run_id"
  gatrace generate-trace "$url" \
    --github-token "$GITHUB_TOKEN" \
    --output-file "after/master-scheduled/${run_id}"
done

echo "All runs processed!"
