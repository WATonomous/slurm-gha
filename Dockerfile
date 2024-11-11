# Use the Slurm base image
FROM ghcr.io/watonomous/slurm-dist:v0.0.12-daemon-base

# Install Python and any dependencies
RUN apt-get update && apt-get install -y python3 python3-pip 

# Set the working directory in the container
WORKDIR /home/watcloud-slurm-ci/

# Copy the Python script and any necessary files
COPY main.py config.py runner_size_config.py RunningJob.py allocate-ephemeral-runner-from-apptainer.sh start.sh /home/watcloud-slurm-ci/

# Install Python requirements
COPY requirements.txt /home/watcloud-slurm-ci/
RUN pip3 install -r requirements.txt
RUN chmod +x start.sh

# Run the Python script
# Note the env variable GITHUB_ACCESS_TOKEN will need to be set in order to authenticate with the GitHub API
ENTRYPOINT ["/home/watcloud-slurm-ci/start.sh"]
# ENTRYPOINT ["python3", "/app/main.py"]

# Custom user for running the CI
# USER watcloud-slurm-ci 