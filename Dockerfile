FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the Python script and any necessary files
COPY main.py config.py runner_size_config.py RunningJob.py allocate-ephemeral-runner-from-apptainer.sh /app/

# Install any required Python packages
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Run the Python script
CMD ["python", "main.py"]
