# Use the Slurm base image
FROM ghcr.io/watonomous/slurm-dist:sha-d71cada1ede70e133c6765e0239840ec97aadd40-slurmctld

# Install Python and any dependencies
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the Python script and any necessary files
COPY main.py config.py runner_size_config.py RunningJob.py allocate-ephemeral-runner-from-apptainer.sh /app/

# Install Python requirements
COPY requirements.txt /app/
RUN pip3 install -r requirements.txt

# Run the Python script
# Note the env variable GITHUB_ACCESS_TOKEN will need to be set in order to authenticate with the GitHub API
# CMD ["python3", "main.py"]
ENTRYPOINT ["python3", "/app/main.py"]

# Custom user for running the CI
# USER watcloud-slurm-ci 