import subprocess
import os
import pwd
import grp
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_user_info():
    """Log current user and group information."""
    try:
        uid = os.getuid()
        user_info = pwd.getpwuid(uid)
        user_name = user_info.pw_name
        groups = [g.gr_name for g in grp.getgrall() if user_name in g.gr_mem]
        logging.info(f"Current User: {user_name} (UID: {uid})")
        logging.info(f"Groups: {groups}")
    except Exception as e:
        logging.error(f"Error fetching user info: {e}")

def check_file_permissions(filepath):
    """Log permissions for a specific file."""
    try:
        stat_info = os.stat(filepath)
        logging.info(f"Permissions for {filepath}: {oct(stat_info.st_mode)} (UID: {stat_info.st_uid}, GID: {stat_info.st_gid})")
    except FileNotFoundError:
        logging.warning(f"File {filepath} not found.")
    except Exception as e:
        logging.error(f"Error checking file permissions for {filepath}: {e}")

def print_environment():
    """Log all environment variables."""
    logging.info("Environment Variables:")
    for key, value in os.environ.items():
        logging.info(f"{key}={value}")

def run_sbatch_incrementally():
    """Run sbatch command incrementally adding options."""
    # Base command 
    base_command = [
        "/opt/slurm/bin/sbatch",
        "--job-name=test-no-gres",
        "echo.sh"
    ]
    
    # Define additional options to add incrementally
    options_to_add = [
        "--mem-per-cpu=2G",
        "--cpus-per-task=1",
        "--time=00:01:00",
        "--gres tmpdisk:4096"
    ]

    # Run the base command first
    logging.info(f"Running command with base options: {' '.join(base_command)}")
    run_command(base_command)
    
    # Incrementally add each option and re-run the command
    for i, option in enumerate(options_to_add, start=1):
        base_command.insert(-1, option)  # Insert each option before the script name
        logging.info(f"Running command with {i} additional option(s): {' '.join(base_command)}")
        run_command(base_command)

def run_command(command):
    """Run the command and log output and errors."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        logging.info(f"Command stdout: {result.stdout}")
        logging.error(f"Command stderr: {result.stderr}")
        logging.info(f"Return code: {result.returncode}")
    except Exception as e:
        logging.error(f"Exception while running sbatch command: {e}")

if __name__ == "__main__":
    # Output user information
    get_user_info()
    
    # Check permissions on important files
    check_file_permissions("/opt/slurm/bin/sbatch")
    check_file_permissions("/var/run/munge/munge.socket.2")
    
    # Output environment variables
    print_environment()
    
    # Run the sbatch command incrementally adding options
    run_sbatch_incrementally()
