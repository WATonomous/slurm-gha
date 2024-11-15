# Use the Slurm base image
FROM ghcr.io/watonomous/slurm-dist:v0.0.12-daemon-base

# Install Python and any dependencies
RUN apt-get update && apt-get install -y python3 python3-pip 

# Set the working directory in the container
WORKDIR /home/watcloud-slurm-ci/

# Copy the Python script and any necessary files
COPY main.py config.py runner_size_config.py debug.py RunningJob.py allocate-ephemeral-runner-from-apptainer.sh start.sh echo.sh /home/watcloud-slurm-ci/

# Install Python requirements
COPY requirements.txt /home/watcloud-slurm-ci/
RUN pip3 install -r requirements.txt
RUN chmod +x *.sh

# Create watcloud-slurm-ci user with UID 1814 within the container
RUN useradd -u 1814 -m -d /home/watcloud-slurm-ci watcloud-slurm-ci
RUN chown -R watcloud-slurm-ci:watcloud-slurm-ci /home/watcloud-slurm-ci/

# Run the Python script
# Note the env variable GITHUB_ACCESS_TOKEN will need to be set
ENTRYPOINT ["/home/watcloud-slurm-ci/start.sh"]