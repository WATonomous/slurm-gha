# Use the specified Slurm base image with slurmdbd
FROM ghcr.io/watonomous/slurm-dist:sha-d71cada1ede70e133c6765e0239840ec97aadd40-slurmdbd

# Install Python and any dependencies
RUN apt-get update && apt-get install -y python3 python3-pip supervisor inotify-tools liblua5.3-0 libmysqlclient21 \
    && rm -rf /var/lib/apt/lists/*

# Create users
RUN groupadd --gid 64029 munge && useradd --uid 64029 --gid 64029 --home-dir /var/spool/munge --no-create-home --shell /bin/false munge
RUN groupadd --gid 64030 slurm && useradd --uid 64030 --gid 64030 --home-dir /var/spool/slurm --no-create-home --shell /bin/false slurm

# Set up munge directory
RUN mkdir /run/munge && chown munge:munge /run/munge

# Set the working directory in the container
WORKDIR /app

# Copy the Python script and any necessary files
COPY main.py config.py runner_size_config.py RunningJob.py allocate-ephemeral-runner-from-apptainer.sh /app/

# Install Python requirements
COPY requirements.txt /app/
RUN pip3 install -r requirements.txt

# Set up runtime configurations for slurmctld
RUN mkdir /etc/slurm /etc/runtime_config && touch /etc/runtime_config/passwd /etc/runtime_config/group
RUN cp /etc/passwd /etc/passwd.system && cp /etc/group /etc/group.system

# Copy and prepare daemon-specific configurations
COPY daemon-base/supervisord.conf /etc/supervisord.conf
COPY daemon-base/prefix-output.sh /opt/prefix-output.sh
RUN chmod +x /opt/prefix-output.sh

# Copy and set permissions for slurmctld and slurmdbd runtime agents
COPY slurmctld/runtime-agent.sh /opt/runtime-agent.sh
RUN chmod +x /opt/runtime-agent.sh
COPY slurmdbd/runtime-agent.sh /opt/runtime-agent.sh
RUN chmod +x /opt/runtime-agent.sh

# Copy entrypoint and supervisor configurations for slurmctld and slurmdbd
COPY slurmctld/entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh
COPY slurmctld/supervisor-conf/ /etc/supervisor/conf.d/
COPY slurmdbd/supervisor-conf/ /etc/supervisor/conf.d/

# Expose ports if needed by Slurm
EXPOSE 6817 6818 6819

# Environment variables for custom configuration paths
ENV MUNGED_ARGS= \
    SLURMCTLD_ARGS= \
    MUNGE_KEY_IMPORT_PATH=/etc/munge/munge.imported.key

# Start supervisord for process management and the Python script
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]

# Run the Python script as the main application entry point
ENTRYPOINT ["python3", "/app/main.py"]
